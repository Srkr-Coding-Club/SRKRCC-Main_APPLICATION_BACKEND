"""Team finder, organizer side: see who is looking for a team and who is recruiting, tidy it up and match people."""
from __future__ import annotations

from datetime import timedelta
from statistics import median

from django.db import transaction
from django.utils import timezone

from apps.audit.utils import log_audit_event

from .errors import HackathonError
from .finder import ACTION_KIND, _action_ref, _person, _phone, _profile_line, _require_finder_on, withdraw_pending_requests
from .models import Hackathon, JoinRequestStatus, Team, TeamJoinRequest, TeamMember, TeamSeeker
from .services import ACTIVE_TEAM_STATUSES, _admit, _audit, _lock_team, _notify_member_joined, _notify_users, problem_label

#: A request still waiting after this long is "stuck": the leader has not answered.
STUCK_AFTER = timedelta(hours=48)
#: Somebody who has been listed this long without a team is worth a nudge.
SEEKER_WAITING_AFTER = timedelta(days=3)
#: The same person or team is never nudged more often than this.
REMIND_GAP = timedelta(hours=24)
REQUEST_LIMIT = 500
REMIND_AUDIENCES = ('seekers', 'teams')


def _log(actor, action: str, target_id, **details) -> None:
    log_audit_event(actor=actor, action=action, target_model='Hackathon', target_id=target_id, details=details)


def _seats_open(hackathon: Hackathon, team: Team, size: int) -> int:
    return max(hackathon.max_team_size - size, 0)


def waiting_seekers(hackathon: Hackathon):
    """Listed people who are still without a team."""
    on_teams = hackathon.memberships.values('user_id')
    return hackathon.seekers.filter(is_active=True).exclude(user_id__in=on_teams)


def recruiting_teams(hackathon: Hackathon):
    return hackathon.teams.filter(looking_for_members=True, status__in=ACTIVE_TEAM_STATUSES)


def _recruiting_with_room(hackathon: Hackathon) -> list[tuple[Team, int]]:
    rows = []
    for team in recruiting_teams(hackathon).select_related('leader').prefetch_related('memberships'):
        size = len(team.memberships.all())
        if size < hackathon.max_team_size:
            rows.append((team, size))
    return rows


def _count_pending(hackathon: Hackathon, field: str) -> dict[int, int]:
    counts: dict[int, int] = {}
    for key in hackathon.join_requests.filter(status=JoinRequestStatus.PENDING).values_list(field, flat=True):
        counts[key] = counts.get(key, 0) + 1
    return counts


def pending_by_team(hackathon: Hackathon) -> dict[int, int]:
    return _count_pending(hackathon, 'team_id')


def pending_by_user(hackathon: Hackathon) -> dict[int, int]:
    return _count_pending(hackathon, 'user_id')


def _seeker_row(seeker: TeamSeeker, membership: TeamMember | None, pending: int) -> dict:
    user = seeker.user
    status = 'MATCHED' if membership else 'WAITING' if seeker.is_active else 'WITHDRAWN'
    return {
        'id': seeker.pk, 'user_id': user.pk, 'name': _person(user), 'email': user.email, 'phone': _phone(user),
        'profile': _profile_line(user), 'skills': seeker.skills, 'note': seeker.note, 'listed_at': seeker.created_at,
        'status': status, 'team': membership.team.name if membership else '', 'pending_requests': pending,
    }


def _team_row(hackathon: Hackathon, team: Team, size: int, pending: int) -> dict:
    leader = team.leader
    return {
        'id': team.pk, 'name': team.name, 'status': team.status, 'looking_for': team.looking_for, 'members': size,
        'max_members': hackathon.max_team_size, 'seats_open': _seats_open(hackathon, team, size), 'problem': problem_label(team) or '',
        'leader': _person(leader) if leader else '', 'leader_email': leader.email if leader else '',
        'leader_phone': _phone(leader) if leader else '', 'pending_requests': pending,
    }


def _request_row(join: TeamJoinRequest, now) -> dict:
    waiting = join.status == JoinRequestStatus.PENDING
    return {
        'id': join.pk, 'team_id': join.team_id, 'team_name': join.team.name, 'user_id': join.user_id, 'name': _person(join.user),
        'email': join.user.email, 'phone': _phone(join.user), 'profile': _profile_line(join.user), 'status': join.status,
        'message': join.message, 'created_at': join.created_at, 'decided_at': join.decided_at,
        'decided_by': _person(join.decided_by) if join.decided_by_id else '',
        'stuck': waiting and now - join.created_at >= STUCK_AFTER,
    }


def _hours_to_match(seekers_by_user: dict, memberships: dict) -> list[float]:
    hours = []
    for user_id, membership in memberships.items():
        seeker = seekers_by_user.get(user_id)
        if seeker and membership.joined_at >= seeker.created_at:
            hours.append((membership.joined_at - seeker.created_at).total_seconds() / 3600)
    return hours


