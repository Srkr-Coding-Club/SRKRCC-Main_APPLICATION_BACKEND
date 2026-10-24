"""
``validate_form_definition`` - checks a form's own configuration.

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

from django.utils import timezone

from apps.forms.models import FieldType, NON_ANSWER_FIELD_TYPES
from . import codes
from . import limits
from .report import DefinitionReport, FieldError
from .formula import FormulaError, parse as parse_formula, references as formula_references
from .values import CoercionError, coerce_value, parse_date, parse_number, parse_time
from .schema import (
    DATE_OPERATORS, DEFERRED_ACTIONS, NUMERIC_OPERATORS, RANGE_OPERATORS, ROUTING_ACTIONS, SELECTION_OPERATORS,
    TEXT_OPERATORS, VALUELESS_OPERATORS,
    RULE_COMPAT, TEXT_FORMATS, MIN_MAX_PAIRS, NON_NEGATIVE_KEYS,
    CONDITIONAL_OPERATORS, CONDITIONAL_ACTIONS,
    normalize_validation_rules, normalize_conditional_logic, iter_condition_field_refs, condition_depth,
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


# Which question types a profile attribute can safely fill in. The profile value is
# injected server-side and then validated like any other answer, so a type that cannot
# hold it (a CHECKBOX for an email, a DATE for a name) would make the form unsubmittable.
_PROFILE_FIELD_TYPES: dict[str, set[str]] = {
    "full_name": {FieldType.TEXT},
    "email": {FieldType.EMAIL, FieldType.TEXT},
    "phone_number": {FieldType.PHONE, FieldType.TEXT},
    "branch": {FieldType.TEXT, FieldType.DROPDOWN, FieldType.RADIO},
    "roll_number": {FieldType.TEXT},
    "college": {FieldType.TEXT, FieldType.DROPDOWN, FieldType.RADIO},
    "year": {FieldType.TEXT, FieldType.NUMBER, FieldType.DROPDOWN, FieldType.RADIO},
    "club_id": {FieldType.TEXT, FieldType.CLUB_ID},
}

# A group that already contains an unbounded quantifier and is itself repeated
# without bound - the classic catastrophic-backtracking shape, e.g. (a+)+ or (\w*\s?)*.
_NESTED_QUANTIFIER_RE = re.compile(r"\((?:[^()\\]|\\.)*[+*](?:[^()\\]|\\.)*\)\s*(?:[+*]|\{\d+,\})")
# Python-only regex syntax that behaves differently (or fails) in the browser's JS engine.
_PYTHON_ONLY_REGEX_RE = re.compile(r"\(\?P[<=]|\\[AZ]|\(\?[aiLmsux]+[:)]|\(\?#")

_DATE_BOUND_KEYS = ("minDate", "maxDate", "notBefore", "notAfter")
_TIME_BOUND_KEYS = ("minTime", "maxTime")


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

    @property
    def profile_field(self):
        return self._g("profile_field")


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

    if len(fields) > limits.MAX_FIELDS_PER_FORM:
        report.add_error(_err(codes.DEF_TOO_MANY_ITEMS,
                              f"A form can have at most {limits.MAX_FIELDS_PER_FORM} questions "
                              f"(this one has {len(fields)})."))

    position: dict[int, int] = {}
    for index, f in enumerate(fields):
        try:
            position[int(f.id)] = index
        except (TypeError, ValueError):
            pass

    orders_seen: set[int] = set()
    labels_seen: set[str] = set()
    any_required = False

    for f in fields:
        ftype = f.type

        if ftype not in _VALID_TYPES:
            report.add_error(_err(codes.DEF_UNKNOWN_FIELD_TYPE,
                                  f"'{ftype}' is not a known question type.", f))
            continue

        any_required = any_required or bool(f._g("is_required", False))

        if ftype not in NON_ANSWER_FIELD_TYPES:
            label_key = " ".join(str(f.label).lower().split())
            if label_key in labels_seen:
                report.add_warning(_err(codes.WARN_DUPLICATE_LABEL,
                                        f"Two questions are both called '{f.label}'; exports will have ambiguous columns.", f))
            labels_seen.add(label_key)

        _check_profile_field(report, f)

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
            opts = _check_option_list(report, f, f.options, "option")
            if len([o for o in opts if o]) < 2:
                report.add_error(_err(codes.DEF_CHOICE_NEEDS_OPTIONS,
                                      f"'{f.label}' needs at least two options.", f))

        if ftype in _MATRIX_TYPES:
            rows = _check_option_list(report, f, f.rows, "row", cap=limits.MAX_MATRIX_ROWS)
            cols = _check_option_list(report, f, f.options, "column")
            if len([r for r in rows if r]) < 1:
                report.add_error(_err(codes.DEF_MATRIX_NEEDS_ROWS,
                                      f"'{f.label}' needs at least one row.", f))
            if len([o for o in cols if o]) < 2:
                report.add_error(_err(codes.DEF_CHOICE_NEEDS_OPTIONS,
                                      f"'{f.label}' needs at least two columns.", f))

        if ftype in _SCALE_TYPES:
            lo, hi = f.min_value, f.max_value
            if lo is not None and hi is not None and lo >= hi:
                report.add_error(_err(codes.DEF_SCALE_RANGE_INVALID,
                                      f"'{f.label}': minimum ({lo}) must be less than maximum ({hi}).", f))
            elif lo is not None and hi is not None and hi - lo > limits.MAX_SCALE_SPAN:
                report.add_error(_err(codes.DEF_SCALE_RANGE_INVALID,
                                      f"'{f.label}': a scale can span at most {limits.MAX_SCALE_SPAN} steps.", f))

        # --- validation_rules --------------------------------------------
        _check_rules(report, f)
        _check_field_extras(report, f, by_id, position)

        # --- conditional_logic -----------------------------------------
        _check_conditional(report, f, by_id)

        # --- cross-field -----------------------------------------------
        _check_cross_field(report, f, by_id)

    _check_condition_cycles(report, fields, by_id)
    _check_formula_cycles(report, fields)
    _check_pages(report, fields)

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
        elif len(pat) > limits.MAX_REGEX_LENGTH:
            report.add_error(_err(codes.DEF_INVALID_REGEX,
                                  f"'{f.label}': the custom pattern is longer than {limits.MAX_REGEX_LENGTH} characters.",
                                  f, rule="pattern"))
        else:
            try:
                re.compile(pat)
            except re.error as exc:
                report.add_error(_err(codes.DEF_INVALID_REGEX,
                                      f"'{f.label}': the custom pattern is not a valid regular expression ({exc}).",
                                      f, rule="pattern"))
            else:
                if _NESTED_QUANTIFIER_RE.search(pat):
                    report.add_error(_err(codes.DEF_RISKY_REGEX,
                                          f"'{f.label}': the custom pattern repeats a group that already repeats "
                                          f"(e.g. (a+)+), which can hang the server. Simplify it.",
                                          f, rule="pattern"))
                if _PYTHON_ONLY_REGEX_RE.search(pat):
                    report.add_warning(_err(codes.WARN_REGEX_DIALECT,
                                            f"'{f.label}': the pattern uses syntax the browser does not support, "
                                            f"so live feedback may differ from the server's verdict.",
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

    _check_bounds_parse(report, f, raw)
    _check_satisfiable(report, f, normalized)


def _check_option_list(report, f, values, noun: str, cap: int = limits.MAX_OPTIONS_PER_FIELD) -> list[str]:
    """Shared shape checks for options / matrix rows / matrix columns. Returns the cleaned strings."""
    if not isinstance(values, list):
        report.add_error(_err(codes.DEF_INVALID_OPTION, f"'{f.label}': the {noun}s must be a list.", f))
        return []
    if len(values) > cap:
        report.add_error(_err(codes.DEF_TOO_MANY_ITEMS,
                              f"'{f.label}' has {len(values)} {noun}s; the limit is {cap}.", f))
    cleaned: list[str] = []
    for v in values:
        if isinstance(v, (dict, list, bool)):
            report.add_error(_err(codes.DEF_INVALID_OPTION, f"'{f.label}' has a {noun} that is not plain text.", f))
            cleaned.append("")
            continue
        text = "" if v is None else str(v).strip()
        if len(text) > limits.MAX_OPTION_LENGTH:
            report.add_error(_err(codes.DEF_INVALID_OPTION,
                                  f"'{f.label}' has a {noun} longer than {limits.MAX_OPTION_LENGTH} characters.", f))
        cleaned.append(text)
    if any(not t for t in cleaned):
        report.add_error(_err(codes.DEF_BLANK_OPTION, f"'{f.label}' has a blank {noun}.", f))
    lowered = [t.lower() for t in cleaned if t]
    if len(lowered) != len(set(lowered)):
        report.add_error(_err(codes.DEF_DUPLICATE_OPTION, f"'{f.label}' has duplicate {noun}s.", f))
    return cleaned


def _check_bounds_parse(report: DefinitionReport, f: _FieldView, raw: dict) -> None:
    """A date / time bound that cannot be parsed is silently ignored at submit time - catch it here."""
    for key in _DATE_BOUND_KEYS:
        v = raw.get(key)
        if v in (None, "") or str(v).strip().lower() == "today":
            continue
        try:
            parse_date(v)
        except CoercionError:
            report.add_error(_err(codes.DEF_INVALID_BOUND,
                                  f"'{f.label}': {key} '{v}' is not a valid date (use YYYY-MM-DD or 'today').",
                                  f, rule=key))
    for key in _TIME_BOUND_KEYS:
        v = raw.get(key)
        if v in (None, ""):
            continue
        try:
            parse_time(v)
        except CoercionError:
            report.add_error(_err(codes.DEF_INVALID_BOUND,
                                  f"'{f.label}': {key} '{v}' is not a valid time (use HH:MM).", f, rule=key))


def _bound_date(value):
    if value in (None, ""):
        return None
    if str(value).strip().lower() == "today":
        return timezone.localdate()
    try:
        return parse_date(value)
    except CoercionError:
        return None


def _check_satisfiable(report: DefinitionReport, f: _FieldView, rules: dict) -> None:
    """Flag rule combinations no answer can ever satisfy - they would make the form unsubmittable."""
    def bad(msg, rule=None):
        report.add_error(_err(codes.DEF_RULE_UNSATISFIABLE, f"'{f.label}': {msg}", f, rule=rule))

    def num(key):
        v = rules.get(key)
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    exact_len, min_len, max_len = num("exactLength"), num("minLength"), num("maxLength")
    if exact_len is not None:
        if (min_len is not None and exact_len < min_len) or (max_len is not None and exact_len > max_len):
            bad(f"exactLength ({exact_len}) is outside minLength/maxLength.", "exactLength")
    ceiling = limits.MAX_TEXT_LENGTH.get(f.type)
    if ceiling is not None:
        for key in ("minLength", "exactLength"):
            v = num(key)
            if v is not None and v > ceiling:
                bad(f"{key} ({v}) exceeds the platform limit of {ceiling} characters.", key)

    exact_val, min_val, max_val = num("exactValue"), num("minValue"), num("maxValue")
    if exact_val is not None:
        if (min_val is not None and exact_val < min_val) or (max_val is not None and exact_val > max_val):
            bad(f"exactValue ({exact_val}) is outside minValue/maxValue.", "exactValue")
    if max_val is not None and rules.get("positiveOnly") and max_val <= 0:
        bad("positiveOnly cannot be combined with a maximum of zero or less.", "positiveOnly")
    if max_val is not None and rules.get("allowNegative") is False and max_val < 0:
        bad("negative numbers are disallowed but the maximum is negative.", "allowNegative")

    if f.type == FieldType.CHECKBOX and not rules.get("allowOther"):
        n_opts = len([o for o in f.options if str(o).strip()])
        for key in ("minSelected", "exactSelected"):
            v = num(key)
            if v is not None and v > n_opts:
                bad(f"{key} ({v}) is more than the {n_opts} available options.", key)
    if f.type == FieldType.CHECKBOX:
        exact_sel = num("exactSelected")
        if exact_sel is not None:
            lo, hi = num("minSelected"), num("maxSelected")
            if (lo is not None and exact_sel < lo) or (hi is not None and exact_sel > hi):
                bad(f"exactSelected ({exact_sel}) is outside minSelected/maxSelected.", "exactSelected")

    if f.type == FieldType.PHONE:
        if num("maxDigits") is not None and num("maxDigits") < 7:
            bad("maxDigits is below the shortest valid phone number (7 digits).", "maxDigits")
        if num("minDigits") is not None and num("minDigits") > 15:
            bad("minDigits is above the longest valid phone number (15 digits).", "minDigits")

    if f.type == FieldType.DATE:
        today = timezone.localdate()
        upper = _bound_date(rules.get("maxDate") or rules.get("notAfter"))
        lower = _bound_date(rules.get("minDate") or rules.get("notBefore"))
        if rules.get("pastOnly") and rules.get("futureOnly"):
            bad("pastOnly and futureOnly cannot both be set.", "pastOnly")
        if rules.get("futureOnly") and upper is not None and upper <= today:
            bad("futureOnly conflicts with a maximum date that is not in the future.", "futureOnly")
        if rules.get("pastOnly") and lower is not None and lower >= today:
            bad("pastOnly conflicts with a minimum date that is not in the past.", "pastOnly")

    if f.type in (FieldType.FILE, FieldType.MULTI_FILE):
        allowed = {e.strip().lower().lstrip(".") for e in str(rules.get("allowedFileTypes") or "").split(",") if e.strip()}
        blocked = {e.strip().lower().lstrip(".") for e in str(rules.get("blockedFileTypes") or "").split(",") if e.strip()}
        if allowed and allowed <= blocked:
            bad("every allowed file type is also blocked.", "allowedFileTypes")
        min_kb, max_mb = num("minFileSizeKB"), num("maxFileSizeMB")
        if min_kb is not None and max_mb is not None and min_kb > max_mb * 1024:
            bad("minFileSizeKB is larger than maxFileSizeMB.", "minFileSizeKB")
        platform_mb = limits.platform_file_limit_bytes() / (1024 * 1024)
        if max_mb is not None and max_mb > platform_mb:
            bad(f"maxFileSizeMB exceeds the platform limit of {platform_mb:g} MB per file.", "maxFileSizeMB")


def _check_profile_field(report: DefinitionReport, f: _FieldView) -> None:
    key = f.profile_field
    if not key:
        return
    allowed = _PROFILE_FIELD_TYPES.get(key)
    if allowed is not None and f.type not in allowed:
        report.add_error(_err(codes.DEF_PROFILE_FIELD_INCOMPATIBLE,
                              f"'{f.label}': a {f.type} question cannot be filled from the profile's {key}.", f))


_PIPE_RE = re.compile(r"\{\{\s*(\d+)\s*\}\}")
_QUERY_PARAM_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")
_FORMULA_INPUT_TYPES = {FieldType.NUMBER, FieldType.RATING, FieldType.LINEAR_SCALE, FieldType.CALCULATED}
_CHOICE_RULE_TYPES = {FieldType.RADIO, FieldType.DROPDOWN, FieldType.CHECKBOX}
MAX_OPTION_LIMIT = 100_000


def _check_field_extras(report: DefinitionReport, f: _FieldView, by_id: dict, position: dict) -> None:
    """Option limits, default values, hidden and calculated questions, and {{id}} answer piping."""
    rules = f.validation_rules if isinstance(f.validation_rules, dict) else {}

    def error(code, message, rule=None):
        report.add_error(_err(code, f"'{f.label}': {message}", f, rule=rule))

    caps = rules.get("optionLimits")
    if caps:
        options = {str(o).strip().lower() for o in f.options}
        for option, cap in (caps.items() if isinstance(caps, dict) else []):
            if str(option).strip().lower() not in options:
                error(codes.DEF_OPTION_LIMIT_INVALID, f"'{option}' has a limit but is not one of the options.", "optionLimits")
            if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= MAX_OPTION_LIMIT:
                error(codes.DEF_OPTION_LIMIT_INVALID, f"the limit for '{option}' must be a whole number from 1 to {MAX_OPTION_LIMIT}.", "optionLimits")

    default = rules.get("defaultValue")
    if default is not None and f.type not in NON_ANSWER_FIELD_TYPES:
        try:
            coerce_value(f, default)
            if f.type in _CHOICE_RULE_TYPES:
                options = {str(o).strip().lower() for o in f.options}
                chosen = default if isinstance(default, list) else [default]
                missing = [str(c) for c in chosen if str(c).strip().lower() not in options]
                if missing:
                    error(codes.DEF_DEFAULT_INVALID, f"the default '{', '.join(missing)}' is not one of the options.", "defaultValue")
        except CoercionError as exc:
            error(codes.DEF_DEFAULT_INVALID, f"the default value does not fit this question ({exc.message})", "defaultValue")

    if f.type == FieldType.HIDDEN:
        param = rules.get("queryParam")
        if param is not None and not _QUERY_PARAM_RE.match(str(param)):
            error(codes.DEF_QUERY_PARAM_INVALID, "the link parameter must start with a letter and use only letters, digits, - or _ (40 characters at most).", "queryParam")
        if not param and default is None:
            report.add_warning(_err(codes.WARN_HIDDEN_FIELD_EMPTY,
                                    f"'{f.label}' is a hidden field with neither a link parameter nor a default, so it will always be empty.", f))

    if f.type == FieldType.CALCULATED:
        formula = str(rules.get("formula") or "").strip()
        decimals = rules.get("decimals", 2)
        if not formula:
            error(codes.DEF_FORMULA_INVALID, "a calculated question needs a formula.", "formula")
        else:
            try:
                tree = parse_formula(formula)
            except FormulaError as exc:
                error(codes.DEF_FORMULA_INVALID, f"the formula is not valid ({exc}).", "formula")
            else:
                try:
                    self_id = int(f.id)
                except (TypeError, ValueError):
                    self_id = None
                for ref in sorted(formula_references(tree)):
                    target = by_id.get(ref)
                    if target is None or ref == self_id:
                        error(codes.DEF_FORMULA_BAD_REFERENCE, f"{{{ref}}} is not another question on this form.", "formula")
                    elif target.type not in _FORMULA_INPUT_TYPES:
                        error(codes.DEF_FORMULA_BAD_REFERENCE,
                              f"{{{ref}}} ('{target.label}') is a {target.type} question; formulas can only use numbers, ratings, scales and other calculated questions.", "formula")
        if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 6:
            error(codes.DEF_FORMULA_INVALID, "decimals must be a whole number from 0 to 6.", "decimals")

    # {{id}} in the text people read is replaced by that question's answer on the form page.
    texts = [str(f.label or ""), str(f._g("description") or "")]
    conditional = f.conditional_logic if isinstance(f.conditional_logic, dict) else {}
    if conditional.get("action") == "display_message":
        texts.append(str(conditional.get("action_value") or ""))
    own_position = None
    try:
        own_position = position.get(int(f.id))
    except (TypeError, ValueError):
        pass
    for text in texts:
        for match in _PIPE_RE.finditer(text):
            ref = int(match.group(1))
            if ref not in by_id or by_id[ref].type in NON_ANSWER_FIELD_TYPES:
                report.add_warning(_err(codes.WARN_PIPE_UNKNOWN_FIELD,
                                        f"'{f.label}' uses {{{{{ref}}}}}, which is not a question on this form.", f))
            elif own_position is not None and position.get(ref, -1) >= own_position:
                report.add_warning(_err(codes.WARN_PIPE_UNKNOWN_FIELD,
                                        f"'{f.label}' uses {{{{{ref}}}}}, which is answered after it, so it will be blank when shown.", f))


def _check_formula_cycles(report: DefinitionReport, fields: list[_FieldView]) -> None:
    """Calculated questions whose formulas depend on each other in a circle can never be worked out."""
    graph: dict[int, set[int]] = {}
    labels: dict[int, _FieldView] = {}
    for f in fields:
        if f.type != FieldType.CALCULATED:
            continue
        try:
            fid = int(f.id)
            tree = parse_formula(str((f.validation_rules or {}).get("formula") or ""))
        except (TypeError, ValueError, FormulaError):
            continue
        graph[fid] = {r for r in formula_references(tree) if r != fid}
        labels[fid] = f
    state: dict[int, int] = {}

    def visit(node: int) -> bool:
        state[node] = 1
        for nxt in graph.get(node, ()):
            if nxt not in graph:
                continue
            if state.get(nxt) == 1 or (state.get(nxt) is None and visit(nxt)):
                return True
        state[node] = 2
        return False

    for node in graph:
        if state.get(node) is None and visit(node):
            report.add_error(_err(codes.DEF_FORMULA_CYCLE,
                                  f"'{labels[node].label}' is part of a circle of calculated questions that depend on each other.",
                                  labels[node]))
            return


def _check_pages(report: DefinitionReport, fields: list[_FieldView]) -> None:
    """Page breaks and the routing rules that sit on them."""
    ordered = sorted(enumerate(fields), key=lambda pair: (_order_key(pair[1]), pair[0]))
    ordered_fields = [f for _, f in ordered]
    page_of: dict[int, int] = {}
    break_index: dict[int, int] = {}   # PAGE_BREAK id -> index of the page it starts
    page = 0
    for f in ordered_fields:
        if f.type == FieldType.PAGE_BREAK:
            page += 1
            try:
                break_index[int(f.id)] = page
            except (TypeError, ValueError):
                pass
        try:
            page_of[int(f.id)] = page
        except (TypeError, ValueError):
            pass

    # Pages with nothing on them
    previous_was_break = True
    saw_any = False
    for f in ordered_fields:
        if f.type == FieldType.PAGE_BREAK:
            if previous_was_break:
                report.add_warning(_err(codes.WARN_EMPTY_PAGE,
                                        "A page break comes straight after another page break (or at the very top), "
                                        "which leaves an empty page.", f))
            previous_was_break = True
        else:
            previous_was_break = False
        saw_any = True
    if saw_any and previous_was_break and any(f.type == FieldType.PAGE_BREAK for f in ordered_fields) \
            and ordered_fields[-1].type == FieldType.PAGE_BREAK:
        report.add_warning(_err(codes.WARN_EMPTY_PAGE, "The form ends with a page break, which leaves an empty last page.",
                                ordered_fields[-1]))

    for f in ordered_fields:
        normalized = normalize_conditional_logic(f.conditional_logic) if isinstance(f.conditional_logic, dict) else {}
        action = normalized.get("action") if normalized else None
        if f.type != FieldType.PAGE_BREAK:
            if action in ROUTING_ACTIONS:
                report.add_error(_err(codes.DEF_ROUTING_ON_QUESTION,
                                      f"'{f.label}': '{action}' only works on a page break, where it decides what follows "
                                      f"the page before it.", f))
            continue
        if not normalized:
            continue
        if action not in ROUTING_ACTIONS:
            report.add_error(_err(codes.DEF_PAGE_BREAK_ACTION,
                                  f"'{f.label}': a page break can only skip to a page or end the form, not '{action}'.", f))
            continue
        this_page = break_index.get(int(f.id)) if f.id is not None else None
        for ref in iter_condition_field_refs(normalized):
            try:
                watched_page = page_of.get(int(ref))
            except (TypeError, ValueError):
                continue
            if watched_page is not None and this_page is not None and watched_page >= this_page:
                report.add_error(_err(codes.DEF_ROUTE_WATCHES_LATER_PAGE,
                                      f"'{f.label}': a routing rule can only watch questions on earlier pages, "
                                      f"because later pages have not been answered yet.", f))
                break
        if action == "skip_to_section":
            try:
                target_page = break_index.get(int(normalized.get("action_value")))
            except (TypeError, ValueError):
                target_page = None
            if target_page is None or this_page is None or target_page <= this_page:
                report.add_error(_err(codes.DEF_ROUTE_TARGET_INVALID,
                                      f"'{f.label}': 'skip to' must name a later page break.", f))


def _order_key(f: _FieldView):
    try:
        return int(f.order)
    except (TypeError, ValueError):
        return 0


def _check_condition_cycles(report: DefinitionReport, fields: list[_FieldView], by_id: dict[int, _FieldView]) -> None:
    """A -> B -> A: each question's visibility depends on the other, so neither can be resolved."""
    graph: dict[int, set[int]] = {}
    for f in fields:
        raw = f.conditional_logic
        if not isinstance(raw, dict) or not raw:
            continue
        try:
            fid = int(f.id)
        except (TypeError, ValueError):
            continue
        refs: set[int] = set()
        for ref in iter_condition_field_refs(normalize_conditional_logic(raw)):
            try:
                refs.add(int(ref))
            except (TypeError, ValueError):
                continue
        refs.discard(fid)  # a self-reference is reported on its own
        graph[fid] = {r for r in refs if r in by_id}

    done: set[int] = set()
    for start in graph:
        if start in done:
            continue
        stack = [(start, iter(graph[start]))]
        on_path = {start}
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                stack.pop()
                on_path.discard(node)
                done.add(node)
                continue
            if nxt in on_path:
                f = by_id[nxt]
                report.add_error(_err(codes.DEF_CONDITION_CYCLE,
                                      f"'{f.label}' is part of a circular conditional rule chain.", f))
                return
            if nxt in done or nxt not in graph:
                continue
            stack.append((nxt, iter(graph[nxt])))
            on_path.add(nxt)


