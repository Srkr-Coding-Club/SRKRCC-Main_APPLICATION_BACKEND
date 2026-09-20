"""
Per-field-type validators — the "does this value make sense for a field of this
type at all" layer, independent of any configured ``validation_rules``.

Each validator receives ``(field, value)`` where ``value`` has already been
coerced by ``values.coerce_value`` and is known non-empty, and returns a list of
``FieldError``. Registered in ``FIELD_VALIDATORS`` keyed by ``FieldType``.

This is where option-membership for choice fields, matrix row/column checks and
the RATING / LINEAR_SCALE range live — things that come from the field's own
``options`` / ``rows`` / ``min_value`` / ``max_value`` rather than
``validation_rules``.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Callable

from django.core.validators import EmailValidator, URLValidator
from django.core.exceptions import ValidationError as DjangoValidationError

from apps.forms.models import FieldType
from . import codes
from .report import FieldError

_email_validator = EmailValidator()
_url_validator = URLValidator(schemes=["http", "https"])

_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,}$")
_SLUG_RE = re.compile(r"^[-a-zA-Z0-9_]+$")
_PHONE_RE = re.compile(r"^\+?[0-9][0-9\s\-().]{5,}$")


def _err(field, code: str, message: str, rule: str | None = None, **context) -> FieldError:
    return FieldError(
        code=code, message=message, field_id=field.id, label=field.label,
        rule=rule, context=context or None,
    )


# ---------------------------------------------------------------------------
# Typed-text fields
# ---------------------------------------------------------------------------

def _validate_email(field, value: Any) -> list[FieldError]:
    rules = field.validation_rules or {}
    raw = str(value).strip()
    candidates = re.split(r"[,;]\s*", raw) if rules.get("allowMultiple") else [raw]
    out: list[FieldError] = []
    for addr in candidates:
        try:
            _email_validator(addr)
        except DjangoValidationError:
            out.append(_err(field, codes.INVALID_EMAIL,
                            f"'{addr}' is not a valid email address."))
    return out


def _validate_url(field, value: Any) -> list[FieldError]:
    try:
        _url_validator(str(value).strip())
    except DjangoValidationError:
        return [_err(field, codes.INVALID_URL, f"'{value}' is not a valid URL.")]
    return []


def _validate_phone(field, value: Any) -> list[FieldError]:
    rules = field.validation_rules or {}
    raw = str(value).strip()
    pattern = rules.get("pattern")
    if pattern:
        try:
            if not re.search(pattern, raw):
                return [_err(field, codes.INVALID_PHONE,
                             rules.get("patternError") or "Phone number format is invalid.",
                             rule="pattern")]
        except re.error:
            pass
        return []
    if not _PHONE_RE.match(raw):
        return [_err(field, codes.INVALID_PHONE,
                     "Enter a valid phone number (digits, spaces, +, - and () only).")]
    return []


def _validate_number(field, value: Decimal) -> list[FieldError]:
    # coerce_value already guaranteed a Decimal; range/integer rules are in rules.py
    return []


def _validate_scale(field, value: Decimal) -> list[FieldError]:
    """RATING / LINEAR_SCALE — range comes from the model columns min_value/max_value."""
    out: list[FieldError] = []
    lo = field.min_value if field.min_value is not None else 1
    hi = field.max_value if field.max_value is not None else 5
    rules = field.validation_rules or {}
    integer_only = rules.get("integerOnly", True)

    if integer_only and value != value.to_integral_value():
        out.append(_err(field, codes.MUST_BE_INTEGER,
                        f"'{value}' must be a whole number.", rule="integerOnly"))
    if value < lo or value > hi:
        out.append(_err(field, codes.OUT_OF_RANGE,
                        f"Choose a value between {lo} and {hi}.",
                        rule="scale", min=lo, max=hi))
    return out


# ---------------------------------------------------------------------------
# Choice fields
# ---------------------------------------------------------------------------

def _option_set(field) -> set[str]:
    return {str(o).strip() for o in (field.options or [])}


def _matches_option(value: str, options: set[str]) -> bool:
    v = value.strip()
    if v in options:
        return True
    low = v.lower()
    return any(low == o.lower() for o in options)


def _validate_single_choice(field, value: Any) -> list[FieldError]:
    options = _option_set(field)
    rules = field.validation_rules or {}
    if not options:
        return []
    if rules.get("allowOther"):
        return []
    if not _matches_option(str(value), options):
        return [_err(field, codes.OPTION_NOT_ALLOWED,
                     f"'{value}' is not one of the allowed choices.",
                     rule="options", allowed=sorted(options))]
    return []


def _validate_checkbox(field, value: list) -> list[FieldError]:
    options = _option_set(field)
    rules = field.validation_rules or {}
    out: list[FieldError] = []

    seen: set[str] = set()
    for item in value:
        key = str(item).strip().lower()
        if key in seen:
            out.append(_err(field, codes.DUPLICATE_SELECTION,
                            f"'{item}' is selected more than once."))
        seen.add(key)
        if options and not rules.get("allowOther") and not _matches_option(str(item), options):
            out.append(_err(field, codes.OPTION_NOT_ALLOWED,
                            f"'{item}' is not one of the allowed choices.",
                            rule="options", allowed=sorted(options)))
    return out


# ---------------------------------------------------------------------------
# Matrix fields
# ---------------------------------------------------------------------------

def _validate_matrix(field, value: dict, multi: bool) -> list[FieldError]:
    rows = {str(r).strip() for r in (field.rows or [])}
    cols = _option_set(field)
    out: list[FieldError] = []

    for row_key, cell in value.items():
        rk = str(row_key).strip()
        if rows and rk not in rows and not any(rk.lower() == r.lower() for r in rows):
            out.append(_err(field, codes.UNKNOWN_MATRIX_ROW,
                            f"'{row_key}' is not a row of this question.",
                            rule="rows"))
            continue
        cell_values = cell if isinstance(cell, (list, tuple)) else [cell]
        if not multi and isinstance(cell, (list, tuple)) and len(cell) > 1:
            out.append(_err(field, codes.EXPECTED_SCALAR,
                            f"Row '{row_key}' allows only one selection."))
        for cv in cell_values:
            if cols and not _matches_option(str(cv), cols):
                out.append(_err(field, codes.UNKNOWN_MATRIX_COLUMN,
                                f"'{cv}' is not a column of this question.",
                                rule="options", allowed=sorted(cols)))
    return out


def _validate_matrix_radio(field, value: dict) -> list[FieldError]:
    return _validate_matrix(field, value, multi=False)


def _validate_matrix_checkbox(field, value: dict) -> list[FieldError]:
    return _validate_matrix(field, value, multi=True)


# ---------------------------------------------------------------------------
# File fields
# ---------------------------------------------------------------------------

def _validate_files(field, value: list[dict]) -> list[FieldError]:
    # Structural file checks (name present, size numeric) already done in coerce.
    # Extension / size / count constraints live in rules.py.
    out: list[FieldError] = []
    for item in value:
        name = item.get("name", "")
        if not name or "\x00" in name or "/" in name or "\\" in name:
            out.append(_err(field, codes.INVALID_FILE_PAYLOAD,
                            f"'{name}' is not a valid file name."))
    return out


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

FIELD_VALIDATORS: dict[str, Callable[[Any, Any], list[FieldError]]] = {
    FieldType.EMAIL: _validate_email,
    FieldType.URL: _validate_url,
    FieldType.PHONE: _validate_phone,
    FieldType.NUMBER: _validate_number,
    FieldType.RATING: _validate_scale,
    FieldType.LINEAR_SCALE: _validate_scale,
    FieldType.RADIO: _validate_single_choice,
    FieldType.DROPDOWN: _validate_single_choice,
    FieldType.CHECKBOX: _validate_checkbox,
    FieldType.MATRIX_RADIO: _validate_matrix_radio,
    FieldType.MATRIX_CHECKBOX: _validate_matrix_checkbox,
    FieldType.FILE: _validate_files,
    FieldType.MULTI_FILE: _validate_files,
}


def validate_field_type(field, value: Any) -> list[FieldError]:
    validator = FIELD_VALIDATORS.get(field.type)
    if not validator:
        return []
    return validator(field, value)
