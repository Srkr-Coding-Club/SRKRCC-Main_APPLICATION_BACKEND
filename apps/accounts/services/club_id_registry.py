"""The Club ID registry: who holds which id, what is held back, what has been retired, and every change, with its reason.

Every write of `User.club_id` (sign-up, an admin editing a member, an import, this module) is recorded by the signal handlers below.
The operations here are the admin's controls: allocate, assign, move, clear, reserve and move a counter.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import (
    ClubIdEvent, ClubIdEventKind, ClubIdReservation, ClubIDSequence, ReservationStatus, User, UserRole,
)
from apps.accounts.services.club_id_service import ClubIDService, ClubIdError, InvalidClubIdError


class ClubIdRegistryError(ClubIdError):
    """A refusal with a machine-readable code and an HTTP status for the API."""

    def __init__(self, message: str, code: str, status: int = 400):
        super().__init__(message)
        self.message, self.code, self.status = message, code, status


_context: ContextVar[dict | None] = ContextVar('club_id_context', default=None)
MAX_REASON = 300
HOLD_ATTEMPTS = 5000


@contextmanager
def recording(*, actor=None, reason: str = '', kind: str = '', other_email: str = '', details: dict | None = None):
    """What the signal should write for the change made inside the block (who did it, why, and which kind of change it is)."""
    token = _context.set({'actor': actor, 'reason': clean_reason(reason), 'kind': kind, 'other_email': other_email, 'details': details or {}})
    try:
        yield
    finally:
        _context.reset(token)


def clean_reason(reason) -> str:
    return ' '.join(str(reason or '').split())[:MAX_REASON]


def canonical(club_id) -> str:
    """The id in its one spelling, or a clear refusal."""
    try:
        return ClubIDService.parse_club_id(str(club_id or '').strip())['canonical_id']
    except InvalidClubIdError as exc:
        raise ClubIdRegistryError(str(exc), 'INVALID_CLUB_ID') from exc


# -- reading -------------------------------------------------------------------------------------------------------------

def holder_of(club_id: str, exclude_user_id: int | None = None) -> User | None:
    found = User.objects.filter(club_id__iexact=club_id)
    if exclude_user_id:
        found = found.exclude(pk=exclude_user_id)
    return found.first()


def active_reservation(club_id: str) -> ClubIdReservation | None:
    return ClubIdReservation.objects.filter(club_id__iexact=club_id, status=ReservationStatus.RESERVED).first()


def is_retired(club_id: str) -> bool:
    return ClubIdEvent.objects.filter(club_id__iexact=club_id, kind=ClubIdEventKind.CLEARED).exists()


def why_unavailable(club_id: str, *, email: str = '', for_user_id: int | None = None, allow_retired: bool = False) -> str:
    """'' when the id may be given to this person, otherwise the sentence saying why not."""
    holder = holder_of(club_id, exclude_user_id=for_user_id)
    if holder:
        return f"Club ID '{club_id}' is already assigned to {holder.email}."
    reservation = active_reservation(club_id)
    if reservation and (not reservation.reserved_for_email or reservation.reserved_for_email != (email or '').strip().lower()):
        who = reservation.reserved_for_email or 'someone, held back by an organizer'
        return f"Club ID '{club_id}' is reserved for {who}."
    if not allow_retired and is_retired(club_id):
        return f"Club ID '{club_id}' was retired when it was cleared from a member, so it is not given out again."
    return ''


def ensure_assignable(club_id: str, *, email: str = '', for_user_id: int | None = None, allow_retired: bool = False) -> None:
    problem = why_unavailable(club_id, email=email, for_user_id=for_user_id, allow_retired=allow_retired)
    if problem:
        raise ClubIdRegistryError(problem, 'CLUB_ID_UNAVAILABLE', 409)


def next_free_sequence(prefix: str, year: int, start: int) -> int:
    """The first sequence from `start` whose id is not held, reserved or retired (a reserved id above the counter is skipped, not reused)."""
    yy = year % 100
    for sequence in range(start, start + HOLD_ATTEMPTS):
        candidate = f"{yy:02d}{prefix}{sequence:03d}"
        if not why_unavailable(candidate):
            return sequence
    raise ClubIdRegistryError('No free Club ID could be found. Check the counter.', 'NO_FREE_ID', 500)


# -- recording (signals) ---------------------------------------------------------------------------------------------------

def record(kind: str, club_id: str = '', *, user=None, other_email: str = '', actor=None, reason: str = '', details: dict | None = None) -> ClubIdEvent:
    return ClubIdEvent.objects.create(
        club_id=club_id, kind=kind, user=user, user_email=getattr(user, 'email', '') or '', other_email=other_email,
        actor=actor, actor_email=getattr(actor, 'email', '') or '', reason=clean_reason(reason), details=details or {},
    )


def remember_previous(instance: User, update_fields) -> None:
    """pre_save: note the stored Club ID so post_save can tell what changed (only when the id could have changed)."""
    instance._previous_club_id = None
    if instance.pk and (update_fields is None or 'club_id' in update_fields):
        instance._previous_club_id = User.objects.filter(pk=instance.pk).values_list('club_id', flat=True).first() or ''
    elif not instance.pk:
        instance._previous_club_id = ''


def log_change(instance: User, created: bool, update_fields) -> None:
    """post_save: write the event for an id that was set, changed or removed, and claim a matching reservation."""
    previous = getattr(instance, '_previous_club_id', None)
    if previous is None:
        return
    current = instance.club_id or ''
    if previous == current:
        return
    context = _context.get() or {}
    actor, reason, details = context.get('actor'), context.get('reason', ''), context.get('details') or {}
    other = context.get('other_email', '')
    if current:
        kind = context.get('kind') or (ClubIdEventKind.ASSIGNED if not previous else ClubIdEventKind.CHANGED)
        record(kind, current, user=instance, other_email=other, actor=actor, reason=reason, details={**details, **({'previous': previous} if previous else {})})
        _claim_reservation(instance, current, actor)
    if previous and not current:
        record(context.get('kind') or ClubIdEventKind.CLEARED, previous, user=instance, other_email=other, actor=actor, reason=reason, details=details)
    elif previous and current:
        record(ClubIdEventKind.REASSIGNED_OUT, previous, user=instance, actor=actor, reason=reason, details={**details, 'replaced_by': current})


def _claim_reservation(user: User, club_id: str, actor) -> None:
    reservation = active_reservation(club_id)
    if reservation is None:
        return
    reservation.status, reservation.claimed_by, reservation.claimed_at = ReservationStatus.CLAIMED, user, timezone.now()
    reservation.save(update_fields=['status', 'claimed_by', 'claimed_at', 'updated_at'])
    record(ClubIdEventKind.CLAIMED, club_id, user=user, actor=actor, reason='Attached to the person it was reserved for')


def attach_reserved(user: User) -> bool:
    """A person registering with an email an id was reserved for gets that id (the reservation is then claimed)."""
    if user.club_id:
        return False
    reservation = ClubIdReservation.objects.filter(reserved_for_email__iexact=user.email, status=ReservationStatus.RESERVED).first()
    if reservation is None or holder_of(reservation.club_id):
        return False
    with recording(kind=ClubIdEventKind.ASSIGNED, reason='Reserved for this email address'):
        user.club_id = reservation.club_id
        user.save(update_fields=['club_id', 'updated_at'])
    return True


# -- the admin's controls ----------------------------------------------------------------------------------------------------

def _locked_user(user_id) -> User:
    user = User.objects.select_for_update().filter(pk=user_id).first()
    if user is None:
        raise ClubIdRegistryError('That person was not found.', 'NOT_FOUND', 404)
    return user


def _need_reason(reason) -> str:
    reason = clean_reason(reason)
    if not reason:
        raise ClubIdRegistryError('Give a reason: it is kept in the history.', 'REASON_REQUIRED')
    return reason


@transaction.atomic
def allocate_for(actor, user_id, *, reason: str, year: int | None = None) -> User:
    """Give a person without an id the next number from the counter."""
    reason = _need_reason(reason)
    user = _locked_user(user_id)
    if user.club_id:
        raise ClubIdRegistryError(f'{user.email} already has Club ID {user.club_id}.', 'ALREADY_HAS_ID', 409)
    club_id = ClubIDService.allocate_next_club_id(year=year)
    with recording(actor=actor, reason=reason, kind=ClubIdEventKind.ISSUED):
        user.club_id = club_id
        user.save(update_fields=['club_id', 'updated_at'])
    return user


@transaction.atomic
def assign(actor, user_id, club_id: str, *, reason: str, allow_retired: bool = False) -> User:
    """Give a person without an id a particular one (an id from the old records, for instance)."""
    reason = _need_reason(reason)
    club_id = canonical(club_id)
    user = _locked_user(user_id)
    if user.club_id:
        raise ClubIdRegistryError(f'{user.email} already has Club ID {user.club_id}. Clear it or move it first.', 'ALREADY_HAS_ID', 409)
    ensure_assignable(club_id, email=user.email, for_user_id=user.pk, allow_retired=allow_retired)
    with recording(actor=actor, reason=reason, kind=ClubIdEventKind.ASSIGNED, details={'allow_retired': allow_retired} if allow_retired else None):
        user.club_id = club_id
        user.save(update_fields=['club_id', 'updated_at'])
    return user


@transaction.atomic
def replace(actor, user_id, club_id: str, *, reason: str) -> User:
    """Give a person who already has an id a different one. The old id is simply freed (not retired)."""
    reason = _need_reason(reason)
    club_id = canonical(club_id)
    user = _locked_user(user_id)
    if not user.club_id:
        raise ClubIdRegistryError(f'{user.email} has no Club ID to replace.', 'NO_CLUB_ID', 409)
    if user.club_id.upper() == club_id:
        raise ClubIdRegistryError('It is already their Club ID.', 'SAME_ID', 409)
    ensure_assignable(club_id, email=user.email, for_user_id=user.pk)
    with recording(actor=actor, reason=reason, kind=ClubIdEventKind.CHANGED):
        user.club_id = club_id
        user.save(update_fields=['club_id', 'updated_at'])
    return user


@transaction.atomic
def reassign(actor, club_id: str, to_user_id, *, reason: str) -> dict:
    """Move an id from its holder to another person who has none. An affiliate who loses their id becomes a non-affiliate."""
    reason = _need_reason(reason)
    club_id = canonical(club_id)
    holder = User.objects.select_for_update().filter(club_id__iexact=club_id).first()
    if holder is None:
        raise ClubIdRegistryError(f"Nobody holds Club ID '{club_id}'.", 'NOT_FOUND', 404)
    target = _locked_user(to_user_id)
    if target.pk == holder.pk:
        raise ClubIdRegistryError('It is already theirs.', 'SAME_PERSON', 409)
    if target.club_id:
        raise ClubIdRegistryError(f'{target.email} already has Club ID {target.club_id}.', 'ALREADY_HAS_ID', 409)
    demoted = holder.role == UserRole.AFFILIATE
    with recording(actor=actor, reason=reason, kind=ClubIdEventKind.REASSIGNED_OUT, other_email=target.email):
        holder.club_id = None
        if demoted:
            holder.role = UserRole.NON_AFFILIATE
        holder.save(update_fields=['club_id', 'role', 'updated_at'] if demoted else ['club_id', 'updated_at'])
    with recording(actor=actor, reason=reason, kind=ClubIdEventKind.REASSIGNED_IN, other_email=holder.email):
        target.club_id = club_id
        target.save(update_fields=['club_id', 'updated_at'])
    return {'from': holder, 'to': target, 'holder_demoted': demoted}


@transaction.atomic
def clear(actor, user_id, *, reason: str) -> dict:
    """Remove a person's id. It is retired: it is not handed out again unless an admin assigns it on purpose."""
    reason = _need_reason(reason)
    user = _locked_user(user_id)
    if not user.club_id:
        raise ClubIdRegistryError(f'{user.email} has no Club ID.', 'NO_CLUB_ID', 409)
    club_id, demoted = user.club_id, user.role == UserRole.AFFILIATE
    with recording(actor=actor, reason=reason, kind=ClubIdEventKind.CLEARED):
        user.club_id = None
        if demoted:
            user.role = UserRole.NON_AFFILIATE
        user.save(update_fields=['club_id', 'role', 'updated_at'] if demoted else ['club_id', 'updated_at'])
    return {'user': user, 'club_id': club_id, 'demoted': demoted}


