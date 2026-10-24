"""
Server-side autosave for unfinished form responses (``ResponseDraft``).

A draft is a convenience, not a response: it is checked only far enough to be safe to store. Each answer
must at least be a value its question could hold (same size and character limits as a real submission),
anything else is dropped. Files and signatures are never stored - they would put multi-megabyte blobs in
a row that is rewritten every few seconds - so the respondent attaches those again.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from django.utils import timezone

from .models import FieldType, FormField, FormStatus, NON_ANSWER_FIELD_TYPES, ResponseDraft
from .validation import limits
from .validation.values import CoercionError, coerce_value, is_empty

DRAFT_TTL_DAYS = 30
MAX_DRAFT_BYTES = 200_000
MAX_PAGE_INDEX = 100
UNSUPPORTED_TYPES = (FieldType.FILE, FieldType.MULTI_FILE, FieldType.SIGNATURE)


def form_is_open(form, now=None) -> bool:
    """Whether the form is currently taking responses (and so worth keeping a draft for)."""
    now = now or timezone.now()
    form.evaluate_schedule_status(now)
    if form.status in (FormStatus.CLOSED, FormStatus.DRAFT, FormStatus.ARCHIVED):
        return False
    if form.status == FormStatus.SCHEDULED and form.open_at and now < form.open_at:
        return False
    return not (form.close_at and now > form.close_at)


def answerable_fields(form) -> dict[int, FormField]:
    return {
        f.id: f for f in form.fields.filter(is_deleted=False).exclude(type__in=NON_ANSWER_FIELD_TYPES)
        if not f.profile_field
    }


def sanitize_answers(form, raw_answers: Any, user=None) -> tuple[dict[str, Any], list[int]]:
    """Keep the answers that are safe to store. Returns ``(kept, dropped_field_ids)``."""
    if not isinstance(raw_answers, dict):
        return {}, []
    fields = answerable_fields(form)
    kept: dict[str, Any] = {}
    dropped: list[int] = []
    for raw_id, value in raw_answers.items():
        try:
            field = fields.get(int(raw_id))
        except (TypeError, ValueError):
            continue
        if field is None or is_empty(value):
            continue
        if field.type in UNSUPPORTED_TYPES:
            references = _file_references(form, user, field, value)
            if references is None:
                dropped.append(field.id)
            else:
                kept[str(field.id)] = references
            continue
        try:
            coerce_value(field, value)
        except CoercionError:
            dropped.append(field.id)
            continue
        kept[str(field.id)] = value
    return kept, dropped


def _file_references(form, user, field, value):
    """Files are never stored in a draft, but a file already uploaded to S3 can be remembered by reference."""
    from . import upload_attach
    if field.type not in upload_attach.FILE_TYPES or not limits.uploads_enabled():
        return None
    return upload_attach.live_references(form, user, field, value)


def fits(answers: dict) -> bool:
    return len(json.dumps(answers, ensure_ascii=False).encode('utf-8')) <= MAX_DRAFT_BYTES


def fresh_draft(form, user) -> ResponseDraft | None:
    cutoff = timezone.now() - timedelta(days=DRAFT_TTL_DAYS)
    return ResponseDraft.objects.filter(form=form, user=user, updated_at__gte=cutoff).first()


def serialize_draft(draft: ResponseDraft, form, user=None) -> dict[str, Any]:
    """The draft as the form page should see it: only answers to questions that still exist."""
    fields = answerable_fields(form)
    answers = {}
    for key, value in (draft.answers or {}).items():
        field = fields.get(int(key)) if key.isdigit() else None
        if field is None:
            continue
        if field.type in UNSUPPORTED_TYPES:
            value = _file_references(form, user, field, value)  # an upload may have expired since it was saved
            if value is None:
                continue
        answers[key] = value
    return {
        'answers': answers,
        'current_page': draft.current_page,
        'updated_at': draft.updated_at,
        'form_changed': draft.form_version != form.version,
    }


def stale_drafts(days: int = DRAFT_TTL_DAYS):
    return ResponseDraft.objects.filter(updated_at__lt=timezone.now() - timedelta(days=days))


def purge_stale(days: int = DRAFT_TTL_DAYS) -> int:
    """Delete drafts nobody has touched for ``days`` days. Returns how many."""
    removed, _ = stale_drafts(days).delete()
    return removed
