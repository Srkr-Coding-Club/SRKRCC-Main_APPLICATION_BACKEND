"""
Phase 1 of a two-phase submission flow: the idea (plan). Teams describe the problem, approach and plan;
organizers review it; the admin may require an approved idea before the project phase (implementation) opens.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from apps.audit.utils import log_audit_event

from . import access, config_validation as validate
from .errors import HackathonError
from .models import Hackathon, IdeaStatus, IdeaSubmission, Team, TeamStatus
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


def _clean(data: dict) -> dict:
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
    }


class IdeaService:

    @staticmethod
    @transaction.atomic
    def save(team: Team, actor, data: dict) -> IdeaSubmission:
        hackathon = team.hackathon
        by_organizer = access.has_permission(hackathon, actor, 'ideas')
        existing = IdeaSubmission.objects.select_for_update().filter(team=team).first()
        if not by_organizer:
            state = window_state(hackathon)
            if state == 'OFF':
                raise HackathonError('Idea submission is not enabled for this hackathon.', 'FEATURE_OFF')
            if not is_leader(team, actor):
                raise PermissionDenied('Only the team leader can submit the idea.')
            if team.status != TeamStatus.REGISTERED or team.is_waitlisted:
                raise HackathonError('Only registered teams can submit an idea.', 'TEAM_NOT_REGISTERED')
            if state == 'NOT_OPEN':
                raise HackathonError('Idea submission has not opened yet.', 'IDEAS_NOT_OPEN')
            if state == 'CLOSED':
                raise HackathonError('The idea deadline has passed.', 'IDEAS_CLOSED')
            if existing and existing.status in LOCKED_STATUSES:
                raise HackathonError(
                    'This idea has already been decided, so it can no longer be edited. Contact the organizers if something is wrong.',
                    'IDEA_LOCKED',
                )
        cleaned = _clean(data)
        status = IdeaStatus.SUBMITTED if hackathon.idea_review_required else IdeaStatus.APPROVED
        idea, created = IdeaSubmission.objects.update_or_create(
            team=team, defaults={**cleaned, 'submitted_by': actor, 'status': status},
        )
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
