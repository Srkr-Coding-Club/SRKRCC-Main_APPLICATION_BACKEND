"""
Phase 1 of a two-phase submission flow: the idea (plan). Teams describe the problem, approach and plan;
organizers review it; the admin may require an approved idea before the project phase (implementation) opens.
"""
from __future__ import annotations

from urllib.parse import urlparse

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from apps.audit.utils import log_audit_event

from . import access, config_validation as validate, idea_files
from .errors import HackathonError
from .models import Hackathon, IdeaFileKind, IdeaStatus, IdeaSubmission, Team, TeamStatus
from .services import _notify_users, is_leader
from .config_validation import guard_field as field_guard

MAX_TITLE = 200
MAX_TEXT = 3000
MAX_FEEDBACK = 1000
MAX_TECH_STACK = 10
REVIEW_OUTCOMES = (IdeaStatus.APPROVED, IdeaStatus.CHANGES_REQUESTED, IdeaStatus.REJECTED)
LOCKED_STATUSES = (IdeaStatus.APPROVED, IdeaStatus.REJECTED)


def window_state(hackathon: Hackathon) -> str:
    """OFF, NOT_OPEN, OPEN or CLOSED for the idea phase."""
    if not hackathon.feature('ideas'):
        return 'OFF'
    now = timezone.now()
    if hackathon.idea_opens_at and now < hackathon.idea_opens_at:
        return 'NOT_OPEN'
    if hackathon.idea_deadline and now >= hackathon.idea_deadline:
        return 'CLOSED'
    return 'OPEN'


def can_team_edit(hackathon: Hackathon, existing: IdeaSubmission | None) -> tuple[bool, str, str]:
    """
    Whether a team may save the idea now: ``(allowed, code, why_not)``.

    A first submission needs the idea window to be open. After that the organizers decide: with editing switched on a
    team may keep editing until the deadline, or until ``idea_edits_until`` when they set it (even past the deadline);
    with it off the idea is locked, except when the organizers asked for changes. An accepted or rejected idea is final.
    """
    state = window_state(hackathon)
    now = timezone.now()
    if state == 'OFF':
        return False, 'FEATURE_OFF', 'Idea submission is not enabled for this hackathon.'
    if state == 'NOT_OPEN':
        return False, 'IDEAS_NOT_OPEN', 'Idea submission has not opened yet.'
    if existing is None:
        return (True, '', '') if state == 'OPEN' else (False, 'IDEAS_CLOSED', 'The idea deadline has passed.')
    if existing.status in LOCKED_STATUSES:
        return False, 'IDEA_LOCKED', 'This idea has already been decided, so it can no longer be edited. Contact the organizers if something is wrong.'
    extended = bool(hackathon.idea_edits_until and now < hackathon.idea_edits_until)
    if existing.status == IdeaStatus.CHANGES_REQUESTED:
        return (True, '', '') if state == 'OPEN' or extended else (False, 'IDEAS_CLOSED', 'The idea deadline has passed.')
    if not hackathon.idea_edits_enabled:
        return False, 'IDEA_EDITS_OFF', 'The organizers have switched off editing, so a submitted idea cannot be changed.'
    open_now = now < hackathon.idea_edits_until if hackathon.idea_edits_until else state == 'OPEN'
    return (True, '', '') if open_now else (False, 'IDEAS_CLOSED', 'The time for editing ideas is over.')


def ensure_can_edit(team: Team, actor) -> IdeaSubmission | None:
    """The checks every team-side change shares (saving the idea, adding or removing a file). Returns the idea, if any."""
    hackathon = team.hackathon
    if access.has_permission(hackathon, actor, 'ideas'):
        return IdeaSubmission.objects.filter(team=team).first()
    if not is_leader(team, actor):
        raise PermissionDenied('Only the team leader can submit the idea.')
    if team.status != TeamStatus.REGISTERED or team.is_waitlisted:
        raise HackathonError('Only registered teams can submit an idea.', 'TEAM_NOT_REGISTERED')
    existing = IdeaSubmission.objects.filter(team=team).first()
    allowed, code, why = can_team_edit(hackathon, existing)
    if not allowed:
        raise HackathonError(why, code)
    return existing


