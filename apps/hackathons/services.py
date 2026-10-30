"""
Business rules for hackathon team formation, rounds and announcements.

Every state change goes through here (never through a raw serializer save) so
the rules - registration window, team size, one team per user per hackathon,
leader-only edits, problem-statement capacity - live in one place and are
enforced identically for every endpoint. Mutations run inside
``transaction.atomic`` with the Team row locked, so two concurrent invite
accepts can't both squeeze past the size limit.
"""
from __future__ import annotations

import csv
import io
import logging
import re

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from apps.audit.utils import log_audit_event
from apps.core.permissions import _is_admin_or_club_lead

from . import access, celebrate, payments, registration, round_rules, team_emails, withdrawals
from .errors import HackathonError
from .models import (
    AnnouncementAudience, EntryStatus, Hackathon, HackathonAnnouncement, InviteStatus,
    EmailInviteStatus, HackathonStage, ProblemStatement, Round, RoundEntry, Team, TeamEmailInvite, TeamInvite, TeamMember, TeamRole,
    TeamStatus, WithdrawalRequest,
)

User = get_user_model()
logger = logging.getLogger(__name__)

ACTIVE_TEAM_STATUSES = (TeamStatus.FORMING, TeamStatus.REGISTERED)
ROUND_SCOPED_AUDIENCES = (
    AnnouncementAudience.ROUND_ALL, AnnouncementAudience.ROUND_SHORTLISTED, AnnouncementAudience.ROUND_REJECTED,
)




# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def missing_profile_fields(hackathon: Hackathon, user) -> list[str]:
    """Labels of ``hackathon.required_profile_fields`` the user hasn't filled in."""
    from apps.forms.models import ProfileField
    from apps.forms.serializers import PROFILE_FIELD_GETTERS

    labels = dict(ProfileField.choices)
    missing = []
    for key in hackathon.required_profile_fields or []:
        getter = PROFILE_FIELD_GETTERS.get(key)
        value = getter(user) if getter else None
        if value in (None, ''):
            missing.append(labels.get(key, key))
    return missing


def membership_of(hackathon: Hackathon, user) -> TeamMember | None:
    if not (user and user.is_authenticated):
        return None
    return (
        TeamMember.objects.select_related('team')
        .filter(hackathon=hackathon, user=user)
        .first()
    )


def is_leader(team: Team, user) -> bool:
    return bool(user and user.is_authenticated and team.leader_id == user.id)


def is_member(team: Team, user) -> bool:
    return bool(user and user.is_authenticated and team.memberships.filter(user=user).exists())


def _require_registration_open(hackathon: Hackathon):
    if not hackathon.is_registration_open:
        raise HackathonError('Team registration is not open for this hackathon.', 'REGISTRATION_CLOSED')


def _is_team_organizer(hackathon: Hackathon, actor) -> bool:
    """Site-wide organizers, or staff of this hackathon allowed to manage teams."""
    return access.has_permission(hackathon, actor, 'teams')


def _require_team_editable(team: Team, actor):
    """Participant-side edits need open registration and an unlocked hackathon; organizers bypass both."""
    if _is_team_organizer(team.hackathon, actor):
        return
    if team.status in (TeamStatus.DISQUALIFIED, TeamStatus.WITHDRAWN):
        raise HackathonError('This team can no longer be changed.', 'TEAM_INACTIVE')
    if team.status in payments.PAYMENT_STATUSES:
        raise HackathonError('This team is waiting for its payment to be verified, so it cannot be changed yet.', 'TEAM_UNPAID')
    if team.hackathon.team_edits_locked:
        raise HackathonError('Team changes are locked for this hackathon.', 'TEAM_EDITS_LOCKED')
    _require_registration_open(team.hackathon)


def _require_leader(team: Team, actor):
    if not (is_leader(team, actor) or _is_team_organizer(team.hackathon, actor)):
        raise PermissionDenied('Only the team leader can do this.')


def _require_profile_complete(hackathon: Hackathon, user, *, who='your'):
    missing = missing_profile_fields(hackathon, user)
    if missing:
        raise HackathonError(
            f"Complete {who} profile first. Missing: {', '.join(missing)}.",
            'PROFILE_INCOMPLETE',
        )


MAX_PROBLEM_TITLE = 255
MAX_PROBLEM_DOMAIN = 100
MAX_PROBLEM_DESCRIPTION = 5000


def _require_problems_revealed(hackathon: Hackathon, ps: ProblemStatement | None):
    if ps is not None and hackathon.problem_statements_reveal_at and hackathon.problem_statements_reveal_at > timezone.now():
        raise HackathonError('Problem statements have not been revealed yet.', 'PROBLEMS_NOT_REVEALED', field='problem_statement')


def _check_problem_statement(hackathon: Hackathon, ps: ProblemStatement | None, *, exclude_team: Team | None = None):
    # A team keeps the statement it already holds even after an admin deactivates it,
    # so it can still be renamed; only picking a new statement needs an active one.
    if exclude_team is not None and exclude_team.problem_statement_id == ps.id:
        return
    if ps.hackathon_id != hackathon.id or not ps.is_active:
        raise HackathonError('That problem statement is not available.', 'PROBLEM_STATEMENT_INVALID', field='problem_statement')
    if ps.max_teams:
        taken = ps.teams.filter(status__in=ACTIVE_TEAM_STATUSES)
        if exclude_team is not None:
            taken = taken.exclude(pk=exclude_team.pk)
        if taken.count() >= ps.max_teams:
            raise HackathonError('That problem statement is full. Pick another.', 'PROBLEM_STATEMENT_FULL', field='problem_statement')


def _clean_open_innovation(data: dict) -> dict:
    """Title, description and domain are all mandatory for an open-innovation problem."""
    limits = {
        'title': ('custom_problem_title', MAX_PROBLEM_TITLE, 'Problem title'),
        'description': ('custom_problem_description', MAX_PROBLEM_DESCRIPTION, 'Problem description'),
        'domain': ('custom_problem_domain', MAX_PROBLEM_DOMAIN, 'Problem domain'),
    }
    cleaned = {}
    for key, (field, limit, label) in limits.items():
        value = str((data or {}).get(key) or '').strip()
        if not value:
            raise HackathonError(f'{label} is required for open innovation.', 'OPEN_INNOVATION_INCOMPLETE', field=field)
        if len(value) > limit:
            raise HackathonError(f'{label} is too long (max {limit} characters).', 'OPEN_INNOVATION_TOO_LONG', field=field)
        cleaned[field] = value
    return cleaned


def _resolve_problem(hackathon: Hackathon, problem_statement, open_innovation, *, exclude_team: Team | None = None) -> dict:
    """Turn the team's problem choice into Team field values, enforcing the rules.

    A team either picks one admin-defined statement or goes open innovation with
    its own title/description/domain - never both. With neither, a problem is only
    mandatory once the hackathon actually has statements to pick from.
    """
    if problem_statement is not None and open_innovation:
        raise HackathonError(
            'Pick a problem statement or go open innovation, not both.', 'PROBLEM_CHOICE_CONFLICT', field='problem_statement',
        )
    blank_custom = {'custom_problem_title': '', 'custom_problem_description': '', 'custom_problem_domain': ''}

    if open_innovation:
        if not hackathon.allow_open_innovation:
            raise HackathonError('Open innovation is not enabled for this hackathon.', 'OPEN_INNOVATION_DISABLED', field='open_innovation')
        return {'problem_statement': None, 'is_open_innovation': True, **_clean_open_innovation(open_innovation)}

    if problem_statement is not None:
        ProblemStatement.objects.select_for_update().filter(pk=problem_statement.pk).first()
        _check_problem_statement(hackathon, problem_statement, exclude_team=exclude_team)
        return {'problem_statement': problem_statement, 'is_open_innovation': False, **blank_custom}

    if hackathon.problem_statements.filter(is_active=True).exists():
        message = 'Pick a problem statement' + (' or go open innovation with your own.' if hackathon.allow_open_innovation else '.')
        raise HackathonError(message, 'PROBLEM_STATEMENT_REQUIRED', field='problem_statement')
    return {'problem_statement': None, 'is_open_innovation': False, **blank_custom}


def problem_label(team: Team) -> str | None:
    """The ID a team's problem is known by: PS-001 for a statement, OI-<team id> for open innovation."""
    if team.problem_statement_id:
        return team.problem_statement.code
    return team.open_innovation_code if team.is_open_innovation else None


def _lock_team(team: Team) -> Team:
    return Team.objects.select_for_update().select_related('hackathon').get(pk=team.pk)


def _recompute_status(team: Team) -> bool:
    """
    FORMING ↔ REGISTERED tracks whether the team currently meets min_team_size.
    Returns True when the team just became registered (everyone is emailed once).
    """
    if team.status not in ACTIVE_TEAM_STATUSES:
        return False
    count = team.memberships.count()
    new_status = TeamStatus.REGISTERED if count >= team.hackathon.min_team_size else TeamStatus.FORMING
    if new_status == team.status:
        return False
    team.status = new_status
    team.save(update_fields=['status', 'updated_at'])
    if new_status == TeamStatus.REGISTERED and not team.is_waitlisted:
        team_emails.notify_team('team_registered', team)
        celebrate.team_registered(team)
        return True
    return False


def _notify_member_joined(team: Team, user, newly_registered: bool) -> None:
    """Tell the team someone joined, unless the 'team registered' email already covers it."""
    celebrate.joined(team, user)
    if newly_registered and team_emails.event_settings(team.hackathon, 'team_registered')[0]:
        return
    team_emails.notify_team('member_joined', team, user)


def _run_in_background(fn):
    from apps.core.tasks import run_in_background
    run_in_background(fn)


