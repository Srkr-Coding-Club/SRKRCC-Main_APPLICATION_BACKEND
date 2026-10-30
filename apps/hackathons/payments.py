"""
The fee paid when a team registers (``Hackathon.payment_stage == REGISTRATION``).

The leader pays outside the platform, then creates the team with the screenshot and the transaction id in one go. The
team starts as PAYMENT_PENDING and holds no seat. An organizer checks the payment: verifying it places the team (or
waitlists it when the hackathon is full), rejecting it tells the leader to contact the club representatives. Only an
organizer can reopen a rejected team or remove it so the leader can start again.
"""
from __future__ import annotations

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone

from apps.audit.utils import log_audit_event
from apps.forms.models import UploadState

from . import access, idea_files, registration
from .errors import HackathonError
from .models import (
    Hackathon, IdeaFile, IdeaFileKind, PaymentStage, PaymentStatus, Team, TeamPayment, TeamStatus, payment_key,
)

STALE_AFTER = timedelta(hours=24)
MAX_TRANSACTION_ID = 60
MAX_REASON = 300
PAYMENT_STATUSES = (TeamStatus.PAYMENT_PENDING, TeamStatus.PAYMENT_REJECTED)
#: Payments whose team no longer exists: kept for the money to be settled, never shown in the queue.
CLOSED_STATUSES = (PaymentStatus.CANCELLED, PaymentStatus.REMOVED)
#: What the queue counts and the analytics report.
QUEUE_STATUSES = (PaymentStatus.PENDING, PaymentStatus.VERIFIED, PaymentStatus.REJECTED)


def required(hackathon: Hackathon) -> bool:
    """Whether creating a team in this hackathon means paying a fee first."""
    return hackathon.payment_stage == PaymentStage.REGISTRATION and bool(hackathon.idea_fee_amount)


def _name(user) -> str:
    return f'{user.first_name} {user.last_name}'.strip() or user.email if user else 'an organizer'


# --- the screenshot, uploaded before the team exists ------------------------------------------------------------

def _own_pending_file(hackathon: Hackathon, user, file_id) -> IdeaFile:
    try:
        return IdeaFile.objects.get(pk=file_id, hackathon=hackathon, team__isnull=True, kind=IdeaFileKind.PAYMENT, uploader=user)
    except Exception:  # noqa: BLE001 - unknown, malformed or someone else's id
        raise HackathonError('That upload was not found.', 'NOT_FOUND', status=404)


def _require_can_pay(hackathon: Hackathon, user) -> None:
    from .models import TeamMember
    from .services import _require_registration_open

    if not required(hackathon):
        raise HackathonError('This hackathon does not ask for a fee when a team is created.', 'PAYMENT_NOT_REQUIRED')
    _require_registration_open(hackathon)
    if TeamMember.objects.filter(hackathon=hackathon, user=user).exists():
        raise HackathonError('You are already in a team for this hackathon.', 'ALREADY_IN_TEAM')


def request_upload(hackathon: Hackathon, user, name, size) -> dict:
    _require_can_pay(hackathon, user)
    return idea_files._issue(hackathon, None, user, IdeaFileKind.PAYMENT, name, size)


def complete_upload(hackathon: Hackathon, user, file_id) -> IdeaFile:
    _require_can_pay(hackathon, user)
    _own_pending_file(hackathon, user, file_id)
    return idea_files._verify(hackathon, None, file_id)


def discard_upload(hackathon: Hackathon, user, file_id) -> None:
    file = _own_pending_file(hackathon, user, file_id)
    with transaction.atomic():
        file = IdeaFile.objects.select_for_update().get(pk=file.pk)
        if file.state not in idea_files._ACTIVE:
            raise HackathonError('That upload was not found.', 'NOT_FOUND', status=404)
        idea_files._discard(file)


# --- creating the team with its payment -------------------------------------------------------------------------

