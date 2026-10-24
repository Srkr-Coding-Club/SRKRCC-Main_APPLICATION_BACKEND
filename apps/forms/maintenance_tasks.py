"""The form-related housekeeping tasks shown on the admin Maintenance page. Registered from FormsConfig.ready."""
from __future__ import annotations

from apps.core import maintenance

from . import drafts, upload_maintenance
from .validation import limits


def _uploads_off():
    return None if limits.uploads_enabled() else 'File uploads to storage are switched off (FORM_UPLOADS_BACKEND is not s3).'


def _scan_off():
    return _uploads_off() or (None if upload_maintenance.scanning_enabled() else 'Virus scanning is switched off (FORM_UPLOAD_SCAN is not guardduty).')


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def register_tasks() -> None:
    maintenance.register(maintenance.Task(
        key='sweep_form_uploads', label='Clear unused uploads',
        description='Removes files people uploaded but never submitted (after the waiting period), and tidies old records.',
        run=lambda: upload_maintenance.sweep_expired(),
        describe=lambda s: f"{_plural(s.get('expired', 0), 'unused upload')} removed, {_plural(s.get('rows_forgotten', 0), 'old record')} tidied.",
        pending=upload_maintenance.count_expired, pending_label='unused upload(s) waiting to be removed',
        every_hours=24, unavailable=_uploads_off,
    ))
    maintenance.register(maintenance.Task(
        key='scan_form_uploads', label='Check uploads for viruses',
        description='Reads the virus-scan result for new files. Clean files become available; infected ones are deleted.',
        run=lambda: upload_maintenance.scan_pending(),
        describe=lambda s: f"{s.get('clean', 0)} clean, {s.get('infected', 0)} infected (deleted), {s.get('waiting', 0)} still being scanned.",
        pending=upload_maintenance.count_unscanned, pending_label='file(s) waiting for a scan result',
        every_hours=1, unavailable=_scan_off,
    ))
    maintenance.register(maintenance.Task(
        key='backfill_form_uploads', label='Move old files to storage',
        description='Moves files saved inside old responses into the storage bucket, so they open and delete like new ones. Safe to repeat.',
        run=lambda: upload_maintenance.backfill_inline(),
        describe=lambda s: f"{_plural(s.get('files_moved', 0), 'file')} moved, {s.get('files_skipped', 0)} skipped.",
        pending=upload_maintenance.count_inline_answers, pending_label='response(s) still holding old inline files',
        every_hours=None, unavailable=_uploads_off,
    ))
    maintenance.register(maintenance.Task(
        key='purge_form_drafts', label='Delete abandoned drafts',
        description='Deletes unfinished form answers nobody has touched for 30 days.',
        run=lambda: {'removed': drafts.purge_stale()},
        describe=lambda s: f"{_plural(s.get('removed', 0), 'abandoned draft')} deleted.",
        pending=lambda: drafts.stale_drafts().count(), pending_label='abandoned draft(s) waiting to be deleted',
        every_hours=24 * 7,
    ))