def send_invite_email(invite_id: int) -> bool:
    """Tell the invitee they've been invited. Looked up by id - runs on a background thread."""
    from django.conf import settings
    from django.utils.html import escape
    from apps.core.services.email_service import EmailNotificationService

    invite = (
        TeamInvite.objects.select_related('team', 'hackathon', 'invited_user', 'invited_by')
        .filter(pk=invite_id, status=InviteStatus.PENDING).first()
    )
    if invite is None:
        return False
    user = invite.invited_user
    first_name = user.first_name or user.email.split('@')[0]
    inviter = (invite.invited_by.get_full_name() or invite.invited_by.email) if invite.invited_by else 'A team leader'
    url = f"{getattr(settings, 'FRONTEND_URL', 'http://localhost:3000').rstrip('/')}/hackathons/{invite.hackathon.slug}/dashboard"
    subject = f'[{invite.hackathon.title}] You are invited to join team {invite.team.name}'
    text = (
        f'Hi {first_name},\n\n{inviter} invited you to join the team "{invite.team.name}" '
        f'for {invite.hackathon.title}.\n\nAccept or decline the invite from your team dashboard:\n{url}\n'
    )
    html = (
        f'<p>Hi {escape(first_name)},</p>'
        f'<p>{escape(inviter)} invited you to join the team <strong>{escape(invite.team.name)}</strong> '
        f'for {escape(invite.hackathon.title)}.</p>'
        f'<p><a href="{escape(url)}">Accept or decline the invite on your team dashboard</a></p>'
    )
    return EmailNotificationService.send_email(user.email, subject, text, html)


def _notify_users(users, title: str, message: str, hackathon: Hackathon, actor=None, *, action_kind: str = '', action_ref: str = '') -> None:
    """In-app notification that can never fail its caller (own savepoint, errors logged)."""
    users = [u for u in users if u is not None]
    if not users:
        return
    try:
        from apps.notifications.models import NotificationCategory, NotificationType
        from apps.notifications.services import NotificationService

        with transaction.atomic():
            NotificationService.create_bulk_notifications(
                users, title=title, message=message, type=NotificationType.INFO,
                category=NotificationCategory.HACKATHON, link_url=f'/hackathons/{hackathon.slug}/dashboard',
                created_by=actor, action_kind=action_kind, action_ref=action_ref,
            )
    except Exception:
        logger.exception('Could not create hackathon notification %r', title)


def _notify_after_commit(users, title: str, message: str, hackathon: Hackathon, actor=None) -> None:
    users = list(users)
    transaction.on_commit(lambda: _notify_users(users, title, message, hackathon, actor))


def _slots_used(team: Team) -> int:
    """Members plus every outstanding invite (account or email) - what max_team_size is checked against."""
    return (
        team.memberships.count()
        + team.invites.filter(status=InviteStatus.PENDING).count()
        + team.email_invites.filter(status=EmailInviteStatus.PENDING).count()
    )


def send_email_invite(invite_id: int) -> bool:
    """Tell an address with no account that a team wants them. Looked up by id - runs on a background thread."""
    from django.conf import settings
    from django.utils.html import escape
    from apps.core.services.email_service import EmailNotificationService

    invite = (
        TeamEmailInvite.objects.select_related('team', 'hackathon', 'invited_by')
        .filter(pk=invite_id, status=EmailInviteStatus.PENDING).first()
    )
    if invite is None:
        return False
    base = getattr(settings, 'FRONTEND_URL', 'http://localhost:3000').rstrip('/')
    url = f"{base}/signup?next=/hackathons/{invite.hackathon.slug}/dashboard"
    inviter = (invite.invited_by.get_full_name() or invite.invited_by.email) if invite.invited_by else 'A team leader'
    subject = f'[{invite.hackathon.title}] {inviter} invited you to join team {invite.team.name}'
    text = (
        f'{inviter} invited you to join the team "{invite.team.name}" for {invite.hackathon.title}.\n\n'
        f'Create your SRKR Coding Club account with this email address and the invite will be waiting on your team dashboard:\n{url}\n'
    )
    html = (
        f'<p>{escape(inviter)} invited you to join the team <strong>{escape(invite.team.name)}</strong> '
        f'for {escape(invite.hackathon.title)}.</p>'
        f'<p><a href="{escape(url)}">Create your account with this email address</a> and the invite will be waiting on your team dashboard.</p>'
    )
    return EmailNotificationService.send_email(invite.email, subject, text, html)


def send_forming_reminder_email(team_id: int) -> bool:
    from django.conf import settings
    from django.utils.html import escape
    from apps.core.services.email_service import EmailNotificationService

    team = Team.objects.select_related('hackathon', 'leader').filter(pk=team_id, status=TeamStatus.FORMING).first()
    if team is None or team.leader is None:
        return False
    hackathon = team.hackathon
    needed = max(hackathon.min_team_size - team.memberships.count(), 1)
    closes = (
        f" Registration closes {timezone.localtime(hackathon.registration_closes_at).strftime('%d %b %Y, %I:%M %p')}."
        if hackathon.registration_closes_at else ''
    )
    url = f"{getattr(settings, 'FRONTEND_URL', 'http://localhost:3000').rstrip('/')}/hackathons/{hackathon.slug}/dashboard"
    first_name = team.leader.first_name or team.leader.email.split('@')[0]
    subject = f'[{hackathon.title}] Your team needs {needed} more member{"s" if needed != 1 else ""}'
    text = (
        f'Hi {first_name},\n\nYour team "{team.name}" has not reached the minimum team size of {hackathon.min_team_size} yet, '
        f'so it is not registered. Invite {needed} more member{"s" if needed != 1 else ""} from your dashboard.{closes}\n\n{url}\n'
    )
    html = (
        f'<p>Hi {escape(first_name)},</p>'
        f'<p>Your team <strong>{escape(team.name)}</strong> has not reached the minimum team size of {hackathon.min_team_size} yet, '
        f'so it is not registered. Invite {needed} more member{"s" if needed != 1 else ""} from your dashboard.{escape(closes)}</p>'
        f'<p><a href="{escape(url)}">Open your team dashboard</a></p>'
    )
    return EmailNotificationService.send_email(team.leader.email, subject, text, html)


def _problem_differs(team: Team, problem: dict) -> bool:
    if problem['is_open_innovation'] != team.is_open_innovation:
        return True
    if problem['is_open_innovation']:
        return any(problem[f] != getattr(team, f) for f in ('custom_problem_title', 'custom_problem_description', 'custom_problem_domain'))
    return problem['problem_statement'] != team.problem_statement


def _audit(actor, action, team: Team, **details):
    log_audit_event(
        actor=actor, action=action, target_model='HackathonTeam', target_id=team.pk,
        details={'team': team.name, 'hackathon': team.hackathon.slug, **details},
    )


# ---------------------------------------------------------------------------
# Team formation
# ---------------------------------------------------------------------------

def _admit(team: Team, user, registration_data: dict | None) -> bool:
    """
    Put ``user`` on ``team`` after every check that applies to somebody joining (the same whether they accepted an invite
    or the leader accepted their request). Call with the team locked. Returns True when the team just became registered.
    Everything else the person was waiting on (other invites, other join requests) is cancelled.
    """
    hackathon = team.hackathon
    if team.status not in ACTIVE_TEAM_STATUSES:
        raise HackathonError('This team is no longer active.', 'TEAM_INACTIVE')
    if hackathon.team_edits_locked:
        raise HackathonError('Team changes are locked for this hackathon.', 'TEAM_EDITS_LOCKED')
    _require_registration_open(hackathon)
    if TeamMember.objects.filter(hackathon=hackathon, user=user).exists():
        raise HackathonError('You are already in a team for this hackathon.', 'ALREADY_IN_TEAM')
    _require_profile_complete(hackathon, user)
    registration.check_eligibility(hackathon, user)
    accepted_at, answers = registration.registration_inputs(hackathon, registration_data)
    if team.memberships.count() >= hackathon.max_team_size:
        raise HackathonError('This team is already full.', 'TEAM_FULL')
    if not team.is_waitlisted and registration.participants_would_exceed(hackathon):
        raise HackathonError('This hackathon has reached its participant limit.', 'HACKATHON_FULL')

    try:
        with transaction.atomic():
            TeamMember.objects.create(
                team=team, hackathon=hackathon, user=user, role=TeamRole.MEMBER,
                rules_accepted_at=accepted_at, answers=answers,
            )
    except IntegrityError:
        raise HackathonError('You are already in a team for this hackathon.', 'ALREADY_IN_TEAM')

    now = timezone.now()
    TeamInvite.objects.filter(hackathon=hackathon, invited_user=user, status=InviteStatus.PENDING).update(status=InviteStatus.CANCELLED, responded_at=now)
    from .finder import withdraw_pending_requests
    withdraw_pending_requests(hackathon, user, now)
    return _recompute_status(team)