def clean_submission(hackathon: Hackathon, user, data: dict) -> tuple[IdeaFile, str]:
    """The screenshot and transaction id a new team's leader must give, checked. Raises a field error for what is missing."""
    data = data or {}
    reference = ' '.join(str(data.get('transaction_id') or '').split())
    if not reference:
        raise HackathonError('Enter the transaction ID of your payment.', 'INVALID', field='transaction_id')
    if len(reference) > MAX_TRANSACTION_ID:
        raise HackathonError(f'The transaction ID is limited to {MAX_TRANSACTION_ID} characters.', 'INVALID', field='transaction_id')
    file_id = data.get('payment_file_id')
    if not file_id:
        raise HackathonError(f'Upload the screenshot of your payment of ₹{hackathon.idea_fee_amount}.', 'INVALID', field='payment_file_id')
    try:
        file = IdeaFile.objects.select_for_update().get(
            pk=file_id, hackathon=hackathon, team__isnull=True, kind=IdeaFileKind.PAYMENT, uploader=user, state=UploadState.UPLOADED,
        )
    except Exception:  # noqa: BLE001 - unknown, malformed, someone else's or not finished uploading
        raise HackathonError('The payment screenshot is no longer available. Please add it again.', 'FILE_NOT_FOUND', field='payment_file_id')
    if TeamPayment.objects.filter(hackathon=hackathon, transaction_key=payment_key(reference)).exclude(status__in=CLOSED_STATUSES).exists():
        raise HackathonError('This transaction ID has already been used. Check it, or contact the club representatives.', 'TRANSACTION_REUSED', field='transaction_id')
    return file, reference


def existing_submission(hackathon: Hackathon, user, data: dict) -> Team | None:
    """The leader's own pending team when the same payment is sent again (a retry after a dropped connection)."""
    team = Team.objects.filter(hackathon=hackathon, leader=user, status=TeamStatus.PAYMENT_PENDING, payment__submitted_by=user).first()
    reference = payment_key((data or {}).get('transaction_id'))
    if team is not None and reference and team.payment.transaction_key == reference:
        return team
    return None


def attach_payment(team: Team, user, file: IdeaFile, reference: str) -> TeamPayment:
    """Record the payment of a team that was just created, and make its screenshot permanent. Call inside the creating transaction."""
    file.team = team
    file.save(update_fields=['team', 'updated_at'])
    try:
        with transaction.atomic():
            payment = TeamPayment.objects.create(
                team=team, hackathon=team.hackathon, submitted_by=user, amount=team.hackathon.idea_fee_amount,
                transaction_id=reference, screenshot=file, team_name=team.name, leader_name=_name(user),
                leader_email=user.email, leader_phone=(getattr(user, 'phone_number', '') or '')[:30],
            )
    except IntegrityError:
        raise HackathonError('This transaction ID has already been used. Check it, or contact the club representatives.', 'TRANSACTION_REUSED', field='transaction_id')
    idea_files.attach(file, None)
    return payment


# --- the leader changes their mind ------------------------------------------------------------------------------

@transaction.atomic
def cancel_pending(team: Team, actor) -> None:
    """The leader withdraws a team that is still waiting for its payment check: nothing was placed, so it is simply deleted."""
    from .services import _audit, _lock_team, is_leader

    team = _lock_team(team)
    if not is_leader(team, actor):
        from rest_framework.exceptions import PermissionDenied
        raise PermissionDenied('Only the team leader can do this.')
    if team.status != TeamStatus.PAYMENT_PENDING:
        raise HackathonError('This team is not waiting for a payment check.', 'NOT_PENDING')
    payment = team.payment
    _audit(actor, 'Cancelled Team Waiting For Payment Check', team, amount=payment.amount, reference=payment.transaction_id)
    payment.status, payment.reviewed_at = PaymentStatus.CANCELLED, timezone.now()
    payment.save(update_fields=['status', 'reviewed_at', 'updated_at'])
    idea_files.release_all(team.idea_files.all())
    team.delete()


# --- organizers: the payments queue ------------------------------------------------------------------------------

def _ensure_organizer(hackathon: Hackathon, actor) -> None:
    if not access.has_permission(hackathon, actor, 'teams'):
        from rest_framework.exceptions import PermissionDenied
        raise PermissionDenied('Only organizers can check payments.')


