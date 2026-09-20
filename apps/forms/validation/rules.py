"""
``validation_rules`` enforcement — one small function per rule key.

Every function signature is ``fn(field, value, rule_value, rules) -> FieldError | None``:
  * ``value``      — the coerced submitted value (str / Decimal / list / date / …)
  * ``rule_value`` — the configured value for this rule key
  * ``rules``      — the whole normalized ``validation_rules`` dict (for custom
                     messages and rules that reference siblings)

``apply_rules(field, value)`` walks the field's configured rules and returns all
failures. Rule keys not applicable to the value's shape are skipped silently
(the definition validator is what rejects incompatible configs at publish time).
"""

from __future__ import annotations

import datetime as _dt
import re
from decimal import Decimal
from typing import Any, Callable

from . import codes
from .report import FieldError
from .values import parse_date, parse_time, parse_number, CoercionError

RuleFn = Callable[[Any, Any, Any, dict], "FieldError | None"]

RULE_VALIDATORS: dict[str, RuleFn] = {}


def _register(*keys: str):
    def deco(fn: RuleFn):
        for k in keys:
            RULE_VALIDATORS[k] = fn
        return fn
    return deco


def _err(field, code, message, rule, **ctx):
    custom = (field.validation_rules or {}).get("patternError")
    return FieldError(
        code=code,
        message=custom or message,
        field_id=field.id,
        label=field.label,
        rule=rule,
        context=ctx or None,
    )


def _text(value) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, dict)):
        return None
    return str(value)


def _num(value) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    try:
        return parse_number(value)
    except CoercionError:
        return None


def _list(value) -> list | None:
    return list(value) if isinstance(value, (list, tuple)) else None


# ---------------------------------------------------------------------------
# Length / text-shape
# ---------------------------------------------------------------------------

@_register("minLength")
def _min_length(field, value, rv, rules):
    s = _text(value)
    if s is None or rv is None:
        return None
    if len(s) < rv:
        return _err(field, codes.MIN_LENGTH,
                    f"Must be at least {rv} character(s).", "minLength", min=rv, actual=len(s))


@_register("maxLength")
def _max_length(field, value, rv, rules):
    s = _text(value)
    if s is None or rv is None:
        return None
    if len(s) > rv:
        return _err(field, codes.MAX_LENGTH,
                    f"Must be at most {rv} character(s).", "maxLength", max=rv, actual=len(s))


@_register("exactLength")
def _exact_length(field, value, rv, rules):
    s = _text(value)
    if s is None or rv is None:
        return None
    if len(s) != rv:
        return _err(field, codes.EXACT_LENGTH,
                    f"Must be exactly {rv} character(s).", "exactLength", expected=rv, actual=len(s))


@_register("minWords")
def _min_words(field, value, rv, rules):
    s = _text(value)
    if s is None or rv is None:
        return None
    if len(s.split()) < rv:
        return _err(field, codes.MIN_WORDS, f"Must be at least {rv} word(s).", "minWords", min=rv)


@_register("maxWords")
def _max_words(field, value, rv, rules):
    s = _text(value)
    if s is None or rv is None:
        return None
    if len(s.split()) > rv:
        return _err(field, codes.MAX_WORDS, f"Must be at most {rv} word(s).", "maxWords", max=rv)


@_register("pattern")
def _pattern(field, value, rv, rules):
    s = _text(value)
    if s is None or not rv:
        return None
    try:
        if re.search(rv, s) is None:
            return _err(field, codes.PATTERN_MISMATCH,
                        "Value does not match the required format.", "pattern")
    except re.error:
        return None  # invalid admin pattern — definition validator flags it; don't block here


