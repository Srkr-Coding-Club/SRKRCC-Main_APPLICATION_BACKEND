"""
Who may take part, and what a participant must provide to join: eligibility, capacity,
rules acceptance and the admin-defined registration questions.

Pure checks only (no team state changes); TeamService calls these before it creates a team
or accepts an invite, so the rules are the same on every path.
"""
from __future__ import annotations

from django.utils import timezone

from apps.forms.serializers import PROFILE_FIELD_GETTERS

from .models import Hackathon, TeamMember, TeamStatus
from .errors import HackathonError

ACTIVE_TEAM_STATUSES = (TeamStatus.FORMING, TeamStatus.REGISTERED)
MAX_ANSWER_LENGTH = 1000


def _profile_value(user, key: str) -> str:
    getter = PROFILE_FIELD_GETTERS.get(key)
    return str(getter(user) if getter else '' or '').strip()


def check_eligibility(hackathon: Hackathon, user, *, who: str = 'You') -> None:
    """Raise NOT_ELIGIBLE when the user's profile falls outside the lists the admin set."""
    rules = hackathon.eligibility or {}
    checks = (('colleges', 'college', 'college'), ('branches', 'branch', 'branch'), ('years', 'year', 'year'))
    for list_key, profile_key, label in checks:
        allowed = rules.get(list_key) or []
        if not allowed:
            continue
        value = _profile_value(user, profile_key)
        if not value:
            raise HackathonError(
                f'This hackathon is limited by {label}. Add your {label} to your profile first.',
                'PROFILE_INCOMPLETE',
            )
        if value.lower() not in {item.lower() for item in allowed}:
            raise HackathonError(
                f"{who} can't take part: this hackathon is open to {label}: {', '.join(allowed)}.",
                'NOT_ELIGIBLE',
            )


def clean_answers(hackathon: Hackathon, raw) -> dict:
    """Validate answers against the admin's questions; returns {key: value} for the known ones."""
    questions = hackathon.registration_questions or []
    raw = raw if isinstance(raw, dict) else {}
    cleaned = {}
    for question in questions:
        key, label, kind = question['key'], question['label'], question.get('type', 'text')
        value = raw.get(key)
        if kind == 'checkbox':
            value = bool(value)
            empty = not value
        else:
            value = str(value if value is not None else '').strip()
            empty = not value
        if empty:
            if question.get('required'):
                raise HackathonError(f'Please answer: {label}', 'ANSWER_REQUIRED', field=key)
            continue
        if kind != 'checkbox' and len(value) > MAX_ANSWER_LENGTH:
            raise HackathonError(f'"{label}" is too long (max {MAX_ANSWER_LENGTH} characters).', 'ANSWER_TOO_LONG', field=key)
        if kind == 'select' and value not in question.get('options', []):
            raise HackathonError(f'Pick one of the listed options for: {label}', 'ANSWER_INVALID', field=key)
        if kind == 'number':
            try:
                float(value)
            except ValueError:
                raise HackathonError(f'"{label}" must be a number.', 'ANSWER_INVALID', field=key)
        if kind == 'url' and not value.lower().startswith(('http://', 'https://')):
            raise HackathonError(f'"{label}" must be a full http(s) link.', 'ANSWER_INVALID', field=key)
        cleaned[key] = value
    return cleaned


def registration_inputs(hackathon: Hackathon, data: dict | None) -> tuple:
    """(rules_accepted_at, answers) from what the participant sent, enforcing what the admin requires."""
    data = data or {}
    accepted_at = None
    if data.get('accept_rules'):
        accepted_at = timezone.now()
    elif hackathon.require_rules_acceptance and (hackathon.rules or '').strip():
        raise HackathonError('Please accept the rules to continue.', 'RULES_NOT_ACCEPTED', field='accept_rules')
    return accepted_at, clean_answers(hackathon, data.get('answers'))


def missing_registration_details(hackathon: Hackathon, membership: TeamMember) -> list[str]:
    """What an existing member still owes (questions added after they joined, or an admin adding them)."""
    missing = []
    answers = membership.answers or {}
    for question in hackathon.registration_questions or []:
        if question.get('required') and not answers.get(question['key']):
            missing.append(question['label'])
    if hackathon.require_rules_acceptance and (hackathon.rules or '').strip() and not membership.rules_accepted_at:
        missing.append('Accept the rules')
    return missing


# -- capacity ------------------------------------------------------------------

def placed_teams(hackathon: Hackathon):
    return hackathon.teams.filter(status__in=ACTIVE_TEAM_STATUSES, is_waitlisted=False)


def placed_participants(hackathon: Hackathon) -> int:
    return hackathon.memberships.filter(
        team__status__in=ACTIVE_TEAM_STATUSES, team__is_waitlisted=False,
    ).count()


def teams_full(hackathon: Hackathon) -> bool:
    return bool(hackathon.max_teams) and placed_teams(hackathon).count() >= hackathon.max_teams


def participants_would_exceed(hackathon: Hackathon, extra: int = 1) -> bool:
    return bool(hackathon.max_participants) and placed_participants(hackathon) + extra > hackathon.max_participants


def should_waitlist_new_team(hackathon: Hackathon) -> bool:
    """True when a new team must queue; raises HACKATHON_FULL when it may not even queue."""
    if teams_full(hackathon) or participants_would_exceed(hackathon):
        if hackathon.waitlist_enabled:
            return True
        raise HackathonError('This hackathon is full.', 'HACKATHON_FULL')
    return False