@transaction.atomic
def reserve(actor, *, email: str = '', club_id: str = '', reason: str = '', year: int | None = None, source: str = 'MANUAL') -> ClubIdReservation:
    """Hold an id for a person without an account (a particular one, or the next number from the counter)."""
    email = (email or '').strip().lower()
    reason = clean_reason(reason)
    if email:
        existing = User.objects.filter(email__iexact=email).first()
        if existing and existing.club_id:
            raise ClubIdRegistryError(f'{email} already has Club ID {existing.club_id}.', 'ALREADY_HAS_ID', 409)
        if ClubIdReservation.objects.filter(reserved_for_email=email, status=ReservationStatus.RESERVED).exists():
            raise ClubIdRegistryError(f'An id is already reserved for {email}.', 'ALREADY_RESERVED', 409)
    elif not reason:
        raise ClubIdRegistryError('Give an email or a reason for holding the id.', 'REASON_REQUIRED')
    if club_id:
        club_id = canonical(club_id)
        ensure_assignable(club_id, email=email)
    else:
        club_id = ClubIDService.allocate_next_club_id(year=year)
    reservation = ClubIdReservation.objects.create(club_id=club_id, reserved_for_email=email, reason=reason, source=source, created_by=actor)
    record(ClubIdEventKind.RESERVED, club_id, actor=actor, reason=reason, details={'for': email})
    return reservation