class TeamService:

    @staticmethod
    @transaction.atomic
    def create_team(hackathon: Hackathon, leader, name: str, problem_statement: ProblemStatement | None = None,
                    open_innovation: dict | None = None, actor=None, registration_data: dict | None = None) -> Team:
        name = (name or '').strip()
        if not name:
            raise HackathonError('Team name is required.', 'NAME_REQUIRED', field='name')
        if len(name) > 150:
            raise HackathonError('Team name is too long (max 150 characters).', 'NAME_TOO_LONG', field='name')

        hackathon = Hackathon.objects.select_for_update().get(pk=hackathon.pk)
        by_admin = bool(actor and _is_team_organizer(hackathon, actor))
        accepted_at, answers, waitlisted = None, {}, False
        pay = not by_admin and payments.required(hackathon)
        if pay:
            again = payments.existing_submission(hackathon, leader, registration_data)
            if again is not None:
                return again  # the same payment sent twice (a retry after a dropped connection)
        if not by_admin:
            _require_registration_open(hackathon)
            if hackathon.team_edits_locked:
                raise HackathonError('Team changes are locked for this hackathon, so new teams cannot be created.', 'TEAM_EDITS_LOCKED')
            _require_profile_complete(hackathon, leader)
            registration.check_eligibility(hackathon, leader)
            withdrawals.check_can_create(hackathon, leader)
            accepted_at, answers = registration.registration_inputs(hackathon, registration_data)
        elif registration_data:
            accepted_at, answers = registration.registration_inputs(hackathon, {**registration_data, 'accept_rules': True})
        if TeamMember.objects.filter(hackathon=hackathon, user=leader).exists():
            raise HackathonError('You are already in a team for this hackathon.' if (not actor or actor == leader) else 'That user is already in a team for this hackathon.', 'ALREADY_IN_TEAM')
        if hackathon.teams.filter(name__iexact=name).exists():
            raise HackathonError('That team name is already taken.', 'NAME_TAKEN', field='name')
        problem = _resolve_problem(hackathon, problem_statement, open_innovation)
        if not by_admin:
            _require_problems_revealed(hackathon, problem_statement)
            waitlisted = registration.should_waitlist_new_team(hackathon)
        if pay:
            # A team that has not been paid for holds no place: the seat (or the waitlist) is decided when an organizer verifies it.
            payment_file, reference = payments.clean_submission(hackathon, leader, registration_data)
            waitlisted = False

        try:
            team = Team.objects.create(
                hackathon=hackathon, name=name, leader=leader, status=TeamStatus.PAYMENT_PENDING if pay else TeamStatus.FORMING,
                is_waitlisted=waitlisted, created_by=None if by_admin else leader, **problem,
            )
            TeamMember.objects.create(
                team=team, hackathon=hackathon, user=leader, role=TeamRole.LEADER,
                rules_accepted_at=accepted_at, answers=answers,
            )
        except IntegrityError:
            raise HackathonError('You are already in a team, or that name is taken.', 'CONFLICT')
        if pay:
            payments.attach_payment(team, leader, payment_file, reference)

        # Being on a team means any other pending invites for this hackathon are moot.
        TeamInvite.objects.filter(
            hackathon=hackathon, invited_user=leader, status=InviteStatus.PENDING,
        ).update(status=InviteStatus.CANCELLED, responded_at=timezone.now())

        _recompute_status(team)
        if team.status == TeamStatus.FORMING:
            team_emails.notify_team('team_created', team)
        audit_actor = actor or leader
        _audit(audit_actor, 'Created Hackathon Team', team, problem=problem_label(team), leader=leader.email, waitlisted=waitlisted)
        if pay:
            _notify_after_commit(
                [leader], f'{team.name}: payment received',
                f"We received your payment for '{team.name}'. The organizers will check it and your team is registered once it is verified.", hackathon, actor,
            )
            _notify_after_commit(
                payments.reviewers(hackathon), f'New payment to check: {team.name}',
                f"{leader.first_name or leader.username} paid ₹{hackathon.idea_fee_amount} to register '{team.name}' for {hackathon.title}. Open the Payments tab to verify or reject it.",
                hackathon, leader,
            )
        if waitlisted:
            _notify_after_commit(
                [leader], f"{team.name} is on the waitlist",
                f"{hackathon.title} is full. Your team is queued and will be placed automatically if a spot opens up.",
                hackathon, actor,
            )
        return team

    @staticmethod
    @transaction.atomic
    def update_team(team: Team, actor, *, name=None, change_problem=False,
                    problem_statement: ProblemStatement | None = None, open_innovation: dict | None = None,
                    recruiting: dict | None = None) -> Team:
        team = _lock_team(team)
        _require_leader(team, actor)
        _require_team_editable(team, actor)
        changes = {}
        if recruiting is not None:
            looking = bool(recruiting.get('looking_for_members'))
            note = ' '.join(str(recruiting.get('looking_for') or '').split())[:200]
            if looking and not team.hackathon.feature('team_finder'):
                raise HackathonError('The team finder is not enabled for this hackathon.', 'FEATURE_OFF')
            if (looking, note) != (team.looking_for_members, team.looking_for):
                changes['recruiting'] = [[team.looking_for_members, team.looking_for], [looking, note]]
                team.looking_for_members, team.looking_for = looking, note
        if name is not None:
            name = name.strip()
            if not name:
                raise HackathonError('Team name is required.', 'NAME_REQUIRED', field='name')
            if team.hackathon.teams.filter(name__iexact=name).exclude(pk=team.pk).exists():
                raise HackathonError('That team name is already taken.', 'NAME_TAKEN', field='name')
            if name != team.name:
                changes['name'] = [team.name, name]
                team.name = name
        if change_problem:
            before = problem_label(team)
            if not _is_team_organizer(team.hackathon, actor):
                _require_problems_revealed(team.hackathon, problem_statement)
            problem = _resolve_problem(team.hackathon, problem_statement, open_innovation, exclude_team=team)
            if (
                not _is_team_organizer(team.hackathon, actor)
                and team.round_entries.exists()
                and _problem_differs(team, problem)
            ):
                raise HackathonError(
                    'Your team is already part of a round, so its problem can no longer be changed. Contact the organizers.',
                    'PROBLEM_LOCKED', field='problem_statement',
                )
            for field, value in problem.items():
                setattr(team, field, value)
            after = problem_label(team)
            if before != after or problem['is_open_innovation']:
                changes['problem'] = [before, after]
        if changes:
            team.save()
            _audit(actor, 'Updated Hackathon Team', team, changes=changes)
        return team

    # -- invites ------------------------------------------------------------

    @staticmethod
    def invite_eligibility(team: Team, user) -> tuple[bool, str]:
        """(can_invite, reason) for showing in the email lookup before sending."""
        hackathon = team.hackathon
        if user.pk == team.leader_id or team.memberships.filter(user=user).exists():
            return False, 'Already in your team.'
        if TeamMember.objects.filter(hackathon=hackathon, user=user).exists():
            return False, 'Already in another team for this hackathon.'
        if team.invites.filter(invited_user=user, status=InviteStatus.PENDING).exists():
            return False, 'Already invited.'
        slots = hackathon.max_team_size - _slots_used(team)
        if slots <= 0:
            return False, f'Team is full (max {hackathon.max_team_size} incl. pending invites).'
        return True, ''

    @staticmethod
    @transaction.atomic
    def invite(team: Team, actor, email: str) -> TeamInvite:
        team = _lock_team(team)
        _require_leader(team, actor)
        _require_team_editable(team, actor)
        email = (email or '').strip()
        target = User.objects.filter(email__iexact=email).first() if email else None
        if target is None:
            raise HackathonError('No SRKRCC account uses that email. Ask them to sign up first.', 'USER_NOT_FOUND', field='email')
        ok, reason = TeamService.invite_eligibility(team, target)
        if not ok:
            raise HackathonError(reason, 'CANNOT_INVITE', field='email')
        try:
            invite = TeamInvite.objects.create(
                team=team, hackathon=team.hackathon, invited_user=target, invited_by=actor,
            )
        except IntegrityError:
            raise HackathonError('Already invited.', 'CANNOT_INVITE', field='email')
        _audit(actor, 'Invited Hackathon Team Member', team, invitee=target.email)
        try:
            from apps.notifications.services import NotificationService
            from apps.notifications.models import NotificationType, NotificationCategory
            # Its own savepoint: if this insert fails the invite itself must still commit.
            with transaction.atomic():
                NotificationService.create_notification(
                    recipient=target,
                    title=f"Team Invite: {team.name}",
                    message=f"You have been invited by {actor.first_name or actor.username} to join team '{team.name}' for {team.hackathon.title}.",
                    type=NotificationType.INFO,
                    category=NotificationCategory.HACKATHON,
                    link_url=f"/hackathons/{team.hackathon.slug}/dashboard",
                    created_by=actor,
                )
        except Exception:
            logger.exception('Could not create the in-app notification for team invite %s', invite.pk)
        invite_id = invite.pk
        transaction.on_commit(lambda: _run_in_background(lambda: send_invite_email(invite_id)))
        return invite

    @staticmethod
    @transaction.atomic
    def invite_by_email(team: Team, actor, email: str) -> TeamEmailInvite:
        """Invite someone who has no account yet; the invite is waiting for them once they sign up."""
        from django.core.exceptions import ValidationError as DjangoValidationError
        from django.core.validators import validate_email

        team = _lock_team(team)
        _require_leader(team, actor)
        _require_team_editable(team, actor)
        email = (email or '').strip().lower()
        try:
            validate_email(email)
        except DjangoValidationError:
            raise HackathonError('Enter a valid email address.', 'EMAIL_INVALID', field='email')
        if User.objects.filter(email__iexact=email).exists():
            raise HackathonError(
                'That email already has an account. Search for them and send a normal invite.', 'ACCOUNT_EXISTS', field='email',
            )
        if _slots_used(team) >= team.hackathon.max_team_size:
            raise HackathonError(
                f'Team is full (max {team.hackathon.max_team_size} incl. pending invites).', 'TEAM_FULL', field='email',
            )
        try:
            invite = TeamEmailInvite.objects.create(team=team, hackathon=team.hackathon, email=email, invited_by=actor)
        except IntegrityError:
            raise HackathonError('Already invited.', 'CANNOT_INVITE', field='email')
        _audit(actor, 'Invited Hackathon Team Member By Email', team, invitee=email)
        invite_id = invite.pk
        transaction.on_commit(lambda: _run_in_background(lambda: send_email_invite(invite_id)))
        return invite

    @staticmethod
    @transaction.atomic
    def cancel_email_invite(invite: TeamEmailInvite, actor) -> TeamEmailInvite:
        team = _lock_team(invite.team)
        _require_leader(team, actor)
        invite = TeamEmailInvite.objects.select_for_update().get(pk=invite.pk)
        if invite.status != EmailInviteStatus.PENDING:
            raise HackathonError('This invite is no longer pending.', 'INVITE_NOT_PENDING')
        invite.status = EmailInviteStatus.CANCELLED
        invite.resolved_at = timezone.now()
        invite.save(update_fields=['status', 'resolved_at'])
        _audit(actor, 'Cancelled Hackathon Team Email Invite', team, invitee=invite.email)
        return invite

    @staticmethod
    @transaction.atomic
    def claim_email_invites(user) -> int:
        """Turn every pending email invite for this new account into a normal pending TeamInvite."""
        claimed = 0
        pending = list(
            TeamEmailInvite.objects.select_for_update()
            .filter(email__iexact=user.email, status=EmailInviteStatus.PENDING).select_related('team__hackathon')
        )
        for email_invite in pending:
            team = Team.objects.select_for_update().select_related('hackathon').get(pk=email_invite.team_id)
            hackathon = team.hackathon
            usable = (
                team.status in ACTIVE_TEAM_STATUSES
                and hackathon.is_registration_open
                and not hackathon.team_edits_locked
                and not TeamMember.objects.filter(hackathon=hackathon, user=user).exists()
                # this email invite itself still counts as one slot, so only others are compared
                and _slots_used(team) - 1 < hackathon.max_team_size
            )
            email_invite.status = EmailInviteStatus.CLAIMED if usable else EmailInviteStatus.CANCELLED
            email_invite.resolved_at = timezone.now()
            email_invite.save(update_fields=['status', 'resolved_at'])
            if not usable:
                continue
            TeamInvite.objects.create(team=team, hackathon=hackathon, invited_user=user, invited_by=email_invite.invited_by)
            claimed += 1
            _notify_after_commit(
                [user], f'Team invite: {team.name}',
                f"You were invited to join team '{team.name}' for {hackathon.title}. Open your team dashboard to accept.",
                hackathon, email_invite.invited_by,
            )
        return claimed

    @staticmethod
    @transaction.atomic
    def cancel_invite(invite: TeamInvite, actor) -> TeamInvite:
        team = _lock_team(invite.team)
        _require_leader(team, actor)
        invite = TeamInvite.objects.select_for_update().get(pk=invite.pk)
        if invite.status != InviteStatus.PENDING:
            raise HackathonError('This invite is no longer pending.', 'INVITE_NOT_PENDING')
        invite.status = InviteStatus.CANCELLED
        invite.responded_at = timezone.now()
        invite.save(update_fields=['status', 'responded_at'])
        _audit(actor, 'Cancelled Hackathon Team Invite', team, invitee=invite.invited_user.email)
        return invite

    @staticmethod
    @transaction.atomic
    def accept_invite(invite: TeamInvite, user, registration_data: dict | None = None) -> Team:
        team = _lock_team(invite.team)
        invite = TeamInvite.objects.select_for_update().get(pk=invite.pk)
        if invite.invited_user_id != user.id:
            raise PermissionDenied('This invite is not for you.')
        if invite.status != InviteStatus.PENDING:
            raise HackathonError('This invite is no longer pending.', 'INVITE_NOT_PENDING')
        hackathon = team.hackathon
        newly_registered = _admit(team, user, registration_data)
        now = timezone.now()
        invite.status = InviteStatus.ACCEPTED
        invite.responded_at = now
        invite.save(update_fields=['status', 'responded_at'])
        _audit(user, 'Joined Hackathon Team', team)
        _notify_member_joined(team, user, newly_registered)
        if team.leader_id and team.leader_id != user.id:
            _notify_after_commit(
                [team.leader], f'{user.first_name or user.email} joined {team.name}',
                f"{user.first_name or user.email} accepted your invite and joined team '{team.name}'.",
                hackathon, user,
            )
        return team

    @staticmethod
    @transaction.atomic
    def decline_invite(invite: TeamInvite, user) -> TeamInvite:
        invite = TeamInvite.objects.select_for_update().select_related('team__hackathon').get(pk=invite.pk)
        if invite.invited_user_id != user.id:
            raise PermissionDenied('This invite is not for you.')
        if invite.status != InviteStatus.PENDING:
            raise HackathonError('This invite is no longer pending.', 'INVITE_NOT_PENDING')
        invite.status = InviteStatus.DECLINED
        invite.responded_at = timezone.now()
        invite.save(update_fields=['status', 'responded_at'])
        _audit(user, 'Declined Hackathon Team Invite', invite.team)
        if invite.invited_by_id and invite.invited_by_id != user.id:
            _notify_after_commit(
                [invite.invited_by], f'{user.first_name or user.email} declined your invite',
                f"{user.first_name or user.email} declined the invite to team '{invite.team.name}'.",
                invite.team.hackathon, user,
            )
        return invite

    # -- membership changes -------------------------------------------------

    @staticmethod
    @transaction.atomic
    def remove_member(team: Team, actor, user_id: int) -> Team:
        team = _lock_team(team)
        _require_leader(team, actor)
        _require_team_editable(team, actor)
        membership = team.memberships.select_related('user').filter(user_id=user_id).first()
        if membership is None:
            raise HackathonError('That user is not in this team.', 'NOT_A_MEMBER')
        if membership.user_id == team.leader_id:
            raise HackathonError('Transfer leadership before removing the leader.', 'CANNOT_REMOVE_LEADER')
        email = membership.user.email
        removed_user = membership.user
        membership.delete()
        _recompute_status(team)
        _audit(actor, 'Removed Hackathon Team Member', team, removed=email)
        WaitlistService.promote_after_commit(team.hackathon)
        team_emails.notify_team('member_left', team, removed_user)
        team_emails.notify_team('member_removed', team, removed_user)
        _notify_after_commit(
            [removed_user], f'You were removed from {team.name}',
            f"You are no longer on team '{team.name}' for {team.hackathon.title}. You can join or create another team while registration is open.",
            team.hackathon, actor,
        )
        return team

    @staticmethod
    @transaction.atomic
    def leave_team(team: Team, user, reason: str = '') -> tuple[Team, WithdrawalRequest | None]:
        """
        A member leaves. The sole leader withdraws the team instead, which needs a reason and, for a team that holds a
        place, an organizer's approval: then the second value is the request that was opened.
        """
        team = _lock_team(team)
        membership = team.memberships.filter(user=user).first()
        if membership is None:
            raise HackathonError('You are not in this team.', 'NOT_A_MEMBER')
        _require_team_editable(team, user)
        others = team.memberships.exclude(user=user)
        if team.leader_id == user.id:
            if others.exists():
                raise HackathonError('Transfer leadership to a teammate before leaving.', 'LEADER_MUST_TRANSFER')
            # Sole member leaving: the team is withdrawn rather than deleted, so admins keep a record of it.
            return withdrawals.WithdrawalService.withdraw(team, user, reason)
        membership.delete()
        _recompute_status(team)
        _audit(user, 'Left Hackathon Team', team)
        team_emails.notify_team('member_left', team, user)
        WaitlistService.promote_after_commit(team.hackathon)
        return team, None

    @staticmethod
    @transaction.atomic
    def transfer_leadership(team: Team, actor, user_id: int) -> Team:
        team = _lock_team(team)
        _require_leader(team, actor)
        if not _is_team_organizer(team.hackathon, actor) and team.status not in ACTIVE_TEAM_STATUSES:
            raise HackathonError('This team can no longer be changed.', 'TEAM_INACTIVE')
        new_leader = team.memberships.select_related('user').filter(user_id=user_id).first()
        if new_leader is None:
            raise HackathonError('The new leader must already be a team member.', 'NOT_A_MEMBER')
        if new_leader.user_id == team.leader_id:
            return team
        team.memberships.filter(role=TeamRole.LEADER).update(role=TeamRole.MEMBER)
        new_leader.role = TeamRole.LEADER
        new_leader.save(update_fields=['role'])
        old = team.leader.email if team.leader else None
        team.leader = new_leader.user
        team.save(update_fields=['leader', 'updated_at'])
        _audit(actor, 'Transferred Hackathon Team Leadership', team, old_leader=old, new_leader=new_leader.user.email)
        _notify_after_commit(
            [new_leader.user], f'You now lead {team.name}',
            f"You are the leader of team '{team.name}' for {team.hackathon.title}. You can edit the team and invite members.",
            team.hackathon, actor,
        )
        return team

    @staticmethod
    def forming_teams(hackathon: Hackathon):
        """Teams still below the minimum size, with how many members they have."""
        return (
            hackathon.teams.filter(status=TeamStatus.FORMING).select_related('leader')
            .annotate(member_total=Count('memberships')).order_by('name')
        )

    @staticmethod
    def remind_forming_teams(hackathon: Hackathon, actor) -> int:
        """Nudge the leader of every team below the minimum size (in-app + email). Returns how many were reminded."""
        teams = [team for team in TeamService.forming_teams(hackathon) if team.leader_id]
        for team in teams:
            needed = max(hackathon.min_team_size - team.member_total, 1)
            _notify_users(
                [team.leader], f'{team.name} needs {needed} more member{"s" if needed != 1 else ""}',
                f"Your team '{team.name}' is below the minimum size of {hackathon.min_team_size}, so it is not registered yet. Invite more members from your dashboard.",
                hackathon, actor,
            )
            team_id = team.pk
            _run_in_background(lambda team_id=team_id: send_forming_reminder_email(team_id))
        log_audit_event(
            actor=actor, action='Reminded Forming Hackathon Teams', target_model='Hackathon', target_id=hackathon.slug,
            details={'teams': len(teams)},
        )
        return len(teams)

    @staticmethod
    @transaction.atomic
    def recompute_statuses(hackathon: Hackathon) -> int:
        """Re-evaluate FORMING/REGISTERED for every active team (after min_team_size changed)."""
        changed = 0
        for team in hackathon.teams.filter(status__in=ACTIVE_TEAM_STATUSES).select_related('hackathon'):
            before = team.status
            _recompute_status(team)
            changed += int(team.status != before)
        return changed

    # -- admin overrides ------------------------------------------------------

    @staticmethod
    @transaction.atomic
    def admin_add_member(team: Team, actor, email: str) -> Team:
        team = _lock_team(team)
        user = User.objects.filter(email__iexact=(email or '').strip()).first()
        if user is None:
            raise HackathonError('No account uses that email.', 'USER_NOT_FOUND', field='email')
        if TeamMember.objects.filter(hackathon=team.hackathon, user=user).exists():
            raise HackathonError('That user is already in a team for this hackathon.', 'ALREADY_IN_TEAM', field='email')
        role = TeamRole.MEMBER
        if team.leader_id is None:
            role = TeamRole.LEADER
        TeamMember.objects.create(team=team, hackathon=team.hackathon, user=user, role=role)
        if role == TeamRole.LEADER:
            team.leader = user
            team.save(update_fields=['leader', 'updated_at'])
        TeamInvite.objects.filter(
            hackathon=team.hackathon, invited_user=user, status=InviteStatus.PENDING,
        ).update(status=InviteStatus.CANCELLED, responded_at=timezone.now())
        newly_registered = _recompute_status(team)
        _audit(actor, 'Admin Added Hackathon Team Member', team, added=user.email)
        _notify_member_joined(team, user, newly_registered)
        _notify_after_commit(
            [user], f'You were added to {team.name}',
            f"An organizer added you to team '{team.name}' for {team.hackathon.title}.",
            team.hackathon, actor,
        )
        return team

    @staticmethod
    @transaction.atomic
    def admin_set_status(team: Team, actor, new_status: str, reason: str = '') -> Team:
        team = _lock_team(team)
        if new_status not in TeamStatus.values:
            raise HackathonError('Unknown team status.', 'INVALID_STATUS', field='status')
        if new_status in (TeamStatus.PAYMENT_PENDING, TeamStatus.PAYMENT_REJECTED) or team.status in (TeamStatus.PAYMENT_PENDING, TeamStatus.PAYMENT_REJECTED):
            raise HackathonError('A team waiting on its payment is handled from the Payments page.', 'USE_PAYMENTS', field='status')
        old = team.status
        team.status = new_status
        team.status_reason = ' '.join((reason or '').split())[:300] if new_status in (TeamStatus.DISQUALIFIED, TeamStatus.WITHDRAWN) else ''
        team.save(update_fields=['status', 'status_reason', 'updated_at'])
        if new_status not in ACTIVE_TEAM_STATUSES:
            withdrawals.WithdrawalService.close_pending(team)
            celebrate.team_gone(team)
        if new_status in ACTIVE_TEAM_STATUSES:
            _recompute_status(team)
        _audit(actor, 'Admin Changed Hackathon Team Status', team, old=old, new=team.status)
        if team.status not in ACTIVE_TEAM_STATUSES:
            WaitlistService.promote_after_commit(team.hackathon)
        if team.status != old:
            wording = {
                TeamStatus.DISQUALIFIED: 'was disqualified by the organizers. Contact them if you think this is a mistake.',
                TeamStatus.WITHDRAWN: 'was marked as withdrawn by the organizers.',
                TeamStatus.REGISTERED: 'is registered.',
                TeamStatus.FORMING: 'is still forming: it needs more members to be registered.',
            }.get(team.status, f'is now {team.status.lower()}.')
            if team.status_reason:
                wording = f'{wording} Reason: {team.status_reason}'
            _notify_after_commit(
                [m.user for m in team.memberships.select_related('user')],
                f"{team.name}: status updated", f"Your team '{team.name}' {wording}", team.hackathon, actor,
            )
            team_emails.notify_team('status_changed', team)
        return team