_FORMAT_CHECKS: dict[str, Callable[[str], bool]] = {
    "alpha": lambda s: bool(re.fullmatch(r"[A-Za-z ]+", s)),
    "alphabetic": lambda s: bool(re.fullmatch(r"[A-Za-z ]+", s)),
    "alphanumeric": lambda s: bool(re.fullmatch(r"[A-Za-z0-9]+", s)),
    "numeric": lambda s: bool(re.fullmatch(r"[0-9]+", s)),
    "integer": lambda s: bool(re.fullmatch(r"-?[0-9]+", s)),
    "decimal": lambda s: bool(re.fullmatch(r"-?[0-9]+(\.[0-9]+)?", s)),
    "username": lambda s: bool(re.fullmatch(r"[A-Za-z0-9._-]{3,}", s)),
    "slug": lambda s: bool(re.fullmatch(r"[-a-zA-Z0-9_]+", s)),
}
_FORMAT_LABEL = {
    "alpha": "Only letters and spaces are allowed.",
    "alphabetic": "Only letters and spaces are allowed.",
    "alphanumeric": "Only letters and digits are allowed.",
    "numeric": "Only digits are allowed.",
    "integer": "Enter a whole number.",
    "decimal": "Enter a number.",
    "username": "Use 3+ letters, digits, dots, dashes or underscores.",
    "slug": "Use letters, digits, dashes and underscores only.",
}


@_register("format")
def _format(field, value, rv, rules):
    s = _text(value)
    if s is None or not rv or rv in ("any", ""):
        return None
    fmt = str(rv).lower()
    if fmt == "email":
        from .field_types import _email_validator
        from django.core.exceptions import ValidationError as DVE
        try:
            _email_validator(s)
        except DVE:
            return _err(field, codes.INVALID_EMAIL, "Enter a valid email address.", "format", format=fmt)
        return None
    if fmt == "url":
        from .field_types import _url_validator
        from django.core.exceptions import ValidationError as DVE
        try:
            _url_validator(s)
        except DVE:
            return _err(field, codes.INVALID_URL, "Enter a valid URL.", "format", format=fmt)
        return None
    if fmt == "phone":
        if not re.fullmatch(r"\+?[0-9][0-9\s\-().]{5,}", s):
            return _err(field, codes.INVALID_PHONE, "Enter a valid phone number.", "format", format=fmt)
        return None
    if fmt == "date":
        try:
            parse_date(s)
        except CoercionError:
            return _err(field, codes.INVALID_DATE, "Enter a valid date (YYYY-MM-DD).", "format", format=fmt)
        return None
    if fmt in ("time",):
        try:
            parse_time(s)
        except CoercionError:
            return _err(field, codes.INVALID_TIME, "Enter a valid time (HH:MM).", "format", format=fmt)
        return None
    check = _FORMAT_CHECKS.get(fmt)
    if check and not check(s):
        return _err(field, codes.INVALID_FORMAT, _FORMAT_LABEL.get(fmt, "Invalid format."),
                    "format", format=fmt)


@_register("allowedChars")
def _allowed_chars(field, value, rv, rules):
    s = _text(value)
    if s is None or not rv:
        return None
    allowed = set(str(rv))
    bad = {c for c in s if c not in allowed and not c.isspace()}
    if bad:
        return _err(field, codes.CHARACTER_NOT_ALLOWED,
                    f"These characters are not allowed: {''.join(sorted(bad))}", "allowedChars")


@_register("disallowedChars")
def _disallowed_chars(field, value, rv, rules):
    s = _text(value)
    if s is None or not rv:
        return None
    banned = set(str(rv))
    hit = {c for c in s if c in banned}
    if hit:
        return _err(field, codes.DISALLOWED_CHARACTER,
                    f"These characters are not allowed: {''.join(sorted(hit))}", "disallowedChars")


@_register("startsWith")
def _starts_with(field, value, rv, rules):
    s = _text(value)
    if s is None or not rv:
        return None
    if not s.startswith(str(rv)):
        return _err(field, codes.MUST_START_WITH, f"Must start with '{rv}'.", "startsWith")


