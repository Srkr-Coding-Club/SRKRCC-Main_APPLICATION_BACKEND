"""
Form history: a snapshot of the whole definition at every save, a readable diff between any two,
and the impact a pending change would have on responses already collected.

A snapshot is plain JSON ``{"form": {...settings}, "fields": [{...question}, ...]}``. It holds the
definition only - never the slug or the status, so going back to an older version cannot unpublish
a form or change its address - and never any responses.
"""

from __future__ import annotations

import datetime as _dt
from collections import Counter
from typing import Any

from .models import Answer, Form, FormVersion

MAX_VERSIONS_KEPT = 50

# Form settings kept in a snapshot (model attribute -> label shown in the diff).
SETTING_LABELS: dict[str, str] = {
    'title': 'Title', 'description': 'Description', 'image_url': 'Image', 'category': 'Category',
    'allow_multiple_responses': 'Allow multiple responses', 'allow_response_editing': 'Allow editing a response',
    'enable_prefill': 'Pre-fill from profile', 'max_responses_per_user': 'Responses per person',
    'max_total_responses': 'Total responses allowed', 'prevent_duplicate_email_answers': 'Block repeated emails',
    'allow_edits_until': 'Edits allowed until', 'open_at': 'Opens at', 'close_at': 'Closes at',
    'club_id_enabled': 'Generate Club ID', 'club_id_prefix': 'Club ID prefix', 'club_id_field_mapping': 'Club ID field mapping',
    'club_id_verification_enabled': 'Verify Club ID',
    'confirmation_email_enabled': 'Confirmation email', 'confirmation_email_template': 'Confirmation email template',
    'confirmation_notification_enabled': 'Confirmation notification', 'notify_admin_on_submission': 'Notify admin on submission',
    'notify_members_on_publish': 'Notify members on publish', 'notification_title': 'Notification title',
    'notification_message': 'Notification message',
    'confirmation_message': 'Thank-you message', 'redirect_url': 'Redirect after submitting',
    'redirect_delay_seconds': 'Redirect delay (seconds)', 'captcha_enabled': 'Bot check (CAPTCHA)', 'language': 'Form language',
    'attendance_enabled': 'QR attendance', 'attendance_start_date': 'Attendance start date', 'attendance_days': 'Attendance days',
    'attendance_sessions_per_day': 'Sessions per day', 'attendance_window_minutes': 'Scan window (minutes)',
}
SETTING_KEYS = tuple(SETTING_LABELS)

FIELD_KEYS = (
    'id', 'label', 'type', 'placeholder', 'description', 'is_required', 'options', 'rows', 'min_value', 'max_value',
    'conditional_logic', 'validation_rules', 'profile_field', 'order',
)
FIELD_LABELS: dict[str, str] = {
    'label': 'Question', 'type': 'Type', 'placeholder': 'Placeholder', 'description': 'Help text', 'is_required': 'Required',
    'min_value': 'Minimum', 'max_value': 'Maximum', 'profile_field': 'Filled from profile',
}
CHOICE_TYPES = ('RADIO', 'DROPDOWN', 'CHECKBOX')


def _plain(value: Any) -> Any:
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    return value


def snapshot_form(form: Form) -> dict[str, Any]:
    settings = {}
    for key in SETTING_KEYS:
        attr = 'confirmation_email_template_id' if key == 'confirmation_email_template' else key
        settings[key] = _plain(getattr(form, attr))
    fields = [
        {key: _plain(getattr(f, key)) for key in FIELD_KEYS}
        for f in form.fields.filter(is_deleted=False).order_by('order', 'id')
    ]
    return {'form': settings, 'fields': fields}


def record_version(form: Form, user=None, note: str = '') -> FormVersion:
    """Store the form's current definition as version ``form.version`` (replacing it if that number was already recorded)."""
    actor = user if (user is not None and getattr(user, 'is_authenticated', False)) else None
    version, _ = FormVersion.objects.update_or_create(
        form=form, version=form.version,
        defaults={'snapshot': snapshot_form(form), 'note': note[:200], 'created_by': actor},
    )
    stale = list(FormVersion.objects.filter(form=form).order_by('-version').values_list('pk', flat=True)[MAX_VERSIONS_KEPT:])
    if stale:
        FormVersion.objects.filter(pk__in=stale).delete()
    return version


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

def _label(field: dict) -> str:
    return field.get('label') or f"Question {field.get('order') or ''}".strip()


def _list_diff(before: list, after: list) -> dict[str, list]:
    b = [str(x) for x in (before or [])]
    a = [str(x) for x in (after or [])]
    return {'added': [x for x in a if x not in b], 'removed': [x for x in b if x not in a]}


