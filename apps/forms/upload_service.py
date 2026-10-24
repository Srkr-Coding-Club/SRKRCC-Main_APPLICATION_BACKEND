"""
Direct-to-S3 file uploads for dynamic-form file questions.

Flow: ``request_upload`` checks the question's rules and hands back a signed form for S3, ``complete_upload``
verifies what actually arrived, and ``discard_upload`` removes one the person no longer wants. Nothing here trusts
the browser: the object key is generated on the server and the size and type limits are signed into the request,
so S3 refuses a file that breaks them.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.core.permissions import _is_admin_or_club_lead

from . import drafts as form_drafts, upload_storage
from .models import FieldType, FormField, FormUpload, UploadState
from .validation import codes, limits
from .validation.field_types import _extension_set
from .validation.uploads import check_magic, extension

logger = logging.getLogger(__name__)

FILE_FIELD_TYPES = (FieldType.FILE, FieldType.MULTI_FILE)
_ACTIVE_STATES = (UploadState.PENDING, UploadState.UPLOADED)
_CONTENT_TYPE = re.compile(r'^[a-z0-9][a-z0-9!#$&^_.+-]{0,60}/[a-z0-9][a-z0-9!#$&^_.+-]{0,60}$')
_FALLBACK_CONTENT_TYPE = 'application/octet-stream'
_MAGIC_BYTES = 32
_BYTES_PER_MB = 1024 * 1024

UPLOADS_UNAVAILABLE = 'UPLOADS_UNAVAILABLE'
FORM_NOT_OPEN = 'FORM_NOT_OPEN'
NOT_A_FILE_QUESTION = 'NOT_A_FILE_QUESTION'
UPLOAD_NOT_FOUND = 'UPLOAD_NOT_FOUND'
UPLOAD_EXPIRED = 'UPLOAD_EXPIRED'
UPLOAD_MISSING = 'UPLOAD_MISSING'
TOO_MANY_PENDING = 'TOO_MANY_PENDING'


class UploadRefused(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


uploads_enabled = limits.uploads_enabled


def max_bytes_for(field: FormField) -> int:
    """The admin's limit for this question, or the platform default; never above the platform ceiling."""
    return limits.file_limit_bytes(field.validation_rules)


def min_bytes_for(field: FormField) -> int:
    try:
        kilobytes = float((field.validation_rules or {}).get('minFileSizeKB') or 0)
    except (TypeError, ValueError):
        kilobytes = 0
    return max(1, int(kilobytes * 1024))


def max_files_for(field: FormField) -> int:
    if field.type == FieldType.FILE:
        return 1
    try:
        return max(1, int((field.validation_rules or {}).get('maxFiles')))
    except (TypeError, ValueError):
        return limits.DEFAULT_MAX_FILES


def _check_name(field: FormField, name: str, content_type: str) -> str:
    cleaned = (name or '').strip()
    if (not cleaned or len(cleaned) > limits.MAX_FILE_NAME_LENGTH or '/' in cleaned or '\\' in cleaned
            or any(ord(char) < 32 or ord(char) == 127 for char in cleaned)):
        raise UploadRefused(codes.INVALID_FILE_PAYLOAD, 'That is not a valid file name.')
    rules = field.validation_rules or {}
    ext = extension(cleaned)
    allowed = _extension_set(rules.get('allowedFileTypes'))
    if allowed and ext not in allowed:
        raise UploadRefused(codes.FILE_TYPE_NOT_ALLOWED, f"Allowed file types: {rules.get('allowedFileTypes')}.")
    if ext in _extension_set(rules.get('blockedFileTypes')):
        raise UploadRefused(codes.FILE_TYPE_BLOCKED, f"'{cleaned}' is a blocked file type.")
    risky = ext in limits.DEFAULT_BLOCKED_EXTENSIONS or content_type in limits.BLOCKED_MIME_TYPES
    if risky and ext not in allowed:
        raise UploadRefused(codes.FILE_TYPE_BLOCKED, f"'{cleaned}' is a blocked file type.")
    return cleaned


def _clean_content_type(raw) -> str:
    value = str(raw or '').strip().lower()
    return value if _CONTENT_TYPE.match(value) else _FALLBACK_CONTENT_TYPE


def _release_object(upload: FormUpload) -> None:
    """Remove the stored object. A failure is logged, not raised: the row is already out of use."""
    try:
        upload_storage.get_upload_storage().delete(upload.key)
    except Exception:  # noqa: BLE001 - never let storage trouble break the person's request
        logger.warning('Could not delete form upload object %s', upload.pk, exc_info=True)


def _active_uploads(form, user, field=None):
    qs = FormUpload.objects.filter(form=form, uploader=user, state__in=_ACTIVE_STATES, response__isnull=True)
    qs = qs.filter(expires_at__gt=timezone.now())
    return qs.filter(field=field) if field is not None else qs