def _check_conditional(report: DefinitionReport, f: _FieldView, by_id: dict[int, _FieldView]) -> None:
    raw = f.conditional_logic
    if not isinstance(raw, dict) or not raw:
        return

    if condition_depth(raw) > limits.MAX_CONDITION_DEPTH:
        report.add_error(_err(codes.DEF_CONDITION_TOO_DEEP,
                              f"The condition on '{f.label}' has groups nested more than {limits.MAX_CONDITION_DEPTH} deep; simplify it.", f))
        return

    normalized = normalize_conditional_logic(raw)
    if not normalized:
        # non-empty but unusable - e.g. the {"if": "parent"} placeholder
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
    elif action in DEFERRED_ACTIONS:
        report.add_warning(_err(codes.WARN_ACTION_NOT_ACTIVE,
                                f"'{f.label}': the '{action}' action needs multi-page forms and does nothing yet.", f))
    else:
        _check_action_value(report, f, action, normalized.get("action_value"))

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
            continue
        if ref not in by_id:
            report.add_error(_err(codes.DEF_CONDITION_UNKNOWN_FIELD,
                                  f"'{f.label}' has a conditional rule referencing a field ({ref}) "
                                  f"that is not on this form.", f))
            continue

        value_field = leaf.get("value_field")
        if value_field is not None:
            if self_id is not None and value_field == self_id:
                report.add_error(_err(codes.DEF_CONDITION_SELF_REFERENCE,
                                      f"'{f.label}' compares a rule against itself.", f))
            elif value_field not in by_id:
                report.add_error(_err(codes.DEF_CONDITION_UNKNOWN_FIELD,
                                      f"'{f.label}' compares a rule against a field ({value_field}) "
                                      f"that is not on this form.", f))
        if op in CONDITIONAL_OPERATORS:
            _check_condition_leaf(report, f, by_id[ref], leaf)


