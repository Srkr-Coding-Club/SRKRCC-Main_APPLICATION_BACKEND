"""Becoming a club member: a person asks, an organizer approves (which gives the next Club ID) or declines with a reason."""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.html import escape

from apps.accounts.models import ClubIdReservation, ClubMembershipRequest, MembershipRequestStatus as Status, MembershipStatus, ReservationStatus, User, UserRole
from apps.accounts.services import club_id_registry as registry
from apps.accounts.services import email_verification as verification
from apps.accounts.services.club_id_registry import ClubIdRegistryError

logger = logging.getLogger(__name__)

#: After a request is not approved, the person waits this long before asking again (withdrawing a request has no wait).
REJECTED_COOLDOWN = timedelta(days=7)
MAX_MESSAGE = 300
BULK_LIMIT = 100
PROFILE_FIELDS = (
    ('first_name', 'First name'), ('last_name', 'Last name'), ('roll_number', 'Roll number'), ('branch', 'Branch'), ('year', 'Year'), ('phone_number', 'Phone number'),
)


class MembershipError(Exception):
    def __init__(self, message: str, code: str, status: int = 400, **extra):
        super().__init__(message)
        self.message, self.code, self.status, self.extra = message, code, status, extra


def missing_profile(user: User) -> list[str]:
    """What the person still has to fill in before organizers can say yes."""
    missing = [label for field, label in PROFILE_FIELDS if not getattr(user, field)]
    if not user.college_id and not (user.other_college_name or '').strip():
        missing.append('College')
    return missing


def latest(user: User) -> ClubMembershipRequest | None:
    return user.membership_requests.order_by('-created_at', '-id').first()


def blockers(user: User, now=None) -> list[dict]:
    """Why this person cannot ask right now (empty = they can). Each has a code, a sentence and where to fix it."""
    now = now or timezone.now()
    if user.club_id:
        return [{'code': 'ALREADY_MEMBER', 'message': f'You are already a member. Your Club ID is {user.club_id}.'}]
    found = []
    if user.membership_status == MembershipStatus.SUSPENDED:
        found.append({'code': 'SUSPENDED', 'message': 'Your membership is suspended. Contact the club representatives.'})
    if verification.mode() != 'off' and not verification.is_verified(user):
        found.append({'code': 'EMAIL_NOT_VERIFIED', 'message': 'Verify your email address first.', 'fix': '/profile'})
    if lacking := missing_profile(user):
        found.append({'code': 'PROFILE_INCOMPLETE', 'message': f"Complete your profile: {', '.join(lacking)}.", 'missing': lacking, 'fix': '/profile'})
    last = latest(user)
    if last and last.status == Status.PENDING:
        found.append({'code': 'PENDING', 'message': 'Your request is waiting for an organizer.'})
    elif last and last.status == Status.REJECTED and last.decided_at and now < last.decided_at + REJECTED_COOLDOWN:
        found.append({'code': 'COOLDOWN', 'message': 'You can ask again after a short wait.', 'until': last.decided_at + REJECTED_COOLDOWN})
    return found


def status_for(user: User) -> dict:
    last = latest(user)
    stop = blockers(user)
    return {
        'club_id': user.club_id or None, 'role': user.role, 'request': last, 'can_request': not stop, 'blockers': stop,
    }


def _reviewers() -> list[User]:
    return list(User.objects.filter(is_active=True, role__in=(UserRole.ADMIN, UserRole.CLUB_LEAD)))


def _notify(users, title: str, message: str, link_url: str, actor=None) -> None:
    """In-app notification that can never fail the real action."""
    users = [u for u in users if u is not None]
    if not users:
        return
    try:
        from apps.notifications.models import NotificationCategory, NotificationType
        from apps.notifications.services import NotificationService
        with transaction.atomic():
            NotificationService.create_bulk_notifications(users, title=title, message=message, type=NotificationType.INFO, category=NotificationCategory.SYSTEM, link_url=link_url, created_by=actor)
    except Exception:
        logger.exception('Could not create club membership notification %r', title)


def _email(user_id: int, subject: str, text: str, html: str) -> None:
    from apps.core.tasks import run_in_background

    def send():
        from apps.core.services.email_service import EmailNotificationService
        person = User.objects.filter(pk=user_id).first()
        if person:
            EmailNotificationService.send_email(person.email, subject, text, html)

    transaction.on_commit(lambda: run_in_background(send))


def _name(user: User) -> str:
    return f'{user.first_name} {user.last_name}'.strip() or user.email.split('@')[0]


def _clean_message(text) -> str:
    return ' '.join(str(text or '').split())[:MAX_MESSAGE]


@transaction.atomic
def request_membership(user: User, message: str = '') -> tuple[ClubMembershipRequest, bool]:
    """The person asks. Returns (request, auto_approved): an id reserved for their email is attached at once, no queue."""
    user = User.objects.select_for_update().get(pk=user.pk)
    stop = blockers(user)
    if stop:
        first = stop[0]
        raise MembershipError(first['message'], first['code'], 409 if first['code'] in ('PENDING', 'ALREADY_MEMBER') else 400, blockers=[b['code'] for b in stop])
    reserved = ClubIdReservation.objects.filter(reserved_for_email__iexact=user.email, status=ReservationStatus.RESERVED).first()
    try:
        entry = ClubMembershipRequest.objects.create(user=user, message=_clean_message(message))
    except IntegrityError as exc:
        raise MembershipError('Your request is already waiting for an organizer.', 'PENDING', 409) from exc
    if reserved and registry.attach_reserved(user):
        return _approved(entry, None, 'An id was already reserved for this email.'), True
    link = '/admin/club-ids?tab=requests'
    _notify(_reviewers(), f'{_name(user)} wants to join the club', f'{_name(user)} ({user.email}) asked to become a club member.', link, user)
    return entry, False


