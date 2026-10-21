"""
Validators for the JSON settings an admin configures on a Hackathon (features, eligibility,
registration questions, certificate wording, people on the public page).

Each ``clean_*`` returns the normalised value or raises ``serializers.ValidationError``.
"""
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from django.utils.text import slugify
from rest_framework import serializers

FEATURE_KEYS = ('ideas', 'submissions', 'judging', 'check_in', 'certificates', 'team_finder', 'showcase')
QUESTION_TYPES = ('text', 'textarea', 'select', 'checkbox', 'url', 'number')
CERTIFICATE_ELIGIBILITY = ('REGISTERED', 'SUBMITTED', 'CHECKED_IN')
DEFAULT_CERTIFICATE_CONFIG = {
    'title': 'Certificate of Participation',
    'body': '{name} of team {team} took part in {hackathon}.',
    'signatory_name': '',
    'signatory_title': '',
    'eligibility': 'REGISTERED',
    'released': False,
}

MAX_QUESTIONS = 15
MAX_QUESTION_OPTIONS = 20
MAX_ELIGIBILITY_ITEMS = 50
MAX_PEOPLE = 30
MAX_CONTACTS = 10
MAX_COMMUNITY_LINKS = 10

_url_validator = URLValidator(schemes=['http', 'https'])


def _text(value, label, limit, *, required=False) -> str:
    text = ' '.join(str(value or '').split())
    if required and not text:
        raise serializers.ValidationError(f'{label} is required.')
    if len(text) > limit:
        raise serializers.ValidationError(f'{label} is limited to {limit} characters.')
    return text


def _url(value, label, *, required=False) -> str:
    text = str(value or '').strip()
    if not text:
        if required:
            raise serializers.ValidationError(f'{label} is required.')
        return ''
    try:
        _url_validator(text)
    except DjangoValidationError:
        raise serializers.ValidationError(f'{label} must be a full http(s) link.')
    return text


def _list_of_dicts(value, limit, what):
    if not isinstance(value, list):
        raise serializers.ValidationError(f'{what} must be a list.')
    if len(value) > limit:
        raise serializers.ValidationError(f'At most {limit} {what}.')
    if not all(isinstance(item, dict) for item in value):
        raise serializers.ValidationError(f'Every entry in {what} must be an object.')
    return value


def clean_features(value) -> dict:
    if value in (None, ''):
        return {}
    if not isinstance(value, dict):
        raise serializers.ValidationError('Must be an object of module switches.')
    unknown = sorted(set(value) - set(FEATURE_KEYS))
    if unknown:
        raise serializers.ValidationError(f"Unknown feature(s): {', '.join(unknown)}.")
    if not all(isinstance(flag, bool) for flag in value.values()):
        raise serializers.ValidationError('Every feature must be true or false.')
    return {key: bool(value.get(key, False)) for key in FEATURE_KEYS if key in value}


def clean_eligibility(value) -> dict:
    if value in (None, ''):
        return {}
    if not isinstance(value, dict) or set(value) - {'colleges', 'branches', 'years'}:
        raise serializers.ValidationError('Must be {colleges, branches, years}.')
    cleaned = {}
    for key in ('colleges', 'branches', 'years'):
        items = value.get(key) or []
        if not isinstance(items, list) or len(items) > MAX_ELIGIBILITY_ITEMS:
            raise serializers.ValidationError(f'{key} must be a list of at most {MAX_ELIGIBILITY_ITEMS} entries.')
        names = []
        for item in items:
            name = _text(item, key, 100)
            if name and name.lower() not in {existing.lower() for existing in names}:
                names.append(name)
        if names:
            cleaned[key] = names
    return cleaned