# Which question types each family of operators makes sense for.
_OPERATOR_TARGETS = (
    (NUMERIC_OPERATORS, {FieldType.NUMBER, FieldType.RATING, FieldType.LINEAR_SCALE, FieldType.CALCULATED,
                         FieldType.DATE, FieldType.TIME, FieldType.RADIO, FieldType.DROPDOWN},
     "numbers, ratings, dates or times"),
    (TEXT_OPERATORS, {FieldType.TEXT, FieldType.PARAGRAPH, FieldType.EMAIL, FieldType.PHONE, FieldType.URL,
                      FieldType.CLUB_ID, FieldType.HIDDEN, FieldType.RADIO, FieldType.DROPDOWN, FieldType.CHECKBOX},
     "text or choices"),
    (SELECTION_OPERATORS, {FieldType.CHECKBOX, FieldType.RADIO, FieldType.DROPDOWN}, "choice questions"),
    (DATE_OPERATORS, {FieldType.DATE}, "date questions"),
)
_OPTION_MATCH_OPERATORS = frozenset({"equals", "not_equals", "selected", "not_selected", "includes", "not_includes"})


def _is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or (isinstance(value, (list, tuple)) and not value)


def _as_parts(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [p.strip() for p in str(value or "").split(",") if p.strip()]


def _check_condition_leaf(report: DefinitionReport, f: _FieldView, target: _FieldView, leaf: dict) -> None:
    op = leaf["operator"]
    for operators, allowed_types, description in _OPERATOR_TARGETS:
        if op in operators and target.type not in allowed_types:
            report.add_error(_err(codes.DEF_CONDITION_OPERATOR_MISMATCH,
                                  f"'{f.label}': '{op}' only works on {description}, but '{target.label}' is a "
                                  f"{target.type} question.", f))
            return
    if op in VALUELESS_OPERATORS or leaf.get("value_field") is not None:
        return

    value = leaf.get("value")
    if _is_blank(value):
        report.add_error(_err(codes.DEF_CONDITION_MISSING_VALUE,
                              f"'{f.label}': the rule on '{target.label}' has no value to compare with.", f))
        return

    def bad(message):
        report.add_error(_err(codes.DEF_CONDITION_BAD_VALUE, f"'{f.label}': {message}", f))

    if op in RANGE_OPERATORS:
        parts = _as_parts(value)
        if len(parts) != 2:
            bad(f"'{op}' needs exactly two values (from and to).")
            return
        check = parse_date if op == "date_between" or target.type == FieldType.DATE else parse_number
        try:
            for part in parts:
                check(part)
        except CoercionError:
            bad(f"'{op}' needs {'dates' if check is parse_date else 'numbers'}, got '{value}'.")
    elif op in ("matches_regex", "not_matches_regex"):
        pattern = str(value)
        try:
            re.compile(pattern)
        except re.error as exc:
            bad(f"the regular expression is not valid ({exc}).")
            return
        if len(pattern) > limits.MAX_REGEX_LENGTH or _NESTED_QUANTIFIER_RE.search(pattern):
            bad("the regular expression is too long or repeats a repeating group, which can hang the server.")
    elif op in ("includes_any", "includes_all"):
        if not _as_parts(value):
            report.add_error(_err(codes.DEF_CONDITION_MISSING_VALUE,
                                  f"'{f.label}': '{op}' needs at least one option.", f))
    elif op in DATE_OPERATORS:
        try:
            parse_date(value)
        except CoercionError:
            bad(f"'{op}' needs a date (YYYY-MM-DD), got '{value}'.")
    elif op in NUMERIC_OPERATORS and target.type in (FieldType.NUMBER, FieldType.RATING, FieldType.LINEAR_SCALE):
        try:
            parse_number(value)
        except CoercionError:
            bad(f"'{op}' needs a number, got '{value}'.")

    if op in _OPTION_MATCH_OPERATORS and target.type in (FieldType.RADIO, FieldType.DROPDOWN, FieldType.CHECKBOX):
        options = {str(o).strip().lower() for o in target.options}
        if str(value).strip().lower() not in options:
            report.add_warning(_err(codes.WARN_CONDITION_VALUE_NOT_AN_OPTION,
                                    f"'{f.label}': '{value}' is not one of the options of '{target.label}', "
                                    f"so this rule can never be true.", f))


def _check_action_value(report: DefinitionReport, f: _FieldView, action: str, payload) -> None:
    def bad(message):
        report.add_error(_err(codes.DEF_CONDITION_BAD_ACTION_VALUE, f"'{f.label}': {message}", f))

    if action not in ("set_value", "restrict_options", "display_message"):
        return
    if _is_blank(payload):
        bad(f"the '{action}' action needs a value.")
        return
    options = [str(o).strip() for o in f.options]
    if action == "display_message":
        if not isinstance(payload, str) or len(payload) > 500:
            bad("the message must be text of at most 500 characters.")
    elif action == "restrict_options":
        if f.type not in (FieldType.RADIO, FieldType.DROPDOWN, FieldType.CHECKBOX):
            bad("'restrict_options' only works on choice questions.")
            return
        listed = payload if isinstance(payload, list) else [payload]
        unknown = [str(p) for p in listed if str(p).strip().lower() not in {o.lower() for o in options}]
        if unknown:
            bad(f"these are not options of this question: {', '.join(unknown)}.")
    else:  # set_value
        if f.type in (*NON_ANSWER_FIELD_TYPES, FieldType.CALCULATED, FieldType.FILE, FieldType.MULTI_FILE, FieldType.SIGNATURE):
            bad(f"'set_value' cannot fill a {f.type} question.")
            return
        try:
            coerce_value(f, payload)
        except CoercionError as exc:
            bad(exc.message)
            return
        if f.type in (FieldType.RADIO, FieldType.DROPDOWN, FieldType.CHECKBOX):
            chosen = payload if isinstance(payload, list) else [payload]
            unknown = [str(p) for p in chosen if str(p).strip().lower() not in {o.lower() for o in options}]
            if unknown:
                bad(f"these are not options of this question: {', '.join(unknown)}.")


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
