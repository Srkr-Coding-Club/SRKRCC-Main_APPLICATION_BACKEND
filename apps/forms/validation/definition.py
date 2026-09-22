"""
``validate_form_definition`` — checks a form's own configuration.

Runs on two shapes:
  * a saved ``Form`` instance (its ``fields`` relation), used by the publish
    action and the ``/validate/`` endpoint;
  * the raw serializer payload dict (``{"fields": [ {...}, ... ]}``), used by
    ``FormSerializer.validate`` before the form is written.

Structural errors block PUBLISHED / SCHEDULED. Soft findings are warnings only.
"""

from __future__ import annotations

import re
from typing import Any

from apps.forms.models import FieldType
from . import codes
from .report import DefinitionReport, FieldError
from .schema import (
    RULE_COMPAT, TEXT_FORMATS, MIN_MAX_PAIRS, NON_NEGATIVE_KEYS,
    CONDITIONAL_OPERATORS, CONDITIONAL_ACTIONS,
    normalize_validation_rules, normalize_conditional_logic, iter_condition_field_refs,
)

_VALID_TYPES = {c for c, _ in FieldType.choices}
_CHOICE_TYPES = {FieldType.RADIO, FieldType.DROPDOWN, FieldType.CHECKBOX}
_MATRIX_TYPES = {FieldType.MATRIX_RADIO, FieldType.MATRIX_CHECKBOX}
_SCALE_TYPES = {FieldType.RATING, FieldType.LINEAR_SCALE}
# Field types whose value can meaningfully take part in an eq/ne cross-field rule.
_COMPARABLE_TYPES = {
    FieldType.TEXT, FieldType.PARAGRAPH, FieldType.EMAIL, FieldType.PHONE,
    FieldType.URL, FieldType.NUMBER, FieldType.DATE, FieldType.TIME,
    FieldType.RADIO, FieldType.DROPDOWN, FieldType.RATING, FieldType.LINEAR_SCALE,
    FieldType.CLUB_ID,
}


class _FieldView:
    """Uniform accessor over either a FormField instance or a payload dict."""

    def __init__(self, src: Any, index: int):
        self._is_dict = isinstance(src, dict)
        self._src = src
        self.index = index

    def _g(self, name, default=None):
        return self._src.get(name, default) if self._is_dict else getattr(self._src, name, default)

    @property
    def id(self):
        return self._g("id")

    @property
    def label(self):
        return self._g("label") or f"Question {self.index + 1}"

    @property
    def type(self):
        return self._g("type")

    @property
    def is_deleted(self):
        return bool(self._g("is_deleted", False))

    @property
    def options(self):
        return self._g("options") or []

    @property
    def rows(self):
        return self._g("rows") or []

    @property
    def min_value(self):
        return self._g("min_value")

    @property
    def max_value(self):
        return self._g("max_value")

    @property
    def validation_rules(self):
        return self._g("validation_rules") or {}

    @property
    def conditional_logic(self):
        return self._g("conditional_logic") or {}

    @property
    def order(self):
        return self._g("order", 0)


def _iter_fields(form_or_payload: Any) -> list[_FieldView]:
    if isinstance(form_or_payload, dict):
        raw = form_or_payload.get("fields") or []
    else:
        raw = list(form_or_payload.fields.all())
    views = [_FieldView(f, i) for i, f in enumerate(raw)]
    return [v for v in views if not v.is_deleted]


def _err(code, message, field: _FieldView | None = None, rule=None, **ctx) -> FieldError:
    return FieldError(
        code=code, message=message,
        field_id=(field.id if field else None),
        label=(field.label if field else None),
        rule=rule, context=ctx or None,
    )