def overview(hackathon: Hackathon) -> dict:
    """Everything the organizer's finder tab shows, in one response."""
    now = timezone.now()
    seekers = list(hackathon.seekers.select_related('user', 'user__college').order_by('-created_at'))
    memberships = {m.user_id: m for m in hackathon.memberships.select_related('team')}
    pending_users = pending_by_user(hackathon)
    seeker_rows = [_seeker_row(s, memberships.get(s.user_id), pending_users.get(s.user_id, 0)) for s in seekers]

    pending_teams = pending_by_team(hackathon)
    team_rows = [
        _team_row(hackathon, team, len(team.memberships.all()), pending_teams.get(team.pk, 0))
        for team in recruiting_teams(hackathon).select_related('leader', 'problem_statement').prefetch_related('memberships').order_by('name')
    ]
    joins = list(hackathon.join_requests.select_related('team', 'user', 'user__college', 'decided_by').order_by('-created_at')[:REQUEST_LIMIT])
    request_rows = [_request_row(join, now) for join in joins]

    typical = _hours_to_match({s.user_id: s for s in seekers}, memberships)
    stats = {
        'seekers_waiting': sum(1 for row in seeker_rows if row['status'] == 'WAITING'),
        'teams_recruiting': sum(1 for row in team_rows if row['seats_open'] > 0),
        'requests_pending': sum(1 for row in request_rows if row['status'] == JoinRequestStatus.PENDING),
        'requests_stuck': sum(1 for row in request_rows if row['stuck']),
        'matched': sum(1 for row in seeker_rows if row['status'] == 'MATCHED'),
        'median_hours_to_match': round(median(typical), 1) if typical else None,
    }
    return {
        'enabled': hackathon.feature('team_finder'), 'stats': stats,
        'seekers': seeker_rows, 'teams': team_rows, 'requests': request_rows,
    }


def _seeker_or_404(hackathon: Hackathon, seeker_id: int) -> TeamSeeker:
    seeker = hackathon.seekers.select_related('user').filter(pk=seeker_id).first()
    if seeker is None:
        raise HackathonError('That person is not in the finder.', 'NOT_FOUND', status=404)
    return seeker


def _clean_reason(reason) -> str:
    return ' '.join(str(reason or '').split())[:300]


def _because(reason: str) -> str:
    return f' Reason: {reason}' if reason else ''


@transaction.atomic
def remove_seeker(hackathon: Hackathon, actor, seeker_id: int, reason: str = '') -> TeamSeeker:
    """Take a person's listing down (their skills and note are cleared too) and withdraw their open requests."""
    _require_finder_on(hackathon)
    seeker = _seeker_or_404(hackathon, seeker_id)
    reason = _clean_reason(reason)
    seeker.is_active, seeker.skills, seeker.note = False, '', ''
    seeker.save(update_fields=['is_active', 'skills', 'note', 'updated_at'])
    withdrawn = withdraw_pending_requests(hackathon, seeker.user)
    _notify_users(
        [seeker.user], 'Your team finder listing was removed',
        f'An organizer removed your listing from the team finder for {hackathon.title}. You can list yourself again with a new note.{_because(reason)}',
        hackathon, actor,
    )
    _log(actor, 'Admin Removed Team Finder Listing', hackathon.slug, person=seeker.user.email, reason=reason, requests_withdrawn=withdrawn)
    return seeker


@transaction.atomic
def stop_recruiting(hackathon: Hackathon, actor, team_id: int, reason: str = '') -> int:
    """Switch off a team's "looking for members" and answer the requests waiting on it. Returns how many were closed."""
    _require_finder_on(hackathon)
    team = hackathon.teams.select_related('leader').filter(pk=team_id).first()
    if team is None:
        raise HackathonError('That team was not found.', 'NOT_FOUND', status=404)
    reason = _clean_reason(reason)
    team.looking_for_members = False
    team.save(update_fields=['looking_for_members', 'updated_at'])
    waiting = list(hackathon.join_requests.select_for_update().select_related('user').filter(team=team, status=JoinRequestStatus.PENDING))
    now = timezone.now()
    for join in waiting:
        join.status, join.decided_by, join.decided_at = JoinRequestStatus.CANCELLED, actor, now
        join.save(update_fields=['status', 'decided_by', 'decided_at', 'updated_at'])
        _resolve(hackathon, join, 'CANCELLED')
        _notify_users([join.user], f'{team.name} stopped recruiting', f"'{team.name}' is no longer taking requests, so your request was closed. You can ask another team.", hackathon, actor)
    if team.leader:
        _notify_users(
            [team.leader], f'{team.name} is no longer listed as recruiting',
            f"An organizer turned off 'looking for members' for your team.{_because(reason)} You can switch it on again from your dashboard.",
            hackathon, actor,
        )
    _audit(actor, 'Admin Stopped Team Recruiting', team, reason=reason, requests_closed=len(waiting))
    return len(waiting)


