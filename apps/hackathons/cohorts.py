"""
The groups of teams a hackathon's screens count, defined once.

Every team falls in exactly one bucket of ``breakdown``:

- ``competing``: registered and holding a place (not waitlisted). The teams that hand in ideas and projects, check in,
  are judged and get certificates.
- ``forming``: still gathering members, holding a place.
- ``waitlisted``: forming or registered, queued because the hackathon is full.
- ``awaiting_payment``: the fee is waiting for an organizer, or was turned down. Holds no place.
- ``removed``: withdrawn or disqualified.

Counts and lists elsewhere use these instead of their own status filters, so the numbers on different tabs add up.
"""
from __future__ import annotations

from django.db.models import Q, QuerySet

from .models import Hackathon, TeamStatus

ACTIVE_STATUSES = (TeamStatus.FORMING, TeamStatus.REGISTERED)
PAYMENT_STATUSES = (TeamStatus.PAYMENT_PENDING, TeamStatus.PAYMENT_REJECTED)
REMOVED_STATUSES = (TeamStatus.WITHDRAWN, TeamStatus.DISQUALIFIED)

BUCKETS = ('competing', 'forming', 'waitlisted', 'awaiting_payment', 'removed')


def competing_q(prefix: str = '') -> Q:
    """Registered and holding a place. ``prefix`` is the path to the team, e.g. ``'team__'`` for an idea."""
    return Q(**{f'{prefix}status': TeamStatus.REGISTERED, f'{prefix}is_waitlisted': False})


def removed_q(prefix: str = '') -> Q:
    return Q(**{f'{prefix}status__in': REMOVED_STATUSES})


def competing(hackathon: Hackathon) -> QuerySet:
    return hackathon.teams.filter(competing_q())


def active(hackathon: Hackathon) -> QuerySet:
    """Forming or registered, waitlisted or not: every team still in the hackathon that has a place or is queued for one."""
    return hackathon.teams.filter(status__in=ACTIVE_STATUSES)


def not_removed(queryset: QuerySet, prefix: str = 'team__') -> QuerySet:
    """Ideas, projects or entries whose team has not withdrawn or been disqualified."""
    return queryset.exclude(removed_q(prefix))


def breakdown(hackathon: Hackathon) -> dict:
    """How many teams are in each bucket, plus ``total``. The buckets never overlap and always add up to the total."""
    counts = {bucket: 0 for bucket in BUCKETS}
    for status, waitlisted in hackathon.teams.values_list('status', 'is_waitlisted'):
        counts[bucket_of(status, waitlisted)] += 1
    counts['total'] = sum(counts[bucket] for bucket in BUCKETS)
    return counts


def bucket_of(status: str, is_waitlisted: bool) -> str:
    if status in REMOVED_STATUSES:
        return 'removed'
    if status in PAYMENT_STATUSES:
        return 'awaiting_payment'
    if is_waitlisted:
        return 'waitlisted'
    return 'competing' if status == TeamStatus.REGISTERED else 'forming'