def validate_form_definition(form_or_payload: Any) -> DefinitionReport:
    report = DefinitionReport()
    fields = _iter_fields(form_or_payload)

    if not fields:
        report.add_warning(_err(codes.WARN_NO_FIELDS, "This form has no questions yet."))
        return report

    # id/label lookup for conditional + cross-field reference checks
    by_id: dict[int, _FieldView] = {}
    for f in fields:
        try:
            by_id[int(f.id)] = f
        except (TypeError, ValueError):
            pass

    orders_seen: set[int] = set()
    any_required = False

    for f in fields:
        ftype = f.type

        if ftype not in _VALID_TYPES:
            report.add_error(_err(codes.DEF_UNKNOWN_FIELD_TYPE,
                                  f"'{ftype}' is not a known question type.", f))
            continue

        any_required = any_required or bool(f._g("is_required", False))

        try:
            o = int(f.order)
            if o in orders_seen:
                report.add_warning(_err(codes.WARN_DUPLICATE_ORDER,
                                        f"Two questions share display order {o}.", f))
            orders_seen.add(o)
        except (TypeError, ValueError):
            pass

        # --- options / rows ------------------------------------------------
        if ftype in _CHOICE_TYPES:
            opts = [str(o).strip() for o in f.options]
            if len([o for o in opts if o]) < 2:
                report.add_error(_err(codes.DEF_CHOICE_NEEDS_OPTIONS,
                                      f"'{f.label}' needs at least two options.", f))
            if any(not o for o in opts):
                report.add_error(_err(codes.DEF_BLANK_OPTION,
                                      f"'{f.label}' has a blank option.", f))
            lowered = [o.lower() for o in opts if o]
            if len(lowered) != len(set(lowered)):
                report.add_error(_err(codes.DEF_DUPLICATE_OPTION,
                                      f"'{f.label}' has duplicate options.", f))

        if ftype in _MATRIX_TYPES:
            if len([r for r in f.rows if str(r).strip()]) < 1:
                report.add_error(_err(codes.DEF_MATRIX_NEEDS_ROWS,
                                      f"'{f.label}' needs at least one row.", f))
            if len([o for o in f.options if str(o).strip()]) < 2:
                report.add_error(_err(codes.DEF_CHOICE_NEEDS_OPTIONS,
                                      f"'{f.label}' needs at least two columns.", f))

        if ftype in _SCALE_TYPES:
            lo, hi = f.min_value, f.max_value
            if lo is not None and hi is not None and lo >= hi:
                report.add_error(_err(codes.DEF_SCALE_RANGE_INVALID,
                                      f"'{f.label}': minimum ({lo}) must be less than maximum ({hi}).", f))

        # --- validation_rules --------------------------------------------
        _check_rules(report, f)

        # --- conditional_logic -----------------------------------------
        _check_conditional(report, f, by_id)

        # --- cross-field -----------------------------------------------
        _check_cross_field(report, f, by_id)

    if not any_required:
        report.add_warning(_err(codes.WARN_NO_REQUIRED_FIELDS,
                                "No question on this form is marked required."))

    return report


def _check_rules(report: DefinitionReport, f: _FieldView) -> None:
    raw = f.validation_rules
    if not isinstance(raw, dict) or not raw:
        return
    allowed = RULE_COMPAT.get(f.type, set())

    for key, value in raw.items():
        if key == "crossField":
            continue
        if key not in allowed:
            report.add_error(_err(codes.DEF_RULE_INCOMPATIBLE,
                                  f"'{f.label}': rule '{key}' does not apply to a {f.type} question.",
                                  f, rule=key))

    normalized = normalize_validation_rules(f.type, raw)

    # regex
    if "pattern" in raw and raw["pattern"] is not None:
        pat = str(raw["pattern"])
        if pat.strip() == "":
            report.add_error(_err(codes.DEF_EMPTY_REGEX,
                                  f"'{f.label}': the custom pattern is empty.", f, rule="pattern"))
        else:
            try:
                re.compile(pat)
            except re.error as exc:
                report.add_error(_err(codes.DEF_INVALID_REGEX,
                                      f"'{f.label}': the custom pattern is not a valid regular expression ({exc}).",
                                      f, rule="pattern"))

    # format
    fmt = raw.get("format")
    if fmt and str(fmt).lower() not in TEXT_FORMATS:
        report.add_error(_err(codes.DEF_UNKNOWN_FORMAT,
                              f"'{f.label}': '{fmt}' is not a recognised format.", f, rule="format"))

    # min/max ordering
    for lo_key, hi_key in MIN_MAX_PAIRS:
        lo, hi = normalized.get(lo_key), normalized.get(hi_key)
        if lo is None or hi is None:
            continue
        try:
            if lo > hi:
                report.add_error(_err(codes.DEF_MIN_GREATER_THAN_MAX,
                                      f"'{f.label}': {lo_key} ({lo}) is greater than {hi_key} ({hi}).",
                                      f, rule=lo_key))
        except TypeError:
            pass

    # non-negative
    for key in NON_NEGATIVE_KEYS:
        v = normalized.get(key)
        if isinstance(v, (int, float)) and v < 0:
            report.add_error(_err(codes.DEF_NEGATIVE_CONSTRAINT,
                                  f"'{f.label}': {key} cannot be negative.", f, rule=key))


