"""
Housekeeping for form file uploads, run from management commands:

* ``sweep_expired``   remove uploads nobody attached in time, and forget old finished rows;
* ``scan_pending``    read malware-scan results and act on them;
* ``backfill_inline`` move old base64 (``data:``) file answers into the bucket.
"""
from __future__ import annotations

import base64
import binascii
import logging
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.audit.utils import log_audit_event

from . import upload_storage
from .models import Answer, FieldType, FormUpload, ScanStatus, UploadState
from .upload_attach import _delete_objects
from .validation import limits
from .validation.uploads import check_magic

logger = logging.getLogger(__name__)

SCAN_PENDING = 'SCAN_PENDING'
_SAFE_VERDICT = 'NO_THREATS_FOUND'
_INFECTED_VERDICT = 'THREATS_FOUND'


def scanning_enabled() -> bool:
    return getattr(settings, 'FORM_UPLOAD_SCAN', 'off') == 'guardduty'


# --- expired uploads ------------------------------------------------------------------------------

def sweep_expired(*, now=None, batch: int = 500, keep_rows_days: int = 30) -> dict[str, int]:
    """Remove uploads that were never attached (and their objects), then drop old finished rows."""
    now = now or timezone.now()
    stale = list(FormUpload.objects.filter(state__in=(UploadState.PENDING, UploadState.UPLOADED), expires_at__lt=now)[:batch])
    if stale:
        FormUpload.objects.filter(pk__in=[u.pk for u in stale]).update(state=UploadState.DELETED, deleted_at=now)
        _delete_objects([u.key for u in stale])
    forgotten, _ = FormUpload.objects.filter(
        state__in=(UploadState.DELETED, UploadState.REJECTED), updated_at__lt=now - timedelta(days=keep_rows_days),
    ).delete()
    return {'expired': len(stale), 'rows_forgotten': forgotten}


# --- malware scan results -------------------------------------------------------------------------

def refresh_scan(upload: FormUpload, storage=None) -> str:
    """Update one file from its scan verdict. Returns the file's scan status afterwards."""
    if upload.scan_status != ScanStatus.NOT_SCANNED:
        return upload.scan_status
    storage = storage or upload_storage.get_upload_storage()
    try:
        verdict = storage.scan_result(upload.key)
    except Exception:  # noqa: BLE001 - try again on the next run
        logger.warning('Could not read the scan result of form upload %s', upload.pk, exc_info=True)
        return upload.scan_status
    if verdict == _SAFE_VERDICT:
        upload.scan_status = ScanStatus.CLEAN
        upload.save(update_fields=['scan_status', 'updated_at'])
    elif verdict == _INFECTED_VERDICT:
        _quarantine(upload)
    return upload.scan_status


def _quarantine(upload: FormUpload) -> None:
    """A file with malware: out of the answer and out of the bucket, and the event is recorded."""
    from .upload_admin import _forget  # the same removal an admin delete does
    with transaction.atomic():
        locked = FormUpload.objects.select_for_update(of=('self',)).get(pk=upload.pk)
        if locked.response_id:
            _forget(locked, by=None)
        locked.scan_status = ScanStatus.INFECTED
        locked.state = UploadState.DELETED
        locked.deleted_at = timezone.now()
        locked.save(update_fields=['scan_status', 'state', 'deleted_at', 'updated_at'])
        log_audit_event(actor=None, action='Malware Found In Form File', target_model='Form', target_id=str(locked.form_id), details={
            'file': locked.original_name, 'upload_id': str(locked.pk), 'response_id': locked.response_id,
            'uploader_id': locked.uploader_id,
        })
        transaction.on_commit(lambda: _delete_objects([locked.key]))
    upload.scan_status, upload.state = ScanStatus.INFECTED, UploadState.DELETED


def scan_pending(*, days: int = 3, batch: int = 500) -> dict[str, int]:
    """Check files still waiting for a verdict (uploaded in the last ``days`` days)."""
    since = timezone.now() - timedelta(days=days)
    waiting = list(FormUpload.objects.filter(
        scan_status=ScanStatus.NOT_SCANNED, state__in=(UploadState.UPLOADED, UploadState.ATTACHED), created_at__gte=since,
    )[:batch])
    storage = upload_storage.get_upload_storage() if waiting else None
    counts = {'clean': 0, 'infected': 0, 'waiting': 0}
    for upload in waiting:
        outcome = refresh_scan(upload, storage)
        counts['clean' if outcome == ScanStatus.CLEAN else 'infected' if outcome == ScanStatus.INFECTED else 'waiting'] += 1
    return counts