def _resolve(hackathon: Hackathon, join: TeamJoinRequest, outcome: str) -> None:
    from apps.notifications.services import NotificationService
    NotificationService.resolve_action(ACTION_KIND, _action_ref(hackathon, join.pk), outcome)


@transaction.atomic
def cancel_request(hackathon: Hackathon, actor, request_id: int) -> TeamJoinRequest:
    """Close a request that is stuck. The sender is told and is free to ask elsewhere."""
    _require_finder_on(hackathon)
    join = hackathon.join_requests.select_for_update().select_related('team', 'user').filter(pk=request_id).first()
    if join is None:
        raise HackathonError('That request was not found.', 'NOT_FOUND', status=404)
    if join.status != JoinRequestStatus.PENDING:
        raise HackathonError(f'This request was already {join.get_status_display().lower()}.', 'REQUEST_NOT_PENDING', status=409, outcome=join.status)
    join.status, join.decided_by, join.decided_at = JoinRequestStatus.CANCELLED, actor, timezone.now()
    join.save(update_fields=['status', 'decided_by', 'decided_at', 'updated_at'])
    _resolve(hackathon, join, 'CANCELLED')
    _notify_users(
        [join.user], f'Your request to {join.team.name} was closed',
        f"An organizer closed your request to join '{join.team.name}'. You can ask another team.", hackathon, actor,
    )
    _audit(actor, 'Admin Cancelled Hackathon Join Request', join.team, requester=join.user.email)
    return join


@transaction.atomic
def place_seeker(hackathon: Hackathon, actor, seeker_id: int, team_id: int) -> Team:
    """Put a listed person on a team. Every check a normal join needs applies except the rules and registration questions,
    which the person completes afterwards (they show up as missing details)."""
    _require_finder_on(hackathon)
    seeker = _seeker_or_404(hackathon, seeker_id)
    team = hackathon.teams.filter(pk=team_id).first()
    if team is None:
        raise HackathonError('That team was not found.', 'NOT_FOUND', status=404)
    team = _lock_team(team)
    if TeamMember.objects.filter(hackathon=hackathon, user=seeker.user).exists():
        raise HackathonError(f'{_person(seeker.user)} is already on a team.', 'ALREADY_IN_TEAM')
    asked = hackathon.join_requests.select_for_update().filter(team=team, user=seeker.user, status=JoinRequestStatus.PENDING).first()
    if asked:
        asked.status, asked.decided_by, asked.decided_at = JoinRequestStatus.ACCEPTED, actor, timezone.now()
        asked.save(update_fields=['status', 'decided_by', 'decided_at', 'updated_at'])
        _resolve(hackathon, asked, 'ACCEPTED')
    newly_registered = _admit(team, seeker.user, None, organizer=True)
    _audit(actor, 'Admin Placed Seeker On Hackathon Team', team, person=seeker.user.email)
    _notify_member_joined(team, seeker.user, newly_registered)
    _notify_users(
        [seeker.user], f'You were placed in {team.name}',
        f"An organizer placed you in team '{team.name}' for {hackathon.title}. Open your dashboard to meet your team and finish any missing details.",
        hackathon, actor,
    )
    return team


def remind(hackathon: Hackathon, actor, audience: str) -> int:
    """Nudge people who have waited (seekers) or recruiting teams with room, at most once a day each. Returns how many."""
    _require_finder_on(hackathon)
    if audience not in REMIND_AUDIENCES:
        raise HackathonError('Choose seekers or teams.', 'INVALID', field='audience')
    now = timezone.now()
    cooled = now - REMIND_GAP
    recruiting = _recruiting_with_room(hackathon)
    waiting = list(waiting_seekers(hackathon).select_related('user'))
    if audience == 'seekers':
        due = [s for s in waiting if s.created_at <= now - SEEKER_WAITING_AFTER and (s.organizer_reminded_at is None or s.organizer_reminded_at <= cooled)]
        if not recruiting:
            due = []
        for seeker in due:
            _notify_users(
                [seeker.user], 'Teams are looking for members',
                f"{len(recruiting)} team{'s are' if len(recruiting) != 1 else ' is'} recruiting for {hackathon.title}. Open the team finder and ask one to take you in.",
                hackathon, actor,
            )
        TeamSeeker.objects.filter(pk__in=[s.pk for s in due]).update(organizer_reminded_at=now)
    else:
        due = [(team, size) for team, size in recruiting if team.finder_reminded_at is None or team.finder_reminded_at <= cooled]
        if not waiting:
            due = []
        for team, _ in due:
            if team.leader:
                _notify_users(
                    [team.leader], 'People are looking for a team',
                    f"{len(waiting)} participant{'s are' if len(waiting) != 1 else ' is'} looking for a team for {hackathon.title}. Open the team finder and invite someone to {team.name}.",
                    hackathon, actor,
                )
        Team.objects.filter(pk__in=[team.pk for team, _ in due]).update(finder_reminded_at=now)
    _log(actor, 'Reminded Team Finder', hackathon.slug, audience=audience, reminded=len(due))
    return len(due)
