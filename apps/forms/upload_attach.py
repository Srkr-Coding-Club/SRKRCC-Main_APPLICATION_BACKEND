"""
Tying uploaded files to a submission.

``resolve_references`` runs before validation: an answer item ``{"upload_id": ...}`` is replaced by the facts the
server holds about that upload (name, real size, type), so every file rule validates what is really in the bucket.
``attach`` runs when the response is saved: it re-checks ownership, moves each object out of the expiring ``tmp/``
prefix, and removes files the response no longer uses. ``release`` removes the objects of uploads whose response or
form is being deleted.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Iterable

from django.db import transaction

from . import upload_storage
from .models import FieldType, FormField, FormUpload, UploadState
from .validation import codes
from .validation.engine import _extract_pairs
from .validation.report import FieldError

logger = logging.getLogger(__name__)

FILE_TYPES = (FieldType.FILE, FieldType.MULTI_FILE)
UPLOAD_NOT_FOUND = 'UPLOAD_NOT_FOUND'
UPLOAD_STORAGE_ERROR = 'UPLOAD_STORAGE_ERROR'
_GONE = 'One of the files is no longer available. Please add it again.'


def _as_uuid(raw) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError, AttributeError):
        return None


def _as_int(raw) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _usable(upload: FormUpload, *, form, user, field_id, response) -> bool:
    """A person's own, finished upload for this question; or one already part of the response being edited."""
    if upload.form_id != form.pk or upload.field_id != field_id or user is None or upload.uploader_id != user.pk:
        return False
    if upload.state == UploadState.UPLOADED:
        return True
    return upload.state == UploadState.ATTACHED and response is not None and upload.response_id == response.pk


def _items(value) -> list:
    return value if isinstance(value, list) else [value]


def resolve_references(form, user, raw_answers: Any, response=None) -> tuple[list[dict], list[FieldError]]:
    """Return the answers with upload references filled in from the database, and any errors found."""
    file_fields = {f.pk: f for f in FormField.objects.filter(form=form, type__in=FILE_TYPES, is_deleted=False)}
    pairs = _extract_pairs(raw_answers)

    wanted = set()
    for raw_id, value in pairs:
        if _as_int(raw_id) in file_fields:
            wanted.update(found for item in _items(value) if isinstance(item, dict) and (found := _as_uuid(item.get('upload_id'))))
    rows = {u.pk: u for u in FormUpload.objects.filter(pk__in=wanted)} if wanted else {}

    errors: list[FieldError] = []
    seen: set[uuid.UUID] = set()
    out: list[dict] = []
    for raw_id, value in pairs:
        field = file_fields.get(_as_int(raw_id))
        if field is None or value is None:
            out.append({'field': raw_id, 'value': value})
            continue
        kept = []
        for item in _items(value):
            if not (isinstance(item, dict) and item.get('upload_id') is not None):
                kept.append(item)
                continue
            key = _as_uuid(item['upload_id'])
            row = rows.get(key) if key else None
            if row is None or not _usable(row, form=form, user=user, field_id=field.pk, response=response):
                errors.append(FieldError(code=UPLOAD_NOT_FOUND, field_id=field.pk, label=field.label, message=_GONE))
            elif key in seen:
                errors.append(FieldError(code=codes.INVALID_FILE_PAYLOAD, field_id=field.pk, label=field.label,
                                         message=f"'{row.original_name}' was attached more than once."))
            else:
                seen.add(key)
                kept.append({'upload_id': str(key), 'name': row.original_name, 'size': row.size, 'type': row.content_type})
        out.append({'field': raw_id, 'value': kept if isinstance(value, list) else (kept[0] if kept else None)})
    return out, errors


def live_references(form, user, field, value):
    """
    The part of a saved (draft) answer that is still good: references to this person's finished, unexpired uploads
    for this question, rewritten from the database. Returns None when nothing is left.
    """
    from django.utils import timezone
    if user is None:
        return None
    asked = {key for item in _items(value) if isinstance(item, dict) and (key := _as_uuid(item.get('upload_id')))}
    if not asked:
        return None
    rows = FormUpload.objects.filter(
        pk__in=asked, form=form, field=field, uploader=user, state=UploadState.UPLOADED, expires_at__gt=timezone.now(),
    )
    kept = [{'upload_id': str(u.pk), 'name': u.original_name, 'size': u.size, 'type': u.content_type} for u in rows]
    if not kept:
        return None
    return kept if field.type == FieldType.MULTI_FILE else kept[0]


def referenced_ids(cleaned: dict[int, Any], file_field_ids: Iterable[int]) -> dict[uuid.UUID, int]:
    found: dict[uuid.UUID, int] = {}
    for field_id in file_field_ids:
        for item in _items(cleaned.get(field_id)):
            if isinstance(item, dict) and (key := _as_uuid(item.get('upload_id'))):
                found[key] = field_id
    return found


def attach(response, user, cleaned: dict[int, Any]) -> list[FieldError]:
    """Make the response's files permanent and release the ones it dropped. Returns errors instead of raising."""
    file_field_ids = list(FormField.objects.filter(form_id=response.form_id, type__in=FILE_TYPES).values_list('pk', flat=True))
    wanted = referenced_ids(cleaned, file_field_ids)

    rows = {u.pk: u for u in FormUpload.objects.select_for_update().filter(pk__in=wanted)}
    for key, field_id in wanted.items():
        row = rows.get(key)
        if row is None or not _usable(row, form=response.form, user=user, field_id=field_id, response=response):
            return [FieldError(code=UPLOAD_NOT_FOUND, field_id=field_id, message=_GONE)]

    moving = [r for r in rows.values() if r.state == UploadState.UPLOADED]
    storage = upload_storage.get_upload_storage() if moving else None
    for row in moving:
        try:
            storage.copy(row.key, upload_storage.final_key(response.form_id, row.pk))
        except Exception:  # noqa: BLE001 - reported to the person as a retryable failure
            logger.warning('Could not move form upload %s', row.pk, exc_info=True)
            return [FieldError(code=UPLOAD_STORAGE_ERROR, field_id=wanted[row.pk],
                               message='We could not save one of the files. Please try submitting again.')]

    stale_keys = []
    for row in moving:
        stale_keys.append(row.key)
        row.key = upload_storage.final_key(response.form_id, row.pk)
        row.state = UploadState.ATTACHED
        row.response = response
        row.expires_at = None
        row.save(update_fields=['key', 'state', 'response', 'expires_at', 'updated_at'])

    dropped = FormUpload.objects.select_for_update().filter(response=response, state=UploadState.ATTACHED).exclude(pk__in=wanted)
    stale_keys.extend(u.key for u in dropped)
    dropped.update(state=UploadState.DELETED)
    if stale_keys:
        transaction.on_commit(lambda: _delete_objects(stale_keys))
    return []


def _delete_objects(keys: Iterable[str]) -> None:
    try:
        storage = upload_storage.get_upload_storage()
    except Exception:  # noqa: BLE001
        logger.warning('Form upload storage unavailable for cleanup', exc_info=True)
        return
    for key in keys:
        try:
            storage.delete(key)
        except Exception:  # noqa: BLE001 - a leftover object is swept later; never fail a request over it
            logger.warning('Could not delete form upload object %s', key, exc_info=True)


def release(uploads) -> None:
    """The response or form owning these uploads is going away: remove their files after the delete commits."""
    keys = [u.key for u in uploads if u.state != UploadState.DELETED]
    if keys:
        transaction.on_commit(lambda: _delete_objects(keys))