def _check_conditional(report: DefinitionReport, f: _FieldView, by_id: dict[int, _FieldView]) -> None:
    raw = f.conditional_logic
    if not isinstance(raw, dict) or not raw:
        return

    normalized = normalize_conditional_logic(raw)
    if not normalized:
        # non-empty but unusable — e.g. the {"if": "parent"} placeholder
        if str(raw.get("if", "")).strip().lower() == "parent" or "parent" in raw:
            report.add_warning(_err(codes.WARN_LEGACY_CONDITION_PLACEHOLDER,
                                    f"'{f.label}' has an unfinished conditional rule (no field selected).", f))
        else:
            report.add_warning(_err(codes.WARN_UNREACHABLE_CONDITION,
                                    f"'{f.label}' has a conditional rule that can never be evaluated.", f))
        return

    action = normalized.get("action", "show")
    if action not in CONDITIONAL_ACTIONS:
        report.add_error(_err(codes.DEF_CONDITION_UNKNOWN_ACTION,
                              f"'{f.label}': conditional action '{action}' is not supported.", f))

    self_id = None
    try:
        self_id = int(f.id)
    except (TypeError, ValueError):
        pass

    def _walk(node):
        if "rules" in node:
            for r in node["rules"]:
                yield from _walk(r)
        else:
            yield node

    for leaf in _walk(normalized):
        op = leaf.get("operator")
        if op not in CONDITIONAL_OPERATORS:
            report.add_error(_err(codes.DEF_CONDITION_UNKNOWN_OPERATOR,
                                  f"'{f.label}': conditional operator '{op}' is not supported.", f))
        ref = leaf.get("field")
        ref_raw = leaf.get("field_raw")
        if ref is None:
            report.add_warning(_err(codes.WARN_LEGACY_CONDITION_PLACEHOLDER,
                                    f"'{f.label}' has a conditional rule with no valid target field "
                                    f"(got '{ref_raw}').", f))
            continue
        if self_id is not None and ref == self_id:
            report.add_error(_err(codes.DEF_CONDITION_SELF_REFERENCE,
                                  f"'{f.label}' has a conditional rule that references itself.", f))
        elif ref not in by_id:
            report.add_error(_err(codes.DEF_CONDITION_UNKNOWN_FIELD,
                                  f"'{f.label}' has a conditional rule referencing a field ({ref}) "
                                  f"that is not on this form.", f))


def _check_cross_field(report: DefinitionReport, f: _FieldView, by_id: dict[int, _FieldView]) -> None:
    cross = f.validation_rules.get("crossField") if isinstance(f.validation_rules, dict) else None
    if not isinstance(cross, list):
        return
    for rule in cross:
        if not isinstance(rule, dict):
            report.add_error(_err(codes.DEF_CROSS_FIELD_INCOMPATIBLE,
                                  f"'{f.label}': malformed cross-field rule.", f, rule="crossField"))
            continue
        other = rule.get("field", rule.get("otherField"))
        try:
            other = int(other)
        except (TypeError, ValueError):
            report.add_error(_err(codes.DEF_CROSS_FIELD_UNKNOWN_FIELD,
                                  f"'{f.label}': cross-field rule has no valid target field.", f, rule="crossField"))
            continue
        target = by_id.get(other)
        if target is None:
            report.add_error(_err(codes.DEF_CROSS_FIELD_UNKNOWN_FIELD,
                                  f"'{f.label}': cross-field rule references a field ({other}) not on this form.",
                                  f, rule="crossField"))
            continue
        op = str(rule.get("op", "")).lower()
        if op in ("lt", "lte", "gt", "gte") and target.type not in _COMPARABLE_TYPES:
            report.add_error(_err(codes.DEF_CROSS_FIELD_INCOMPATIBLE,
                                  f"'{f.label}': cannot order-compare against a {target.type} question.",
                                  f, rule="crossField"))
