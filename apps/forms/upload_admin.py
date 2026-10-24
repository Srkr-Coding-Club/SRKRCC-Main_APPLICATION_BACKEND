"""
What form admins can do with uploaded files: list them per form, get a short-lived link to view or download one,
delete files, and replace a file with another. Every change is written to the audit log.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date

from apps.audit.utils import log_audit_event

from . import upload_attach, upload_maintenance, upload_storage
from .models import Answer, FormUpload, ScanStatus, UploadState
from .upload_attach import UPLOAD_NOT_FOUND, _delete_objects
from .upload_service import UploadRefused

logger = logging.getLogger(__name__)

LINK_SECONDS = 60
MAX_BULK = 100
INLINE_TYPES = frozenset({'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'application/pdf'})
NOT_A_REPLACEMENT = 'NOT_A_REPLACEMENT'


def is_image(content_type: str) -> bool:
    return content_type.startswith('image/') and content_type != 'image/svg+xml'


def serialize_file(upload: FormUpload) -> dict[str, Any]:
    uploader = upload.uploader
    return {
        'id': str(upload.pk),
        'name': upload.original_name,
        'size': upload.size,
        'type': upload.content_type,
        'is_image': is_image(upload.content_type),
        'state': upload.state,
        'scan_status': upload.scan_status,
        'uploaded_at': upload.created_at,
        'field': {'id': upload.field_id, 'label': upload.field.label} if upload.field_id else None,
        'response_id': upload.response_id,
        'uploader': {
            'id': uploader.pk, 'name': (uploader.get_full_name() or uploader.username), 'email': uploader.email,
        } if uploader else None,
        'replaced_a_file': upload.replaces_id is not None,
    }


def list_files(form, params) -> tuple[Any, dict[str, Any]]:
    """The attached files of a form, filtered by the query string, plus totals over the same filter."""
    qs = FormUpload.objects.filter(form=form, state=UploadState.ATTACHED).select_related('field', 'uploader')
    field = params.get('field')
    if field and str(field).isdigit():
        qs = qs.filter(field_id=int(field))
    kind = params.get('kind')
    if kind == 'images':
        qs = qs.filter(content_type__startswith='image/').exclude(content_type='image/svg+xml')
    elif kind == 'documents':
        qs = qs.filter(~Q(content_type__startswith='image/') | Q(content_type='image/svg+xml'))
    if params.get('scan_status'):
        qs = qs.filter(scan_status=params['scan_status'])
    search = (params.get('search') or '').strip()
    if search:
        qs = qs.filter(
            Q(original_name__icontains=search) | Q(uploader__email__icontains=search)
            | Q(uploader__first_name__icontains=search) | Q(uploader__last_name__icontains=search),
        )
    start, end = parse_date(params.get('date_from') or ''), parse_date(params.get('date_to') or '')
    if start:
        qs = qs.filter(created_at__date__gte=start)
    if end:
        qs = qs.filter(created_at__date__lte=end)
    totals = qs.aggregate(bytes=Sum('size'))
    return qs.order_by('-created_at', '-pk'), {'bytes': totals['bytes'] or 0}


def get_visible(form, upload_id, user, *, admin: bool) -> FormUpload:
    """An upload this person may see: any attached file for an admin; their own file otherwise."""
    try:
        upload = FormUpload.objects.select_related('field').get(pk=upload_id, form=form)
    except (FormUpload.DoesNotExist, ValueError, TypeError, ValidationError):
        raise UploadRefused(UPLOAD_NOT_FOUND, 'That file was not found.', 404)
    live = upload.state in (UploadState.UPLOADED, UploadState.ATTACHED)
    if not live or not (admin or (user is not None and upload.uploader_id == user.pk)):
        raise UploadRefused(UPLOAD_NOT_FOUND, 'That file was not found.', 404)
    return upload


def make_link(upload: FormUpload, *, inline: bool) -> dict[str, Any]:
    if upload_maintenance.scanning_enabled() and upload_maintenance.refresh_scan(upload) != ScanStatus.CLEAN:
        raise UploadRefused(upload_maintenance.SCAN_PENDING, 'This file is still being checked for viruses. Try again in a minute.', 423)
    show_here = inline and upload.content_type in INLINE_TYPES
    url = upload_storage.get_upload_storage().presign_get(
        key=upload.key, filename=upload.original_name, content_type=upload.content_type, inline=show_here, expires=LINK_SECONDS,
    )
    return {'url': url, 'expires_in': LINK_SECONDS, 'inline': show_here}


def _answer_without(answer: Answer, upload_id: str):
    value = answer.value
    items = value if isinstance(value, list) else [value]
    kept = [i for i in items if not (isinstance(i, dict) and str(i.get('upload_id')) == upload_id)]
    if not kept:
        return None
    return kept if isinstance(value, list) else kept[0]


def _answer_with(answer: Answer, old_id: str, new_item: dict):
    value = answer.value
    items = value if isinstance(value, list) else [value]
    swapped = [new_item if isinstance(i, dict) and str(i.get('upload_id')) == old_id else i for i in items]
    return swapped if isinstance(value, list) else swapped[0]


def _forget(upload: FormUpload, *, by) -> bool:
    """Take the file out of its response's answer and mark it deleted. Returns True if the answer is now empty."""
    emptied = False
    answer = Answer.objects.select_for_update().filter(response_id=upload.response_id, field_id=upload.field_id).first()
    if answer is not None:
        remaining = _answer_without(answer, str(upload.pk))
        if remaining is None:
            answer.delete()
            emptied = True
        else:
            answer.value = remaining
            answer.save(update_fields=['value', 'updated_at'])
    upload.state = UploadState.DELETED
    upload.deleted_at = timezone.now()
    upload.deleted_by = by
    upload.save(update_fields=['state', 'deleted_at', 'deleted_by', 'updated_at'])
    return emptied


