"""Team finder: solo participants list themselves, teams that are recruiting say what they need."""
from __future__ import annotations

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from apps.core.permissions import _is_admin_or_club_lead

from . import config_validation as validate, registration
from .errors import HackathonError
from .models import Hackathon, JoinRequestStatus, Team, TeamJoinRequest, TeamMember, TeamSeeker, TeamStatus
from .services import (
    ACTIVE_TEAM_STATUSES, TeamService, _admit, _audit, _lock_team, _notify_member_joined, _notify_users,
    _require_leader, _require_profile_complete, _require_registration_open, is_leader, problem_label,
)

ACTION_KIND = 'JOIN_REQUEST'
MAX_PENDING_REQUESTS = 5
ASK_AGAIN_AFTER_DECLINE = timedelta(hours=24)


def _action_ref(hackathon: Hackathon, request_id: int) -> str:
    return f'{hackathon.slug}:{request_id}'


def _resolve_notification(hackathon: Hackathon, request_id: int, outcome: str) -> None:
    """The leader's notification about this request shows the result instead of buttons, wherever it was decided."""
    from apps.notifications.services import NotificationService
    NotificationService.resolve_action(ACTION_KIND, _action_ref(hackathon, request_id), outcome)


def withdraw_pending_requests(hackathon: Hackathon, user, now=None) -> int:
    """Somebody joined a team (or left the finder): their other open requests no longer mean anything."""
    now = now or timezone.now()
    pending = list(TeamJoinRequest.objects.filter(hackathon=hackathon, user=user, status=JoinRequestStatus.PENDING).values_list('pk', flat=True))
    if pending:
        TeamJoinRequest.objects.filter(pk__in=pending).update(status=JoinRequestStatus.CANCELLED, decided_at=now)
        for request_id in pending:
            _resolve_notification(hackathon, request_id, 'CANCELLED')
    return len(pending)


def _require_finder_on(hackathon: Hackathon):
    if not hackathon.feature('team_finder'):
        raise HackathonError('The team finder is not enabled for this hackathon.', 'FEATURE_OFF')


def _person(user) -> str:
    return f'{user.first_name} {user.last_name}'.strip() or user.email.split('@')[0]


def _profile_line(user) -> str:
    college = user.college.name if user.college_id else (user.other_college_name or '')
    return ' · '.join(part for part in (user.branch, f'Year {user.year}' if user.year else '', college) if part)