# --- old inline answers -> bucket -----------------------------------------------------------------

def _split_data_url(url: str) -> tuple[str, bytes] | None:
    header, sep, payload = url.partition(',')
    if not sep or not header.lower().startswith('data:') or not header.lower().endswith(';base64'):
        return None
    try:
        return header[5:].split(';', 1)[0].strip().lower() or 'application/octet-stream', base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return None


def _backfill_item(answer: Answer, item: Any, storage, *, dry_run: bool) -> tuple[Any, str]:
    """Return ``(new_item, outcome)``; outcome is moved / kept / skipped:<why>."""
    if not (isinstance(item, dict) and isinstance(item.get('url'), str) and item['url'].lower().startswith('data:')):
        return item, 'kept'
    parsed = _split_data_url(item['url'])
    name = str(item.get('name') or 'file')
    if parsed is None:
        return item, 'skipped:unreadable'
    content_type, body = parsed
    if len(body) > limits.platform_file_limit_bytes():
        return item, 'skipped:too-large'
    if check_magic(name, body[:32], len(body)):
        return item, 'skipped:content-mismatch'
    if dry_run:
        return item, 'moved'
    response = answer.response
    upload = FormUpload(
        form_id=response.form_id, field_id=answer.field_id, uploader=response.user, response=response, original_name=name[:255],
        content_type=content_type[:255], declared_size=len(body), size=len(body), state=UploadState.ATTACHED,
    )
    upload.key = upload_storage.final_key(response.form_id, upload.pk)
    storage.put(upload.key, body, content_type)
    upload.save()
    return {'upload_id': str(upload.pk), 'name': upload.original_name, 'size': upload.size, 'type': upload.content_type}, 'moved'


def backfill_inline(*, form_slug: str | None = None, limit: int = 0, dry_run: bool = False) -> dict[str, int]:
    """Move base64 file answers into the bucket. Safe to run again: moved answers no longer hold ``data:`` links."""
    answers = Answer.objects.filter(field__type__in=(FieldType.FILE, FieldType.MULTI_FILE), value__isnull=False).select_related('response', 'field')
    if form_slug:
        answers = answers.filter(response__form__slug=form_slug)
    storage = None if dry_run else upload_storage.get_upload_storage()
    counts = {'answers': 0, 'files_moved': 0, 'files_skipped': 0}
    for answer in answers.order_by('pk').iterator():
        value = answer.value
        items = value if isinstance(value, list) else [value]
        if not any(isinstance(i, dict) and str(i.get('url', '')).lower().startswith('data:') for i in items):
            continue
        if limit and counts['answers'] >= limit:
            break
        rewritten, moved, skipped = [], 0, 0
        try:
            with transaction.atomic():
                for item in items:
                    new_item, outcome = _backfill_item(answer, item, storage, dry_run=dry_run)
                    rewritten.append(new_item)
                    moved += outcome == 'moved'
                    skipped += outcome.startswith('skipped')
                if moved and not dry_run:
                    answer.value = rewritten if isinstance(value, list) else rewritten[0]
                    answer.save(update_fields=['value', 'updated_at'])
        except Exception:  # noqa: BLE001 - leave this answer as it was and carry on
            logger.warning('Backfill failed for answer %s', answer.pk, exc_info=True)
            skipped += 1
            moved = 0
        counts['answers'] += 1
        counts['files_moved'] += moved
        counts['files_skipped'] += skipped
    return counts


# --- how much work is waiting (for the Maintenance page) --------------------------------------------

def count_expired() -> int:
    return FormUpload.objects.filter(state__in=(UploadState.PENDING, UploadState.UPLOADED), expires_at__lt=timezone.now()).count()


def count_unscanned(days: int = 3) -> int:
    return FormUpload.objects.filter(
        scan_status=ScanStatus.NOT_SCANNED, state__in=(UploadState.UPLOADED, UploadState.ATTACHED),
        created_at__gte=timezone.now() - timedelta(days=days),
    ).count()


def count_inline_answers() -> int:
    from django.db.models import TextField
    from django.db.models.functions import Cast
    return Answer.objects.filter(field__type__in=(FieldType.FILE, FieldType.MULTI_FILE)).annotate(
        as_text=Cast('value', TextField()),
    ).filter(as_text__contains='"url": "data:').count()