@_register("endsWith")
def _ends_with(field, value, rv, rules):
    s = _text(value)
    if s is None or not rv:
        return None
    if not s.endswith(str(rv)):
        return _err(field, codes.MUST_END_WITH, f"Must end with '{rv}'.", "endsWith")


@_register("contains")
def _contains(field, value, rv, rules):
    s = _text(value)
    if s is None or not rv:
        return None
    if str(rv) not in s:
        return _err(field, codes.MUST_CONTAIN, f"Must contain '{rv}'.", "contains")


@_register("notContains")
def _not_contains(field, value, rv, rules):
    s = _text(value)
    if s is None or not rv:
        return None
    if str(rv) in s:
        return _err(field, codes.MUST_NOT_CONTAIN, f"Must not contain '{rv}'.", "notContains")


# ---------------------------------------------------------------------------
# Numeric
# ---------------------------------------------------------------------------

def _num_bound(field, value, rv, code, op, msg, rule):
    n = _num(value)
    b = _num(rv)
    if n is None or b is None:
        return None
    ok = {
        "gte": n >= b, "lte": n <= b, "gt": n > b, "lt": n < b, "eq": n == b,
    }[op]
    if not ok:
        return _err(field, code, msg.format(b=b), rule, limit=str(b), actual=str(n))


@_register("minValue")
def _min_value(field, value, rv, rules):
    return _num_bound(field, value, rv, codes.MIN_VALUE, "gte", "Must be at least {b}.", "minValue")


@_register("maxValue")
def _max_value(field, value, rv, rules):
    return _num_bound(field, value, rv, codes.MAX_VALUE, "lte", "Must be at most {b}.", "maxValue")


@_register("exactValue")
def _exact_value(field, value, rv, rules):
    return _num_bound(field, value, rv, codes.EXACT_VALUE, "eq", "Must be exactly {b}.", "exactValue")


@_register("gt")
def _gt(field, value, rv, rules):
    return _num_bound(field, value, rv, codes.NOT_GREATER_THAN, "gt", "Must be greater than {b}.", "gt")


@_register("gte")
def _gte(field, value, rv, rules):
    return _num_bound(field, value, rv, codes.NOT_GREATER_OR_EQUAL, "gte",
                      "Must be greater than or equal to {b}.", "gte")


@_register("lt")
def _lt(field, value, rv, rules):
    return _num_bound(field, value, rv, codes.NOT_LESS_THAN, "lt", "Must be less than {b}.", "lt")


@_register("lte")
def _lte(field, value, rv, rules):
    return _num_bound(field, value, rv, codes.NOT_LESS_OR_EQUAL, "lte",
                      "Must be less than or equal to {b}.", "lte")


@_register("integerOnly")
def _integer_only(field, value, rv, rules):
    if not rv:
        return None
    n = _num(value)
    if n is None:
        return None
    if n != n.to_integral_value():
        return _err(field, codes.MUST_BE_INTEGER, "Must be a whole number.", "integerOnly")


@_register("positiveOnly")
def _positive_only(field, value, rv, rules):
    if not rv:
        return None
    n = _num(value)
    if n is not None and n <= 0:
        return _err(field, codes.MUST_BE_POSITIVE, "Must be a positive number.", "positiveOnly")


@_register("allowNegative")
def _allow_negative(field, value, rv, rules):
    if rv:
        return None
    n = _num(value)
    if n is not None and n < 0:
        return _err(field, codes.NEGATIVE_NOT_ALLOWED, "Negative numbers are not allowed.", "allowNegative")


@_register("step")
def _step(field, value, rv, rules):
    n = _num(value)
    step = _num(rv)
    if n is None or step is None or step == 0:
        return None
    base = _num(rules.get("minValue")) or Decimal(0)
    rem = (n - base) % step
    if rem != 0:
        return _err(field, codes.NOT_MULTIPLE_OF, f"Must be in increments of {step}.", "step", step=str(step))


# ---------------------------------------------------------------------------
# Email domain
# ---------------------------------------------------------------------------