@transaction.atomic
def release(actor, reservation_id, *, reason: str = '') -> ClubIdReservation:
    reservation = ClubIdReservation.objects.select_for_update().filter(pk=reservation_id).first()
    if reservation is None:
        raise ClubIdRegistryError('That reservation was not found.', 'NOT_FOUND', 404)
    if reservation.status != ReservationStatus.RESERVED:
        raise ClubIdRegistryError(f'It is already {reservation.get_status_display().lower()}.', 'NOT_ACTIVE', 409)
    reservation.status, reservation.released_at = ReservationStatus.RELEASED, timezone.now()
    reservation.save(update_fields=['status', 'released_at', 'updated_at'])
    record(ClubIdEventKind.RESERVATION_RELEASED, reservation.club_id, actor=actor, reason=reason, details={'for': reservation.reserved_for_email})
    return reservation


_SEQUENCE = re.compile(r'^(\d{1,2})([A-Z]{3})(\d{3,})$')


def used_sequences() -> dict[tuple[str, int], set[int]]:
    """(prefix, full year) -> the sequence numbers held by someone or reserved."""
    used: dict[tuple[str, int], set[int]] = {}
    held = list(User.objects.exclude(club_id__isnull=True).exclude(club_id='').values_list('club_id', flat=True))
    held += list(ClubIdReservation.objects.filter(status=ReservationStatus.RESERVED).values_list('club_id', flat=True))
    for value in held:
        match = _SEQUENCE.match(value.upper())
        if match:
            used.setdefault((match.group(2), 2000 + int(match.group(1))), set()).add(int(match.group(3)))
    return used


