"""
The conditional-logic engine.

Given the active fields of a form and a map of the current answers, compute:
  * which fields are VISIBLE  (``show`` / ``hide`` actions)
  * which fields have their required-ness overridden (``require`` / ``optional``)

``compute_layout`` runs a bounded fixpoint (a rule may depend on a field that is
itself conditionally shown), reusing the same cycle guard the original
``evaluate_visible_fields`` used.

Operator evaluation is intentionally forgiving with types: the trigger value is
whatever was submitted (already coerced where possible); comparisons fall back to
string comparison when a numeric/date interpretation is not possible, and an
un-interpretable comparison is simply ``False`` rather than an error - a
misconfigured rule must never break a submission.
"""

from __future__ import annotations

import datetime as _dt
import re
from decimal import Decimal
from typing import Any

from apps.forms.models import FieldType
from .schema import ROUTING_ACTIONS, normalize_conditional_logic
from .values import is_empty, parse_date, parse_number, CoercionError


# ---------------------------------------------------------------------------
# Operator primitives
# ---------------------------------------------------------------------------

def _as_number(v: Any):
    try:
        return parse_number(v)
    except CoercionError:
        return None


def _as_date(v: Any):
    try:
        return parse_date(v)
    except CoercionError:
        return None


def _as_list(v: Any) -> list:
    if isinstance(v, (list, tuple)):
        return list(v)
    if is_empty(v):
        return []
    return [v]


def _str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    return str(v)


def _cmp_pair(left: Any, right: Any):
    """Return (l, r) coerced to a comparable pair: number, date, or string."""
    ln, rn = _as_number(left), _as_number(right)
    if ln is not None and rn is not None:
        return ln, rn
    ld, rd = _as_date(left), _as_date(right)
    if ld is not None and rd is not None:
        return ld, rd
    return _str(left).strip().lower(), _str(right).strip().lower()


def evaluate_operator(op: str, left: Any, right: Any) -> bool:
    try:
        if op == "equals":
            l, r = _cmp_pair(left, right)
            return l == r
        if op == "not_equals":
            l, r = _cmp_pair(left, right)
            return l != r

        if op in ("gt", "gte", "lt", "lte"):
            l, r = _cmp_pair(left, right)
            try:
                if op == "gt":
                    return l > r
                if op == "gte":
                    return l >= r
                if op == "lt":
                    return l < r
                return l <= r
            except TypeError:
                return False

        if op in ("between", "not_between", "date_between"):
            bounds = right if isinstance(right, (list, tuple)) else _str(right).split(",")
            if len(bounds) < 2:
                return False
            lo, hi = _cmp_pair(left, bounds[0])[1], _cmp_pair(left, bounds[1])[1]
            lv = _cmp_pair(left, bounds[0])[0]
            try:
                inside = lo <= lv <= hi
            except TypeError:
                return False
            return inside if op != "not_between" else not inside

        if op == "contains":
            return _str(right).strip().lower() in _str(left).strip().lower()
        if op == "not_contains":
            return _str(right).strip().lower() not in _str(left).strip().lower()
        if op == "starts_with":
            return _str(left).strip().lower().startswith(_str(right).strip().lower())
        if op == "ends_with":
            return _str(left).strip().lower().endswith(_str(right).strip().lower())
        if op == "matches_regex":
            return re.search(_str(right), _str(left)) is not None
        if op == "not_matches_regex":
            return re.search(_str(right), _str(left)) is None
        if op == "is_empty":
            return is_empty(left)
        if op == "is_not_empty":
            return not is_empty(left)

        if op in ("selected", "includes"):
            return _str(right).strip().lower() in {_str(x).strip().lower() for x in _as_list(left)}
        if op in ("not_selected", "not_includes"):
            return _str(right).strip().lower() not in {_str(x).strip().lower() for x in _as_list(left)}
        if op == "includes_any":
            want = {_str(x).strip().lower() for x in _as_list(right)}
            have = {_str(x).strip().lower() for x in _as_list(left)}
            return bool(want & have)
        if op == "includes_all":
            want = {_str(x).strip().lower() for x in _as_list(right)}
            have = {_str(x).strip().lower() for x in _as_list(left)}
            return want.issubset(have)

        if op in ("before", "after", "on", "before_or_equal", "after_or_equal"):
            ld, rd = _as_date(left), _as_date(right)
            if ld is None or rd is None:
                return False
            if op == "before":
                return ld < rd
            if op == "after":
                return ld > rd
            if op == "on":
                return ld == rd
            if op == "before_or_equal":
                return ld <= rd
            return ld >= rd
    except re.error:
        return False
    except Exception:
        return False
    return False


