"""
The canonical schema for ``FormField.validation_rules`` and
``FormField.conditional_logic`` — plus the normalizers that turn whatever the
frontend (or a legacy form) stored into that canonical shape.

Design rules:
  * The canonical ``validation_rules`` schema is a strict SUPERSET of the keys
    the current frontend builder writes (``src/lib/types.ts::ValidationRules``),
    so every existing form keeps working untouched.
  * Keys are camelCase (matching the frontend) so the eventual frontend/backend
    "merge" is additive.
  * ``normalize_*`` never raises — it best-effort canonicalizes. Genuine
    misconfiguration is reported by ``definition.validate_form_definition``.
"""

from __future__ import annotations

from typing import Any

from apps.forms.models import FieldType


# ---------------------------------------------------------------------------
# validation_rules — which keys each field type accepts
# ---------------------------------------------------------------------------

# Keys every field type may carry.
_COMMON_KEYS = {"patternError", "crossField"}

_TEXT_KEYS = {
    "minLength", "maxLength", "exactLength",
    "pattern", "format",
    "allowedChars", "disallowedChars",
    "startsWith", "endsWith", "contains", "notContains",
}
_PARAGRAPH_KEYS = _TEXT_KEYS | {"minWords", "maxWords"}
_NUMBER_KEYS = {
    "minValue", "maxValue", "exactValue",
    "gt", "gte", "lt", "lte",
    "integerOnly", "allowNegative", "positiveOnly", "step",
}
_EMAIL_KEYS = {"allowedDomains", "blockedDomains", "allowMultiple", "normalizeCase", "pattern"}
_PHONE_KEYS = {"pattern", "minDigits", "maxDigits", "numericOnly"}
_URL_KEYS = {"pattern", "allowedDomains", "blockedDomains", "requireHttps"}
_DATE_KEYS = {
    "minDate", "maxDate", "notBefore", "notAfter",
    "pastOnly", "futureOnly", "allowToday",
}
_TIME_KEYS = {"minTime", "maxTime"}
_CHOICE_KEYS = {"allowOther"}                          # RADIO / DROPDOWN
_CHECKBOX_KEYS = {"minSelected", "maxSelected", "exactSelected", "allowOther"}
_FILE_KEYS = {
    "allowedFileTypes", "blockedFileTypes",
    "maxFileSizeMB", "minFileSizeKB",
}
_MULTI_FILE_KEYS = _FILE_KEYS | {"minFiles", "maxFiles"}
_SCALE_KEYS = {"minValue", "maxValue", "integerOnly", "step"}   # RATING / LINEAR_SCALE
_MATRIX_KEYS = {"requiredRows", "allRowsRequired"}
_MATRIX_CHECKBOX_KEYS = _MATRIX_KEYS | {"minPerRow", "maxPerRow"}

RULE_COMPAT: dict[str, set[str]] = {
    FieldType.TEXT: _COMMON_KEYS | _TEXT_KEYS,
    FieldType.PARAGRAPH: _COMMON_KEYS | _PARAGRAPH_KEYS,
    FieldType.EMAIL: _COMMON_KEYS | _EMAIL_KEYS | {"minLength", "maxLength"},
    FieldType.NUMBER: _COMMON_KEYS | _NUMBER_KEYS,
    FieldType.PHONE: _COMMON_KEYS | _PHONE_KEYS,
    FieldType.URL: _COMMON_KEYS | _URL_KEYS,
    FieldType.DROPDOWN: _COMMON_KEYS | _CHOICE_KEYS,
    FieldType.RADIO: _COMMON_KEYS | _CHOICE_KEYS,
    FieldType.CHECKBOX: _COMMON_KEYS | _CHECKBOX_KEYS,
    FieldType.FILE: _COMMON_KEYS | _FILE_KEYS,
    FieldType.MULTI_FILE: _COMMON_KEYS | _MULTI_FILE_KEYS,
    FieldType.DATE: _COMMON_KEYS | _DATE_KEYS,
    FieldType.TIME: _COMMON_KEYS | _TIME_KEYS,
    FieldType.SECTION: set(),
    FieldType.RATING: _COMMON_KEYS | _SCALE_KEYS,
    FieldType.LINEAR_SCALE: _COMMON_KEYS | _SCALE_KEYS,
    FieldType.MATRIX_RADIO: _COMMON_KEYS | _MATRIX_KEYS,
    FieldType.MATRIX_CHECKBOX: _COMMON_KEYS | _MATRIX_CHECKBOX_KEYS,
    FieldType.SIGNATURE: _COMMON_KEYS,
}

