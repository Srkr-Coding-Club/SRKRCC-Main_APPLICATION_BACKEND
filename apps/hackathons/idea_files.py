"""
Files for the idea phase: the slides (.ppt / .pptx) and the payment screenshot.

They go straight to the storage bucket like form uploads (presigned POST, checked on completion) but belong to a team.
The payment screenshot and the transaction id are for organizers only: judges never see them.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.audit.utils import log_audit_event
from apps.forms import upload_attach, upload_storage
from apps.forms.models import ScanStatus, UploadState
from apps.forms.validation import limits
from apps.forms.validation.uploads import check_magic, extension

from . import access
from .errors import HackathonError
from .models import Hackathon, IdeaFile, IdeaFileKind, IdeaSubmission, Team

logger = logging.getLogger(__name__)

_MB = 1024 * 1024
_MAGIC_BYTES = 32
_ACTIVE = (UploadState.PENDING, UploadState.UPLOADED)

#: kind -> (allowed extensions, content types we accept, biggest file in MB)
RULES = {
    IdeaFileKind.DECK: (('.ppt', '.pptx'), None, 25),
    IdeaFileKind.PAYMENT: (('.png', '.jpg', '.jpeg', '.webp'), ('image/png', 'image/jpeg', 'image/webp'), 5),
    IdeaFileKind.QR: (('.png', '.jpg', '.jpeg', '.webp'), ('image/png', 'image/jpeg', 'image/webp'), 5),
}
LABEL = {IdeaFileKind.DECK: 'slides', IdeaFileKind.PAYMENT: 'payment screenshot', IdeaFileKind.QR: 'QR code'}
#: The request field each kind is sent in, so errors point at the right input.
FIELD = {IdeaFileKind.DECK: 'deck_file_id', IdeaFileKind.PAYMENT: 'payment_file_id', IdeaFileKind.QR: 'file'}
_CONTENT_TYPE_FOR = {
    '.ppt': 'application/vnd.ms-powerpoint',
    '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp',
}


def available() -> bool:
    return limits.uploads_enabled()


def max_bytes(kind: str) -> int:
    return int(min(RULES[kind][2], settings.FORM_UPLOAD_CEILING_MB) * _MB)


def _key(hackathon_id: int, team_id: int | None, file_id) -> str:
    return f'hackathon-ideas/tmp/{hackathon_id}/{team_id or "hackathon"}/{file_id}'


def _final_key(hackathon_id: int, team_id: int | None, file_id) -> str:
    return f'hackathon-ideas/files/{hackathon_id}/{team_id or "hackathon"}/{file_id}'


def _release(file: IdeaFile) -> None:
    try:
        upload_storage.get_upload_storage().delete(file.key)
    except Exception:  # noqa: BLE001 - never fail a request over storage trouble
        logger.warning('Could not delete idea file object %s', file.pk, exc_info=True)


def _discard(file: IdeaFile) -> None:
    file.state, file.deleted_at = UploadState.DELETED, timezone.now()
    file.save(update_fields=['state', 'deleted_at', 'updated_at'])
    _release(file)


def request_upload(team: Team, user, kind: str, name, size, content_type) -> dict:
    """A team's slides or payment screenshot: check the file against the rules for its kind and return a signed form to post to."""
    from . import ideas
    ideas.ensure_can_edit(team, user)
    if kind == IdeaFileKind.QR:
        raise HackathonError('Unknown file kind.', 'INVALID', field='kind')
    return _issue(team.hackathon, team, user, kind, name, size)