def _row(payment: TeamPayment) -> dict:
    team = payment.team
    leader = team.leader
    return {
        'id': payment.id, 'team_id': team.id, 'team_name': team.name, 'team_status': team.status,
        'leader_name': _name(leader) if leader else '', 'leader_email': leader.email if leader else '',
        'leader_phone': (getattr(leader, 'phone_number', '') or '') if leader else '',
        'members': team.memberships.count(), 'amount': payment.amount, 'transaction_id': payment.transaction_id,
        'status': payment.status, 'reason': payment.reason, 'submitted_at': payment.created_at,
        'reviewed_by': _name(payment.reviewed_by) if payment.reviewed_by_id else None, 'reviewed_at': payment.reviewed_at,
        'screenshot': idea_files.serialize(payment.screenshot),
        'problem': team.problem_statement.code if team.problem_statement_id else (team.open_innovation_code if team.is_open_innovation else ''),
    }


def summary(team: Team) -> dict | None:
    """What the team's own members may see of its payment. The organizers' reason is never part of it."""
    payment = TeamPayment.objects.filter(team=team).first()
    if payment is None:
        return None
    return {
        'id': payment.id, 'status': payment.status, 'amount': payment.amount, 'transaction_id': payment.transaction_id,
        'submitted_at': payment.created_at, 'reviewed_at': payment.reviewed_at,
    }


def row_of(hackathon: Hackathon, payment_id) -> dict:
    payment = hackathon.team_payments.select_related('team', 'team__leader', 'team__problem_statement', 'reviewed_by', 'screenshot').get(pk=payment_id)
    return _row(payment)


def queue(hackathon: Hackathon, actor, params=None) -> dict:
    """Every payment with who submitted it, plus how many are in each state. Oldest waiting first."""
    _ensure_organizer(hackathon, actor)
    params = params or {}
    live = hackathon.team_payments.exclude(status__in=CLOSED_STATUSES)
    base = live.select_related('team', 'team__leader', 'team__problem_statement', 'reviewed_by', 'screenshot')
    counts = {row['status']: row['n'] for row in live.values('status').annotate(n=Count('id'))}
    rows = base
    if params.get('status') in PaymentStatus.values:
        rows = rows.filter(status=params['status'])
    search = (params.get('search') or '').strip()
    if search:
        rows = rows.filter(
            Q(team__name__icontains=search) | Q(transaction_id__icontains=search) | Q(team__leader__email__icontains=search)
            | Q(team__leader__first_name__icontains=search) | Q(team__leader__last_name__icontains=search),
        )
    ordered = sorted(rows, key=lambda p: (p.status != PaymentStatus.PENDING, p.created_at, p.id))
    return {
        'counts': {status: counts.get(status, 0) for status in QUEUE_STATUSES},
        'fee_amount': hackathon.idea_fee_amount, 'stage': hackathon.payment_stage,
        'payments': [_row(payment) for payment in ordered],
    }


def screenshot_link(hackathon: Hackathon, user, payment_id, *, inline: bool) -> dict:
    """A 60-second link to the screenshot: the organizers and the team's own leader."""
    payment = hackathon.team_payments.select_related('screenshot', 'team').filter(pk=payment_id).first()
    file = payment.screenshot if payment else None
    if file is None or file.state != UploadState.ATTACHED:
        raise HackathonError('That file was not found.', 'NOT_FOUND', status=404)
    if not (access.has_permission(hackathon, user, 'teams') or payment.team.leader_id == user.id):
        raise HackathonError('That file was not found.', 'NOT_FOUND', status=404)
    log_audit_event(actor=user, action='Viewed Team Payment Proof', target_model='TeamPayment', target_id=payment.pk, details={'hackathon': hackathon.slug, 'team': payment.team.name})
    return idea_files._signed_link(file, inline=inline)


def _locked(hackathon: Hackathon, actor, payment_id) -> tuple[Hackathon, TeamPayment, Team]:
    """The hackathon, the payment and its team, locked in that order so two organizers cannot both act on one payment."""
    _ensure_organizer(hackathon, actor)
    from .services import _lock_team

    hackathon = Hackathon.objects.select_for_update().get(pk=hackathon.pk)
    payment = TeamPayment.objects.select_for_update().filter(pk=payment_id, hackathon=hackathon).first()
    if payment is None or payment.team_id is None:
        raise HackathonError('That payment was not found.', 'NOT_FOUND', status=404)
    return hackathon, payment, _lock_team(payment.team)


