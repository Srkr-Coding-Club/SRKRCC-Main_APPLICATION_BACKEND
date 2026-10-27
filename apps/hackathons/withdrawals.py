"""
Stops people from creating and withdrawing teams over and over.

* A person may create ``MAX_SELF_CREATED_TEAMS`` teams per hackathon (organizers can allow more) and must wait
  ``WITHDRAW_COOLDOWN`` after withdrawing one before creating the next.
* Withdrawing needs a reason. A team that is still forming (or waitlisted) is withdrawn at once; one that holds a place
  (registered, or already in a round) becomes a request the organizers approve or deny.
* Organizers also see who keeps withdrawing.
"""
from __future__ import annotations

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Count
from django.utils import timezone

from apps.audit.utils import log_audit_event

from .errors import HackathonError
from .models import (
    Hackathon, InviteStatus, Team, TeamAllowance, TeamStatus, WithdrawalRequest, WithdrawalStatus,
)

MAX_SELF_CREATED_TEAMS = 2
WITHDRAW_COOLDOWN = timedelta(minutes=10)
MAX_REASON_LENGTH = 300
RECENT_DECISIONS = 20


def _name(user) -> str:
    return f'{user.first_name} {user.last_name}'.strip() or user.email


def clean_reason(reason) -> str:
    return ' '.join(str(reason or '').split())[:MAX_REASON_LENGTH]


def needs_approval(team: Team) -> bool:
    """A team that holds a place (registered, or in a round) cannot just disappear: an organizer decides."""
    if team.status == TeamStatus.REGISTERED and not team.is_waitlisted:
        return True
    return team.round_entries.exists()


def allowance_for(hackathon: Hackathon, user) -> int:
    allowance = TeamAllowance.objects.filter(hackathon=hackathon, user=user).first()
    return allowance.extra if allowance else 0