def _issue(hackathon: Hackathon, team: Team | None, user, kind: str, name, size) -> dict:
    if kind not in IdeaFileKind.values:
        raise HackathonError('Unknown file kind.', 'INVALID', field='kind')
    if not available():
        raise HackathonError('File uploads are not available right now.', 'UPLOADS_UNAVAILABLE', status=503)
    extensions, types, _ = RULES[kind]
    cleaned = str(name or '').strip()
    ext = extension(cleaned)
    if (not cleaned or len(cleaned) > limits.MAX_FILE_NAME_LENGTH or '/' in cleaned or '\\' in cleaned
            or any(ord(char) < 32 or ord(char) == 127 for char in cleaned)):
        raise HackathonError('That is not a valid file name.', 'INVALID', field=FIELD[kind])
    if ext not in extensions:
        raise HackathonError(f"The {LABEL[kind]} must be {' or '.join(extensions)}.", 'FILE_TYPE_NOT_ALLOWED', field=FIELD[kind])
    try:
        declared = int(size)
    except (TypeError, ValueError):
        raise HackathonError('The file size is missing.', 'INVALID', field=FIELD[kind])
    if declared <= 0:
        raise HackathonError('That file is empty.', 'FILE_TOO_SMALL', field=FIELD[kind])
    if declared > max_bytes(kind):
        raise HackathonError(f'The {LABEL[kind]} is larger than {max_bytes(kind) // _MB} MB.', 'FILE_TOO_LARGE', field=FIELD[kind], status=413)
    content_type = _CONTENT_TYPE_FOR[ext]

    with transaction.atomic():
        for earlier in IdeaFile.objects.select_for_update().filter(hackathon=hackathon, team=team, kind=kind, state__in=_ACTIVE):
            _discard(earlier)  # a new pick replaces the one not yet saved with the idea
        pending = IdeaFile(
            hackathon=hackathon, team=team, kind=kind, uploader=user, original_name=cleaned, content_type=content_type,
            declared_size=declared, expires_at=timezone.now() + timedelta(hours=settings.FORM_UPLOAD_PENDING_HOURS),
        )
        pending.key = _key(hackathon.pk, team.pk if team else None, pending.pk)
        pending.save()
    signed = upload_storage.get_upload_storage().presign_post(
        key=pending.key, content_type=content_type, min_bytes=1, max_bytes=max_bytes(kind), expires=settings.FORM_UPLOAD_PRESIGN_SECONDS,
    )
    return {'file': pending, 'post': signed, 'max_bytes': max_bytes(kind), 'expires_in': settings.FORM_UPLOAD_PRESIGN_SECONDS}


def complete_upload(team: Team, user, file_id) -> IdeaFile:
    """Check what reached the bucket. Safe to call twice; a file that fails the checks is removed."""
    from . import ideas
    ideas.ensure_can_edit(team, user)
    return _verify(team.hackathon, team, file_id)


def _verify(hackathon: Hackathon, team: Team | None, file_id) -> IdeaFile:
    refusal = None
    with transaction.atomic():
        try:
            file = IdeaFile.objects.select_for_update().get(pk=file_id, hackathon=hackathon, team=team)
        except (IdeaFile.DoesNotExist, ValueError, TypeError):
            raise HackathonError('That upload was not found.', 'NOT_FOUND', status=404)
        except Exception:  # noqa: BLE001 - a malformed id
            raise HackathonError('That upload was not found.', 'NOT_FOUND', status=404)
        if file.state == UploadState.UPLOADED:
            return file
        if file.state != UploadState.PENDING:
            raise HackathonError('That upload was not found.', 'NOT_FOUND', status=404)
        if file.expires_at and file.expires_at <= timezone.now():
            raise HackathonError('That upload took too long. Please choose the file again.', 'UPLOAD_EXPIRED', status=410)
        storage = upload_storage.get_upload_storage()
        stored = storage.head(file.key)
        if stored is None:
            raise HackathonError('The file did not arrive. Please try again.', 'UPLOAD_MISSING', status=409)
        if stored.size > max_bytes(file.kind):
            refusal = HackathonError(f'The {LABEL[file.kind]} is larger than {max_bytes(file.kind) // _MB} MB.', 'FILE_TOO_LARGE', field=FIELD[file.kind], status=413)
        elif stored.size <= 0:
            refusal = HackathonError('That file is empty.', 'FILE_TOO_SMALL', field=FIELD[file.kind])
        else:
            mismatch = check_magic(file.original_name, storage.read_start(file.key, _MAGIC_BYTES), stored.size)
            if mismatch:
                refusal = HackathonError(mismatch, 'FILE_CONTENT_MISMATCH', field=FIELD[file.kind])
        if refusal:
            file.state = UploadState.REJECTED
        else:
            file.state, file.size = UploadState.UPLOADED, stored.size
        file.save(update_fields=['state', 'size', 'updated_at'])
    if refusal:
        _release(file)
        raise refusal
    return file


