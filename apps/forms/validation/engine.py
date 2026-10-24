"""
``validate_submission`` - the submission validation orchestrator.

Pipeline (each step feeds the next):

  1. Structural   - every answer references a real, non-deleted field of THIS
                    form; no duplicate answers; SECTION fields not answered.
  2. Coerce       - raw value -> typed value (str / Decimal / date / list / …).
  3. Layout       - conditional show/hide + required overrides. Answers for
                    hidden fields are dropped.
  4. Required     - visible + effectively-required + empty  -> REQUIRED.
  5. Type         - field-type sanity (option membership, matrix shape, …).
  6. Rules        - configured ``validation_rules`` constraints.
  7. Cross-field  - comparisons against other answers.

Modes:
  "strict"   - public submission / response edit. Every failure is an error.
  "partial"  - admin manual-entry / CSV import / backup importer. Structural,
               type and option-membership failures stay errors; required and
               constraint failures are downgraded to warnings so partial legacy
               data can still be stored.
"""

from __future__ import annotations

from typing import Any, Iterable

from dataclasses import replace as _replace

from apps.forms.models import FieldType, FormField, NEVER_REQUIRED_FIELD_TYPES, NON_ANSWER_FIELD_TYPES
from . import codes
from .report import FieldError, ValidationReport
from .schema import normalize_validation_rules
from .values import is_empty, coerce_value, CoercionError
from .conditional import compute_layout
from .field_types import validate_field_type, _matches_option
from .rules import apply_rules
from .cross_field import evaluate_cross_field
from .formula import evaluate as evaluate_formula

STRICT = "strict"
PARTIAL = "partial"

_ANSWERABLE = lambda ft: ft not in NON_ANSWER_FIELD_TYPES


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
        if field.type in NON_ANSWER_FIELD_TYPES:
            continue  # section headers / page breaks carry no answer; ignore silently
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
        if is_empty(raw_value) or field.type == FieldType.CALCULATED:
            continue  # a calculated answer is the server's to work out, whatever the client sent
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

    # set_value: the server, not the browser, decides these answers.
    for fid, raw_value in layout.set_values.items():
        try:
            coerced[fid] = coerce_value(active_by_id[fid], raw_value)
        except CoercionError as exc:
            report.add_error(FieldError(
                code=exc.code, field_id=fid, label=active_by_id[fid].label, message=exc.message,
            ))

    # Calculated questions: worked out here from the settled answers.
    _apply_calculated(active_fields, layout, layout_values, coerced)

    # Drop answers for fields that are conditionally hidden.
    cleaned: dict[int, Any] = {}
    for fid, value in coerced.items():
        if layout.is_visible(fid):
            cleaned[fid] = value

    # --- 4. Required ----------------------------------------------------
    for field in active_fields:
        if field.type in NON_ANSWER_FIELD_TYPES or field.type in NEVER_REQUIRED_FIELD_TYPES or not layout.is_visible(field.id):
            continue
        if not layout.effective_required(field):
            continue
        if field.id not in cleaned or is_empty(cleaned.get(field.id)):
            err = FieldError(
                code=codes.REQUIRED, field_id=field.id, label=field.label,
                message=_custom_message(field, "required") or f"'{field.label}' is required.", rule="required",
            )
            (report.add_warning if partial else report.add_error)(err)

    # --- 5. Type + 6. Rules ------------------------------------------
    for fid, value in list(cleaned.items()):
        field = active_by_id[fid]
        for err in validate_field_type(field, value):
            _place(report, _retext(field, err, "type"), partial)
        for err in _outside_allowed_options(field, value, layout.allowed_options.get(fid)):
            _place(report, err, partial)
        for err in apply_rules(field, value):
            _place(report, _retext(field, err, "type"), partial)

    # --- 7. Cross-field ---------------------------------------------------
    for err in evaluate_cross_field(active_by_id, cleaned, layout.visible_ids):
        _place(report, err, partial)

    # --- normalize stored values --------------------------------------
    report.cleaned_answers = {
        fid: _serialize_for_storage(active_by_id[fid], value)
        for fid, value in cleaned.items()
    }
    return report