class FinderService:

    @staticmethod
    @transaction.atomic
    def save_profile(hackathon: Hackathon, user, data: dict) -> TeamSeeker:
        _require_finder_on(hackathon)
        _require_registration_open(hackathon)
        if TeamMember.objects.filter(hackathon=hackathon, user=user).exists():
            raise HackathonError('You are already on a team.', 'ALREADY_IN_TEAM')
        try:
            skills = validate._text(data.get('skills'), 'Skills', 200)
            note = validate._text(data.get('note'), 'Note', 300)
        except Exception as exc:
            raise HackathonError(' '.join(str(m) for m in getattr(exc, 'detail', [exc])), 'INVALID')
        seeker, _ = TeamSeeker.objects.update_or_create(
            hackathon=hackathon, user=user, defaults={'skills': skills, 'note': note, 'is_active': True},
        )
        return seeker

    @staticmethod
    def withdraw_profile(hackathon: Hackathon, user) -> None:
        TeamSeeker.objects.filter(hackathon=hackathon, user=user).update(is_active=False)

    @staticmethod
    def listing(hackathon: Hackathon, viewer) -> dict:
        _require_finder_on(hackathon)
        in_team = set(hackathon.memberships.values_list('user_id', flat=True))
        seekers = [
            {
                'id': seeker.id, 'name': _person(seeker.user), 'skills': seeker.skills, 'note': seeker.note,
                'profile': _profile_line(seeker.user), 'is_me': seeker.user_id == viewer.id,
            }
            for seeker in hackathon.seekers.select_related('user', 'user__college').filter(is_active=True)
            if seeker.user_id not in in_team
        ]
        teams = []
        for team in (
            hackathon.teams.filter(looking_for_members=True, status__in=ACTIVE_TEAM_STATUSES)
            .select_related('problem_statement', 'leader').prefetch_related('memberships')
        ):
            size = team.memberships.count()
            if size >= hackathon.max_team_size:
                continue
            teams.append({
                'id': team.id, 'name': team.name, 'looking_for': team.looking_for, 'members': size,
                'max_members': hackathon.max_team_size, 'problem': problem_label(team),
                'leader': _person(team.leader) if team.leader else '',
            })
        mine = TeamSeeker.objects.filter(hackathon=hackathon, user=viewer, is_active=True).first()
        return {
            'seekers': seekers, 'teams': teams,
            'my_profile': {'skills': mine.skills, 'note': mine.note} if mine else None,
            'my_requests': [FinderService.serialize_request(r) for r in FinderService.my_requests(hackathon, viewer)],
        }

    @staticmethod
    def invite_seeker(hackathon: Hackathon, actor, seeker_id: int) -> None:
        """A team leader invites a listed participant; the participant's email is never shown, only used here."""
        _require_finder_on(hackathon)
        seeker = hackathon.seekers.select_related('user').filter(pk=seeker_id, is_active=True).first()
        if seeker is None:
            raise HackathonError('That person is no longer looking for a team.', 'NOT_FOUND', status=404)
        membership = TeamMember.objects.select_related('team').filter(hackathon=hackathon, user=actor).first()
        if membership is None or not is_leader(membership.team, actor):
            raise HackathonError('Create a team first, then invite people to it.', 'NOT_A_LEADER')
        TeamService.invite(membership.team, actor, seeker.user.email)

    @staticmethod
    @transaction.atomic
    def request_to_join(hackathon: Hackathon, user, team_id: int, message: str = '', registration_data: dict | None = None) -> TeamJoinRequest:
        """Ask a recruiting team to take you in. The leader accepts or declines; nothing changes for the team until then."""
        _require_finder_on(hackathon)
        _require_registration_open(hackathon)
        team = hackathon.teams.select_related('leader').filter(
            pk=team_id, looking_for_members=True, status__in=ACTIVE_TEAM_STATUSES,
        ).first()
        if team is None or team.leader is None:
            raise HackathonError('That team is not recruiting.', 'NOT_FOUND', status=404)
        if TeamMember.objects.filter(hackathon=hackathon, user=user).exists():
            raise HackathonError('You are already on a team.', 'ALREADY_IN_TEAM')
        if team.memberships.count() >= hackathon.max_team_size:
            raise HackathonError('That team is already full.', 'TEAM_FULL')
        try:
            note = validate._text(message, 'Message', 300)
        except Exception as exc:
            raise HackathonError(' '.join(str(m) for m in getattr(exc, 'detail', [exc])), 'INVALID', field='message')
        # Everything the leader's "accept" will need is checked now, so a request that cannot succeed is never sent.
        _require_profile_complete(hackathon, user)
        registration.check_eligibility(hackathon, user)
        accepted_at, answers = registration.registration_inputs(hackathon, registration_data)

        mine = TeamJoinRequest.objects.select_for_update().filter(hackathon=hackathon, user=user)
        if mine.filter(team=team, status=JoinRequestStatus.PENDING).exists():
            raise HackathonError('You already asked this team. Wait for the leader to answer.', 'ALREADY_REQUESTED', status=409)
        if mine.filter(status=JoinRequestStatus.PENDING).count() >= MAX_PENDING_REQUESTS:
            raise HackathonError(f'You have {MAX_PENDING_REQUESTS} requests waiting. Cancel one or wait for an answer.', 'TOO_MANY_REQUESTS', status=429)
        declined = mine.filter(team=team, status=JoinRequestStatus.DECLINED, decided_at__gt=timezone.now() - ASK_AGAIN_AFTER_DECLINE).exists()
        if declined:
            raise HackathonError('This team declined your request recently. Try again tomorrow, or ask another team.', 'DECLINED_RECENTLY', status=409)

        try:
            request = TeamJoinRequest.objects.create(
                hackathon=hackathon, team=team, user=user, message=note,
                registration_data={'accept_rules': bool(accepted_at), 'answers': answers},
            )
        except IntegrityError:
            raise HackathonError('You already asked this team. Wait for the leader to answer.', 'ALREADY_REQUESTED', status=409)
        seeker = TeamSeeker.objects.filter(hackathon=hackathon, user=user, is_active=True).first()
        skills = f' Skills: {seeker.skills}.' if seeker and seeker.skills else ''
        request_id, leader = request.pk, team.leader
        transaction.on_commit(lambda: _notify_users(
            [leader], f'{_person(user)} wants to join {team.name}',
            f"{_person(user)} ({_profile_line(user) or 'no profile details'}) asked to join your team.{skills}"
            + (f' "{note}"' if note else ''),
            hackathon, user, action_kind=ACTION_KIND, action_ref=_action_ref(hackathon, request_id),
        ))
        _audit(user, 'Asked To Join Hackathon Team', team)
        return request

    @staticmethod
    def my_requests(hackathon: Hackathon, user):
        return TeamJoinRequest.objects.filter(hackathon=hackathon, user=user).select_related('team').order_by('-created_at')[:20]

    @staticmethod
    def incoming_requests(hackathon: Hackathon, user):
        """Pending requests to the teams this person leads."""
        return (
            TeamJoinRequest.objects.filter(hackathon=hackathon, team__leader=user, status=JoinRequestStatus.PENDING)
            .select_related('team', 'user', 'user__college').order_by('created_at')
        )

    @staticmethod
    def blocked_reason(request: TeamJoinRequest) -> str:
        """Why accepting would fail right now (shown next to the button), or ''."""
        team, hackathon = request.team, request.hackathon
        if team.status not in ACTIVE_TEAM_STATUSES:
            return 'This team is no longer active.'
        if team.memberships.count() >= hackathon.max_team_size:
            return 'Your team is already full.'
        if hackathon.team_edits_locked:
            return 'Team changes are locked.'
        if not hackathon.is_registration_open:
            return 'Registration is closed.'
        if TeamMember.objects.filter(hackathon=hackathon, user=request.user).exists():
            return f'{_person(request.user)} joined another team.'
        return ''

    @staticmethod
    def serialize_request(request: TeamJoinRequest, *, incoming: bool = False) -> dict:
        data = {
            'id': request.pk, 'team_id': request.team_id, 'team_name': request.team.name, 'status': request.status,
            'message': request.message, 'created_at': request.created_at, 'decided_at': request.decided_at,
        }
        if incoming:
            seeker = TeamSeeker.objects.filter(hackathon=request.hackathon, user=request.user).first()
            data.update({
                'name': _person(request.user), 'profile': _profile_line(request.user), 'skills': seeker.skills if seeker else '',
                'blocked_reason': FinderService.blocked_reason(request),
            })
        return data

    @staticmethod
    @transaction.atomic
    def decide_request(hackathon: Hackathon, request_id: int, actor, decision: str) -> TeamJoinRequest:
        """The leader (or a team organizer) accepts or declines. Accepting adds the person, with every check a join needs."""
        if decision not in ('accept', 'decline'):
            raise HackathonError('Choose accept or decline.', 'INVALID', field='decision')
        found = TeamJoinRequest.objects.filter(pk=request_id, hackathon=hackathon).select_related('team').first()
        if found is None:
            raise HackathonError('That request was not found.', 'NOT_FOUND', status=404)
        team = _lock_team(found.team)
        request = TeamJoinRequest.objects.select_for_update().select_related('user', 'team').get(pk=found.pk)
        _require_leader(team, actor)
        if request.status != JoinRequestStatus.PENDING:
            raise HackathonError(f'This request was already {request.get_status_display().lower()}.', 'REQUEST_NOT_PENDING', status=409, outcome=request.status)
        now = timezone.now()
        who = request.user
        if decision == 'accept':
            newly_registered = _admit(team, who, request.registration_data)
            request.status = JoinRequestStatus.ACCEPTED
            _audit(actor, 'Accepted Hackathon Join Request', team, requester=who.email)
            _notify_member_joined(team, who, newly_registered)
            _notify_users([who], f"You're in: {team.name}", f"{_person(actor)} accepted your request. You are now part of team '{team.name}'.", hackathon, actor)
            outcome = 'ACCEPTED'
        else:
            request.status = JoinRequestStatus.DECLINED
            _audit(actor, 'Declined Hackathon Join Request', team, requester=who.email)
            _notify_users([who], f'{team.name} declined your request', f"The leader of '{team.name}' can't take you in right now. You can ask another team.", hackathon, actor)
            outcome = 'DECLINED'
        request.decided_by, request.decided_at = actor, now
        request.save(update_fields=['status', 'decided_by', 'decided_at', 'updated_at'])
        _resolve_notification(hackathon, request.pk, outcome)
        return request

    @staticmethod
    @transaction.atomic
    def cancel_request(hackathon: Hackathon, request_id: int, actor) -> TeamJoinRequest:
        request = TeamJoinRequest.objects.select_for_update().select_related('team').filter(pk=request_id, hackathon=hackathon).first()
        if request is None or request.user_id != actor.id:
            raise HackathonError('That request was not found.', 'NOT_FOUND', status=404)
        if request.status != JoinRequestStatus.PENDING:
            raise HackathonError(f'This request was already {request.get_status_display().lower()}.', 'REQUEST_NOT_PENDING', status=409, outcome=request.status)
        request.status, request.decided_at = JoinRequestStatus.CANCELLED, timezone.now()
        request.save(update_fields=['status', 'decided_at', 'updated_at'])
        _resolve_notification(hackathon, request.pk, 'CANCELLED')
        return request