def discard_upload(team: Team, user, file_id) -> None:
    from . import ideas
    ideas.ensure_can_edit(team, user)
    with transaction.atomic():
        try:
            file = IdeaFile.objects.select_for_update().get(pk=file_id, team=team)
        except Exception:  # noqa: BLE001 - unknown or malformed id
            raise HackathonError('That upload was not found.', 'NOT_FOUND', status=404)
        if file.state not in _ACTIVE:
            raise HackathonError('That upload was not found or is already part of the idea.', 'NOT_FOUND', status=404)
        _discard(file)


# --- using them with the idea --------------------------------------------------------------------

def resolve(team: Team, kind: str, file_id, current: IdeaFile | None) -> IdeaFile | None:
    """The file the idea should hold for this kind. ``file_id`` is the one the browser chose (or the current one)."""
    if file_id in (None, ''):
        return None
    if current is not None and str(current.pk) == str(file_id):
        return current
    try:
        file = IdeaFile.objects.select_for_update().get(pk=file_id, team=team, kind=kind, state=UploadState.UPLOADED)
    except Exception:  # noqa: BLE001 - unknown, malformed or someone else's id
        raise HackathonError(f'The {LABEL[kind]} is no longer available. Please add it again.', 'FILE_NOT_FOUND', field=FIELD[kind])
    return file


def attach(file: IdeaFile | None, previous: IdeaFile | None) -> None:
    """Make a newly chosen file permanent and release the one it replaces. Call inside the saving transaction."""
    stale: list[str] = []
    if file is not None and file.state == UploadState.UPLOADED:
        target = _final_key(file.hackathon_id, file.team_id, file.pk)
        try:
            upload_storage.get_upload_storage().copy(file.key, target)
        except Exception:  # noqa: BLE001
            logger.warning('Could not move idea file %s', file.pk, exc_info=True)
            raise HackathonError('We could not save the file. Please try submitting again.', 'UPLOAD_STORAGE_ERROR', field=FIELD[file.kind], status=503)
        stale.append(file.key)
        file.key, file.state, file.expires_at = target, UploadState.ATTACHED, None
        file.save(update_fields=['key', 'state', 'expires_at', 'updated_at'])
    if previous is not None and (file is None or previous.pk != file.pk) and previous.state == UploadState.ATTACHED:
        previous.state, previous.deleted_at = UploadState.DELETED, timezone.now()
        previous.save(update_fields=['state', 'deleted_at', 'updated_at'])
        stale.append(previous.key)
    if stale:
        transaction.on_commit(lambda: upload_attach._delete_objects(stale))


def release_all(files) -> None:
    """The team or hackathon owning these files is going away: remove the objects after the delete commits."""
    keys = [f.key for f in files if f.state != UploadState.DELETED]
    if keys:
        transaction.on_commit(lambda: upload_attach._delete_objects(keys))


# --- who may see what ------------------------------------------------------------------------------

def can_see(hackathon: Hackathon, user, idea: IdeaSubmission, kind: str) -> bool:
    """Slides: organizers, judges and the team. Payment proof: organizers and the team only, never judges."""
    from .access import can_judge
    from .models import TeamMember
    if access.has_permission(hackathon, user, 'ideas'):
        return True
    if user and user.is_authenticated and TeamMember.objects.filter(team_id=idea.team_id, user=user).exists():
        return True
    return kind == IdeaFileKind.DECK and can_judge(hackathon, user)


def link(hackathon: Hackathon, user, idea: IdeaSubmission, kind: str, *, inline: bool) -> dict:
    file = idea.deck_file if kind == IdeaFileKind.DECK else idea.payment_proof
    if kind not in IdeaFileKind.values or file is None or file.state != UploadState.ATTACHED or not can_see(hackathon, user, idea, kind):
        raise HackathonError('That file was not found.', 'NOT_FOUND', status=404)
    result = _signed_link(file, inline=inline)
    if kind == IdeaFileKind.PAYMENT:
        log_audit_event(actor=user, action='Viewed Idea Payment Proof', target_model='IdeaSubmission', target_id=idea.pk, details={'hackathon': hackathon.slug, 'team': idea.team.name})
    return result