# ---------------------------------------------------------------------------
# Rule-tree evaluation
# ---------------------------------------------------------------------------

_EMPTY_OK_OPERATORS = frozenset({"is_empty", "is_not_empty"})


def _evaluate_node(node: dict[str, Any], values: dict[int, Any]) -> bool | None:
    """
    Evaluate one node. Returns:
      True / False  - the node's truth value
      None          - the node can't be evaluated (unresolved field ref) and
                      should be ignored by its parent group.
    """
    if "rules" in node:
        results = [_evaluate_node(r, values) for r in node["rules"]]
        usable = [r for r in results if r is not None]
        if not usable:
            return None
        logic = node.get("logic", "AND")
        return any(usable) if logic == "OR" else all(usable)

    field_ref = node.get("field")
    if field_ref is None:
        return None  # broken {"if": "parent"} placeholder - ignore
    left = values.get(int(field_ref))
    operator = node.get("operator", "equals")
    right = node.get("value")
    value_field = node.get("value_field")
    if value_field is not None:
        # Compare against another answer; dormant until that one is answered too.
        right = values.get(int(value_field))
        if operator not in _EMPTY_OK_OPERATORS and is_empty(right):
            return False
    # A rule whose trigger field is still blank stays dormant - a `show` /
    # `require` condition should only fire once the user has actually answered
    # that field, not on first render. `is_empty` / `is_not_empty` are the
    # exceptions: they are explicitly about the blank state.
    if operator not in _EMPTY_OK_OPERATORS and is_empty(left):
        return False
    return evaluate_operator(operator, left, right)


def evaluate_tree(normalized: dict[str, Any], values: dict[int, Any]) -> bool:
    """
    Evaluate a normalized condition. An entirely-unevaluable condition is
    treated as satisfied (so ``show`` keeps the field visible, ``hide`` does
    nothing, ``require`` does not force-require).
    """
    if not normalized or "rules" not in normalized:
        return True
    top = _evaluate_node(normalized, values)
    return True if top is None else bool(top)


# ---------------------------------------------------------------------------
# Layout computation
# ---------------------------------------------------------------------------

class Layout:
    """What the answers so far imply: which questions show, which are forced required / optional,
    which have a value set for them, which have their options limited, which show a message."""

    __slots__ = ("visible_ids", "required_overrides", "set_values", "allowed_options", "messages",
                 "pages", "skipped_pages")

    def __init__(self, visible_ids: set[int], required_overrides: dict[int, bool],
                 set_values: dict[int, Any] | None = None,
                 allowed_options: dict[int, list] | None = None,
                 messages: dict[int, str] | None = None,
                 pages: list[list[int]] | None = None,
                 skipped_pages: set[int] | None = None):
        self.visible_ids = visible_ids
        self.required_overrides = required_overrides
        self.set_values = set_values or {}
        self.allowed_options = allowed_options or {}
        self.messages = messages or {}
        # Question ids per page (a PAGE_BREAK heads the page it starts) and the pages routing skips.
        self.pages = pages or []
        self.skipped_pages = skipped_pages or set()

    def is_visible(self, field_id: int) -> bool:
        return field_id in self.visible_ids

    def effective_required(self, field) -> bool:
        return self.required_overrides.get(field.id, field.is_required)


def split_pages(active_fields) -> list[list[int]]:
    """Question ids per page, in order. A PAGE_BREAK starts a new page and heads it; page 0 has no break."""
    pages: list[list[int]] = [[]]
    for f in active_fields:
        if getattr(f, "type", None) == FieldType.PAGE_BREAK:
            pages.append([f.id])
        else:
            pages[-1].append(f.id)
    return pages