def check_can_create(hackathon: Hackathon, user) -> None:
    """Raise unless ``user`` may create another team of their own in this hackathon."""
    limit = MAX_SELF_CREATED_TEAMS + allowance_for(hackathon, user)
    created = Team.objects.filter(hackathon=hackathon, created_by=user).count()
    if created >= limit:
        raise HackathonError(
            f'You have already created {created} team{"s" if created != 1 else ""} for this hackathon, which is the most one person can. '
            'Ask the organizers if you need another.',
            'TEAM_LIMIT_REACHED', limit=limit,
        )
    last = Team.objects.filter(hackathon=hackathon, withdrawn_by=user, withdrawn_at__isnull=False).order_by('-withdrawn_at').first()
    if last and timezone.now() < last.withdrawn_at + WITHDRAW_COOLDOWN:
        retry_at = last.withdrawn_at + WITHDRAW_COOLDOWN
        minutes = max(1, int((retry_at - timezone.now()).total_seconds() // 60) + 1)
        raise HackathonError(
            f'You withdrew a team a moment ago. You can create a new one in {minutes} minute{"s" if minutes != 1 else ""}.',
            'WITHDRAW_COOLDOWN', retry_at=retry_at,
        )


def _organizers_to_tell(hackathon: Hackathon):
    from .access import my_permissions

    users = []
    for staff in hackathon.staff.select_related('user'):
        if 'teams' in my_permissions(hackathon, staff.user):
            users.append(staff.user)
    return users


def _finish_withdrawal(team: Team, member, actor, reason: str) -> Team:
    """The leader leaves and the team is withdrawn (kept, not deleted, so organizers keep a record)."""
    from .services import WaitlistService

    team.memberships.filter(user=member).delete()
    team.leader = None
    team.status = TeamStatus.WITHDRAWN
    team.status_reason = reason
    team.withdrawn_at = timezone.now()
    team.withdrawn_by = member
    team.save(update_fields=['leader', 'status', 'status_reason', 'withdrawn_at', 'withdrawn_by', 'updated_at'])
    team.invites.filter(status=InviteStatus.PENDING).update(status=InviteStatus.CANCELLED, responded_at=timezone.now())
    log_audit_event(
        actor=actor, action='Withdrew Hackathon Team', target_model='HackathonTeam', target_id=team.pk,
        details={'team': team.name, 'hackathon': team.hackathon.slug, 'reason': reason},
    )
    WaitlistService.promote_after_commit(team.hackathon)
    return team


class WithdrawalService:

    @staticmethod
    @transaction.atomic
    def withdraw(team: Team, user, reason) -> tuple[Team, WithdrawalRequest | None]:
        """
        The sole leader gives up the team. Returns ``(team, None)`` when it is withdrawn at once and
        ``(team, request)`` when an organizer has to approve it first.
        """
        reason = clean_reason(reason)
        if not reason:
            raise HackathonError('Tell the organizers why you are withdrawing.', 'REASON_REQUIRED', field='reason')
        if not needs_approval(team):
            return _finish_withdrawal(team, user, user, reason), None
        try:
            with transaction.atomic():
                request = WithdrawalRequest.objects.create(team=team, requested_by=user, reason=reason)
        except IntegrityError:
            raise HackathonError('You have already asked to withdraw this team. The organizers will decide.', 'WITHDRAWAL_PENDING')
        log_audit_event(
            actor=user, action='Requested Hackathon Team Withdrawal', target_model='HackathonTeam', target_id=team.pk,
            details={'team': team.name, 'hackathon': team.hackathon.slug, 'reason': reason},
        )
        from .services import _notify_after_commit

        _notify_after_commit(
            _organizers_to_tell(team.hackathon), f'{team.name} wants to withdraw',
            f"{_name(user)} asked to withdraw team '{team.name}': {reason}", team.hackathon, user,
        )
        return team, request

    @staticmethod
    @transaction.atomic
    def cancel(team: Team, user) -> WithdrawalRequest:
        request = team.withdrawal_requests.select_for_update().filter(status=WithdrawalStatus.PENDING).first()
        if request is None:
            raise HackathonError('There is no withdrawal request to cancel.', 'NO_WITHDRAWAL_PENDING')
        request.status = WithdrawalStatus.CANCELLED
        request.decided_at = timezone.now()
        request.save(update_fields=['status', 'decided_at', 'updated_at'])
        return request

    @staticmethod
    @transaction.atomic
    def decide(request: WithdrawalRequest, actor, approve: bool, note='') -> WithdrawalRequest:
        from .services import _notify_after_commit

        request = WithdrawalRequest.objects.select_for_update().select_related('team__hackathon', 'requested_by').get(pk=request.pk)
        if request.status != WithdrawalStatus.PENDING:
            raise HackathonError('That request has already been decided.', 'ALREADY_DECIDED')
        team = request.team
        note = clean_reason(note)
        if approve:
            if team.status not in (TeamStatus.FORMING, TeamStatus.REGISTERED):
                raise HackathonError('That team is no longer active, so there is nothing to withdraw.', 'TEAM_INACTIVE')
            if team.memberships.exclude(user=request.requested_by).exists():
                raise HackathonError(
                    'The team has other members now. They must leave or be removed first, or deny this request.', 'TEAM_NOT_EMPTY',
                )
            _finish_withdrawal(team, request.requested_by, actor, request.reason)
        request.status = WithdrawalStatus.APPROVED if approve else WithdrawalStatus.DENIED
        request.decided_by = actor
        request.decided_at = timezone.now()
        request.decision_note = note
        request.save(update_fields=['status', 'decided_by', 'decided_at', 'decision_note', 'updated_at'])
        log_audit_event(
            actor=actor, action=f"{'Approved' if approve else 'Denied'} Hackathon Team Withdrawal", target_model='HackathonTeam',
            target_id=team.pk, details={'team': team.name, 'hackathon': team.hackathon.slug, 'requested_by': request.requested_by.email, 'note': note},
        )
        wording = 'was approved. Your team is withdrawn.' if approve else 'was denied, so your team stays.'
        _notify_after_commit(
            [request.requested_by], f'{team.name}: withdrawal {"approved" if approve else "denied"}',
            f"Your request to withdraw '{team.name}' {wording}" + (f' Note: {note}' if note else ''), team.hackathon, actor,
        )
        return request

    @staticmethod
    def close_pending(team: Team) -> None:
        """An organizer changed the team's status themselves, so a waiting request no longer applies."""
        team.withdrawal_requests.filter(status=WithdrawalStatus.PENDING).update(
            status=WithdrawalStatus.CANCELLED, decided_at=timezone.now(),
        )

    @staticmethod
    def latest_for(team: Team) -> WithdrawalRequest | None:
        return team.withdrawal_requests.order_by('-created_at', '-id').first()

    @staticmethod
    def grant_allowance(hackathon: Hackathon, actor, user, extra: int, reason='') -> TeamAllowance:
        extra = max(1, min(int(extra), 10))
        allowance, _ = TeamAllowance.objects.update_or_create(
            hackathon=hackathon, user=user, defaults={'extra': extra, 'reason': clean_reason(reason)[:200], 'granted_by': actor},
        )
        log_audit_event(
            actor=actor, action='Allowed Extra Hackathon Teams', target_model='Hackathon', target_id=hackathon.slug,
            details={'user': user.email, 'extra': extra, 'reason': allowance.reason},
        )
        return allowance

    @staticmethod
    def overview(hackathon: Hackathon) -> dict:
        """What the organizers see: requests waiting, recent decisions, and people who keep creating or withdrawing teams."""
        requests = WithdrawalRequest.objects.filter(team__hackathon=hackathon).select_related('team', 'requested_by', 'decided_by')
        pending = [_request_row(r) for r in requests.filter(status=WithdrawalStatus.PENDING).order_by('created_at')]
        decided = [_request_row(r) for r in requests.exclude(status=WithdrawalStatus.PENDING).order_by('-decided_at', '-id')[:RECENT_DECISIONS]]
        return {
            'pending': pending, 'decided': decided, 'repeaters': _repeaters(hackathon),
            'limits': {'max_teams': MAX_SELF_CREATED_TEAMS, 'cooldown_minutes': int(WITHDRAW_COOLDOWN.total_seconds() // 60)},
        }


def _request_row(request: WithdrawalRequest) -> dict:
    team = request.team
    return {
        'id': request.id, 'status': request.status, 'reason': request.reason, 'created_at': request.created_at,
        'team_id': team.id, 'team_name': team.name, 'team_status': team.status, 'is_waitlisted': team.is_waitlisted,
        'requested_by': _name(request.requested_by), 'requested_by_email': request.requested_by.email,
        'decided_by': _name(request.decided_by) if request.decided_by else None, 'decided_at': request.decided_at,
        'decision_note': request.decision_note,
    }


def _repeaters(hackathon: Hackathon) -> list[dict]:
    """People who created two or more teams here or withdrew more than one, with how many and whether they are at the limit."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    created = {r['created_by']: r['n'] for r in Team.objects.filter(hackathon=hackathon, created_by__isnull=False).values('created_by').annotate(n=Count('id'))}
    withdrawn = {r['withdrawn_by']: r['n'] for r in Team.objects.filter(hackathon=hackathon, withdrawn_by__isnull=False).values('withdrawn_by').annotate(n=Count('id'))}
    allowances = {a.user_id: a.extra for a in TeamAllowance.objects.filter(hackathon=hackathon)}
    ids = {uid for uid, n in created.items() if n >= 2} | {uid for uid, n in withdrawn.items() if n >= 2}
    rows = []
    for user in User.objects.filter(pk__in=ids).order_by('first_name', 'email'):
        extra = allowances.get(user.id, 0)
        rows.append({
            'user_id': user.id, 'name': _name(user), 'email': user.email, 'teams_created': created.get(user.id, 0),
            'teams_withdrawn': withdrawn.get(user.id, 0), 'extra_allowed': extra,
            'at_limit': created.get(user.id, 0) >= MAX_SELF_CREATED_TEAMS + extra,
        })
    return rows