def _signed_link(file: IdeaFile, *, inline: bool) -> dict:
    """A 60-second link to one stored file; held back while a virus scan is still pending."""
    from apps.forms import upload_maintenance
    if upload_maintenance.scanning_enabled() and upload_maintenance.refresh_scan(file) != ScanStatus.CLEAN:
        raise HackathonError('This file is still being checked for viruses. Try again in a minute.', 'SCAN_PENDING', status=423)
    from apps.forms.upload_admin import INLINE_TYPES
    show_here = inline and file.content_type in INLINE_TYPES
    url = upload_storage.get_upload_storage().presign_get(
        key=file.key, filename=file.original_name, content_type=file.content_type, inline=show_here, expires=60,
    )
    return {'url': url, 'expires_in': 60, 'inline': show_here}


def serialize(file: IdeaFile | None) -> dict | None:
    if file is None:
        return None
    return {'id': str(file.pk), 'name': file.original_name, 'size': file.size or file.declared_size, 'type': file.content_type, 'state': file.state}


# --- the payment QR code (set by the organizers, shown to teams) ---------------------------------------

def _require_settings(hackathon: Hackathon, user) -> None:
    if not access.has_permission(hackathon, user, 'settings'):
        from rest_framework.exceptions import PermissionDenied
        raise PermissionDenied('You are not allowed to change this hackathon.')


def request_qr_upload(hackathon: Hackathon, user, name, size) -> dict:
    _require_settings(hackathon, user)
    return _issue(hackathon, None, user, IdeaFileKind.QR, name, size)


def complete_qr_upload(hackathon: Hackathon, user, file_id) -> IdeaFile:
    """Check the uploaded image and make it the hackathon's payment QR code, replacing the earlier one."""
    _require_settings(hackathon, user)
    current = hackathon.idea_payment_qr
    if current is not None and str(current.pk) == str(file_id):
        return current   # confirming again is harmless
    file = _verify(hackathon, None, file_id)
    if file.kind != IdeaFileKind.QR:
        raise HackathonError('That upload was not found.', 'NOT_FOUND', status=404)
    with transaction.atomic():
        locked = Hackathon.objects.select_for_update().get(pk=hackathon.pk)
        previous = locked.idea_payment_qr
        if previous is not None and previous.pk == file.pk:
            return file
        attach(file, previous)
        locked.idea_payment_qr = file
        locked.save(update_fields=['idea_payment_qr', 'updated_at'])
        log_audit_event(actor=user, action='Set Hackathon Payment QR', target_model='Hackathon', target_id=hackathon.slug, details={'file': file.original_name})
    hackathon.idea_payment_qr = file
    return file


def remove_qr(hackathon: Hackathon, user) -> None:
    _require_settings(hackathon, user)
    with transaction.atomic():
        locked = Hackathon.objects.select_for_update().get(pk=hackathon.pk)
        previous = locked.idea_payment_qr
        if previous is None:
            return
        locked.idea_payment_qr = None
        locked.save(update_fields=['idea_payment_qr', 'updated_at'])
        attach(None, previous)
        log_audit_event(actor=user, action='Removed Hackathon Payment QR', target_model='Hackathon', target_id=hackathon.slug, details={'file': previous.original_name})
    hackathon.idea_payment_qr = None


def can_see_qr(hackathon: Hackathon, user) -> bool:
    """Organizers, and members of a registered team of this hackathon (the people who have to pay). Nobody else."""
    from .models import TeamMember, TeamStatus
    if access.has_any_permission(hackathon, user, ('settings', 'ideas')):
        return True
    return bool(user and user.is_authenticated and TeamMember.objects.filter(
        hackathon=hackathon, user=user, team__status=TeamStatus.REGISTERED, team__is_waitlisted=False,
    ).exists())


def qr_link(hackathon: Hackathon, user) -> dict:
    file = hackathon.idea_payment_qr
    if file is None or file.state != UploadState.ATTACHED or not can_see_qr(hackathon, user):
        raise HackathonError('That file was not found.', 'NOT_FOUND', status=404)
    return _signed_link(file, inline=True)


def serialize_qr(hackathon: Hackathon) -> dict | None:
    file = hackathon.idea_payment_qr
    return {'id': str(file.pk), 'name': file.original_name} if file is not None and file.state == UploadState.ATTACHED else None