def _field_changes(old: dict, new: dict) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for key, label in FIELD_LABELS.items():
        if old.get(key) != new.get(key):
            changes.append({'key': key, 'label': label, 'before': old.get(key), 'after': new.get(key)})
    for key, label in (('options', 'Options'), ('rows', 'Rows')):
        delta = _list_diff(old.get(key), new.get(key))
        if delta['added'] or delta['removed']:
            changes.append({'key': key, 'label': label, **delta})
        elif list(old.get(key) or []) != list(new.get(key) or []):
            changes.append({'key': key, 'label': f'{label} order', 'before': old.get(key), 'after': new.get(key)})
    old_rules, new_rules = old.get('validation_rules') or {}, new.get('validation_rules') or {}
    rule_changes = [
        {'rule': rule, 'before': old_rules.get(rule), 'after': new_rules.get(rule)}
        for rule in sorted(set(old_rules) | set(new_rules)) if old_rules.get(rule) != new_rules.get(rule)
    ]
    if rule_changes:
        changes.append({'key': 'validation_rules', 'label': 'Rules', 'changed': rule_changes})
    if (old.get('conditional_logic') or {}) != (new.get('conditional_logic') or {}):
        changes.append({'key': 'conditional_logic', 'label': 'Conditions',
                        'before': old.get('conditional_logic') or {}, 'after': new.get('conditional_logic') or {}})
    return changes


def diff_snapshots(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """What changed from ``before`` to ``after``. Questions are matched by id; one without an id is new."""
    settings = [
        {'key': key, 'label': SETTING_LABELS[key], 'before': before['form'].get(key), 'after': after['form'].get(key)}
        for key in SETTING_KEYS
        # A setting added after the older snapshot was taken is not a change the person made.
        if key in before['form'] and before['form'].get(key) != after['form'].get(key)
    ]

    old_by_id = {f['id']: f for f in before['fields'] if f.get('id') is not None}
    new_ids = {f['id'] for f in after['fields'] if f.get('id') is not None}
    added = [f for f in after['fields'] if f.get('id') is None or f['id'] not in old_by_id]
    removed = [f for f in before['fields'] if f['id'] not in new_ids]
    changed = []
    for new in after['fields']:
        old = old_by_id.get(new.get('id'))
        if old is None:
            continue
        changes = _field_changes(old, new)
        if changes:
            changed.append({'id': new['id'], 'label': _label(new), 'type': new['type'], 'changes': changes})

    kept_old = [f['id'] for f in before['fields'] if f['id'] in new_ids]
    kept_new = [f['id'] for f in after['fields'] if f.get('id') in old_by_id]
    reordered = kept_old != kept_new

    brief = lambda f: {'id': f.get('id'), 'label': _label(f), 'type': f['type']}  # noqa: E731
    result = {
        'form': settings,
        'fields': {'added': [brief(f) for f in added], 'removed': [brief(f) for f in removed], 'changed': changed, 'reordered': reordered},
    }
    result['summary'] = {
        'settings': len(settings), 'added': len(added), 'removed': len(removed), 'changed': len(changed), 'reordered': reordered,
    }
    result['is_empty'] = not (settings or added or removed or changed or reordered)
    return result


# ---------------------------------------------------------------------------
# Impact on collected responses
# ---------------------------------------------------------------------------

def _answers(field_id: int):
    return Answer.objects.filter(field_id=field_id, response__is_test_submission=False)


def change_impact(form: Form, before: dict[str, Any], after: dict[str, Any], diff: dict[str, Any]) -> dict[str, Any]:
    """What applying ``after`` would mean for the responses already stored under ``before``."""
    response_count = form.responses.filter(is_test_submission=False).count()
    old_by_id = {f['id']: f for f in before['fields']}
    new_by_id = {f['id']: f for f in after['fields'] if f.get('id') is not None}

    # Questions that start being required: people who already answered never saw them.
    new_required = [
        {'id': f.get('id'), 'label': _label(f)} for f in after['fields']
        if f.get('is_required') and (f.get('id') is None or f['id'] not in old_by_id)
    ]
    now_required, options_removed, type_changes = [], [], []
    for entry in diff['fields']['changed']:
        old, new = old_by_id[entry['id']], new_by_id[entry['id']]
        if not old.get('is_required') and new.get('is_required'):
            now_required.append({'id': entry['id'], 'label': entry['label']})
        if old['type'] != new['type']:
            type_changes.append({'id': entry['id'], 'label': entry['label'], 'from': old['type'], 'to': new['type'],
                                 'answers': _answers(entry['id']).count()})
        gone = [c for c in entry['changes'] if c['key'] == 'options' and c.get('removed')]
        if gone and new['type'] in CHOICE_TYPES:
            removed_options = {str(o).strip().lower(): str(o) for o in gone[0]['removed']}
            counts: Counter = Counter()
            for value in _answers(entry['id']).values_list('value', flat=True).iterator():
                for item in value if isinstance(value, list) else [value]:
                    key = str(item).strip().lower()
                    if key in removed_options:
                        counts[removed_options[key]] += 1
            options_removed += [
                {'id': entry['id'], 'label': entry['label'], 'option': option, 'answers': n} for option, n in counts.items()
            ]

    removed_with_answers = []
    for f in diff['fields']['removed']:
        n = _answers(f['id']).count()
        if n:
            removed_with_answers.append({'id': f['id'], 'label': f['label'], 'answers': n})

    return {
        'response_count': response_count,
        'new_required': new_required if response_count else [],
        'now_required': now_required if response_count else [],
        'removed_with_answers': removed_with_answers,
        'options_removed_with_answers': options_removed,
        'type_changes_blocked': [t for t in type_changes if t['answers']],
    }