def _skipped_pages(pages, normalized, visible, values) -> set[int]:
    """
    Walk the pages from the start. Leaving a page, the PAGE_BREAK that starts the next one may route:
    ``skip_to_section`` jumps ahead to a later PAGE_BREAK, ``end_form`` ends the form there. A rule that
    cannot be evaluated never routes. Only answers on reachable pages count.
    """
    break_page = {page[0]: index for index, page in enumerate(pages) if index > 0}
    effective = {fid: v for fid, v in values.items() if fid in visible}
    skipped: set[int] = set()
    current = 0
    while current < len(pages) - 1:
        following = current + 1
        norm = normalized.get(pages[following][0])
        if norm and norm.get("action") in ROUTING_ACTIONS and _evaluate_node(norm, effective) is True:
            if norm["action"] == "end_form":
                skipped.update(range(following, len(pages)))
                break
            target = break_page.get(_as_int(norm.get("action_value")))
            if target is not None and target > following:
                skipped.update(range(following, target))
                following = target
        current = following
    return skipped


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def compute_layout(active_fields, values: dict[int, Any]) -> Layout:
    """
    ``active_fields`` - the form's non-deleted FormField rows, in display order.
    ``values``        - {field_id: submitted value} (coerced where possible).

    Bounded fixpoint: at most ``len(fields) + 1`` passes so a cyclic dependency
    (A shows if B, B shows if A) terminates. Page routing wraps it: pages the route skips are
    hidden, which changes what the remaining conditions can see, so the two settle together.
    """
    normalized: dict[int, dict[str, Any]] = {}
    for f in active_fields:
        norm = normalize_conditional_logic(f.conditional_logic)
        if norm:
            normalized[f.id] = norm

    pages = split_pages(active_fields)
    skipped_pages: set[int] = set()
    visible: set[int] = set()
    overrides: dict[int, bool] = {}

    for _route_pass in range(len(pages) + 1):
        forced_hidden = {fid for index in skipped_pages for fid in pages[index]}
        visible = {f.id for f in active_fields} - forced_hidden
        overrides = {}

        max_passes = len(active_fields) + 1
        for _ in range(max_passes):
            changed = False
            # Only fields the user can currently see contribute to condition inputs.
            effective_values = {fid: v for fid, v in values.items() if fid in visible}

            for f in active_fields:
                norm = normalized.get(f.id)
                if not norm or f.id in forced_hidden:
                    continue
                satisfied = evaluate_tree(norm, effective_values)
                action = norm.get("action", "show")

                if action == "show":
                    want_visible = satisfied
                    if want_visible and f.id not in visible:
                        visible.add(f.id)
                        changed = True
                    elif not want_visible and f.id in visible:
                        visible.discard(f.id)
                        changed = True
                elif action == "hide":
                    if satisfied and f.id in visible:
                        visible.discard(f.id)
                        changed = True
                    elif not satisfied and f.id not in visible and not _has_show_rule(norm):
                        visible.add(f.id)
                        changed = True
                elif action == "require":
                    new_val = True if satisfied else None
                    if overrides.get(f.id) != new_val:
                        if new_val is None:
                            overrides.pop(f.id, None)
                        else:
                            overrides[f.id] = True
                        changed = True
                elif action == "optional":
                    new_val = False if satisfied else None
                    if (f.id in overrides) != (new_val is not None) or overrides.get(f.id) != new_val:
                        if new_val is None:
                            overrides.pop(f.id, None)
                        else:
                            overrides[f.id] = False
                        changed = True

            if not changed:
                break

        routed = _skipped_pages(pages, normalized, visible, values)
        if routed == skipped_pages:
            break
        skipped_pages = routed

    # Value-style actions read the settled visibility, so they run once, after the fixpoint.
    set_values: dict[int, Any] = {}
    allowed_options: dict[int, list] = {}
    messages: dict[int, str] = {}
    final_values = {fid: v for fid, v in values.items() if fid in visible}
    for f in active_fields:
        norm = normalized.get(f.id)
        action = norm.get("action") if norm else None
        if action not in ("set_value", "restrict_options", "display_message") or f.id not in visible:
            continue
        payload = norm.get("action_value")
        if payload is None or not evaluate_tree(norm, final_values):
            continue
        if action == "set_value":
            set_values[f.id] = payload
        elif action == "restrict_options":
            allowed_options[f.id] = payload if isinstance(payload, list) else [payload]
        else:
            messages[f.id] = str(payload)

    return Layout(visible, overrides, set_values, allowed_options, messages, pages, skipped_pages)


def _has_show_rule(norm: dict[str, Any]) -> bool:
    return norm.get("action", "show") == "show"


# ---------------------------------------------------------------------------
# Backward-compat shims - the module-level names the rest of the codebase
# already imports from apps.forms.serializers.
# ---------------------------------------------------------------------------

def evaluate_condition(conditional_logic, submitted_map: dict) -> bool:
    """
    Legacy signature: ``submitted_map`` is keyed by *string* field id.
    Kept so callers outside the engine keep working.
    """
    norm = normalize_conditional_logic(conditional_logic)
    if not norm:
        return True
    values = {}
    for k, v in (submitted_map or {}).items():
        try:
            values[int(k)] = v
        except (TypeError, ValueError):
            continue
    return evaluate_tree(norm, values)


def evaluate_visible_fields(form_fields, submitted_map: dict) -> set:
    """Legacy signature: returns the set of visible field ids."""
    values = {}
    for k, v in (submitted_map or {}).items():
        try:
            values[int(k)] = v
        except (TypeError, ValueError):
            continue
    return compute_layout(list(form_fields), values).visible_ids
