"""
``validate_submission`` — the submission validation orchestrator.

Pipeline (each step feeds the next):

  1. Structural   — every answer references a real, non-deleted field of THIS
                    form; no duplicate answers; SECTION fields not answered.
  2. Coerce       — raw value -> typed value (str / Decimal / date / list / …).
  3. Layout       — conditional show/hide + required overrides. Answers for
                    hidden fields are dropped.
  4. Required     — visible + effectively-required + empty  -> REQUIRED.
  5. Type         — field-type sanity (option membership, matrix shape, …).
  6. Rules        — configured ``validation_rules`` constraints.
  7. Cross-field  — comparisons against other answers.

Modes:
  "strict"   — public submission / response edit. Every failure is an error.
  "partial"  — admin manual-entry / CSV import / backup importer. Structural,
               type and option-membership failures stay errors; required and
               constraint failures are downgraded to warnings so partial legacy
               data can still be stored.
"""

from __future__ import annotations

from typing import Any, Iterable

from apps.forms.models import FieldType, FormField
from . import codes
from .report import FieldError, ValidationReport
from .schema import normalize_validation_rules
from .values import is_empty, coerce_value, CoercionError
from .conditional import compute_layout
from .field_types import validate_field_type
from .rules import apply_rules
from .cross_field import evaluate_cross_field

STRICT = "strict"
PARTIAL = "partial"

_ANSWERABLE = lambda ft: ft != FieldType.SECTION


def _extract_pairs(raw_answers: Any) -> list[tuple[Any, Any]]:
    """Accept the wire shape ``[{"field": id, "value": v}, ...]`` (and a dict fallback)."""
    pairs: list[tuple[Any, Any]] = []
    if isinstance(raw_answers, dict):
        return [(k, v) for k, v in raw_answers.items()]
    for item in raw_answers or []:
        if isinstance(item, dict) and ("field" in item or "field_id" in item):
            pairs.append((item.get("field", item.get("field_id")), item.get("value")))
    return pairs


def validate_submission(
    form,
    raw_answers: Any,
    *,
    mode: str = STRICT,
    is_edit: bool = False,
    existing_answers: dict[int, Any] | None = None,
) -> ValidationReport:
    report = ValidationReport()
    partial = mode == PARTIAL

    # All fields ever attached to this form, active + soft-deleted, keyed by id.
    all_fields = {f.id: f for f in FormField.all_objects.filter(form=form)}
    active_fields = [f for f in all_fields.values() if not f.is_deleted]
    active_fields.sort(key=lambda f: (f.order, f.id))
    active_by_id = {f.id: f for f in active_fields}

    # --- 1. Structural -----------------------------------------------------
    seen_ids: set[int] = set()
    incoming: dict[int, Any] = {}

    for raw_fid, raw_value in _extract_pairs(raw_answers):
        try:
            fid = int(raw_fid)
        except (TypeError, ValueError):
            report.add_error(FieldError(
                code=codes.UNKNOWN_FIELD,
                message=f"'{raw_fid}' is not a valid field id.",
            ))
            continue

        field = all_fields.get(fid)
        if field is None:
            report.add_error(FieldError(
                code=codes.UNKNOWN_FIELD, field_id=fid,
                message=f"Field {fid} does not belong to this form.",
            ))
            continue
        if getattr(field, "is_deleted", False):
            if partial:
                continue  # admins may carry answers for retired fields; just skip them
            report.add_error(FieldError(
                code=codes.DELETED_FIELD, field_id=fid, label=field.label,
                message=f"Field '{field.label}' has been removed and no longer accepts answers.",
            ))
            continue
        if field.type == FieldType.SECTION:
            continue  # section headers carry no answer; ignore silently
        if fid in seen_ids:
            report.add_error(FieldError(
                code=codes.DUPLICATE_ANSWER, field_id=fid, label=field.label,
                message=f"Field '{field.label}' was answered more than once.",
            ))
            continue
        seen_ids.add(fid)
        incoming[fid] = raw_value

    # --- 2. Coerce -------------------------------------------------------
    coerced: dict[int, Any] = {}
    for fid, raw_value in incoming.items():
        field = active_by_id[fid]
        if is_empty(raw_value):
            continue
        try:
            coerced[fid] = coerce_value(field, raw_value)
        except CoercionError as exc:
            report.add_error(FieldError(
                code=exc.code, field_id=fid, label=field.label, message=exc.message,
            ))

    # On edit: fold in the previously-stored answers so conditional logic and
    # required checks see the full picture, then re-validate everything.
    layout_values = dict(coerced)
    if is_edit and existing_answers:
        for fid, val in existing_answers.items():
            if fid in active_by_id and fid not in layout_values and not is_empty(val):
                field = active_by_id[fid]
                try:
                    layout_values[fid] = coerce_value(field, val)
                except CoercionError:
                    layout_values[fid] = val

    # --- 3. Conditional layout -----------------------------------------
    layout = compute_layout(active_fields, layout_values)

    # Drop answers for fields that are conditionally hidden.
    cleaned: dict[int, Any] = {}
    for fid, value in coerced.items():
        if layout.is_visible(fid):
            cleaned[fid] = value

    # --- 4. Required ----------------------------------------------------
    for field in active_fields:
        if field.type == FieldType.SECTION or not layout.is_visible(field.id):
            continue
        if not layout.effective_required(field):
            continue
        if field.id not in cleaned or is_empty(cleaned.get(field.id)):
            err = FieldError(
                code=codes.REQUIRED, field_id=field.id, label=field.label,
                message=f"'{field.label}' is required.", rule="required",
            )
            (report.add_warning if partial else report.add_error)(err)

    # --- 5. Type + 6. Rules ------------------------------------------
    for fid, value in list(cleaned.items()):
        field = active_by_id[fid]
        for err in validate_field_type(field, value):
            _place(report, err, partial)
        for err in apply_rules(field, value):
            _place(report, err, partial)

    # --- 7. Cross-field ---------------------------------------------------
    for err in evaluate_cross_field(active_by_id, cleaned, layout.visible_ids):
        _place(report, err, partial)

    # --- normalize stored values --------------------------------------
    report.cleaned_answers = {
        fid: _serialize_for_storage(active_by_id[fid], value)
        for fid, value in cleaned.items()
    }
    return report


def _place(report: ValidationReport, err: FieldError, partial: bool) -> None:
    if partial and err.code in codes.PARTIAL_MODE_DOWNGRADABLE:
        report.add_warning(err)
    else:
        report.add_error(err)


def _serialize_for_storage(field, value: Any) -> Any:
    """
    Return the JSON-safe value to persist in ``Answer.value``. Shapes match what
    the codebase already stores today (string / number / list / dict) — no
    wrapping — so every downstream consumer keeps working.
    """
    import datetime as _dt
    from decimal import Decimal

    rules = field.validation_rules or {}

    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, _dt.datetime):
        return value.isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    if isinstance(value, _dt.time):
        return value.isoformat(timespec="minutes")
    if field.type == FieldType.EMAIL and isinstance(value, str) and rules.get("normalizeCase"):
        return value.strip().lower()
    if isinstance(value, str):
        return value.strip()
    return value
