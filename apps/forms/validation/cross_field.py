"""
Cross-field validation — rules that compare one answer against another.

Configured on the field that "owns" the comparison, inside its
``validation_rules``::

    "crossField": [
      { "op": "eq",  "field": 12, "message": "Passwords must match." },
      { "op": "lte", "field": 20 },
      { "op": "required_if", "field": 8, "equals": "Employed" }
    ]

Operators:
  eq / ne / lt / lte / gt / gte  — compare this field's value to the other field's
  required_if                    — this field must be non-empty when the other
                                   field equals ``equals`` (or is simply non-empty
                                   if ``equals`` is omitted)

Only rules where BOTH fields are currently visible are evaluated.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from . import codes
from .report import FieldError
from .conditional import _cmp_pair
from .values import is_empty


_COMPARATORS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
}
_PHRASING = {
    "eq": "must match", "ne": "must be different from",
    "lt": "must be less than", "lte": "must be less than or equal to",
    "gt": "must be greater than", "gte": "must be greater than or equal to",
}


def evaluate_cross_field(form_fields_by_id: dict[int, Any],
                         values: dict[int, Any],
                         visible_ids: set[int]) -> list[FieldError]:
    out: list[FieldError] = []

    for fid, field in form_fields_by_id.items():
        if fid not in visible_ids:
            continue
        rules = field.validation_rules or {}
        cross = rules.get("crossField")
        if not isinstance(cross, list):
            continue

        for rule in cross:
            if not isinstance(rule, dict):
                continue
            op = str(rule.get("op", "")).strip().lower()
            other_id = rule.get("field", rule.get("otherField"))
            try:
                other_id = int(other_id)
            except (TypeError, ValueError):
                continue
            other_field = form_fields_by_id.get(other_id)
            if other_field is None or other_id not in visible_ids:
                continue

            this_val = values.get(fid)
            other_val = values.get(other_id)
            msg = rule.get("message")

            if op == "required_if":
                trigger = rule.get("equals", rule.get("value"))
                other_matches = (
                    not is_empty(other_val) if trigger in (None, "")
                    else str(other_val).strip().lower() == str(trigger).strip().lower()
                )
                if other_matches and is_empty(this_val):
                    out.append(FieldError(
                        code=codes.CROSS_FIELD_REQUIRED,
                        message=msg or f"'{field.label}' is required when '{other_field.label}' is answered.",
                        field_id=fid, label=field.label, rule="crossField",
                        context={"op": op, "other_field": other_id},
                    ))
                continue

            comparator = _COMPARATORS.get(op)
            if not comparator:
                continue
            # Skip when either side is unanswered — required-ness is a separate concern.
            if is_empty(this_val) or is_empty(other_val):
                continue

            left, right = _cmp_pair(this_val, other_val)
            try:
                ok = comparator(left, right)
            except TypeError:
                ok = comparator(str(this_val).strip().lower(), str(other_val).strip().lower())

            if not ok:
                code = codes.CROSS_FIELD_MISMATCH if op in ("eq", "ne") else codes.CROSS_FIELD_ORDER
                out.append(FieldError(
                    code=code,
                    message=msg or f"'{field.label}' {_PHRASING.get(op, 'is invalid relative to')} '{other_field.label}'.",
                    field_id=fid, label=field.label, rule="crossField",
                    context={"op": op, "other_field": other_id},
                ))

    return out