def _domains(value: str) -> list[str]:
    out = []
    for addr in re.split(r"[,;]\s*", str(value)):
        if "@" in addr:
            out.append(addr.rsplit("@", 1)[1].strip().lower())
    return out


@_register("allowedDomains")
def _allowed_domains(field, value, rv, rules):
    if not rv:
        return None
    allow = {str(d).strip().lower().lstrip("@") for d in rv}
    for dom in _domains(value):
        if dom not in allow:
            return _err(field, codes.EMAIL_DOMAIN_NOT_ALLOWED,
                        f"Email must be on one of: {', '.join(sorted(allow))}.",
                        "allowedDomains", allowed=sorted(allow))


@_register("blockedDomains")
def _blocked_domains(field, value, rv, rules):
    if not rv:
        return None
    block = {str(d).strip().lower().lstrip("@") for d in rv}
    for dom in _domains(value):
        if dom in block:
            return _err(field, codes.EMAIL_DOMAIN_BLOCKED,
                        f"Emails from '{dom}' are not accepted.", "blockedDomains")


# ---------------------------------------------------------------------------
# Phone
# ---------------------------------------------------------------------------

@_register("minDigits")
def _min_digits(field, value, rv, rules):
    s = _text(value)
    if s is None or rv is None:
        return None
    digits = sum(c.isdigit() for c in s)
    if digits < rv:
        return _err(field, codes.MIN_LENGTH, f"Must contain at least {rv} digits.", "minDigits", min=rv)


@_register("maxDigits")
def _max_digits(field, value, rv, rules):
    s = _text(value)
    if s is None or rv is None:
        return None
    digits = sum(c.isdigit() for c in s)
    if digits > rv:
        return _err(field, codes.MAX_LENGTH, f"Must contain at most {rv} digits.", "maxDigits", max=rv)


@_register("numericOnly")
def _numeric_only(field, value, rv, rules):
    if not rv:
        return None
    s = _text(value)
    if s is None:
        return None
    if not re.fullmatch(r"\+?[0-9\s\-().]+", s):
        return _err(field, codes.INVALID_PHONE, "Only digits and phone punctuation are allowed.", "numericOnly")


@_register("requireHttps")
def _require_https(field, value, rv, rules):
    if not rv:
        return None
    s = _text(value)
    if s and not s.lower().startswith("https://"):
        return _err(field, codes.INVALID_URL, "URL must start with https://.", "requireHttps")


# ---------------------------------------------------------------------------
# Date / time
# ---------------------------------------------------------------------------

def _as_date(value):
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    try:
        return parse_date(value)
    except CoercionError:
        return None


def _as_time(value):
    if isinstance(value, _dt.time):
        return value
    try:
        return parse_time(value)
    except CoercionError:
        return None


def _bound_date(rv):
    if str(rv).strip().lower() == "today":
        return _dt.date.today()
    return _as_date(rv)


@_register("minDate")
def _min_date(field, value, rv, rules):
    d, b = _as_date(value), _bound_date(rv)
    if d and b and d < b:
        return _err(field, codes.DATE_TOO_EARLY, f"Date must be on or after {b.isoformat()}.", "minDate")


@_register("maxDate")
def _max_date(field, value, rv, rules):
    d, b = _as_date(value), _bound_date(rv)
    if d and b and d > b:
        return _err(field, codes.DATE_TOO_LATE, f"Date must be on or before {b.isoformat()}.", "maxDate")


@_register("notBefore")
def _not_before(field, value, rv, rules):
    return _min_date(field, value, rv, rules)


@_register("notAfter")
def _not_after(field, value, rv, rules):
    return _max_date(field, value, rv, rules)


@_register("pastOnly")
def _past_only(field, value, rv, rules):
    if not rv:
        return None
    d = _as_date(value)
    if d and d >= _dt.date.today():
        return _err(field, codes.DATE_MUST_BE_PAST, "Date must be in the past.", "pastOnly")