# ---------------------------------------------------------------------------
# Problem statements
# ---------------------------------------------------------------------------

CSV_COLUMN_ALIASES = {
    'title': ('title', 'problem title', 'problem statement', 'name'),
    'description': ('description', 'problem description', 'details', 'summary'),
    'domain': ('domain', 'category', 'track', 'theme'),
}
CSV_MAX_ROWS = 500


class ProblemStatementService:
    """Admin-defined problems. The application, not the admin, assigns every ID."""

    @staticmethod
    def next_code(hackathon: Hackathon) -> str:
        highest = 0
        for code in hackathon.problem_statements.values_list('code', flat=True):
            match = re.fullmatch(r'PS-(\d+)', code or '')
            if match:
                highest = max(highest, int(match.group(1)))
        return f'PS-{highest + 1:03d}'

    @staticmethod
    @transaction.atomic
    def reorder(hackathon: Hackathon, actor, ids: list[int]) -> None:
        """Set the display order to the given id sequence (1, 2, 3, ...) in one transaction."""
        Hackathon.objects.select_for_update().get(pk=hackathon.pk)
        statements = {s.pk: s for s in hackathon.problem_statements.all()}
        if len(ids) != len(set(ids)) or set(ids) != set(statements):
            raise HackathonError('Send every problem statement of this hackathon exactly once.', 'REORDER_MISMATCH', field='ids')
        changed = []
        for position, statement_id in enumerate(ids, start=1):
            statement = statements[statement_id]
            if statement.order != position:
                statement.order = position
                changed.append(statement)
        ProblemStatement.objects.bulk_update(changed, ['order'])
        log_audit_event(
            actor=actor, action='Reordered Problem Statements', target_model='Hackathon', target_id=hackathon.slug,
            details={'moved': len(changed)},
        )

    @staticmethod
    @transaction.atomic
    def create(hackathon: Hackathon, actor, **fields) -> ProblemStatement:
        # Serialise ID generation per hackathon so two admins can't be handed the same ID.
        Hackathon.objects.select_for_update().get(pk=hackathon.pk)
        if not fields.get('order'):
            # New statements go to the end; admins reorder from the list.
            fields['order'] = (hackathon.problem_statements.order_by('-order').values_list('order', flat=True).first() or 0) + 1
        statement = ProblemStatement.objects.create(
            hackathon=hackathon, code=ProblemStatementService.next_code(hackathon), **fields,
        )
        log_audit_event(
            actor=actor, action='Created Problem Statement', target_model='ProblemStatement', target_id=statement.pk,
            details={'hackathon': hackathon.slug, 'code': statement.code, 'title': statement.title},
        )
        return statement

    @staticmethod
    def _map_headers(fieldnames) -> dict:
        mapping = {}
        for raw in fieldnames or []:
            key = (raw or '').strip().lower()
            for canonical, aliases in CSV_COLUMN_ALIASES.items():
                if key in aliases and canonical not in mapping.values():
                    mapping[raw] = canonical
        return mapping

    @staticmethod
    @transaction.atomic
    def import_csv(hackathon: Hackathon, actor, raw: bytes) -> dict:
        """Create statements from a CSV with title, description and domain columns.

        Valid rows are imported and invalid ones reported by row number; a row that
        repeats an existing (title, domain) is skipped rather than duplicated.
        """
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            raise HackathonError('The file must be a UTF-8 encoded CSV.', 'CSV_ENCODING', field='file')
        reader = csv.DictReader(io.StringIO(text))
        mapping = ProblemStatementService._map_headers(reader.fieldnames)
        missing = [c for c in CSV_COLUMN_ALIASES if c not in mapping.values()]
        if missing:
            raise HackathonError(
                f"The CSV needs these columns: {', '.join(CSV_COLUMN_ALIASES)}. Missing: {', '.join(missing)}.",
                'CSV_MISSING_COLUMNS', field='file',
            )

        rows = list(reader)
        if len(rows) > CSV_MAX_ROWS:
            raise HackathonError(f'Too many rows (max {CSV_MAX_ROWS} per upload).', 'CSV_TOO_MANY_ROWS', field='file')

        Hackathon.objects.select_for_update().get(pk=hackathon.pk)
        domains = list(hackathon.problem_domains or [])
        seen = {(t.lower(), d.lower()) for t, d in hackathon.problem_statements.values_list('title', 'domain')}
        next_number = int(ProblemStatementService.next_code(hackathon).split('-')[1])
        next_order = (hackathon.problem_statements.order_by('-order').values_list('order', flat=True).first() or 0) + 1
        created, skipped, errors = [], 0, []

        for line_number, row in enumerate(rows, start=2):
            values = {canonical: (row.get(raw_header) or '').strip() for raw_header, canonical in mapping.items()}
            problem = next((f'{name} is required' for name in CSV_COLUMN_ALIASES if not values.get(name)), None)
            if problem is None and len(values['title']) > MAX_PROBLEM_TITLE:
                problem = f'title is too long (max {MAX_PROBLEM_TITLE} characters)'
            if problem is None and len(values['domain']) > MAX_PROBLEM_DOMAIN:
                problem = f'domain is too long (max {MAX_PROBLEM_DOMAIN} characters)'
            if problem is None and len(values['description']) > MAX_PROBLEM_DESCRIPTION:
                problem = f'description is too long (max {MAX_PROBLEM_DESCRIPTION} characters)'
            if problem:
                errors.append({'row': line_number, 'message': problem})
                continue
            values['domain'] = ' '.join(values['domain'].split())
            known = next((name for name in domains if name.lower() == values['domain'].lower()), None)
            if known is None:
                domains.append(values['domain'])
            else:
                values['domain'] = known
            key = (values['title'].lower(), values['domain'].lower())
            if key in seen:
                skipped += 1
                continue
            seen.add(key)
            created.append(ProblemStatement(
                hackathon=hackathon, code=f'PS-{next_number:03d}', order=next_order,
                title=values['title'], description=values['description'], domain=values['domain'],
            ))
            next_number += 1
            next_order += 1

        ProblemStatement.objects.bulk_create(created)
        # An uploaded CSV introduces its domains: keep the configured list in step with the statements.
        if domains != list(hackathon.problem_domains or []):
            hackathon.problem_domains = domains[:30]
            hackathon.save(update_fields=['problem_domains', 'updated_at'])
        log_audit_event(
            actor=actor, action='Imported Problem Statements', target_model='Hackathon', target_id=hackathon.slug,
            details={'created': len(created), 'skipped_duplicates': skipped, 'errors': len(errors)},
        )
        return {
            'created': len(created), 'skipped': skipped, 'errors': errors,
            'codes': [statement.code for statement in created],
        }


