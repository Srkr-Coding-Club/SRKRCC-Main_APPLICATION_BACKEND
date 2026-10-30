"""Project submissions: the window, validation, who may submit, and reminders to teams that haven't."""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from apps.audit.utils import log_audit_event

from . import access, celebrate, config_validation as validate
from .link_rules import clean_link
from .config_validation import guard_field as field_guard
from .ideas import project_gate
from .errors import HackathonError
from .models import Hackathon, Submission, Team, TeamStatus
from .services import _notify_users, is_leader

MAX_TITLE = 200
MIN_TITLE = 3
MAX_DESCRIPTION = 5000
MIN_DESCRIPTION = 30   # a sentence or two: enough for a judge to know what was built
MAX_TECH_STACK = 10


def window_state(hackathon: Hackathon) -> str:
    """OFF, NOT_OPEN, OPEN or CLOSED."""
    if not hackathon.feature('submissions'):
        return 'OFF'
    now = timezone.now()
    if hackathon.submission_opens_at and now < hackathon.submission_opens_at:
        return 'NOT_OPEN'
    if hackathon.submission_deadline and now >= hackathon.submission_deadline:
        return 'CLOSED'
    return 'OPEN'


def _clean_stack(stack: list) -> list[str]:
    """Trimmed technology names, empty ones dropped, repeats removed (case-insensitive) keeping the first spelling."""
    seen, cleaned = set(), []
    for item in stack:
        if not str(item).strip():
            continue
        name = field_guard('tech_stack', lambda item=item: validate._text(item, 'Tech', 40))
        if name.lower() not in seen:
            seen.add(name.lower())
            cleaned.append(name)
    return cleaned


def _clean(data: dict) -> dict:
    data = data or {}
    stack = data.get('tech_stack') or []
    if not isinstance(stack, list) or len(stack) > MAX_TECH_STACK:
        raise HackathonError(f'Tech stack takes at most {MAX_TECH_STACK} items.', 'INVALID', field='tech_stack')
    description = str(data.get('description') or '').strip()
    if not description:
        raise HackathonError('Describe what you built.', 'INVALID', field='description')
    if len(description) < MIN_DESCRIPTION:
        raise HackathonError(f'Say a little more about what you built (at least {MIN_DESCRIPTION} characters).', 'INVALID', field='description')
    if len(description) > MAX_DESCRIPTION:
        raise HackathonError(f'The description is limited to {MAX_DESCRIPTION} characters.', 'INVALID', field='description')
    title = field_guard('project_title', lambda: validate._text(data.get('project_title'), 'Project title', MAX_TITLE, required=True))
    if len(title) < MIN_TITLE:
        raise HackathonError(f'The project title needs at least {MIN_TITLE} characters.', 'INVALID', field='project_title')

    return {
        'project_title': title,
        'tagline': field_guard('tagline', lambda: validate._text(data.get('tagline'), 'Tagline', 200)),
        'description': description,
        'repo_url': field_guard('repo_url', lambda: clean_link(data.get('repo_url'), 'Repository link', kind='repo', required=True)),
        'demo_url': field_guard('demo_url', lambda: clean_link(data.get('demo_url'), 'Demo link')) or None,
        'video_url': field_guard('video_url', lambda: clean_link(data.get('video_url'), 'Video link', kind='video')) or None,
        'slides_url': field_guard('slides_url', lambda: clean_link(data.get('slides_url'), 'Slides link')) or None,
        'tech_stack': _clean_stack(stack),
        'is_public': bool(data.get('is_public', True)),
    }


class SubmissionService:

    @staticmethod
    @transaction.atomic
    def save(team: Team, actor, data: dict) -> Submission:
        hackathon = team.hackathon
        by_admin = access.has_permission(hackathon, actor, 'submissions')
        if not by_admin:
            state = window_state(hackathon)
            if state == 'OFF':
                raise HackathonError('Submissions are not enabled for this hackathon.', 'FEATURE_OFF')
            if not is_leader(team, actor):
                raise PermissionDenied('Only the team leader can submit for this team.')
            if team.status != TeamStatus.REGISTERED or team.is_waitlisted:
                raise HackathonError('Only registered teams can submit a project.', 'TEAM_NOT_REGISTERED')
            if state == 'NOT_OPEN':
                raise HackathonError('Submissions have not opened yet.', 'SUBMISSIONS_NOT_OPEN')
            if state == 'CLOSED':
                raise HackathonError('The submission deadline has passed.', 'SUBMISSIONS_CLOSED')
            gate = project_gate(hackathon, team)
            if not gate['satisfied']:
                raise HackathonError(gate['reason'], 'IDEA_REQUIRED')
        cleaned = _clean(data)
        submission, created = Submission.objects.update_or_create(
            team=team, defaults={**cleaned, 'submitted_by': actor},
        )
        log_audit_event(
            actor=actor, action='Submitted Hackathon Project' if created else 'Updated Hackathon Submission',
            target_model='Submission', target_id=submission.pk,
            details={'hackathon': hackathon.slug, 'team': team.name, 'title': submission.project_title},
        )
        if created:
            celebrate.project_submitted(team, submission)
            transaction.on_commit(lambda: _notify_users(
                [m.user for m in team.memberships.select_related('user')],
                f'{team.name}: submission received',
                f"'{submission.project_title}' was submitted for {hackathon.title}. You can edit it until the deadline.",
                hackathon, actor,
            ))
        return submission

    @staticmethod
    def teams_without_submission(hackathon: Hackathon):
        return hackathon.teams.filter(status=TeamStatus.REGISTERED, is_waitlisted=False, submission__isnull=True)

    @staticmethod
    def remind_missing(hackathon: Hackathon, actor) -> int:
        """Nudge every registered team that has not submitted. Returns how many teams were reminded."""
        if window_state(hackathon) == 'CLOSED':
            raise HackathonError('The deadline has already passed.', 'SUBMISSIONS_CLOSED')
        teams = list(SubmissionService.teams_without_submission(hackathon).prefetch_related('memberships__user'))
        deadline = hackathon.submission_deadline
        when = f" Deadline: {timezone.localtime(deadline).strftime('%d %b %Y, %I:%M %p')}." if deadline else ''
        for team in teams:
            _notify_users(
                [m.user for m in team.memberships.all()], f'{team.name}: submit your project',
                f"Your team has not submitted a project for {hackathon.title} yet.{when}", hackathon, actor,
            )
        log_audit_event(
            actor=actor, action='Reminded Teams To Submit', target_model='Hackathon', target_id=hackathon.slug,
            details={'teams': len(teams)},
        )
        return len(teams)