@_register("futureOnly")
def _future_only(field, value, rv, rules):
    if not rv:
        return None
    d = _as_date(value)
    if d and d <= _dt.date.today():
        return _err(field, codes.DATE_MUST_BE_FUTURE, "Date must be in the future.", "futureOnly")


@_register("allowToday")
def _allow_today(field, value, rv, rules):
    if rv:
        return None
    d = _as_date(value)
    if d and d == _dt.date.today():
        return _err(field, codes.DATE_TODAY_NOT_ALLOWED, "Today's date is not allowed.", "allowToday")


@_register("minTime")
def _min_time(field, value, rv, rules):
    t, b = _as_time(value), _as_time(rv)
    if t and b and t < b:
        return _err(field, codes.TIME_TOO_EARLY, f"Time must be at or after {b.isoformat(timespec='minutes')}.", "minTime")


@_register("maxTime")
def _max_time(field, value, rv, rules):
    t, b = _as_time(value), _as_time(rv)
    if t and b and t > b:
        return _err(field, codes.TIME_TOO_LATE, f"Time must be at or before {b.isoformat(timespec='minutes')}.", "maxTime")


# ---------------------------------------------------------------------------
# Selection count (CHECKBOX / MATRIX_CHECKBOX)
# ---------------------------------------------------------------------------

@_register("minSelected")
def _min_selected(field, value, rv, rules):
    lst = _list(value)
    if lst is None or rv is None:
        return None
    if len(lst) < rv:
        return _err(field, codes.MIN_SELECTIONS, f"Select at least {rv} option(s).", "minSelected", min=rv, actual=len(lst))


@_register("maxSelected")
def _max_selected(field, value, rv, rules):
    lst = _list(value)
    if lst is None or rv is None:
        return None
    if len(lst) > rv:
        return _err(field, codes.MAX_SELECTIONS, f"Select at most {rv} option(s).", "maxSelected", max=rv, actual=len(lst))


@_register("exactSelected")
def _exact_selected(field, value, rv, rules):
    lst = _list(value)
    if lst is None or rv is None:
        return None
    if len(lst) != rv:
        return _err(field, codes.EXACT_SELECTIONS, f"Select exactly {rv} option(s).", "exactSelected", expected=rv, actual=len(lst))


# ---------------------------------------------------------------------------
# Matrix
# ---------------------------------------------------------------------------

@_register("requiredRows")
def _required_rows(field, value, rv, rules):
    if not isinstance(value, dict) or not rv:
        return None
    answered = {str(k).strip().lower() for k, v in value.items() if v not in (None, "", [], {})}
    missing = [r for r in rv if str(r).strip().lower() not in answered]
    if missing:
        return _err(field, codes.MATRIX_ROW_REQUIRED,
                    f"These rows require an answer: {', '.join(missing)}.", "requiredRows")


@_register("allRowsRequired")
def _all_rows_required(field, value, rv, rules):
    if not rv or not isinstance(value, dict):
        return None
    rows = [str(r) for r in (field.rows or [])]
    answered = {str(k).strip().lower() for k, v in value.items() if v not in (None, "", [], {})}
    missing = [r for r in rows if r.strip().lower() not in answered]
    if missing:
        return _err(field, codes.MATRIX_ROW_REQUIRED,
                    f"Every row requires an answer; missing: {', '.join(missing)}.", "allRowsRequired")


@_register("minPerRow")
def _min_per_row(field, value, rv, rules):
    if not isinstance(value, dict) or rv is None:
        return None
    for k, cell in value.items():
        n = len(cell) if isinstance(cell, (list, tuple)) else (1 if cell not in (None, "") else 0)
        if n < rv:
            return _err(field, codes.MATRIX_MIN_PER_ROW,
                        f"Row '{k}' needs at least {rv} selection(s).", "minPerRow")


