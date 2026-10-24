"""
Value-level helpers shared by every validation layer.

``is_empty``      - the single definition of "unanswered", consistent across
                    required-checks and conditional evaluation.
``coerce_value``  - turn a raw submitted value into the typed Python value the
                    field's validators expect, or raise ``CoercionError``.

Coercion is deliberately lenient about *input* shape (the frontend sends numbers
as strings, dates as ``"YYYY-MM-DD"`` strings, etc.) but strict about *type*
(a CHECKBOX value that is a bare string is a client bug, not a single-selection)
and about *safety*: control characters, oversized values, non-finite numbers and
unverifiable file payloads are rejected here, before any rule runs.
"""

from __future__ import annotations

import datetime as _dt
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from apps.forms.models import FieldType, NON_ANSWER_FIELD_TYPES
from . import codes
from . import limits
from .uploads import PayloadError, PNG_MAGIC, check_magic, check_plain_url, inspect_data_url


class CoercionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


_SCALAR_TEXT_TYPES = {
    FieldType.TEXT, FieldType.PARAGRAPH, FieldType.EMAIL,
    FieldType.PHONE, FieldType.URL,
    FieldType.CLUB_ID, FieldType.HIDDEN,
    FieldType.RADIO, FieldType.DROPDOWN,
}
_MATRIX_TYPES = {FieldType.MATRIX_RADIO, FieldType.MATRIX_CHECKBOX}
_FILE_TYPES = {FieldType.FILE, FieldType.MULTI_FILE}

_GROUPED_NUMBER_RE = re.compile(r"[+-]?\d{1,3}(,\d{3})+(\.\d+)?")
# Control characters (except tab / LF / CR) and lone surrogates: PostgreSQL JSON
# cannot store them and they are never legitimate form input.
_FORBIDDEN_CHARS_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]")


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
            d = Decimal(str(raw))
        except InvalidOperation:
            raise CoercionError(codes.INVALID_NUMBER, f"'{raw}' is not a valid number.")
        return _check_number(d, raw)
    s = str(raw).strip()
    if s == "":
        raise CoercionError(codes.INVALID_NUMBER, "Expected a number.")
    if len(s) > limits.MAX_NUMBER_INPUT_LENGTH:
        raise CoercionError(codes.INVALID_NUMBER, "That number is too long.")
    if "," in s:
        # Thousands separators are only accepted in proper groups (1,234 / 1,234.5);
        # "1,2,3" must not silently become 123.
        if not _GROUPED_NUMBER_RE.fullmatch(s):
            raise CoercionError(codes.INVALID_NUMBER, f"'{raw}' is not a valid number.")
        s = s.replace(",", "")
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise CoercionError(codes.INVALID_NUMBER, f"'{raw}' is not a valid number.")
    return _check_number(d, raw)


def _check_number(d: Decimal, raw: Any) -> Decimal:
    """Reject NaN / Infinity and values too large or too precise to store safely."""
    if not d.is_finite():
        raise CoercionError(codes.INVALID_NUMBER, f"'{raw}' is not a valid number.")
    if d.adjusted() > limits.MAX_NUMBER_MAGNITUDE:
        raise CoercionError(codes.INVALID_NUMBER, "That number is too large.")
    if d != d.to_integral_value():
        parts = d.as_tuple()
        if -parts.exponent > limits.MAX_FRACTIONAL_DIGITS or len(parts.digits) > limits.MAX_FRACTIONAL_DIGITS:
            raise CoercionError(codes.INVALID_NUMBER, "That number has too many decimal places.")
    return d


def clean_text(label: str, s: str, max_length: int | None = None) -> str:
    """Validate the characters and length of one submitted string."""
    if _FORBIDDEN_CHARS_RE.search(s):
        raise CoercionError(codes.INVALID_CHARACTERS, f"'{label}' contains characters that are not allowed.")
    if max_length is not None and len(s) > max_length:
        raise CoercionError(codes.VALUE_TOO_LONG, f"'{label}' is too long (maximum {max_length} characters).")
    return s


def _scalar_item(label: str, v: Any) -> str:
    if isinstance(v, (list, dict)):
        raise CoercionError(codes.EXPECTED_SCALAR, f"'{label}' contains a nested value where a single value was expected.")
    return clean_text(label, str(v).strip(), limits.MAX_ITEM_LENGTH)