# ---------------------------------------------------------------------------
# Public timeline
# ---------------------------------------------------------------------------

class StageService:
    """Admin-curated entries of the public "Stages and Timelines" section."""

    @staticmethod
    @transaction.atomic
    def create(hackathon: Hackathon, actor, **fields) -> HackathonStage:
        Hackathon.objects.select_for_update().get(pk=hackathon.pk)
        last = hackathon.stages.order_by('-order').values_list('order', flat=True).first() or 0
        stage = HackathonStage.objects.create(hackathon=hackathon, order=last + 1, **fields)
        log_audit_event(
            actor=actor, action='Created Hackathon Stage', target_model='HackathonStage', target_id=stage.pk,
            details={'hackathon': hackathon.slug, 'title': stage.title},
        )
        return stage

    @staticmethod
    @transaction.atomic
    def reorder(hackathon: Hackathon, actor, ids: list[int]) -> None:
        Hackathon.objects.select_for_update().get(pk=hackathon.pk)
        stages = {stage.pk: stage for stage in hackathon.stages.all()}
        if len(ids) != len(set(ids)) or set(ids) != set(stages):
            raise HackathonError('Send every stage of this hackathon exactly once.', 'REORDER_MISMATCH', field='ids')
        changed = []
        for position, stage_id in enumerate(ids, start=1):
            if stages[stage_id].order != position:
                stages[stage_id].order = position
                changed.append(stages[stage_id])
        HackathonStage.objects.bulk_update(changed, ['order'])
        log_audit_event(
            actor=actor, action='Reordered Hackathon Stages', target_model='Hackathon', target_id=hackathon.slug,
            details={'moved': len(changed)},
        )

    @staticmethod
    @transaction.atomic
    def import_rounds(hackathon: Hackathon, actor) -> int:
        """Copy every round that has no stage yet into the timeline (name, description, dates)."""
        Hackathon.objects.select_for_update().get(pk=hackathon.pk)
        already = set(hackathon.stages.exclude(source_round__isnull=True).values_list('source_round_id', flat=True))
        order = hackathon.stages.order_by('-order').values_list('order', flat=True).first() or 0
        created = []
        for round_obj in hackathon.rounds.exclude(pk__in=already):
            order += 1
            created.append(HackathonStage(
                hackathon=hackathon, title=round_obj.name, description=round_obj.description,
                starts_at=round_obj.starts_at, ends_at=round_obj.ends_at, order=order, source_round=round_obj,
            ))
        HackathonStage.objects.bulk_create(created)
        log_audit_event(
            actor=actor, action='Imported Rounds Into Timeline', target_model='Hackathon', target_id=hackathon.slug,
            details={'created': len(created)},
        )
        return len(created)