def project_gate(hackathon: Hackathon, team: Team | None) -> dict:
    """Whether this team's idea lets it into the project phase. ``required`` is False when the admin did not ask for it."""
    required = hackathon.feature('ideas') and hackathon.project_requires_approved_idea
    if not required:
        return {'required': False, 'satisfied': True, 'reason': ''}
    idea = IdeaSubmission.objects.filter(team=team).first() if team else None
    if idea is None:
        return {'required': True, 'satisfied': False, 'reason': 'Submit your idea first: the project phase opens for teams whose idea is accepted.'}
    if hackathon.idea_review_required and idea.status != IdeaStatus.APPROVED:
        reason = {
            IdeaStatus.SUBMITTED: 'Your idea is waiting for the organizers to review it.',
            IdeaStatus.CHANGES_REQUESTED: 'The organizers asked for changes to your idea. Update it and resubmit.',
            IdeaStatus.REJECTED: 'Your idea was not accepted, so this team cannot submit a project.',
        }.get(idea.status, 'Your idea has not been approved yet.')
        return {'required': True, 'satisfied': False, 'reason': reason}
    return {'required': True, 'satisfied': True, 'reason': ''}


_LINKEDIN_HOSTS = ('linkedin.com', 'lnkd.in')
MAX_TRANSACTION_ID = 60


def _linkedin(data: dict, hackathon: Hackathon | None, by_organizer: bool) -> str | None:
    required = bool(hackathon and hackathon.idea_linkedin_required and not by_organizer)
    value = field_guard('linkedin_url', lambda: validate._url(data.get('linkedin_url'), 'LinkedIn post link', required=required))
    if not value:
        return None
    host = (urlparse(value).hostname or '').lower()
    if not any(host == name or host.endswith('.' + name) for name in _LINKEDIN_HOSTS):
        raise HackathonError('Share the link of your LinkedIn post (linkedin.com).', 'INVALID', field='linkedin_url')
    return value


def _transaction_id(data: dict) -> str:
    value = ' '.join(str(data.get('transaction_id') or '').split())
    if len(value) > MAX_TRANSACTION_ID:
        raise HackathonError(f'The transaction ID is limited to {MAX_TRANSACTION_ID} characters.', 'INVALID', field='transaction_id')
    return value


def _clean(data: dict, hackathon: Hackathon | None = None, *, by_organizer: bool = False) -> dict:
    data = data or {}
    stack = data.get('tech_stack') or []
    if not isinstance(stack, list) or len(stack) > MAX_TECH_STACK:
        raise HackathonError(f'Tech stack takes at most {MAX_TECH_STACK} items.', 'INVALID', field='tech_stack')

    def long_text(key: str, label: str, *, required: bool) -> str:
        value = str(data.get(key) or '').strip()
        if required and not value:
            raise HackathonError(f'{label} is required.', 'INVALID', field=key)
        if len(value) > MAX_TEXT:
            raise HackathonError(f'{label} is limited to {MAX_TEXT} characters.', 'INVALID', field=key)
        return value

    return {
        'title': field_guard('title', lambda: validate._text(data.get('title'), 'Idea title', MAX_TITLE, required=True)),
        'problem': long_text('problem', 'The problem', required=True),
        'solution': long_text('solution', 'Your approach', required=True),
        'plan': long_text('plan', 'The plan', required=False),
        'tech_stack': [field_guard('tech_stack', lambda item=item: validate._text(item, 'Tech', 40)) for item in stack if str(item).strip()],
        'deck_url': field_guard('deck_url', lambda: validate._url(data.get('deck_url'), 'Slides link')) or None,
        'linkedin_url': _linkedin(data, hackathon, by_organizer),
        'transaction_id': _transaction_id(data),
    }


def _chosen_files(team: Team, hackathon: Hackathon, data: dict, existing: IdeaSubmission | None, by_organizer: bool):
    """The slides and payment screenshot this save should end with, and a check that the required ones are there."""
    data = data or {}

    def pick(kind: str, key: str, current):
        # Left out = keep what is there; null = remove it; an id = use that upload.
        chosen = idea_files.resolve(team, kind, data[key], current) if key in data else current
        return chosen

    deck = pick(IdeaFileKind.DECK, 'deck_file_id', existing.deck_file if existing else None)
    payment = pick(IdeaFileKind.PAYMENT, 'payment_file_id', existing.payment_proof if existing else None)
    if not by_organizer:
        if hackathon.idea_deck_required and deck is None:
            raise HackathonError('Upload your slides (.ppt or .pptx).', 'INVALID', field='deck_file_id')
        if hackathon.idea_fee_amount and payment is None:
            raise HackathonError(f'Upload the screenshot of your payment of \u20b9{hackathon.idea_fee_amount}.', 'INVALID', field='payment_file_id')
    return deck, payment