def _become_member(user: User) -> None:
    changed = ['updated_at']
    if user.role == UserRole.NON_AFFILIATE:
        user.role = UserRole.AFFILIATE
        changed.append('role')
    if user.membership_status != MembershipStatus.ACTIVE:
        user.membership_status = MembershipStatus.ACTIVE
        changed.append('membership_status')
    if not user.registered_at:
        user.registered_at = timezone.now()
        changed.append('registered_at')
    user.save(update_fields=changed)


def _approved(entry: ClubMembershipRequest, actor, note: str) -> ClubMembershipRequest:
    user = entry.user
    user.refresh_from_db()
    _become_member(user)
    entry.status, entry.club_id, entry.decided_by, entry.decided_at, entry.note = Status.APPROVED, user.club_id or '', actor, timezone.now(), note[:300]
    entry.save(update_fields=['status', 'club_id', 'decided_by', 'decided_at', 'note', 'updated_at'])
    club_id = entry.club_id
    _notify([user], 'You are a club member', f'Welcome to SRKR Coding Club. Your Club ID is {club_id}.', '/profile', actor)
    _email(user.pk, 'Welcome to SRKR Coding Club', f'Hi {_name(user)},\n\nYour club membership is approved. Your Club ID is {club_id}.\n',
           f'<p>Hi {escape(_name(user))},</p><p>Your club membership is approved. Your Club ID is <strong>{escape(club_id)}</strong>.</p>')
    from apps.celebrations import services as celebrations
    celebrations.award_after_commit([user], 'account.club_member', f'club-id:{club_id}', link_url='/profile', club_id=club_id)
    return entry


def _pending(request_id) -> ClubMembershipRequest:
    entry = ClubMembershipRequest.objects.select_for_update(of=('self',)).select_related('user').filter(pk=request_id).first()
    if entry is None:
        raise MembershipError('That request was not found.', 'NOT_FOUND', 404)
    if entry.status != Status.PENDING:
        raise MembershipError(f'This request was already {entry.get_status_display().lower()}.', 'NOT_PENDING', 409, status_now=entry.status)
    return entry


@transaction.atomic
def approve(actor, request_id) -> ClubMembershipRequest:
    """Give the person the next Club ID, make them an affiliate, tell them and celebrate."""
    entry = _pending(request_id)
    user = entry.user
    if user.club_id:
        raise MembershipError(f'{user.email} already holds Club ID {user.club_id}. Withdraw or reject this request.', 'ALREADY_MEMBER', 409)
    try:
        registry.allocate_for(actor, user.pk, reason='Club membership approved')
    except ClubIdRegistryError as exc:
        raise MembershipError(exc.message, exc.code, exc.status) from exc
    return _approved(entry, actor, '')


@transaction.atomic
def reject(actor, request_id, reason: str) -> ClubMembershipRequest:
    reason = _clean_message(reason)
    if not reason:
        raise MembershipError('Give the person a reason: they are shown it.', 'REASON_REQUIRED')
    entry = _pending(request_id)
    entry.status, entry.decided_by, entry.decided_at, entry.note = Status.REJECTED, actor, timezone.now(), reason
    entry.save(update_fields=['status', 'decided_by', 'decided_at', 'note', 'updated_at'])
    until = (entry.decided_at + REJECTED_COOLDOWN).strftime('%d %b %Y')
    _notify([entry.user], 'Your club membership request was not approved', f'{reason} You can ask again after {until}.', '/profile', actor)
    _email(entry.user.pk, 'Your SRKR Coding Club membership request', f'Hi {_name(entry.user)},\n\nYour membership request was not approved this time: {reason}\nYou can ask again after {until}.\n',
           f'<p>Hi {escape(_name(entry.user))},</p><p>Your membership request was not approved this time: {escape(reason)}</p><p>You can ask again after {escape(until)}.</p>')
    return entry


@transaction.atomic
def cancel(user: User) -> ClubMembershipRequest:
    entry = ClubMembershipRequest.objects.select_for_update(of=('self',)).filter(user=user, status=Status.PENDING).first()
    if entry is None:
        raise MembershipError('You have no request waiting.', 'NOT_PENDING', 409)
    entry.status, entry.decided_at = Status.CANCELLED, timezone.now()
    entry.save(update_fields=['status', 'decided_at', 'updated_at'])
    return entry


def bulk_approve(actor, ids: list) -> dict:
    """Approve each listed request on its own: one that cannot be approved is reported and the rest go ahead."""
    ids = list(dict.fromkeys(ids))
    if not ids or len(ids) > BULK_LIMIT:
        raise MembershipError(f'Choose between 1 and {BULK_LIMIT} requests.', 'INVALID')
    done, failed = 0, []
    for request_id in ids:
        try:
            approve(actor, request_id)
            done += 1
        except MembershipError as exc:
            failed.append({'id': request_id, 'error': exc.message, 'code': exc.code})
    return {'done': done, 'failed': failed}