# ---------------------------------------------------------------------------
# Rounds & shortlisting
# ---------------------------------------------------------------------------

def effective_round_status(round_obj: Round) -> str:
    """What the round's dates say it is right now; the stored status only matters when no dates are set."""
    from .models import RoundStatus

    now = timezone.now()
    if round_obj.starts_at and now < round_obj.starts_at:
        return RoundStatus.UPCOMING
    if round_obj.ends_at and now >= round_obj.ends_at:
        return RoundStatus.COMPLETED
    if round_obj.starts_at:
        return RoundStatus.ACTIVE
    return round_obj.status


class RoundService:

    @staticmethod
    def eligible_teams(round_obj: Round):
        """First round: every registered team. Later rounds: teams shortlisted in the previous round."""
        hackathon = round_obj.hackathon
        previous = hackathon.rounds.filter(order__lt=round_obj.order).order_by('-order').first()
        if previous is None:
            return hackathon.teams.filter(status=TeamStatus.REGISTERED, is_waitlisted=False)
        return hackathon.teams.filter(
            status=TeamStatus.REGISTERED, is_waitlisted=False,
            round_entries__round=previous,
            round_entries__status=EntryStatus.SHORTLISTED,
        )

    @staticmethod
    @transaction.atomic
    def populate_entries(round_obj: Round, actor=None) -> int:
        existing = set(round_obj.entries.values_list('team_id', flat=True))
        new_entries = [
            RoundEntry(round=round_obj, team=team)
            for team in RoundService.eligible_teams(round_obj).distinct()
            if team.pk not in existing
        ]
        RoundEntry.objects.bulk_create(new_entries, ignore_conflicts=True)
        if new_entries:
            log_audit_event(
                actor=actor, action='Populated Hackathon Round', target_model='HackathonRound',
                target_id=round_obj.pk, details={'round': round_obj.name, 'added': len(new_entries)},
            )
        return len(new_entries)

    @staticmethod
    @transaction.atomic
    def create_round(hackathon: Hackathon, actor, *, populate=True, **fields) -> Round:
        if not fields.get('order'):
            last = hackathon.rounds.order_by('-order').values_list('order', flat=True).first() or 0
            fields['order'] = last + 1
        round_obj = Round.objects.create(hackathon=hackathon, **fields)
        if round_obj.details_form_id:
            _prepare_details_form(round_obj.details_form)
        log_audit_event(
            actor=actor, action='Created Hackathon Round', target_model='HackathonRound',
            target_id=round_obj.pk, details={'hackathon': hackathon.slug, 'round': round_obj.name},
        )
        if populate:
            RoundService.populate_entries(round_obj, actor)
        return round_obj

    @staticmethod
    @transaction.atomic
    def delete_round(round_obj: Round, actor) -> None:
        """Delete a round without losing the announcements that were tied to it.

        Announcements aimed at "teams in round X" would otherwise vanish with the
        round (including ones already emailed), so they are converted to the same
        audience expressed as specific teams.
        """
        for announcement in round_obj.announcements.all():
            if announcement.audience == AnnouncementAudience.ROUND_SHORTLISTED:
                entries = round_obj.entries.filter(status=EntryStatus.SHORTLISTED)
            else:
                entries = round_obj.entries.all()
            team_ids = list(entries.values_list('team_id', flat=True))
            announcement.audience = AnnouncementAudience.TEAMS
            announcement.round = None
            announcement.save(update_fields=['audience', 'round', 'updated_at'])
            announcement.target_teams.set(team_ids)
        name, round_id = round_obj.name, round_obj.pk
        hackathon_slug = round_obj.hackathon.slug
        round_obj.delete()
        log_audit_event(
            actor=actor, action='Deleted Hackathon Round', target_model='HackathonRound',
            target_id=round_id, details={'hackathon': hackathon_slug, 'round': name},
        )

    @staticmethod
    @transaction.atomic
    def decide(round_obj: Round, actor, team_ids: list[int], status: str, *, feedback=None, admin_notes=None,
               confirm_published=False, confirm_over_limit=False) -> int:
        if status not in EntryStatus.values:
            raise HackathonError('Unknown entry status.', 'INVALID_STATUS', field='status')
        if status == EntryStatus.SHORTLISTED and round_obj.shortlist_limit and not confirm_over_limit:
            already = round_obj.entries.filter(status=EntryStatus.SHORTLISTED)
            newly = round_obj.entries.filter(team_id__in=team_ids).exclude(status=EntryStatus.SHORTLISTED).count()
            total = already.count() + newly
            if total > round_obj.shortlist_limit:
                raise HackathonError(
                    f'This round is meant to shortlist {round_obj.shortlist_limit} teams and this would make it {total}. Confirm to go over.',
                    'SHORTLIST_LIMIT', field='confirm_over_limit', status=409, limit=round_obj.shortlist_limit, would_be=total,
                )
        if round_obj.results_published and not confirm_published:
            raise HackathonError(
                'Results for this round are already published, so this change is visible to the teams immediately. '
                'Confirm to continue.', 'RESULTS_PUBLISHED', field='confirm_published', status=409,
            )
        entries = round_obj.entries.filter(team_id__in=team_ids)
        changed_team_ids = list(entries.exclude(status=status).values_list('team_id', flat=True))
        update = {'status': status, 'decided_by': actor, 'decided_at': timezone.now()}
        if feedback is not None:
            update['feedback'] = feedback
        if admin_notes is not None:
            update['admin_notes'] = admin_notes
        count = entries.update(**update)
        log_audit_event(
            actor=actor, action='Decided Hackathon Round Entries', target_model='HackathonRound',
            target_id=round_obj.pk, details={
                'round': round_obj.name, 'status': status, 'teams': list(team_ids), 'updated': count,
                'after_publish': round_obj.results_published,
            },
        )
        if round_obj.results_published and changed_team_ids:
            transaction.on_commit(lambda: _notify_result_changed(round_obj, changed_team_ids, actor))
            if status == EntryStatus.SHORTLISTED:
                celebrate.shortlisted(round_obj, changed_team_ids)
            else:
                celebrate.shortlist_withdrawn(round_obj, changed_team_ids)
        return count

    @staticmethod
    @transaction.atomic
    def publish_results(round_obj: Round, actor, *, announce=False, message='', email=False,
                        announce_rejected=False, rejected_message='') -> Round:
        if round_obj.results_published:
            raise HackathonError('Results for this round are already published.', 'ALREADY_PUBLISHED', status=409)
        round_obj.results_published = True
        round_obj.save(update_fields=['results_published', 'updated_at'])
        log_audit_event(
            actor=actor, action='Published Hackathon Round Results', target_model='HackathonRound',
            target_id=round_obj.pk, details={'round': round_obj.name},
        )
        celebrate.shortlisted(round_obj, round_obj.entries.filter(status=EntryStatus.SHORTLISTED).values_list('team_id', flat=True))
        if announce:
            AnnouncementService.create(
                round_obj.hackathon, actor,
                title=f'{round_obj.name}: results are out',
                message=message or (
                    f'Congratulations! Your team has been shortlisted in **{round_obj.name}**. '
                    'Check your team dashboard for next steps.'
                ),
                audience=AnnouncementAudience.ROUND_SHORTLISTED, round=round_obj, type='SUCCESS',
                send_email=email,
            )
        if announce_rejected:
            AnnouncementService.create(
                round_obj.hackathon, actor,
                title=f'{round_obj.name}: results are out',
                message=rejected_message or (
                    f'Thank you for taking part in **{round_obj.name}**. Your team was not shortlisted this time. '
                    'Open your team dashboard to read any feedback from the organizers.'
                ),
                audience=AnnouncementAudience.ROUND_REJECTED, round=round_obj, type='INFO',
                send_email=email,
            )
        return round_obj

    @staticmethod
    @transaction.atomic
    def unpublish_results(round_obj: Round, actor) -> Round:
        round_obj.results_published = False
        round_obj.save(update_fields=['results_published', 'updated_at'])
        log_audit_event(
            actor=actor, action='Unpublished Hackathon Round Results', target_model='HackathonRound',
            target_id=round_obj.pk, details={'round': round_obj.name},
        )
        celebrate.shortlist_withdrawn(round_obj)
        return round_obj

    @staticmethod
    @transaction.atomic
    def auto_shortlist(round_obj: Round, actor, rule: str, value=None, *, reject_rest=False, apply=False,
                       confirm_published=False, confirm_over_limit=False) -> dict:
        """Pick the teams a rule selects. ``apply=False`` only previews; ``apply=True`` records the decisions."""
        rows = round_rules.evaluate(round_obj, rule, value)
        selected = [row for row in rows if row['selected']]
        rest = [row for row in rows if not row['selected']]
        result = {
            'rule': rule, 'value': value, 'selected': selected, 'not_selected': rest, 'applied': False,
            'limit': round_obj.shortlist_limit, 'over_limit': bool(round_obj.shortlist_limit and len(selected) > round_obj.shortlist_limit),
        }
        if not apply:
            return result
        if not selected:
            raise HackathonError('That rule selects no team, so nothing was changed.', 'NOTHING_SELECTED')
        RoundService.decide(
            round_obj, actor, [row['team_id'] for row in selected], EntryStatus.SHORTLISTED,
            confirm_published=confirm_published, confirm_over_limit=confirm_over_limit,
        )
        if reject_rest:
            undecided = [row['team_id'] for row in rest if row['status'] == EntryStatus.PENDING]
            if undecided:
                RoundService.decide(round_obj, actor, undecided, EntryStatus.REJECTED, confirm_published=confirm_published)
        log_audit_event(
            actor=actor, action='Auto-shortlisted Hackathon Round', target_model='HackathonRound', target_id=round_obj.pk,
            details={'round': round_obj.name, 'rule': rule, 'value': value, 'shortlisted': len(selected), 'rejected_rest': bool(reject_rest)},
        )
        result['applied'] = True
        return result

    @staticmethod
    @transaction.atomic
    def reorder(hackathon: Hackathon, actor, ids: list[int]) -> None:
        """Change the order of rounds. Only while nobody has been decided yet: later rounds depend on earlier shortlists."""
        rounds = {r.id: r for r in hackathon.rounds.all()}
        if sorted(ids) != sorted(rounds) or len(set(ids)) != len(ids):
            raise HackathonError('Send every round exactly once.', 'INVALID', field='ids')
        if RoundEntry.objects.filter(round__hackathon=hackathon).exclude(status=EntryStatus.PENDING).exists():
            raise HackathonError('Teams have already been decided, so the order can no longer change.', 'ROUNDS_IN_USE', status=409)
        offset = max(r.order for r in rounds.values()) + len(rounds) + 1
        for round_obj in rounds.values():
            Round.objects.filter(pk=round_obj.pk).update(order=round_obj.order + offset)
        for position, round_id in enumerate(ids, start=1):
            Round.objects.filter(pk=round_id).update(order=position)
        log_audit_event(
            actor=actor, action='Reordered Hackathon Rounds', target_model='Hackathon', target_id=hackathon.slug,
            details={'order': [rounds[i].name for i in ids]},
        )

    @staticmethod
    @transaction.atomic
    def advance_shortlisted(round_obj: Round, actor=None) -> tuple[Round, int, int]:
        """
        Advances all SHORTLISTED teams in round_obj to the next sequential round.
        If the next round doesn't exist yet, it creates it automatically.
        Returns: (target_round, newly_promoted_count, already_in_round_count)
        """
        hackathon = round_obj.hackathon
        next_round = hackathon.rounds.filter(order__gt=round_obj.order).order_by('order').first()
        if next_round is None:
            next_order = round_obj.order + 1
            from .models import RoundStatus
            next_round = Round.objects.create(
                hackathon=hackathon,
                order=next_order,
                name=f"Round {next_order}",
                status=RoundStatus.UPCOMING,
            )
            log_audit_event(
                actor=actor, action='Auto-Created Hackathon Round for Promotion',
                target_model='HackathonRound', target_id=next_round.pk,
                details={'hackathon': hackathon.slug, 'round': next_round.name, 'order': next_order},
            )

        shortlisted_teams = hackathon.teams.filter(
            status=TeamStatus.REGISTERED,
            round_entries__round=round_obj,
            round_entries__status=EntryStatus.SHORTLISTED,
        ).distinct()

        existing_team_ids = set(next_round.entries.values_list('team_id', flat=True))
        to_create = [
            RoundEntry(round=next_round, team=team, status=EntryStatus.PENDING)
            for team in shortlisted_teams
            if team.pk not in existing_team_ids
        ]
        if to_create:
            RoundEntry.objects.bulk_create(to_create, ignore_conflicts=True)

        already_present = len(shortlisted_teams) - len(to_create)
        log_audit_event(
            actor=actor, action='Advanced Shortlisted Teams',
            target_model='HackathonRound', target_id=round_obj.pk,
            details={
                'from_round': round_obj.name,
                'to_round': next_round.name,
                'promoted': len(to_create),
                'already_present': already_present,
            },
        )
        return next_round, len(to_create), already_present

    @staticmethod
    @transaction.atomic
    def add_team_entry(round_obj: Round, team: Team, actor=None) -> tuple[RoundEntry, bool]:
        entry, created = RoundEntry.objects.get_or_create(
            round=round_obj, team=team,
            defaults={'status': EntryStatus.PENDING}
        )
        log_audit_event(
            actor=actor, action='Added Team to Hackathon Round',
            target_model='HackathonRound', target_id=round_obj.pk,
            details={'round': round_obj.name, 'team': team.name, 'created': created},
        )
        return entry, created

    @staticmethod
    @transaction.atomic
    def remove_team_entry(round_obj: Round, team: Team, actor=None) -> bool:
        deleted_count, _ = round_obj.entries.filter(team=team).delete()
        log_audit_event(
            actor=actor, action='Removed Team from Hackathon Round',
            target_model='HackathonRound', target_id=round_obj.pk,
            details={'round': round_obj.name, 'team': team.name},
        )
        return deleted_count > 0