def _audit(by, action: str, form, upload: FormUpload, **extra) -> None:
    log_audit_event(actor=by, action=action, target_model='Form', target_id=form.slug, details={
        'file': upload.original_name, 'upload_id': str(upload.pk), 'response_id': upload.response_id,
        'field': upload.field.label if upload.field_id else None, **extra,
    })


def remove_files(form, ids: Iterable[str], *, by) -> dict[str, Any]:
    """Delete attached files. Returns how many went and which responses are now missing a required file."""
    wanted = list(dict.fromkeys(str(i) for i in ids))[:MAX_BULK]
    removed, missing_required, keys = 0, [], []
    with transaction.atomic():
        for upload in FormUpload.objects.select_for_update(of=('self',)).select_related('field').filter(
            pk__in=[i for i in wanted if _is_uuid(i)], form=form, state=UploadState.ATTACHED,
        ):
            emptied = _forget(upload, by=by)
            keys.append(upload.key)
            removed += 1
            if emptied and upload.field and upload.field.is_required:
                missing_required.append(upload.response_id)
            _audit(by, 'Deleted Form File', form, upload)
        if keys:
            transaction.on_commit(lambda: _delete_objects(keys))
    return {'removed': removed, 'responses_missing_required_file': sorted(set(missing_required))}


def replace_file(form, old_id, new_id, *, by) -> FormUpload:
    """Swap an attached file for a finished upload the admin just made for the same question."""
    with transaction.atomic():
        try:
            old = FormUpload.objects.select_for_update(of=('self',)).select_related('field').get(pk=old_id, form=form, state=UploadState.ATTACHED)
        except (FormUpload.DoesNotExist, ValueError, TypeError, ValidationError):
            raise UploadRefused(UPLOAD_NOT_FOUND, 'That file was not found.', 404)
        try:
            new = FormUpload.objects.select_for_update().get(pk=new_id, form=form, uploader=by, state=UploadState.UPLOADED)
        except (FormUpload.DoesNotExist, ValueError, TypeError, ValidationError):
            raise UploadRefused(NOT_A_REPLACEMENT, 'Upload the replacement file first.', 400)
        if new.field_id != old.field_id:
            raise UploadRefused(NOT_A_REPLACEMENT, 'The replacement was uploaded for a different question.', 400)

        target = upload_storage.final_key(form.pk, new.pk)
        try:
            upload_storage.get_upload_storage().copy(new.key, target)
        except Exception as error:  # noqa: BLE001
            logger.warning('Could not move replacement upload %s', new.pk, exc_info=True)
            raise UploadRefused(upload_attach.UPLOAD_STORAGE_ERROR, 'We could not save the new file. Please try again.', 503) from error
        stale = [new.key, old.key]
        new.key, new.state, new.response_id, new.expires_at, new.replaces = target, UploadState.ATTACHED, old.response_id, None, old
        new.save(update_fields=['key', 'state', 'response', 'expires_at', 'replaces', 'updated_at'])

        answer = Answer.objects.select_for_update().filter(response_id=old.response_id, field_id=old.field_id).first()
        reference = {'upload_id': str(new.pk), 'name': new.original_name, 'size': new.size, 'type': new.content_type}
        if answer is not None:
            answer.value = _answer_with(answer, str(old.pk), reference)
            answer.save(update_fields=['value', 'updated_at'])
        else:
            Answer.objects.create(response_id=old.response_id, field_id=old.field_id, value=[reference])
        old.state, old.deleted_at, old.deleted_by = UploadState.DELETED, timezone.now(), by
        old.save(update_fields=['state', 'deleted_at', 'deleted_by', 'updated_at'])
        _audit(by, 'Replaced Form File', form, old, replaced_with=new.original_name, new_upload_id=str(new.pk))
        transaction.on_commit(lambda: _delete_objects(stale))
    return new


def _is_uuid(value: str) -> bool:
    return upload_attach._as_uuid(value) is not None