# Numeric-ish keys whose value should be coerced to int|float on normalize.
_NUMERIC_RULE_KEYS = {
    "minLength", "maxLength", "exactLength", "minWords", "maxWords",
    "minValue", "maxValue", "exactValue", "gt", "gte", "lt", "lte", "step",
    "minSelected", "maxSelected", "exactSelected",
    "minDigits", "maxDigits",
    "maxFileSizeMB", "minFileSizeKB", "minFiles", "maxFiles",
    "minPerRow", "maxPerRow",
}
_BOOL_RULE_KEYS = {
    "integerOnly", "allowNegative", "positiveOnly",
    "allowMultiple", "normalizeCase", "numericOnly", "requireHttps",
    "pastOnly", "futureOnly", "allowToday", "allowOther", "allRowsRequired",
}
_LIST_RULE_KEYS = {"allowedDomains", "blockedDomains", "requiredRows"}

# Recognised values for the short-answer ``format`` rule.
TEXT_FORMATS = frozenset({
    "any", "alpha", "alphabetic", "alphanumeric", "numeric", "integer",
    "decimal", "email", "phone", "url", "username", "slug",
    "date", "time", "datetime",
})

# Pairs where the first must never exceed the second.
MIN_MAX_PAIRS = (
    ("minLength", "maxLength"),
    ("minWords", "maxWords"),
    ("minValue", "maxValue"),
    ("gte", "lte"),
    ("minSelected", "maxSelected"),
    ("minDigits", "maxDigits"),
    ("minFiles", "maxFiles"),
    ("minPerRow", "maxPerRow"),
    ("minDate", "maxDate"),
    ("minTime", "maxTime"),
)

# Keys whose value must not be negative.
NON_NEGATIVE_KEYS = frozenset({
    "minLength", "maxLength", "exactLength", "minWords", "maxWords",
    "minSelected", "maxSelected", "exactSelected",
    "minDigits", "maxDigits", "maxFileSizeMB", "minFileSizeKB",
    "minFiles", "maxFiles", "minPerRow", "maxPerRow", "step",
})


def _to_number(v: Any) -> Any:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    try:
        s = str(v).strip()
        if s == "":
            return None
        f = float(s)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return v


def _to_bool(v: Any) -> Any:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        low = v.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off", ""):
            return False
    return bool(v) if v is not None else None


def _to_str_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [p.strip() for p in v.split(",") if p.strip()]
    if isinstance(v, (list, tuple)):
        return [str(p).strip() for p in v if str(p).strip()]
    return [str(v).strip()]


def normalize_validation_rules(field_type: str, raw: Any) -> dict[str, Any]:
    """
    Return a cleaned ``validation_rules`` dict:
      * unknown keys for this field type are dropped
      * numeric keys coerced to int/float, blanks dropped
      * boolean keys coerced to bool
      * list keys ("allowedDomains", ...) coerced to list[str]
      * empty strings / None values dropped (so "unset" is unambiguous)
    Never raises. ``crossField`` is passed through as-is (validated separately).
    """
    if not isinstance(raw, dict):
        return {}

    allowed = RULE_COMPAT.get(field_type, set())
    out: dict[str, Any] = {}

    for key, value in raw.items():
        if key not in allowed:
            continue
        if key == "crossField":
            if isinstance(value, list) and value:
                out[key] = value
            continue
        if key in _NUMERIC_RULE_KEYS:
            num = _to_number(value)
            if isinstance(num, (int, float)) and not isinstance(num, bool):
                out[key] = num
            continue
        if key in _BOOL_RULE_KEYS:
            b = _to_bool(value)
            if b is not None:
                out[key] = b
            continue
        if key in _LIST_RULE_KEYS:
            lst = _to_str_list(value)
            if lst:
                out[key] = lst
            continue
        # string-valued keys (pattern, format, startsWith, minDate, patternError, ...)
        if value is None:
            continue
        s = str(value).strip()
        if s == "":
            continue
        out[key] = s

    return out


# ---------------------------------------------------------------------------
# conditional_logic — operators, actions, normalizer
# ---------------------------------------------------------------------------

CONDITIONAL_OPERATORS = frozenset({
    # equality
    "equals", "not_equals",
    # numeric
    "gt", "gte", "lt", "lte", "between", "not_between",
    # text
    "contains", "not_contains", "starts_with", "ends_with",
    "matches_regex", "not_matches_regex", "is_empty", "is_not_empty",
    # selection
    "selected", "not_selected", "includes", "not_includes",
    "includes_any", "includes_all",
    # date
    "before", "after", "on", "before_or_equal", "after_or_equal", "date_between",
})