def _notify_result_changed(round_obj: Round, team_ids: list[int], actor) -> None:
    """Tell team members their already-published round result was changed. Never fails the caller."""
    try:
        from apps.notifications.models import NotificationCategory, NotificationType
        from apps.notifications.services import NotificationService

        recipients = User.objects.filter(hackathon_memberships__team_id__in=team_ids).distinct()
        NotificationService.create_bulk_notifications(
            recipients,
            title=f'{round_obj.name}: your result was updated',
            message='The organizers updated your team\'s result for this round. Open your team dashboard for details.',
            type=NotificationType.INFO,
            category=NotificationCategory.HACKATHON,
            link_url=f'/hackathons/{round_obj.hackathon.slug}/dashboard',
            created_by=actor,
        )
    except Exception:
        logger.exception('Could not notify teams about a changed round result')


def _prepare_details_form(form):
    """A round's details form takes one response per team leader."""
    if form.allow_multiple_responses:
        form.allow_multiple_responses = False
        form.save(update_fields=['allow_multiple_responses', 'updated_at'])


def round_entry_for_details_form(form, user) -> RoundEntry | None:
    """The RoundEntry this user's response to ``form`` belongs to, if they may submit it."""
    if not (user and user.is_authenticated):
        return None
    return (
        RoundEntry.objects.select_related('round', 'team')
        .filter(
            round__details_form=form,
            round__results_published=True,
            status=EntryStatus.SHORTLISTED,
            team__leader=user,
            team__status=TeamStatus.REGISTERED,
        )
        .order_by('-round__order')
        .first()
    )


