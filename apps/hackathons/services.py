"""
Business rules for hackathon team formation, rounds and announcements.

Every state change goes through here (never through a raw serializer save) so
the rules — registration window, team size, one team per user per hackathon,
leader-only edits, problem-statement capacity — live in one place and are
enforced identically for every endpoint. Mutations run inside
``transaction.atomic`` with the Team row locked, so two concurrent invite
accepts can't both squeeze past the size limit.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework.exceptions import APIException, PermissionDenied

from apps.audit.utils import log_audit_event
from apps.core.permissions import _is_admin_or_club_lead

from .models import (
    AnnouncementAudience, EntryStatus, Hackathon, HackathonAnnouncement, InviteStatus,
    ProblemStatement, Round, RoundEntry, Team, TeamInvite, TeamMember, TeamRole, TeamStatus,
)

User = get_user_model()

ACTIVE_TEAM_STATUSES = (TeamStatus.FORMING, TeamStatus.REGISTERED)


class HackathonError(APIException):
    """400 with a stable machine-readable ``code`` the frontend can branch on."""
    status_code = 400
    default_code = 'HACKATHON_ERROR'

    def __init__(self, message, code='HACKATHON_ERROR', field=None):
        detail = {'detail': message, 'code': code}
        if field:
            detail['field'] = field
        self.detail = detail


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


def _require_team_editable(team: Team, actor):
    """Participant-side edits need open registration and an unlocked hackathon; admins bypass both."""
    if _is_admin_or_club_lead(actor):
        return
    if team.status in (TeamStatus.DISQUALIFIED, TeamStatus.WITHDRAWN):
        raise HackathonError('This team can no longer be changed.', 'TEAM_INACTIVE')
    if team.hackathon.team_edits_locked:
        raise HackathonError('Team changes are locked for this hackathon.', 'TEAM_EDITS_LOCKED')
    _require_registration_open(team.hackathon)


def _require_leader(team: Team, actor):
    if not (is_leader(team, actor) or _is_admin_or_club_lead(actor)):
        raise PermissionDenied('Only the team leader can do this.')


def _require_profile_complete(hackathon: Hackathon, user, *, who='your'):
    missing = missing_profile_fields(hackathon, user)
    if missing:
        raise HackathonError(
            f"Complete {who} profile first — missing: {', '.join(missing)}.",
            'PROFILE_INCOMPLETE',
        )


def _check_problem_statement(hackathon: Hackathon, ps: ProblemStatement | None, *, exclude_team: Team | None = None):
    if ps is None:
        if hackathon.problem_statements.filter(is_active=True).exists():
            raise HackathonError('Pick a problem statement.', 'PROBLEM_STATEMENT_REQUIRED', field='problem_statement')
        return
    if ps.hackathon_id != hackathon.id or not ps.is_active:
        raise HackathonError('That problem statement is not available.', 'PROBLEM_STATEMENT_INVALID', field='problem_statement')
    if ps.max_teams:
        taken = ps.teams.filter(status__in=ACTIVE_TEAM_STATUSES)
        if exclude_team is not None:
            taken = taken.exclude(pk=exclude_team.pk)
        if taken.count() >= ps.max_teams:
            raise HackathonError('That problem statement is full — pick another.', 'PROBLEM_STATEMENT_FULL', field='problem_statement')


def _lock_team(team: Team) -> Team:
    return Team.objects.select_for_update().select_related('hackathon').get(pk=team.pk)


def _recompute_status(team: Team):
    """FORMING ↔ REGISTERED tracks whether the team currently meets min_team_size."""
    if team.status not in ACTIVE_TEAM_STATUSES:
        return
    count = team.memberships.count()
    new_status = TeamStatus.REGISTERED if count >= team.hackathon.min_team_size else TeamStatus.FORMING
    if new_status != team.status:
        team.status = new_status
        team.save(update_fields=['status', 'updated_at'])


def _audit(actor, action, team: Team, **details):
    log_audit_event(
        actor=actor, action=action, target_model='HackathonTeam', target_id=team.pk,
        details={'team': team.name, 'hackathon': team.hackathon.slug, **details},
    )


# ---------------------------------------------------------------------------
# Team formation
# ---------------------------------------------------------------------------

class TeamService:

    @staticmethod
    @transaction.atomic
    def create_team(hackathon: Hackathon, leader, name: str, problem_statement: ProblemStatement | None) -> Team:
        name = (name or '').strip()
        if not name:
            raise HackathonError('Team name is required.', 'NAME_REQUIRED', field='name')
        if len(name) > 150:
            raise HackathonError('Team name is too long (max 150 characters).', 'NAME_TOO_LONG', field='name')

        hackathon = Hackathon.objects.select_for_update().get(pk=hackathon.pk)
        _require_registration_open(hackathon)
        if TeamMember.objects.filter(hackathon=hackathon, user=leader).exists():
            raise HackathonError('You are already in a team for this hackathon.', 'ALREADY_IN_TEAM')
        _require_profile_complete(hackathon, leader)
        if hackathon.teams.filter(name__iexact=name).exists():
            raise HackathonError('That team name is already taken.', 'NAME_TAKEN', field='name')
        if problem_statement is not None:
            ProblemStatement.objects.select_for_update().filter(pk=problem_statement.pk).first()
        _check_problem_statement(hackathon, problem_statement)

        try:
            team = Team.objects.create(
                hackathon=hackathon, name=name, leader=leader,
                problem_statement=problem_statement, status=TeamStatus.FORMING,
            )
            TeamMember.objects.create(team=team, hackathon=hackathon, user=leader, role=TeamRole.LEADER)
        except IntegrityError:
            raise HackathonError('You are already in a team, or that name is taken.', 'CONFLICT')

        # Being on a team means any other pending invites for this hackathon are moot.
        TeamInvite.objects.filter(
            hackathon=hackathon, invited_user=leader, status=InviteStatus.PENDING,
        ).update(status=InviteStatus.CANCELLED, responded_at=timezone.now())

        _recompute_status(team)
        _audit(leader, 'Created Hackathon Team', team, problem_statement=getattr(problem_statement, 'code', None))
        return team

    @staticmethod
    @transaction.atomic
    def update_team(team: Team, actor, *, name=None, problem_statement=..., ) -> Team:
        team = _lock_team(team)
        _require_leader(team, actor)
        _require_team_editable(team, actor)
        changes = {}
        if name is not None:
            name = name.strip()
            if not name:
                raise HackathonError('Team name is required.', 'NAME_REQUIRED', field='name')
            if team.hackathon.teams.filter(name__iexact=name).exclude(pk=team.pk).exists():
                raise HackathonError('That team name is already taken.', 'NAME_TAKEN', field='name')
            if name != team.name:
                changes['name'] = [team.name, name]
                team.name = name
        if problem_statement is not ...:
            if problem_statement is not None:
                ProblemStatement.objects.select_for_update().filter(pk=problem_statement.pk).first()
            _check_problem_statement(team.hackathon, problem_statement, exclude_team=team)
            if problem_statement != team.problem_statement:
                changes['problem_statement'] = [
                    getattr(team.problem_statement, 'code', None), getattr(problem_statement, 'code', None),
                ]
                team.problem_statement = problem_statement
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
        slots = hackathon.max_team_size - team.memberships.count() - team.invites.filter(status=InviteStatus.PENDING).count()
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
        return invite

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
    def accept_invite(invite: TeamInvite, user) -> Team:
        team = _lock_team(invite.team)
        invite = TeamInvite.objects.select_for_update().get(pk=invite.pk)
        if invite.invited_user_id != user.id:
            raise PermissionDenied('This invite is not for you.')
        if invite.status != InviteStatus.PENDING:
            raise HackathonError('This invite is no longer pending.', 'INVITE_NOT_PENDING')
        hackathon = team.hackathon
        if team.status not in ACTIVE_TEAM_STATUSES:
            raise HackathonError('This team is no longer active.', 'TEAM_INACTIVE')
        if hackathon.team_edits_locked:
            raise HackathonError('Team changes are locked for this hackathon.', 'TEAM_EDITS_LOCKED')
        _require_registration_open(hackathon)
        if TeamMember.objects.filter(hackathon=hackathon, user=user).exists():
            raise HackathonError('You are already in a team for this hackathon.', 'ALREADY_IN_TEAM')
        _require_profile_complete(hackathon, user)
        if team.memberships.count() >= hackathon.max_team_size:
            raise HackathonError('This team is already full.', 'TEAM_FULL')

        try:
            with transaction.atomic():
                TeamMember.objects.create(team=team, hackathon=hackathon, user=user, role=TeamRole.MEMBER)
        except IntegrityError:
            raise HackathonError('You are already in a team for this hackathon.', 'ALREADY_IN_TEAM')

        now = timezone.now()
        invite.status = InviteStatus.ACCEPTED
        invite.responded_at = now
        invite.save(update_fields=['status', 'responded_at'])
        TeamInvite.objects.filter(
            hackathon=hackathon, invited_user=user, status=InviteStatus.PENDING,
        ).exclude(pk=invite.pk).update(status=InviteStatus.CANCELLED, responded_at=now)

        _recompute_status(team)
        _audit(user, 'Joined Hackathon Team', team)
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
        membership.delete()
        _recompute_status(team)
        _audit(actor, 'Removed Hackathon Team Member', team, removed=email)
        return team

    @staticmethod
    @transaction.atomic
    def leave_team(team: Team, user) -> Team:
        team = _lock_team(team)
        membership = team.memberships.filter(user=user).first()
        if membership is None:
            raise HackathonError('You are not in this team.', 'NOT_A_MEMBER')
        _require_team_editable(team, user)
        others = team.memberships.exclude(user=user)
        if team.leader_id == user.id:
            if others.exists():
                raise HackathonError('Transfer leadership to a teammate before leaving.', 'LEADER_MUST_TRANSFER')
            # Sole member leaving: the team is withdrawn rather than deleted, so
            # admins keep a record of it.
            membership.delete()
            team.leader = None
            team.status = TeamStatus.WITHDRAWN
            team.save(update_fields=['leader', 'status', 'updated_at'])
            team.invites.filter(status=InviteStatus.PENDING).update(
                status=InviteStatus.CANCELLED, responded_at=timezone.now(),
            )
            _audit(user, 'Withdrew Hackathon Team', team)
            return team
        membership.delete()
        _recompute_status(team)
        _audit(user, 'Left Hackathon Team', team)
        return team

    @staticmethod
    @transaction.atomic
    def transfer_leadership(team: Team, actor, user_id: int) -> Team:
        team = _lock_team(team)
        _require_leader(team, actor)
        if not _is_admin_or_club_lead(actor) and team.status not in ACTIVE_TEAM_STATUSES:
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
        return team

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
        _recompute_status(team)
        _audit(actor, 'Admin Added Hackathon Team Member', team, added=user.email)
        return team

    @staticmethod
    @transaction.atomic
    def admin_set_status(team: Team, actor, new_status: str) -> Team:
        team = _lock_team(team)
        if new_status not in TeamStatus.values:
            raise HackathonError('Unknown team status.', 'INVALID_STATUS', field='status')
        old = team.status
        team.status = new_status
        team.save(update_fields=['status', 'updated_at'])
        if new_status in ACTIVE_TEAM_STATUSES:
            _recompute_status(team)
        _audit(actor, 'Admin Changed Hackathon Team Status', team, old=old, new=team.status)
        return team


# ---------------------------------------------------------------------------
# Rounds & shortlisting
# ---------------------------------------------------------------------------

class RoundService:

    @staticmethod
    def eligible_teams(round_obj: Round):
        """First round: every registered team. Later rounds: teams shortlisted in the previous round."""
        hackathon = round_obj.hackathon
        previous = hackathon.rounds.filter(order__lt=round_obj.order).order_by('-order').first()
        if previous is None:
            return hackathon.teams.filter(status=TeamStatus.REGISTERED)
        return hackathon.teams.filter(
            status=TeamStatus.REGISTERED,
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
    def decide(round_obj: Round, actor, team_ids: list[int], status: str, *, feedback=None, admin_notes=None) -> int:
        if status not in EntryStatus.values:
            raise HackathonError('Unknown entry status.', 'INVALID_STATUS', field='status')
        entries = round_obj.entries.filter(team_id__in=team_ids)
        update = {'status': status, 'decided_by': actor, 'decided_at': timezone.now()}
        if feedback is not None:
            update['feedback'] = feedback
        if admin_notes is not None:
            update['admin_notes'] = admin_notes
        count = entries.update(**update)
        log_audit_event(
            actor=actor, action='Decided Hackathon Round Entries', target_model='HackathonRound',
            target_id=round_obj.pk, details={'round': round_obj.name, 'status': status, 'teams': list(team_ids), 'updated': count},
        )
        return count

    @staticmethod
    @transaction.atomic
    def publish_results(round_obj: Round, actor, *, announce=False, message='') -> Round:
        round_obj.results_published = True
        round_obj.save(update_fields=['results_published', 'updated_at'])
        log_audit_event(
            actor=actor, action='Published Hackathon Round Results', target_model='HackathonRound',
            target_id=round_obj.pk, details={'round': round_obj.name},
        )
        if announce:
            AnnouncementService.create(
                round_obj.hackathon, actor,
                title=f'{round_obj.name}: results are out',
                message=message or (
                    f'Congratulations! Your team has been shortlisted in **{round_obj.name}**. '
                    'Check your team dashboard for next steps.'
                ),
                audience=AnnouncementAudience.ROUND_SHORTLISTED, round=round_obj, type='SUCCESS',
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
        return round_obj


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
        else:
            teams = announcement.target_teams.all()
        return User.objects.filter(hackathon_memberships__team__in=teams).distinct()

    @staticmethod
    def validate(audience, round_obj, target_team_ids, hackathon):
        if audience in (AnnouncementAudience.ROUND_ALL, AnnouncementAudience.ROUND_SHORTLISTED):
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
        if announcement.send_email:
            transaction.on_commit(lambda: AnnouncementService.notify(announcement))
        return announcement

    @staticmethod
    def notify(announcement: HackathonAnnouncement) -> int:
        """Email every recipient via the shared email-job engine (background thread)."""
        from django.utils.html import escape, linebreaks
        from apps.core.models import EmailTemplate
        from apps.core.services.email_service import EmailNotificationService, MemberEmailContext
        from apps.core.tasks import run_in_background, process_email_job

        users = list(AnnouncementService.recipients(announcement))
        if not users:
            return 0

        # The background job re-derives each recipient's context from their
        # profile (EmailNotificationService.process_email_job), so per-call
        # context can't carry the message — it's baked into a template of its
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