def _custom_message(field, key: str) -> str | None:
    """The admin's own wording for this kind of failure, if they wrote one (``validation_rules.messages``)."""
    messages = (field.validation_rules or {}).get("messages")
    text = messages.get(key) if isinstance(messages, dict) else None
    return text.strip() if isinstance(text, str) and text.strip() else None


def _retext(field, err: FieldError, default_key: str) -> FieldError:
    """Swap in the admin's message for the rule that failed (or for "type" when no rule is named)."""
    text = _custom_message(field, err.rule or default_key)
    return _replace(err, message=text) if text else err


def _apply_calculated(active_fields, layout, layout_values: dict[int, Any], coerced: dict[int, Any]) -> None:
    """Fill ``coerced`` with the value of every visible CALCULATED question, or drop it when it has none."""
    calculated = [f for f in active_fields if f.type == FieldType.CALCULATED and layout.is_visible(f.id)]
    if not calculated:
        return
    known = {fid: v for fid, v in {**layout_values, **coerced}.items() if layout.is_visible(fid)}
    for f in calculated:
        known.pop(f.id, None)  # never feed on a previously stored result
    # One pass per question settles any chain of calculated questions (cycles are refused at publish).
    for _ in range(len(calculated)):
        for f in calculated:
            rules = f.validation_rules or {}
            result = evaluate_formula(str(rules.get("formula") or ""), known, rules.get("decimals", 2))
            if result is None:
                known.pop(f.id, None)
            else:
                known[f.id] = result
    for f in calculated:
        if f.id in known:
            coerced[f.id] = known[f.id]
        else:
            coerced.pop(f.id, None)


def _outside_allowed_options(field, value, allowed) -> list[FieldError]:
    """restrict_options: the chosen option(s) must be among those a condition currently allows."""
    if allowed is None:
        return []
    allowed_set = {str(a).strip() for a in allowed}
    chosen = value if isinstance(value, list) else [value]
    return [
        FieldError(
            code=codes.OPTION_NOT_ALLOWED, field_id=field.id, label=field.label, rule="restrictOptions",
            message=f"'{item}' is not available given your other answers.",
            context={"allowed": sorted(allowed_set)},
        )
        for item in chosen if not _matches_option(str(item), allowed_set)
    ]


def _place(report: ValidationReport, err: FieldError, partial: bool) -> None:
    if partial and err.code in codes.PARTIAL_MODE_DOWNGRADABLE:
        report.add_warning(err)
    else:
        report.add_error(err)


def _serialize_for_storage(field, value: Any) -> Any:
    """
    Return the JSON-safe value to persist in ``Answer.value``. Shapes match what
    the codebase already stores today (string / number / list / dict) - no
    wrapping - so every downstream consumer keeps working.
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
    if field.type in (FieldType.RADIO, FieldType.DROPDOWN) and isinstance(value, str):
        return _canonical_option(value, field.options)
    if field.type == FieldType.CHECKBOX and isinstance(value, list):
        return [_canonical_option(v, field.options) for v in value]
    if field.type in (FieldType.MATRIX_RADIO, FieldType.MATRIX_CHECKBOX) and isinstance(value, dict):
        return {
            _canonical_option(row, field.rows): (
                [_canonical_option(c, field.options) for c in cell] if isinstance(cell, list)
                else _canonical_option(cell, field.options) if isinstance(cell, str) else cell
            )
            for row, cell in value.items()
        }
    if isinstance(value, str):
        return value.strip()
    return value


def _canonical_option(value: str, options) -> str:
    """Store the admin's spelling of a matched option, not whatever casing the client sent."""
    wanted = value.strip()
    folded = wanted.lower()
    for option in options or []:
        text = str(option).strip()
        if text == wanted:
            return text
    for option in options or []:
        text = str(option).strip()
        if text.lower() == folded:
            return text
    return wanted
