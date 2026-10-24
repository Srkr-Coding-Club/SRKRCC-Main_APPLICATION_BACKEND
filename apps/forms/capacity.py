"""How many responses already picked each option of a limited choice question (``validation_rules.optionLimits``)."""

from __future__ import annotations

from collections import Counter

from .models import Answer, FieldType

LIMITED_TYPES = (FieldType.RADIO, FieldType.DROPDOWN, FieldType.CHECKBOX)


def limited_fields(form):
    return [
        f for f in form.fields.filter(is_deleted=False, type__in=LIMITED_TYPES)
        if isinstance(f.validation_rules, dict) and f.validation_rules.get("optionLimits")
    ]


def option_counts(form, field, exclude_response=None) -> Counter:
    """{lower-cased option: responses that chose it}, test submissions excluded."""
    answers = Answer.objects.filter(field=field, response__form=form, response__is_test_submission=False)
    if exclude_response is not None:
        answers = answers.exclude(response=exclude_response)
    counts: Counter = Counter()
    for value in answers.values_list("value", flat=True).iterator():
        for item in value if isinstance(value, list) else [value]:
            if item is not None:
                counts[str(item).strip().lower()] += 1
    return counts


def caps_by_option(field) -> dict[str, int]:
    """{lower-cased option: limit}."""
    return {str(k).strip().lower(): int(v) for k, v in (field.validation_rules.get("optionLimits") or {}).items()}


def availability(form) -> dict[str, dict[str, int]]:
    """{question id: {option as written: places left}} for every limited question on the form."""
    out: dict[str, dict[str, int]] = {}
    for field in limited_fields(form):
        counts = option_counts(form, field)
        caps = caps_by_option(field)
        out[str(field.id)] = {
            str(option): max(caps[str(option).strip().lower()] - counts.get(str(option).strip().lower(), 0), 0)
            for option in field.options if str(option).strip().lower() in caps
        }
    return out