def clean_registration_questions(value) -> list:
    if value in (None, ''):
        return []
    items = _list_of_dicts(value, MAX_QUESTIONS, 'questions')
    cleaned, used_keys = [], set()
    for item in items:
        label = _text(item.get('label'), 'Question', 150, required=True)
        kind = item.get('type', 'text')
        if kind not in QUESTION_TYPES:
            raise serializers.ValidationError(f"Question type must be one of: {', '.join(QUESTION_TYPES)}.")
        base = slugify(item.get('key') or label).replace('-', '_')[:40] or 'question'
        key, suffix = base, 2
        while key in used_keys:
            key, suffix = f'{base}_{suffix}', suffix + 1
        used_keys.add(key)
        question = {'key': key, 'label': label, 'type': kind, 'required': bool(item.get('required', False))}
        if kind == 'select':
            options = item.get('options') or []
            if not isinstance(options, list):
                raise serializers.ValidationError('Options must be a list.')
            names = list(dict.fromkeys(_text(option, 'Option', 80) for option in options if str(option).strip()))
            if not 2 <= len(names) <= MAX_QUESTION_OPTIONS:
                raise serializers.ValidationError(f'"{label}" needs between 2 and {MAX_QUESTION_OPTIONS} options.')
            question['options'] = names
        cleaned.append(question)
    return cleaned


def clean_certificate_config(value) -> dict:
    if value in (None, ''):
        return {}
    if not isinstance(value, dict):
        raise serializers.ValidationError('Must be an object.')
    unknown = sorted(set(value) - set(DEFAULT_CERTIFICATE_CONFIG))
    if unknown:
        raise serializers.ValidationError(f"Unknown setting(s): {', '.join(unknown)}.")
    merged = {**DEFAULT_CERTIFICATE_CONFIG, **value}
    if merged['eligibility'] not in CERTIFICATE_ELIGIBILITY:
        raise serializers.ValidationError(f"eligibility must be one of: {', '.join(CERTIFICATE_ELIGIBILITY)}.")
    return {
        'title': _text(merged['title'], 'Title', 120, required=True),
        'body': _text(merged['body'], 'Wording', 600, required=True),
        'signatory_name': _text(merged['signatory_name'], 'Signatory name', 100),
        'signatory_title': _text(merged['signatory_title'], 'Signatory title', 100),
        'eligibility': merged['eligibility'],
        'released': bool(merged['released']),
    }


def clean_people(value, *, kind: str) -> list:
    """mentors / judges: [{name, role, bio, photo_url, link}]."""
    if value in (None, ''):
        return []
    people = []
    for item in _list_of_dicts(value, MAX_PEOPLE, kind):
        people.append({
            'name': _text(item.get('name'), 'Name', 100, required=True),
            'role': _text(item.get('role'), 'Role', 100),
            'bio': _text(item.get('bio'), 'Bio', 500),
            'photo_url': _url(item.get('photo_url'), 'Photo link'),
            'link': _url(item.get('link'), 'Profile link'),
        })
    return people


def clean_sponsors(value) -> list:
    if value in (None, ''):
        return []
    return [
        {
            'name': _text(item.get('name'), 'Sponsor name', 100, required=True),
            'logo_url': _url(item.get('logo_url'), 'Logo link'),
            'url': _url(item.get('url'), 'Website'),
            'tier': _text(item.get('tier'), 'Tier', 40),
        }
        for item in _list_of_dicts(value, MAX_PEOPLE, 'sponsors')
    ]


def clean_contacts(value) -> list:
    if value in (None, ''):
        return []
    contacts = []
    for item in _list_of_dicts(value, MAX_CONTACTS, 'contacts'):
        email = _text(item.get('email'), 'Email', 120)
        phone = _text(item.get('phone'), 'Phone', 30)
        if not (email or phone):
            raise serializers.ValidationError('Every contact needs an email or a phone number.')
        contacts.append({'name': _text(item.get('name'), 'Contact name', 100, required=True),
                         'role': _text(item.get('role'), 'Role', 100), 'email': email, 'phone': phone})
    return contacts


def clean_community_links(value) -> list:
    if value in (None, ''):
        return []
    return [
        {'label': _text(item.get('label'), 'Label', 60, required=True), 'url': _url(item.get('url'), 'Link', required=True)}
        for item in _list_of_dicts(value, MAX_COMMUNITY_LINKS, 'links')
    ]


def certificate_settings(hackathon) -> dict:
    """The hackathon's certificate wording with defaults filled in."""
    return {**DEFAULT_CERTIFICATE_CONFIG, **(hackathon.certificate_config or {})}


def guard_field(field, check):
    """Run a validator above and re-raise its message as a field-level HackathonError (for service-layer input)."""
    from .errors import HackathonError

    try:
        return check()
    except serializers.ValidationError as exc:
        raise HackathonError(' '.join(str(message) for message in exc.detail), 'INVALID', field=field)