def _already(payment: TeamPayment) -> HackathonError:
    who = f' by {_name(payment.reviewed_by)}' if payment.reviewed_by_id else ''
    word = {PaymentStatus.VERIFIED: 'verified', PaymentStatus.REJECTED: 'rejected'}.get(payment.status, 'handled')
    return HackathonError(f'This payment was already {word}{who}.', 'PAYMENT_ALREADY_HANDLED', status=409, payment_status=payment.status)


def _contacts_line(hackathon: Hackathon) -> str:
    names = [c.get('name') or c.get('email') for c in (hackathon.contacts or [])[:3] if isinstance(c, dict) and (c.get('name') or c.get('email'))]
    return f' Contact: {", ".join(names)}.' if names else ''


@transaction.atomic
def verify(hackathon: Hackathon, actor, payment_id) -> TeamPayment:
    """Accept the payment: the team is placed, or waitlisted when the hackathon is full and has a waitlist."""
    from .services import _audit, _notify_after_commit, _recompute_status, team_emails

    hackathon, payment, team = _locked(hackathon, actor, payment_id)
    if payment.status != PaymentStatus.PENDING or team.status != TeamStatus.PAYMENT_PENDING:
        raise _already(payment)
    try:
        waitlisted = registration.should_waitlist_new_team(hackathon)
    except HackathonError:
        raise HackathonError('No seats are left and the waitlist is off, so this team cannot be placed. Reject it or make room first.', 'NO_SEATS')
    payment.status, payment.reviewed_by, payment.reviewed_at, payment.reason = PaymentStatus.VERIFIED, actor, timezone.now(), ''
    payment.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'reason', 'updated_at'])
    team.status, team.is_waitlisted = TeamStatus.FORMING, waitlisted
    team.save(update_fields=['status', 'is_waitlisted', 'updated_at'])
    _recompute_status(team)
    if team.status == TeamStatus.FORMING:
        team_emails.notify_team('team_created', team)
    _audit(actor, 'Verified Team Payment', team, amount=payment.amount, reference=payment.transaction_id, waitlisted=waitlisted)
    leader = [team.leader] if team.leader_id else []
    if waitlisted:
        _notify_after_commit(
            leader, f'{team.name} is on the waitlist',
            f'Your payment was verified. {hackathon.title} is full, so your team is queued and will be placed automatically if a spot opens up.', hackathon, actor,
        )
    else:
        _notify_after_commit(leader, f'{team.name}: payment verified', f"Your payment for '{team.name}' was verified. Your team is registered for {hackathon.title}.", hackathon, actor)
    return payment


@transaction.atomic
def reject(hackathon: Hackathon, actor, payment_id, reason: str = '') -> TeamPayment:
    """Turn the payment down. The leader is told to contact the club; the reason stays with the organizers."""
    from .services import _audit, _notify_after_commit

    hackathon, payment, team = _locked(hackathon, actor, payment_id)
    if payment.status != PaymentStatus.PENDING or team.status != TeamStatus.PAYMENT_PENDING:
        raise _already(payment)
    reason = ' '.join((reason or '').split())
    if len(reason) > MAX_REASON:
        raise HackathonError(f'Keep the reason under {MAX_REASON} characters.', 'INVALID', field='reason')
    payment.status, payment.reviewed_by, payment.reviewed_at, payment.reason = PaymentStatus.REJECTED, actor, timezone.now(), reason
    payment.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'reason', 'updated_at'])
    team.status = TeamStatus.PAYMENT_REJECTED
    team.save(update_fields=['status', 'updated_at'])
    _audit(actor, 'Rejected Team Payment', team, amount=payment.amount, reference=payment.transaction_id, reason=reason)
    _notify_after_commit(
        [team.leader] if team.leader_id else [], f'{team.name}: payment could not be verified',
        f"Your payment for '{team.name}' could not be verified. Please contact the club representatives.{_contacts_line(hackathon)}", hackathon, actor,
    )
    return payment