def request_upload(*, form, user, field_id, name, size, content_type) -> dict:
    if not uploads_enabled():
        raise UploadRefused(UPLOADS_UNAVAILABLE, 'File uploads are not available right now.', 503)
    if not (form_drafts.form_is_open(form) or _is_admin_or_club_lead(user)):
        raise UploadRefused(FORM_NOT_OPEN, 'This form is not taking responses.', 409)
    field = FormField.objects.filter(form=form, pk=field_id if isinstance(field_id, int) else -1, is_deleted=False).first()
    if field is None or field.type not in FILE_FIELD_TYPES:
        raise UploadRefused(NOT_A_FILE_QUESTION, 'That question does not take files.', 400)

    content_type = _clean_content_type(content_type)
    clean_name = _check_name(field, name, content_type)
    try:
        declared = int(size)
    except (TypeError, ValueError):
        raise UploadRefused(codes.INVALID_FILE_PAYLOAD, 'The file size is missing.')
    max_bytes, min_bytes = max_bytes_for(field), min_bytes_for(field)
    if declared > max_bytes:
        raise UploadRefused(codes.FILE_TOO_LARGE, f"'{clean_name}' is larger than the {max_bytes / _BYTES_PER_MB:g} MB limit.", 413)
    if declared < min_bytes:
        raise UploadRefused(codes.FILE_TOO_SMALL, f"'{clean_name}' is too small to be a real file.")

    with transaction.atomic():
        if field.type == FieldType.FILE:  # one file per question: a new pick replaces the earlier one
            for earlier in _active_uploads(form, user, field):
                _discard(earlier, by=user)
        elif _active_uploads(form, user, field).count() >= max_files_for(field):
            raise UploadRefused(codes.TOO_MANY_FILES, f'Attach at most {max_files_for(field)} file(s).')
        if _active_uploads(form, user).count() >= settings.FORM_UPLOAD_MAX_PENDING_PER_FORM:
            raise UploadRefused(TOO_MANY_PENDING, 'Too many files are waiting on this form. Remove some and try again.', 429)

        upload = FormUpload(
            form=form, field=field, uploader=user, original_name=clean_name, content_type=content_type,
            declared_size=declared, expires_at=timezone.now() + timedelta(hours=settings.FORM_UPLOAD_PENDING_HOURS),
        )
        upload.key = upload_storage.object_key(form.pk, upload.pk)
        upload.save()

    signed = upload_storage.get_upload_storage().presign_post(
        key=upload.key, content_type=content_type, min_bytes=min_bytes, max_bytes=max_bytes,
        expires=settings.FORM_UPLOAD_PRESIGN_SECONDS,
    )
    return {'upload': upload, 'post': signed, 'max_bytes': max_bytes, 'expires_in': settings.FORM_UPLOAD_PRESIGN_SECONDS}


def _owned(form, user, upload_id) -> FormUpload:
    try:
        return FormUpload.objects.select_for_update().get(pk=upload_id, form=form, uploader=user)
    except (FormUpload.DoesNotExist, ValueError, TypeError):
        raise UploadRefused(UPLOAD_NOT_FOUND, 'That upload was not found.', 404)


def _refusal_for(upload: FormUpload, stored, head: bytes) -> Optional[UploadRefused]:
    field = upload.field
    if field is None:
        return UploadRefused(NOT_A_FILE_QUESTION, 'That question no longer exists.')
    if stored.size > max_bytes_for(field):
        return UploadRefused(codes.FILE_TOO_LARGE, f"'{upload.original_name}' is larger than the allowed size.", 413)
    if stored.size < min_bytes_for(field):
        return UploadRefused(codes.FILE_TOO_SMALL, f"'{upload.original_name}' is too small to be a real file.")
    mismatch = check_magic(upload.original_name, head, stored.size)
    return UploadRefused(codes.FILE_CONTENT_MISMATCH, mismatch) if mismatch else None


def complete_upload(*, form, user, upload_id) -> FormUpload:
    """Check what reached the bucket. Safe to call twice. A file that fails the checks is removed."""
    refusal = None
    with transaction.atomic():
        upload = _owned(form, user, upload_id)
        if upload.state == UploadState.UPLOADED:
            return upload
        if upload.state != UploadState.PENDING:
            raise UploadRefused(UPLOAD_NOT_FOUND, 'That upload was not found.', 404)
        if upload.expires_at and upload.expires_at <= timezone.now():
            raise UploadRefused(UPLOAD_EXPIRED, 'That upload took too long. Please choose the file again.', 410)

        storage = upload_storage.get_upload_storage()
        stored = storage.head(upload.key)
        if stored is None:
            raise UploadRefused(UPLOAD_MISSING, 'The file did not arrive. Please try again.', 409)
        refusal = _refusal_for(upload, stored, storage.read_start(upload.key, _MAGIC_BYTES))
        if refusal:
            upload.state = UploadState.REJECTED
            upload.save(update_fields=['state', 'updated_at'])
        else:
            upload.size = stored.size
            upload.content_type = _clean_content_type(stored.content_type) if stored.content_type else upload.content_type
            upload.state = UploadState.UPLOADED
            upload.save(update_fields=['size', 'content_type', 'state', 'updated_at'])
    if refusal:  # raised outside the transaction so the REJECTED row is kept
        _release_object(upload)
        raise refusal
    return upload


def _discard(upload: FormUpload, *, by) -> None:
    upload.state = UploadState.DELETED
    upload.deleted_at = timezone.now()
    upload.deleted_by = by
    upload.save(update_fields=['state', 'deleted_at', 'deleted_by', 'updated_at'])
    _release_object(upload)


def discard_upload(*, form, user, upload_id) -> None:
    """The person removes a file they picked but have not submitted."""
    with transaction.atomic():
        upload = _owned(form, user, upload_id)
        if upload.state not in _ACTIVE_STATES:
            raise UploadRefused(UPLOAD_NOT_FOUND, 'That upload was not found or is already part of a response.', 404)
        _discard(upload, by=user)


def serialize(upload: FormUpload) -> dict:
    return {
        'id': str(upload.pk), 'name': upload.original_name, 'size': upload.size or upload.declared_size,
        'type': upload.content_type, 'state': upload.state,
    }