@transaction.atomic
def set_counter(actor, *, prefix: str, year: int, next_sequence: int, reason: str) -> ClubIDSequence:
    """Move a counter forward (never back, and never to a number already in use)."""
    reason = _need_reason(reason)
    prefix = (prefix or '').strip().upper()
    if prefix != ClubIDService.DEFAULT_PREFIX:
        raise ClubIdRegistryError('Only the club\'s own SCC counter can be moved.', 'INVALID', 400)
    if not 2000 <= year <= 2099 or next_sequence < 1:
        raise ClubIdRegistryError('Choose a real year and a positive number.', 'INVALID', 400)
    counter, _ = ClubIDSequence.objects.select_for_update().get_or_create(prefix=prefix, year=year, defaults={'next_sequence': 1})
    highest = max(used_sequences().get((prefix, year), {0}))
    if next_sequence <= counter.next_sequence:
        raise ClubIdRegistryError(f'The counter is already at {counter.next_sequence}. It only moves forward.', 'NOT_FORWARD', 409)
    if next_sequence <= highest:
        raise ClubIdRegistryError(f'Number {highest} is already in use, so the counter must be higher than that.', 'IN_USE', 409)
    previous = counter.next_sequence
    counter.next_sequence = next_sequence
    counter.save(update_fields=['next_sequence', 'updated_at'])
    record(ClubIdEventKind.COUNTER_SET, f"{year % 100:02d}{prefix}{next_sequence:03d}", actor=actor, reason=reason, details={'prefix': prefix, 'year': year, 'from': previous, 'to': next_sequence})
    return counter


def overview() -> dict:
    """Counters with what has been issued and where the gaps are, plus the totals."""
    used = used_sequences()
    counters = {(row.prefix, row.year): row for row in ClubIDSequence.objects.all()}
    # This year's counter is always listed, so it can be moved before the first id of the year is given.
    keys = sorted(set(used) | set(counters) | {(ClubIDService.DEFAULT_PREFIX, timezone.now().year)}, key=lambda key: (key[0], -key[1]))
    rows = []
    for prefix, year in keys:
        taken = used.get((prefix, year), set())
        counter = counters.get((prefix, year))
        highest = max(taken) if taken else 0
        next_sequence = counter.next_sequence if counter else highest + 1
        rows.append({
            'prefix': prefix, 'year': year, 'next_sequence': next_sequence, 'next_id': f"{year % 100:02d}{prefix}{next_sequence:03d}",
            'in_use': len(taken), 'highest': highest, 'gaps': max(highest - len(taken), 0), 'has_counter': counter is not None,
            'editable': prefix == ClubIDService.DEFAULT_PREFIX,
        })
    return {
        'counters': rows,
        'totals': {
            'holders': User.objects.exclude(club_id__isnull=True).exclude(club_id='').count(),
            'reserved': ClubIdReservation.objects.filter(status=ReservationStatus.RESERVED).count(),
            'retired': ClubIdEvent.objects.filter(kind=ClubIdEventKind.CLEARED).values('club_id').distinct().count(),
        },
    }


def lookup_id(club_id: str) -> dict:
    """Everything known about one id: holder, reservation, whether it is retired, and its history."""
    holder = holder_of(club_id)
    reservation = active_reservation(club_id)
    return {
        'club_id': club_id, 'holder': holder, 'reservation': reservation, 'retired': is_retired(club_id),
        'events': list(ClubIdEvent.objects.filter(club_id__iexact=club_id)[:100]),
    }
