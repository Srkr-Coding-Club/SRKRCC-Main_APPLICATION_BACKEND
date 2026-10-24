"""
What the responses to a form add up to: how many and when, how each question was answered, and where people stop.

Everything is computed from the stored answers on request (no extra tables). To keep a very popular form cheap, only the
newest ``MAX_RESPONSES_SCANNED`` responses are looked at; the result says so when that cut applies.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from django.db.models import Count
from django.db.models.functions import TruncDate
from django.utils import timezone

from .models import Answer, FieldType, Form, NON_ANSWER_FIELD_TYPES, Response, ResponseDraft
from .validation.values import is_empty

MAX_RESPONSES_SCANNED = 5000
MAX_DRAFTS_SCANNED = 5000
MAX_DISTINCT_VALUES = 12       # most common answers listed for a free-text question
CHOICE_TYPES = {FieldType.DROPDOWN, FieldType.RADIO, FieldType.CHECKBOX}
NUMERIC_TYPES = {FieldType.NUMBER, FieldType.RATING, FieldType.LINEAR_SCALE}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else [value]


def _field_stats(field, values: list, total: int) -> dict[str, Any]:
    answered = [v for v in values if not is_empty(v)]
    row: dict[str, Any] = {
        'field_id': field.id, 'label': field.label, 'type': field.type,
        'answered': len(answered),
        'answer_rate': round(len(answered) / total, 4) if total else 0,
    }
    if field.type in CHOICE_TYPES:
        counts = Counter(str(item) for v in answered for item in _as_list(v))
        row['options'] = [{'option': o, 'count': counts.get(str(o), 0)} for o in (field.options or [])]
        known = {str(o) for o in (field.options or [])}
        other = sum(c for o, c in counts.items() if o not in known)
        if other:
            row['other_count'] = other
    elif field.type in NUMERIC_TYPES:
        numbers = [n for n in (_number(v) for v in answered) if n is not None]
        if numbers:
            row['numeric'] = {
                'min': min(numbers), 'max': max(numbers), 'average': round(sum(numbers) / len(numbers), 2),
                'distribution': sorted(Counter(numbers).items())[:20] if field.type != FieldType.NUMBER else None,
            }
    elif field.type in (FieldType.TEXT, FieldType.EMAIL, FieldType.PHONE, FieldType.URL, FieldType.DATE, FieldType.TIME):
        counts = Counter(str(v).strip().lower() for v in answered)
        row['distinct'] = len(counts)
        row['most_common'] = [{'value': v, 'count': c} for v, c in counts.most_common(MAX_DISTINCT_VALUES) if c > 1]
    return row


def form_analytics(form: Form, days: int = 30) -> dict[str, Any]:
    days = max(1, min(int(days), 365))
    fields = [f for f in form.fields.filter(is_deleted=False).order_by('order', 'id') if f.type not in NON_ANSWER_FIELD_TYPES]

    real = Response.objects.filter(form=form, is_test_submission=False)
    total = real.count()
    ids = list(real.order_by('-submitted_at', '-id').values_list('id', flat=True)[:MAX_RESPONSES_SCANNED])

    by_field: dict[int, list] = {f.id: [] for f in fields}
    if ids:
        for field_id, value in Answer.objects.filter(response_id__in=ids, field_id__in=by_field).values_list('field_id', 'value'):
            by_field[field_id].append(value)
    scanned = len(ids)

    today = timezone.localdate()
    since = timezone.make_aware(datetime.combine(today - timedelta(days=days - 1), datetime.min.time()))
    per_day = {
        row['day'].isoformat(): row['n']
        for row in real.filter(submitted_at__gte=since).annotate(day=TruncDate('submitted_at')).values('day').annotate(n=Count('id'))
    }
    timeline = []
    for offset in range(days - 1, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        timeline.append({'date': day, 'count': per_day.get(day, 0)})

    # Where people who started but did not finish stopped: the last question each unfinished draft has an answer for.
    unfinished_total = ResponseDraft.objects.filter(form=form).count()
    drafts = list(ResponseDraft.objects.filter(form=form).order_by('-updated_at').values_list('answers', flat=True)[:MAX_DRAFTS_SCANNED])
    order = {str(f.id): i for i, f in enumerate(fields)}
    labels = {str(f.id): f.label for f in fields}
    stopped = Counter()
    for answers in drafts:
        answered = [fid for fid, value in (answers or {}).items() if fid in order and not is_empty(value)]
        stopped[max(answered, key=order.get) if answered else None] += 1
    dropoff = [{'field_id': int(fid) if fid else None, 'label': labels.get(fid, 'Before answering anything'), 'count': n}
               for fid, n in stopped.most_common()]

    return {
        'form': {'id': form.id, 'slug': form.slug, 'title': form.title},
        'total_responses': total,
        'responses_analysed': scanned,
        'truncated': total > scanned,
        'first_response_at': real.order_by('submitted_at').values_list('submitted_at', flat=True).first(),
        'last_response_at': real.order_by('-submitted_at').values_list('submitted_at', flat=True).first(),
        'days': days,
        'timeline': timeline,
        'unfinished': {'count': unfinished_total, 'stopped_after': dropoff},
        'fields': [_field_stats(f, by_field[f.id], scanned) for f in fields],
    }