def _coerce_files(field, raw: Any) -> list[dict[str, Any]]:
    items = raw if isinstance(raw, list) else [raw]
    if len(items) > limits.MAX_COLLECTION_ITEMS:
        raise CoercionError(codes.TOO_MANY_ITEMS, f"Field '{field.label}' has too many files.")
    cleaned: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or "name" not in item:
            raise CoercionError(
                codes.INVALID_FILE_PAYLOAD,
                f"Field '{field.label}' expects file objects shaped {{\"name\": ..., \"size\": ...}}.",
            )
        name = clean_text(field.label, str(item["name"]).strip(), limits.MAX_FILE_NAME_LENGTH)
        try:
            size = max(int(item.get("size", 0)), 0)
        except (TypeError, ValueError):
            size = 0
        entry: dict[str, Any] = {"name": name, "size": size}
        if item.get("type"):
            entry["type"] = clean_text(field.label, str(item["type"]).strip(), 255)
        url = item.get("url")
        if url:
            url = str(url).strip()
            try:
                if url.lower().startswith("data:"):
                    mime, real_size, head = inspect_data_url(url, max_bytes=limits.MAX_INLINE_FILE_BYTES)
                    mismatch = check_magic(name, head, real_size)
                    if mismatch:
                        raise PayloadError(codes.FILE_CONTENT_MISMATCH, mismatch)
                    # The client's size / type claims are replaced with what the payload really is.
                    entry["size"] = real_size
                    entry["type"] = mime
                else:
                    check_plain_url(url)
            except PayloadError as exc:
                raise CoercionError(exc.code, f"'{name}': {exc.message}")
            entry["url"] = url
        cleaned.append(entry)
    return cleaned


def _coerce_signature(field, raw: Any) -> str:
    if isinstance(raw, (list, dict)):
        raise CoercionError(codes.EXPECTED_SCALAR, f"Field '{field.label}' expects a single value.")
    s = str(raw).strip()
    try:
        if s.lower().startswith("data:"):
            mime, _size, head = inspect_data_url(
                s, max_bytes=limits.MAX_SIGNATURE_BYTES, code=codes.INVALID_SIGNATURE,
            )
            if mime != "image/png" or not head.startswith(PNG_MAGIC):
                raise PayloadError(codes.INVALID_SIGNATURE, "A signature must be a PNG image.")
        else:
            check_plain_url(s, codes.INVALID_SIGNATURE)
    except PayloadError as exc:
        raise CoercionError(exc.code, f"'{field.label}': {exc.message}")
    return s


def coerce_value(field, raw: Any) -> Any:
    """
    Return the typed value for ``field``. Assumes the caller has already decided
    the value is non-empty. Raises ``CoercionError`` on a genuine type mismatch.
    """
    ftype = field.type

    if ftype in _SCALAR_TEXT_TYPES:
        if isinstance(raw, (list, dict)):
            raise CoercionError(codes.EXPECTED_SCALAR, f"Field '{field.label}' expects a single value.")
        return clean_text(field.label, str(raw).strip(), limits.MAX_TEXT_LENGTH.get(ftype))

    if ftype == FieldType.SIGNATURE:
        return _coerce_signature(field, raw)

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
            return [_scalar_item(field.label, raw)] if raw.strip() else []
        if not isinstance(raw, (list, tuple)):
            raise CoercionError(codes.EXPECTED_LIST, f"Field '{field.label}' expects a list of selections.")
        if len(raw) > limits.MAX_COLLECTION_ITEMS:
            raise CoercionError(codes.TOO_MANY_ITEMS, f"Field '{field.label}' has too many selections.")
        return [_scalar_item(field.label, v) for v in raw]

    if ftype in _MATRIX_TYPES:
        if not isinstance(raw, dict):
            raise CoercionError(
                codes.EXPECTED_OBJECT,
                f"Field '{field.label}' expects a matrix answer shaped {{\"Row\": \"Column\"}}.",
            )
        if len(raw) > limits.MAX_COLLECTION_ITEMS:
            raise CoercionError(codes.TOO_MANY_ITEMS, f"Field '{field.label}' has too many rows.")
        out: dict[str, Any] = {}
        for k, cell in raw.items():
            key = _scalar_item(field.label, k)
            if isinstance(cell, (list, tuple)):
                if len(cell) > limits.MAX_COLLECTION_ITEMS:
                    raise CoercionError(codes.TOO_MANY_ITEMS, f"Field '{field.label}' has too many selections.")
                out[key] = [_scalar_item(field.label, c) for c in cell]
            elif cell is None or cell == "":
                out[key] = cell
            else:
                out[key] = _scalar_item(field.label, cell)
        return out

    if ftype in _FILE_TYPES:
        return _coerce_files(field, raw)

    if ftype in NON_ANSWER_FIELD_TYPES:
        return None

    # Unknown type - pass through untouched.
    return raw
