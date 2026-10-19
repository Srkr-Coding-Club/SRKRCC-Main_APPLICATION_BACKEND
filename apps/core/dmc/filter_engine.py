"""
dmc/filter_engine.py
--------------------
One declarative filter engine shared by every DMC adapter.

An adapter declares its filters once as ``Spec`` objects. The same declaration
drives three things, so they can never drift apart:

  * the schema sent to the frontend (``to_definitions``) - which filters exist,
    their operators and their option lists;
  * request validation (``validate``) - an unknown field, an operator the
    filter does not support, or an unparseable value raises ``FilterError``
    (the views turn it into a 400) instead of being silently ignored;
  * execution - ``apply_queryset`` for database-backed datasets and
    ``apply_rows`` for datasets that are assembled in Python.

Dates are interpreted in the project timezone (Asia/Kolkata): "on or before
2025-03-01" includes the whole of 1 March.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from django.db.models import Q, QuerySet
from django.utils import timezone

from apps.core.dmc.contracts import FilterClause, FilterDefinition, FilterOption


class FilterError(ValueError):
    """The client sent a filter this dataset cannot apply."""


OPERATORS: dict[str, tuple[str, ...]] = {
    "text":    ("contains", "eq", "neq", "starts_with", "empty", "not_empty"),
    "select":  ("eq", "neq", "in"),
    "boolean": ("eq",),
    "number":  ("eq", "neq", "gte", "lte", "gt", "lt", "empty", "not_empty"),
    "date":    ("gte", "lte", "eq", "empty", "not_empty"),
}

# Spec kind -> FilterType understood by the frontend.
DEFINITION_TYPE = {
    "text": "text", "select": "select", "boolean": "boolean",
    "number": "number_range", "date": "date_range",
}

NO_VALUE_OPERATORS = {"empty", "not_empty"}


@dataclass
class Spec:
    """Declares one filter."""

    key: str
    label: str
    kind: str                                   # text | select | boolean | number | date
    lookups: tuple[str, ...] = ()               # ORM paths; several are OR-ed
    options: Any = None                         # list[FilterOption] or a callable returning one
    value_type: str = "str"                     # select values: "str" or "int"
    is_datetime: bool = True                    # date kind: DateTimeField (True) or DateField
    tokenize: bool = False                      # text: "john doe" must match every word (full names)
    operators: tuple[str, ...] | None = None    # narrow the kind's default operators
    row_key: str | None = None                  # key to read when filtering python rows
    custom: Callable[[str, Any], Q] | None = None   # derived filters: (operator, parsed value) -> Q
    wrap: Callable[[QuerySet, Q], QuerySet] | None = None   # how a Q reaches the queryset (default: qs.filter)

    @property
    def allowed_operators(self) -> tuple[str, ...]:
        return self.operators or OPERATORS[self.kind]


# ---------------------------------------------------------------------------
# Value parsing
# ---------------------------------------------------------------------------

def _fail(spec: Spec, message: str) -> FilterError:
    return FilterError(f"Filter '{spec.label}': {message}")


def _parse_bool(spec: Spec, raw: Any) -> bool:
    text = str(raw).strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    raise _fail(spec, "expected Yes or No.")


def _parse_number(spec: Spec, raw: Any) -> int | float:
    try:
        number = float(str(raw).strip())
    except (TypeError, ValueError):
        raise _fail(spec, "expected a number.") from None
    return int(number) if number.is_integer() else number


def _parse_date(spec: Spec, raw: Any) -> dt.date:
    if isinstance(raw, dt.datetime):
        return raw.date()
    if isinstance(raw, dt.date):
        return raw
    try:
        return dt.date.fromisoformat(str(raw).strip()[:10])
    except ValueError:
        raise _fail(spec, "expected a date (YYYY-MM-DD).") from None


def _parse_choice(spec: Spec, raw: Any) -> str | int:
    text = str(raw).strip()
    if not text:
        raise _fail(spec, "a value is required.")
    if spec.value_type == "int":
        try:
            return int(text)
        except ValueError:
            raise _fail(spec, "invalid selection.") from None
    return text


def _parse_value(spec: Spec, op: str, raw: Any) -> Any:
    if op in NO_VALUE_OPERATORS:
        return None
    if spec.kind == "boolean":
        return _parse_bool(spec, raw)
    if spec.kind == "number":
        return _parse_number(spec, raw)
    if spec.kind == "date":
        return _parse_date(spec, raw)
    if spec.kind == "select":
        if op == "in":
            items = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
            values = [_parse_choice(spec, item) for item in items if str(item).strip()]
            if not values:
                raise _fail(spec, "pick at least one value.")
            return values
        return _parse_choice(spec, raw)
    text = str(raw).strip()
    if not text:
        raise _fail(spec, "a value is required.")
    return text


def resolve(clause: FilterClause, specs: dict[str, Spec]) -> tuple[Spec, str, Any]:
    """Validate one clause and return (spec, operator, parsed value)."""
    spec = specs.get(clause.field)
    if spec is None:
        raise FilterError(f"'{clause.field}' cannot be filtered on this dataset.")
    op = clause.operator
    if op not in spec.allowed_operators:
        raise FilterError(f"Filter '{spec.label}' does not support the '{op}' operator.")
    return spec, op, _parse_value(spec, op, clause.value)


def validate(clauses: Iterable[FilterClause], specs: dict[str, Spec]) -> None:
    for clause in clauses:
        resolve(clause, specs)


# ---------------------------------------------------------------------------
# Database filtering
# ---------------------------------------------------------------------------

def _any(lookups: Iterable[str], suffix: str, value: Any) -> Q:
    q = Q()
    for lookup in lookups:
        q |= Q(**{f"{lookup}{suffix}": value})
    return q


def _blank(lookups: Iterable[str], spec: Spec) -> Q:
    """Rows where every lookup is empty."""
    q = Q()
    for lookup in lookups:
        empty = Q(**{f"{lookup}__isnull": True})
        if spec.kind == "text":
            empty |= Q(**{lookup: ""})
        q &= empty
    return q


def _day_bounds(spec: Spec, day: dt.date) -> tuple[Any, Any]:
    if not spec.is_datetime:
        return day, day + dt.timedelta(days=1)
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(dt.datetime.combine(day, dt.time.min), tz)
    return start, start + dt.timedelta(days=1)


def _text_q(spec: Spec, op: str, value: str | None) -> Q:
    lookups = spec.lookups
    if op == "empty":
        return _blank(lookups, spec)
    if op == "not_empty":
        return ~_blank(lookups, spec)
    suffix = {"eq": "__iexact", "neq": "__iexact", "contains": "__icontains", "starts_with": "__istartswith"}[op]
    words = value.split() if spec.tokenize and len(lookups) > 1 else [value]
    q = Q()
    for word in words:
        q &= _any(lookups, suffix, word)
    return ~q if op == "neq" else q


def build_q(spec: Spec, op: str, value: Any) -> Q:
    if spec.custom:
        return spec.custom(op, value)
    lookups = spec.lookups
    if spec.kind == "text":
        return _text_q(spec, op, value)
    if spec.kind == "select":
        suffix = "__iexact" if spec.value_type == "str" else ""
        if op == "in":
            return _any(lookups, "__in", value)
        q = _any(lookups, suffix, value)
        return ~q if op == "neq" else q
    if spec.kind == "boolean":
        return _any(lookups, "", value)
    if spec.kind == "number":
        if op == "empty":
            return _blank(lookups, spec)
        if op == "not_empty":
            return ~_blank(lookups, spec)
        q = _any(lookups, {"eq": "", "neq": "", "gte": "__gte", "lte": "__lte", "gt": "__gt", "lt": "__lt"}[op], value)
        return ~q if op == "neq" else q
    # date
    if op == "empty":
        return _blank(lookups, spec)
    if op == "not_empty":
        return ~_blank(lookups, spec)
    start, next_day = _day_bounds(spec, value)
    if op == "gte":
        return _any(lookups, "__gte", start)
    if op == "lte":
        return _any(lookups, "__lt", next_day)
    return _any(lookups, "__gte", start) & _any(lookups, "__lt", next_day)


def apply_queryset(qs: QuerySet, clauses: Iterable[FilterClause], specs: dict[str, Spec]) -> QuerySet:
    for clause in clauses:
        spec, op, value = resolve(clause, specs)
        q = build_q(spec, op, value)
        qs = spec.wrap(qs, q) if spec.wrap else qs.filter(q)
    return qs


# ---------------------------------------------------------------------------
# Python-row filtering (datasets assembled in memory)
# ---------------------------------------------------------------------------

def _row_date(raw: Any) -> dt.date | None:
    if not raw:
        return None
    if isinstance(raw, dt.datetime):
        moment = raw
    elif isinstance(raw, dt.date):
        return raw
    else:
        try:
            moment = dt.datetime.fromisoformat(str(raw))
        except ValueError:
            return None
    if timezone.is_aware(moment):
        moment = timezone.localtime(moment)
    return moment.date()


def _row_matches(spec: Spec, op: str, value: Any, cell: Any) -> bool:
    blank = cell is None or cell == ""
    if op == "empty":
        return blank
    if op == "not_empty":
        return not blank

    if spec.kind == "boolean":
        return bool(cell) == value

    if spec.kind == "number":
        if blank:
            return False
        try:
            number = float(cell)
        except (TypeError, ValueError):
            return False
        return {
            "eq": number == value, "neq": number != value, "gte": number >= value,
            "lte": number <= value, "gt": number > value, "lt": number < value,
        }[op]

    if spec.kind == "date":
        day = _row_date(cell)
        if day is None:
            return False
        return {"gte": day >= value, "lte": day <= value, "eq": day == value}[op]

    text = str(cell if cell is not None else "").lower()
    if op == "in":
        return text in {str(v).lower() for v in value}
    needle = str(value).lower()
    if op == "eq":
        return text == needle
    if op == "neq":
        return text != needle
    words = needle.split() if spec.tokenize else [needle]
    if op == "contains":
        return all(word in text for word in words)
    return text.startswith(needle)      # starts_with


def apply_rows(rows: list[dict], clauses: Iterable[FilterClause], specs: dict[str, Spec]) -> list[dict]:
    for clause in clauses:
        spec, op, value = resolve(clause, specs)
        key = spec.row_key or spec.key
        rows = [row for row in rows if _row_matches(spec, op, value, row.get(key))]
    return rows


# ---------------------------------------------------------------------------
# Schema output
# ---------------------------------------------------------------------------

def _boolean_options() -> list[FilterOption]:
    return [FilterOption("Yes", "true"), FilterOption("No", "false")]


def to_definitions(specs: dict[str, Spec]) -> list[FilterDefinition]:
    definitions = []
    for spec in specs.values():
        if spec.kind == "boolean":
            options = _boolean_options()
        else:
            raw = spec.options() if callable(spec.options) else (spec.options or [])
            options = [o if isinstance(o, FilterOption) else FilterOption(o[1], o[0]) for o in raw]
        definitions.append(FilterDefinition(
            key=spec.key,
            label=spec.label,
            type=DEFINITION_TYPE[spec.kind],
            operators=list(spec.allowed_operators),
            options=options,
        ))
    return definitions


def specs_by_key(*specs: Spec) -> dict[str, Spec]:
    return {spec.key: spec for spec in specs}


def person_specs(prefix: str) -> list[Spec]:
    """Name / email / roll number of the person behind a row (``prefix`` is the ORM path to the user)."""
    return [
        Spec("name", "Full Name", "text", (f"{prefix}first_name", f"{prefix}last_name"), tokenize=True),
        Spec("email", "Email", "text", (f"{prefix}email",)),
        Spec("roll_number", "Roll Number", "text", (f"{prefix}roll_number",)),
    ]


def choices(choice_class: type, relabel: dict[str, str] | None = None) -> list[FilterOption]:
    """FilterOptions from a Django TextChoices class, optionally renaming labels."""
    relabel = relabel or {}
    return [FilterOption(relabel.get(value, label), value) for value, label in choice_class.choices]


def distinct_values(queryset: QuerySet, field: str) -> list[FilterOption]:
    values = queryset.exclude(**{f"{field}__isnull": True}).exclude(**{field: ""}).order_by(field).values_list(field, flat=True).distinct()
    return [FilterOption(str(v), str(v)) for v in values]