class IdeaService:

    @staticmethod
    @transaction.atomic
    def save(team: Team, actor, data: dict) -> IdeaSubmission:
        hackathon = team.hackathon
        by_organizer = access.has_permission(hackathon, actor, 'ideas')
        existing = IdeaSubmission.objects.select_for_update().filter(team=team).first()
        if not by_organizer:
            ensure_can_edit(team, actor)
        cleaned = _clean(data, hackathon, by_organizer=by_organizer)
        deck, payment = _chosen_files(team, hackathon, data, existing, by_organizer)
        status = IdeaStatus.SUBMITTED if hackathon.idea_review_required else IdeaStatus.APPROVED
        idea, created = IdeaSubmission.objects.update_or_create(
            team=team, defaults={**cleaned, 'submitted_by': actor, 'status': status, 'deck_file': deck, 'payment_proof': payment},
        )
        idea_files.attach(deck, existing.deck_file if existing else None)
        idea_files.attach(payment, existing.payment_proof if existing else None)
        log_audit_event(
            actor=actor, action='Submitted Hackathon Idea' if created else 'Updated Hackathon Idea',
            target_model='IdeaSubmission', target_id=idea.pk, details={'hackathon': hackathon.slug, 'team': team.name, 'title': idea.title},
        )
        if created:
            transaction.on_commit(lambda: _notify_users(
                [m.user for m in team.memberships.select_related('user')], f'{team.name}: idea received',
                f"'{idea.title}' was submitted for {hackathon.title}."
                + (' The organizers will review it.' if hackathon.idea_review_required else ''),
                hackathon, actor,
            ))
        return idea

    @staticmethod
    @transaction.atomic
    def review(idea: IdeaSubmission, reviewer, outcome: str, feedback: str = '') -> IdeaSubmission:
        hackathon = idea.team.hackathon
        if not access.has_permission(hackathon, reviewer, 'ideas'):
            raise PermissionDenied('You are not allowed to review ideas for this hackathon.')
        if outcome not in REVIEW_OUTCOMES:
            raise HackathonError('Choose approve, request changes or reject.', 'INVALID', field='status')
        feedback = ' '.join(str(feedback or '').split())
        if len(feedback) > MAX_FEEDBACK:
            raise HackathonError(f'Feedback is limited to {MAX_FEEDBACK} characters.', 'INVALID', field='feedback')
        if outcome != IdeaStatus.APPROVED and not feedback:
            raise HackathonError('Tell the team why, so they know what to do next.', 'FEEDBACK_REQUIRED', field='feedback')
        idea.status, idea.feedback = outcome, feedback
        idea.reviewed_by, idea.reviewed_at = reviewer, timezone.now()
        idea.save(update_fields=['status', 'feedback', 'reviewed_by', 'reviewed_at', 'updated_at'])
        log_audit_event(
            actor=reviewer, action='Reviewed Hackathon Idea', target_model='IdeaSubmission', target_id=idea.pk,
            details={'hackathon': hackathon.slug, 'team': idea.team.name, 'outcome': outcome},
        )
        wording = {
            IdeaStatus.APPROVED: 'was approved. You can move on to the project phase.',
            IdeaStatus.CHANGES_REQUESTED: 'needs changes. Update it and resubmit before the deadline.',
            IdeaStatus.REJECTED: 'was not accepted.',
        }[outcome]
        transaction.on_commit(lambda: _notify_users(
            [m.user for m in idea.team.memberships.select_related('user')], f'{idea.team.name}: idea reviewed',
            f"Your idea '{idea.title}' {wording}" + (f' Note: {feedback}' if feedback else ''), hackathon, reviewer,
        ))
        return idea

    @staticmethod
    def teams_without_idea(hackathon: Hackathon):
        return hackathon.teams.filter(status=TeamStatus.REGISTERED, is_waitlisted=False, idea__isnull=True)

    @staticmethod
    def remind_missing(hackathon: Hackathon, actor) -> int:
        if window_state(hackathon) == 'CLOSED':
            raise HackathonError('The idea deadline has already passed.', 'IDEAS_CLOSED')
        teams = list(IdeaService.teams_without_idea(hackathon).prefetch_related('memberships__user'))
        when = f" Deadline: {timezone.localtime(hackathon.idea_deadline).strftime('%d %b %Y, %I:%M %p')}." if hackathon.idea_deadline else ''
        for team in teams:
            _notify_users([m.user for m in team.memberships.all()], f'{team.name}: submit your idea',
                          f'Your team has not submitted an idea for {hackathon.title} yet.{when}', hackathon, actor)
        log_audit_event(actor=actor, action='Reminded Teams To Submit Ideas', target_model='Hackathon', target_id=hackathon.slug, details={'teams': len(teams)})
        return len(teams)