@_register("maxPerRow")
def _max_per_row(field, value, rv, rules):
    if not isinstance(value, dict) or rv is None:
        return None
    for k, cell in value.items():
        n = len(cell) if isinstance(cell, (list, tuple)) else (1 if cell not in (None, "") else 0)
        if n > rv:
            return _err(field, codes.MATRIX_MAX_PER_ROW,
                        f"Row '{k}' allows at most {rv} selection(s).", "maxPerRow")


# ---------------------------------------------------------------------------
# Files (metadata-only: name / extension / size / count)
# ---------------------------------------------------------------------------

def _files(value) -> list[dict] | None:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list) and all(isinstance(x, dict) for x in value):
        return value
    return None


def _ext(name: str) -> str:
    return "." + name.rsplit(".", 1)[1].lower() if "." in name else ""


@_register("allowedFileTypes")
def _allowed_file_types(field, value, rv, rules):
    files = _files(value)
    if files is None or not rv:
        return None
    allowed = {e if e.startswith(".") else f".{e}"
               for e in (p.strip().lower() for p in str(rv).split(",")) if e}
    for f in files:
        if _ext(f.get("name", "")) not in allowed:
            return _err(field, codes.FILE_TYPE_NOT_ALLOWED,
                        f"Allowed file types: {rv}.", "allowedFileTypes", allowed=sorted(allowed))


@_register("blockedFileTypes")
def _blocked_file_types(field, value, rv, rules):
    files = _files(value)
    if files is None or not rv:
        return None
    blocked = {e if e.startswith(".") else f".{e}"
               for e in (p.strip().lower() for p in str(rv).split(",")) if e}
    for f in files:
        if _ext(f.get("name", "")) in blocked:
            return _err(field, codes.FILE_TYPE_BLOCKED,
                        f"'{f.get('name')}' is a blocked file type.", "blockedFileTypes")


@_register("maxFileSizeMB")
def _max_file_size(field, value, rv, rules):
    files = _files(value)
    if files is None or rv is None:
        return None
    limit = float(rv) * 1024 * 1024
    for f in files:
        if f.get("size", 0) > limit:
            return _err(field, codes.FILE_TOO_LARGE,
                        f"'{f.get('name')}' exceeds the {rv} MB limit.", "maxFileSizeMB")


@_register("minFileSizeKB")
def _min_file_size(field, value, rv, rules):
    files = _files(value)
    if files is None or rv is None:
        return None
    limit = float(rv) * 1024
    for f in files:
        if 0 < f.get("size", 0) < limit:
            return _err(field, codes.FILE_TOO_SMALL,
                        f"'{f.get('name')}' is smaller than the {rv} KB minimum.", "minFileSizeKB")


@_register("maxFiles")
def _max_files(field, value, rv, rules):
    files = _files(value)
    if files is None or rv is None:
        return None
    if len(files) > rv:
        return _err(field, codes.TOO_MANY_FILES, f"Attach at most {rv} file(s).", "maxFiles", max=rv, actual=len(files))


@_register("minFiles")
def _min_files(field, value, rv, rules):
    files = _files(value)
    if files is None or rv is None:
        return None
    if len(files) < rv:
        return _err(field, codes.TOO_FEW_FILES, f"Attach at least {rv} file(s).", "minFiles", min=rv, actual=len(files))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# Keys handled elsewhere (definition-time, or purely a message override).
_NON_ENFORCED_KEYS = {"patternError", "crossField", "normalizeCase", "allowMultiple", "allowOther"}


def apply_rules(field, value: Any) -> list[FieldError]:
    rules = field.validation_rules or {}
    if not isinstance(rules, dict):
        return []
    out: list[FieldError] = []
    for key, rule_value in rules.items():
        if key in _NON_ENFORCED_KEYS:
            continue
        fn = RULE_VALIDATORS.get(key)
        if not fn:
            continue
        try:
            err = fn(field, value, rule_value, rules)
        except Exception:
            err = None
        if err is not None:
            out.append(err)
    return out
