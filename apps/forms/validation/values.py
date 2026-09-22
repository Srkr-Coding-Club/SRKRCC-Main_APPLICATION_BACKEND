"""
Value-level helpers shared by every validation layer.

``is_empty``      — the single definition of "unanswered", consistent across
                    required-checks and conditional evaluation.
``coerce_value``  — turn a raw submitted value into the typed Python value the
                    field's validators expect, or raise ``CoercionError``.

Coercion is deliberately lenient about *input* shape (the frontend sends numbers
as strings, dates as ``"YYYY-MM-DD"`` strings, etc.) but strict about *type*
(a CHECKBOX value that is a bare string is a client bug, not a single-selection).
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal, InvalidOperation
from typing import Any

from apps.forms.models import FieldType
from . import codes


class CoercionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


_SCALAR_TEXT_TYPES = {
    FieldType.TEXT, FieldType.PARAGRAPH, FieldType.EMAIL,
    FieldType.PHONE, FieldType.URL, FieldType.SIGNATURE,
    FieldType.CLUB_ID,
    FieldType.RADIO, FieldType.DROPDOWN,
}
_MATRIX_TYPES = {FieldType.MATRIX_RADIO, FieldType.MATRIX_CHECKBOX}
_FILE_TYPES = {FieldType.FILE, FieldType.MULTI_FILE}


def is_empty(value: Any) -> bool:
    """True when a value counts as "not answered"."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def parse_date(raw: Any) -> _dt.date:
    if isinstance(raw, _dt.datetime):
        return raw.date()
    if isinstance(raw, _dt.date):
        return raw
    s = str(raw).strip()
    # Accept a full ISO datetime too, take the date part.
    for parser in (_dt.date.fromisoformat, lambda x: _dt.datetime.fromisoformat(x).date()):
        try:
            return parser(s)
        except (ValueError, TypeError):
            continue
    raise CoercionError(codes.INVALID_DATE, f"'{s}' is not a valid date (expected YYYY-MM-DD).")


def parse_time(raw: Any) -> _dt.time:
    if isinstance(raw, _dt.time):
        return raw
    if isinstance(raw, _dt.datetime):
        return raw.time()
    s = str(raw).strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return _dt.datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    try:
        return _dt.time.fromisoformat(s)
    except (ValueError, TypeError):
        raise CoercionError(codes.INVALID_TIME, f"'{s}' is not a valid time (expected HH:MM).")


def parse_number(raw: Any) -> Decimal:
    if isinstance(raw, bool):
        raise CoercionError(codes.INVALID_NUMBER, "Expected a number, got a boolean.")
    if isinstance(raw, (int, float, Decimal)):
        try:
            return Decimal(str(raw))
        except InvalidOperation:
            raise CoercionError(codes.INVALID_NUMBER, f"'{raw}' is not a valid number.")
    s = str(raw).strip().replace(",", "")
    if s == "":
        raise CoercionError(codes.INVALID_NUMBER, "Expected a number.")
    try:
        return Decimal(s)
    except InvalidOperation:
        raise CoercionError(codes.INVALID_NUMBER, f"'{raw}' is not a valid number.")


def coerce_value(field, raw: Any) -> Any:
    """
    Return the typed value for ``field``. Assumes the caller has already decided
    the value is non-empty. Raises ``CoercionError`` on a genuine type mismatch.
    """
    ftype = field.type

    if ftype in _SCALAR_TEXT_TYPES:
        if isinstance(raw, (list, dict)):
            raise CoercionError(codes.EXPECTED_SCALAR, f"Field '{field.label}' expects a single value.")
        return str(raw).strip()

    if ftype in (FieldType.NUMBER, FieldType.RATING, FieldType.LINEAR_SCALE):
        if isinstance(raw, (list, dict)):
            raise CoercionError(codes.EXPECTED_SCALAR, f"Field '{field.label}' expects a single number.")
        return parse_number(raw)

    if ftype == FieldType.DATE:
        return parse_date(raw)

    if ftype == FieldType.TIME:
        return parse_time(raw)

    if ftype == FieldType.CHECKBOX:
        if isinstance(raw, str):
            # tolerate a single value sent as a bare string
            return [raw.strip()] if raw.strip() else []
        if not isinstance(raw, (list, tuple)):
            raise CoercionError(codes.EXPECTED_LIST, f"Field '{field.label}' expects a list of selections.")
        return [str(v).strip() for v in raw]

    if ftype in _MATRIX_TYPES:
        if not isinstance(raw, dict):
            raise CoercionError(
                codes.EXPECTED_OBJECT,
                f"Field '{field.label}' expects a matrix answer shaped {{\"Row\": \"Column\"}}.",
            )
        return {str(k): v for k, v in raw.items()}

    if ftype in _FILE_TYPES:
        items = raw if isinstance(raw, list) else [raw]
        cleaned: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict) or "name" not in item:
                raise CoercionError(
                    codes.INVALID_FILE_PAYLOAD,
                    f"Field '{field.label}' expects file objects shaped {{\"name\": ..., \"size\": ...}}.",
                )
            size = item.get("size", 0)
            try:
                size = int(size)
            except (TypeError, ValueError):
                size = 0
            entry = {"name": str(item["name"]), "size": size}
            if item.get("type"):
                entry["type"] = str(item["type"])
            if item.get("url"):
                # data: URI (inline capture) or an absolute URL — kept verbatim
                # so the responses viewer can render / download it.
                entry["url"] = str(item["url"])
            cleaned.append(entry)
        return cleaned

    if ftype == FieldType.SECTION:
        return None

    # Unknown type — pass through untouched.
    return raw