# Actions the backend actually enforces on submission validity.
ENFORCED_ACTIONS = frozenset({"show", "hide", "require", "optional"})
# Actions the schema accepts and stores but the backend does not act on yet
# (pure frontend flow — skip to section, end the form, prefill, restrict options).
DEFERRED_ACTIONS = frozenset({
    "skip", "skip_to_section", "end_form", "set_value", "restrict_options",
    "display_message",
})
CONDITIONAL_ACTIONS = ENFORCED_ACTIONS | DEFERRED_ACTIONS

# Legacy operator aliases -> canonical.
_OPERATOR_ALIASES = {
    "greater_than": "gt",
    "greater_than_or_equal": "gte",
    "less_than": "lt",
    "less_than_or_equal": "lte",
    "eq": "equals",
    "ne": "not_equals",
    "neq": "not_equals",
    "==": "equals",
    "!=": "not_equals",
    ">": "gt",
    ">=": "gte",
    "<": "lt",
    "<=": "lte",
}

# Sentinel for a rule whose ``field`` reference could not be resolved to an int
# (the broken frontend ``{if: "parent"}`` placeholder). Such a rule is treated as
# "cannot evaluate" and never hides / requires a field.
UNEVALUABLE_FIELD = None


def _coerce_ref(raw: Any) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return UNEVALUABLE_FIELD


def _normalize_rule_node(node: Any) -> dict[str, Any] | None:
    """Normalize one entry of a ``rules`` array: either a leaf rule or a nested group."""
    if not isinstance(node, dict):
        return None

    # Nested group
    if "rules" in node and isinstance(node.get("rules"), list):
        sub = [n for n in (_normalize_rule_node(r) for r in node["rules"]) if n]
        if not sub:
            return None
        return {"logic": str(node.get("logic", "AND")).upper() or "AND", "rules": sub}

    # Leaf rule — accept both {field, operator, value} and legacy {if, operator|equals, value}
    ref_raw = node.get("field", node.get("if"))
    field_ref = _coerce_ref(ref_raw)

    if "equals" in node and "operator" not in node:
        operator = "equals"
        value = node.get("equals")
    else:
        operator = str(node.get("operator", "equals")).strip().lower()
        operator = _OPERATOR_ALIASES.get(operator, operator)
        value = node.get("value")

    leaf = {
        "field": field_ref,           # int, or None when unevaluable
        "operator": operator,
        "value": value,
    }
    if field_ref is None and ref_raw not in (None, ""):
        # keep the original (e.g. the broken "parent" placeholder) for diagnostics
        leaf["field_raw"] = ref_raw
    return leaf


def normalize_conditional_logic(raw: Any) -> dict[str, Any]:
    """
    Canonical shape:
        {"logic": "AND"|"OR", "rules": [ <leaf|group>, ... ], "action": "show"}

    Accepts and upgrades:
        * {} / falsy                       -> {} (no condition; field always visible)
        * {"if": <id>, "equals": <v>}      -> single-rule AND group
        * {"if": <id>, "operator", "value"}
        * {"logic", "rules": [{"if", ...}]}
        * {"field", "operator", "value"} at top level (single leaf)
        * the broken {"if": "parent", "equals": <v>} placeholder -> rule kept with
          field=None so the engine ignores it (field stays visible).
    """
    if not raw or not isinstance(raw, dict):
        return {}

    action = str(raw.get("action", "show")).strip().lower() or "show"
    if action not in CONDITIONAL_ACTIONS:
        action = "show"

    # Multi-rule / grouped form
    if isinstance(raw.get("rules"), list):
        rules = [n for n in (_normalize_rule_node(r) for r in raw["rules"]) if n]
        logic = str(raw.get("logic", "AND")).upper()
        logic = logic if logic in ("AND", "OR") else "AND"
        if not rules:
            return {}
        return {"logic": logic, "rules": rules, "action": action}

    # Single top-level leaf (legacy {"if"/"field", "equals"/"operator"})
    leaf = _normalize_rule_node(raw)
    if not leaf or ("rules" not in leaf and leaf.get("operator") not in CONDITIONAL_OPERATORS
                    and "field" not in leaf):
        return {}
    if leaf.get("field") is None and leaf.get("field_raw") in (None, ""):
        # genuinely no condition
        return {}
    return {"logic": "AND", "rules": [leaf], "action": action}


def iter_condition_field_refs(normalized: dict[str, Any]):
    """Yield every raw ``field`` reference used anywhere in a normalized condition tree."""
    def _walk(node: dict[str, Any]):
        if "rules" in node:
            for r in node["rules"]:
                yield from _walk(r)
        else:
            yield node.get("field_raw", node.get("field"))

    if not normalized or "rules" not in normalized:
        return
    for r in normalized["rules"]:
        yield from _walk(r)