def check_round_form_access(form, user) -> RoundEntry | None:
    """Gate used by the forms submission endpoint.

    Returns None when ``form`` isn't any round's details form (no gating);
    raises PermissionDenied when it is and the caller isn't the leader of a
    team shortlisted in that round; otherwise returns the matching RoundEntry.
    """
    if not Round.objects.filter(details_form=form).exists():
        return None
    entry = round_entry_for_details_form(form, user)
    if entry is None:
        raise PermissionDenied(
            'This form is only for the team leaders of teams shortlisted in this hackathon round.'
        )
    return entry


# ---------------------------------------------------------------------------
# Announcements
# ---------------------------------------------------------------------------

class AnnouncementService:

    @staticmethod
    def live_all():
        now = timezone.now()
        return (
            HackathonAnnouncement.objects.filter(is_active=True, publish_at__lte=now)
            .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        )

    @staticmethod
    def live(hackathon: Hackathon):
        now = timezone.now()
        return (
            hackathon.announcements.filter(is_active=True, publish_at__lte=now)
            .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        )

    @staticmethod
    def visible_for(hackathon: Hackathon, user):
        qs = AnnouncementService.live(hackathon)
        membership = membership_of(hackathon, user)
        if membership is None or membership.team.status not in ACTIVE_TEAM_STATUSES:
            return qs.filter(audience=AnnouncementAudience.PUBLIC)
        team = membership.team
        entries = RoundEntry.objects.filter(team=team)
        return qs.filter(
            Q(audience=AnnouncementAudience.PUBLIC)
            | Q(audience=AnnouncementAudience.PARTICIPANTS)
            | Q(audience=AnnouncementAudience.ROUND_ALL, round__in=entries.values('round'))
            | Q(
                audience=AnnouncementAudience.ROUND_SHORTLISTED,
                round__in=entries.filter(status=EntryStatus.SHORTLISTED, round__results_published=True).values('round'),
            )
            | Q(
                audience=AnnouncementAudience.ROUND_REJECTED,
                round__in=entries.filter(status=EntryStatus.REJECTED, round__results_published=True).values('round'),
            )
            | Q(audience=AnnouncementAudience.TEAMS, target_teams=team)
        ).distinct()

    @staticmethod
    def recipients(announcement: HackathonAnnouncement):
        hackathon = announcement.hackathon
        audience = announcement.audience
        if audience == AnnouncementAudience.PUBLIC or audience == AnnouncementAudience.PARTICIPANTS:
            teams = hackathon.teams.filter(status__in=ACTIVE_TEAM_STATUSES)
        elif audience == AnnouncementAudience.ROUND_ALL:
            teams = hackathon.teams.filter(round_entries__round=announcement.round)
        elif audience == AnnouncementAudience.ROUND_SHORTLISTED:
            teams = hackathon.teams.filter(
                round_entries__round=announcement.round, round_entries__status=EntryStatus.SHORTLISTED,
            )
        elif audience == AnnouncementAudience.ROUND_REJECTED:
            teams = hackathon.teams.filter(
                round_entries__round=announcement.round, round_entries__status=EntryStatus.REJECTED,
            )
        else:
            teams = announcement.target_teams.all()
        return User.objects.filter(hackathon_memberships__team__in=teams).distinct()

    @staticmethod
    def validate(audience, round_obj, target_team_ids, hackathon):
        if audience in ROUND_SCOPED_AUDIENCES:
            if round_obj is None:
                raise HackathonError('Pick the round this announcement is for.', 'ROUND_REQUIRED', field='round')
            if round_obj.hackathon_id != hackathon.id:
                raise HackathonError('That round belongs to another hackathon.', 'ROUND_INVALID', field='round')
        if audience == AnnouncementAudience.TEAMS:
            if not target_team_ids:
                raise HackathonError('Pick at least one team.', 'TEAMS_REQUIRED', field='target_teams')
            if hackathon.teams.filter(pk__in=target_team_ids).count() != len(set(target_team_ids)):
                raise HackathonError('Some selected teams are not in this hackathon.', 'TEAMS_INVALID', field='target_teams')

    @staticmethod
    @transaction.atomic
    def create(hackathon: Hackathon, actor, *, target_team_ids=None, **fields) -> HackathonAnnouncement:
        AnnouncementService.validate(fields.get('audience'), fields.get('round'), target_team_ids, hackathon)
        announcement = HackathonAnnouncement.objects.create(hackathon=hackathon, created_by=actor, **fields)
        if target_team_ids:
            announcement.target_teams.set(target_team_ids)
        log_audit_event(
            actor=actor, action='Created Hackathon Announcement', target_model='HackathonAnnouncement',
            target_id=announcement.pk, details={'hackathon': hackathon.slug, 'title': announcement.title, 'audience': announcement.audience},
        )
        if announcement.send_email and announcement.publish_at <= timezone.now():
            transaction.on_commit(lambda: AnnouncementService.dispatch_email(announcement))
        return announcement

    @staticmethod
    def dispatch_email(announcement: HackathonAnnouncement) -> int:
        """Email the audience exactly once: the first caller to stamp ``emailed_at`` sends."""
        claimed = HackathonAnnouncement.objects.filter(
            pk=announcement.pk, emailed_at__isnull=True,
        ).update(emailed_at=timezone.now())
        return AnnouncementService.notify(announcement) if claimed else 0

    @staticmethod
    def send_due_emails(hackathon: Hackathon | None = None) -> int:
        """Send emails for announcements that asked for one and have now been published."""
        due = AnnouncementService.live_all().filter(send_email=True, emailed_at__isnull=True)
        if hackathon is not None:
            due = due.filter(hackathon=hackathon)
        return sum(AnnouncementService.dispatch_email(announcement) for announcement in due)

    @staticmethod
    def notify(announcement: HackathonAnnouncement) -> int:
        """Email every recipient via the shared email-job engine (background thread)."""
        from django.utils.html import escape, linebreaks
        from apps.core.models import EmailTemplate
        from apps.core.services.email_service import EmailNotificationService, MemberEmailContext
        from apps.core.tasks import run_in_background, process_email_job

        HackathonAnnouncement.objects.filter(pk=announcement.pk).update(emailed_at=timezone.now())
        users = list(AnnouncementService.recipients(announcement))
        if not users:
            return 0

        # The background job re-derives each recipient's context from their
        # profile (EmailNotificationService.process_email_job), so per-call
        # context can't carry the message - it's baked into a template of its
        # own instead. `{{` is defused so admin-typed text can't be read as a
        # template parameter.
        def defuse(text):
            return text.replace('{{', '{ {').replace('}}', '} }')

        hackathon = announcement.hackathon
        subject = defuse(f'[{hackathon.title}] {announcement.title}')[:255]
        body_text = defuse(announcement.message)
        template, _ = EmailTemplate.objects.update_or_create(
            name=f'hackathon_announcement_{announcement.pk}',
            defaults={
                'display_title': f'Hackathon announcement: {announcement.title}'[:200],
                'subject_template': subject,
                'html_template': (
                    '<p>Hi {{first_name}},</p>'
                    f'<h3>{escape(defuse(announcement.title))}</h3>'
                    f'{linebreaks(escape(body_text))}'
                    '<p><a href="{{portal_url}}/hackathons/' f'{hackathon.slug}/dashboard">Open your team dashboard</a></p>'
                ),
                'text_template': (
                    'Hi {{first_name}},\n\n'
                    f'{defuse(announcement.title)}\n\n{body_text}\n\n'
                    '{{portal_url}}/hackathons/' f'{hackathon.slug}/dashboard'
                ),
                'allowed_parameters': ['first_name', 'portal_url'],
                'created_by': announcement.created_by,
            },
        )
        recipients = [(u.email, u, MemberEmailContext.build_for_user(u)) for u in users]
        job = EmailNotificationService.create_email_job(
            template, recipients, campaign_name=f'hackathon:{announcement.hackathon.slug}:{announcement.pk}',
            created_by=announcement.created_by,
        )
        run_in_background(lambda: process_email_job(job.id))
        return len(recipients)


# ---------------------------------------------------------------------------
# Waitlist
# ---------------------------------------------------------------------------

class WaitlistService:
    """Places queued teams (oldest first) whenever a team or participant slot opens up."""

    @staticmethod
    @transaction.atomic
    def promote(hackathon: Hackathon) -> list[Team]:
        if not hackathon.waitlist_enabled and not hackathon.teams.filter(is_waitlisted=True).exists():
            return []
        promoted = []
        queue = (
            hackathon.teams.select_for_update(of=('self',))
            .filter(is_waitlisted=True, status__in=ACTIVE_TEAM_STATUSES)
            # A paid team queues from the moment its payment was verified, not from when it was created.
            .annotate(queued_at=Coalesce('payment__reviewed_at', 'created_at')).order_by('queued_at', 'id')
        )
        for team in queue:
            if registration.teams_full(hackathon):
                break
            if registration.participants_would_exceed(hackathon, team.memberships.count()):
                continue
            team.is_waitlisted = False
            team.save(update_fields=['is_waitlisted', 'updated_at'])
            promoted.append(team)
            _audit(None, 'Promoted Team From Waitlist', team)
            celebrate.waitlist_promoted(team)
            _notify_after_commit(
                [m.user for m in team.memberships.select_related('user')],
                f'{team.name} is in!',
                f"A spot opened up and your team '{team.name}' is now registered for {hackathon.title}.",
                hackathon,
            )
        return promoted

    @staticmethod
    def promote_after_commit(hackathon: Hackathon) -> None:
        transaction.on_commit(lambda: WaitlistService.promote(hackathon))