@transaction.atomic
def reopen(hackathon: Hackathon, actor, payment_id) -> TeamPayment:
    """Put a rejected payment back in the queue, for example after the leader sorted it out with the club."""
    from .services import _audit, _notify_after_commit

    hackathon, payment, team = _locked(hackathon, actor, payment_id)
    if payment.status != PaymentStatus.REJECTED or team.status != TeamStatus.PAYMENT_REJECTED:
        raise HackathonError('Only a rejected payment can be reopened.', 'NOT_REJECTED', status=409, payment_status=payment.status)
    payment.status, payment.reviewed_by, payment.reviewed_at, payment.reason = PaymentStatus.PENDING, None, None, ''
    payment.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'reason', 'updated_at'])
    team.status = TeamStatus.PAYMENT_PENDING
    team.save(update_fields=['status', 'updated_at'])
    _audit(actor, 'Reopened Team Payment', team, amount=payment.amount, reference=payment.transaction_id)
    _notify_after_commit([team.leader] if team.leader_id else [], f'{team.name}: payment is being checked again', f"The organizers are checking the payment for '{team.name}' again.", hackathon, actor)
    return payment


@transaction.atomic
def remove(hackathon: Hackathon, actor, payment_id) -> None:
    """Delete a team whose payment is pending or rejected so its leader can start again (the transaction id is freed)."""
    from .services import _audit, _notify_after_commit

    hackathon, payment, team = _locked(hackathon, actor, payment_id)
    if payment.status == PaymentStatus.VERIFIED or team.status not in PAYMENT_STATUSES:
        raise HackathonError('This team is already placed. Change its status from the teams page instead.', 'ALREADY_PLACED', status=409, payment_status=payment.status)
    _audit(actor, 'Removed Team With Unverified Payment', team, amount=payment.amount, reference=payment.transaction_id, payment_status=payment.status)
    payment.status, payment.reviewed_by, payment.reviewed_at = PaymentStatus.REMOVED, actor, timezone.now()
    payment.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'updated_at'])
    leader = team.leader
    name = team.name
    idea_files.release_all(team.idea_files.all())
    team.delete()
    _notify_after_commit(
        [leader] if leader else [], f'{name}: registration removed',
        f"Your registration for '{name}' was removed. You can create a team again, or contact the club representatives.{_contacts_line(hackathon)}", hackathon, actor,
    )


# --- telling the organizers a payment is still waiting -----------------------------------------------------------

def reviewers(hackathon: Hackathon) -> list:
    """The people who can check payments: staff holding the teams permission, or the site's admins when the hackathon has no such staff."""
    from django.contrib.auth import get_user_model

    people = [
        staff.user for staff in hackathon.staff.select_related('user') if access.has_permission(hackathon, staff.user, 'teams')
    ]
    return people or list(get_user_model().objects.filter(role='ADMIN', is_active=True))


def remind_organizers(now=None) -> int:
    """
    One digest per hackathon for registration payments that have waited more than a day, repeated every day until they
    are handled. Returns how many payments were mentioned. Safe to run as often as you like (``remind_pending_payments``).
    """
    from .models import HackathonStatus
    from .services import _notify_users

    now = now or timezone.now()
    mentioned = 0
    for hackathon in Hackathon.objects.filter(status=HackathonStatus.LIVE, payment_stage=PaymentStage.REGISTRATION):
        due = hackathon.team_payments.filter(status=PaymentStatus.PENDING, created_at__lte=now - STALE_AFTER).filter(
            Q(organizer_reminded_at__isnull=True) | Q(organizer_reminded_at__lte=now - STALE_AFTER),
        )
        ids = list(due.values_list('pk', flat=True))
        if not ids:
            continue
        _notify_users(
            reviewers(hackathon), f'{hackathon.title}: {len(ids)} payment{"s" if len(ids) != 1 else ""} waiting for a check',
            f'{len(ids)} registration payment{"s have" if len(ids) != 1 else " has"} been waiting for more than a day. Open the Payments tab to verify or reject '
            f'{"them" if len(ids) != 1 else "it"}: those teams hold no place until you do.', hackathon,
        )
        hackathon.team_payments.filter(pk__in=ids).update(organizer_reminded_at=now)
        mentioned += len(ids)
    return mentioned
