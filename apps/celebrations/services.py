"""
Raising and reading celebrations.

Other apps only call ``award`` / ``award_after_commit`` (and ``retract`` when a moment is undone); the site reads
``pending`` and marks what it showed with ``mark_seen``. Awarding never fails the action that caused it.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Case, IntegerField, Value, When
from django.utils import timezone

from .kinds import KINDS, render
from .models import Celebration, CelebrationSetting, CelebrationSize

logger = logging.getLogger(__name__)

#: How long a celebration waits for its person before it is dropped.
LIFETIME = timedelta(days=14)
#: Seen ones are kept a while for support questions, then swept.
KEEP_SEEN = timedelta(days=30)
#: What one request returns: the site shows a few and the rest stay for the next visit.
PENDING_LIMIT = 10

_PRIORITY = Case(
    When(size=CelebrationSize.BIG, then=Value(0)),
    When(size=CelebrationSize.MEDIUM, then=Value(1)),
    default=Value(2), output_field=IntegerField(),
)


def is_enabled(user) -> bool:
    """Whether this person wants celebrations (everyone does until they turn them off)."""
    return not CelebrationSetting.objects.filter(user=user, enabled=False).exists()


def set_enabled(user, enabled: bool) -> bool:
    setting, _ = CelebrationSetting.objects.update_or_create(user=user, defaults={'enabled': bool(enabled)})
    return setting.enabled


def award(user, kind: str, key: str, *, link_url: str = '', size: str | None = None, **context) -> Celebration | None:
    """
    Celebrate one moment for one person, once. Returns the celebration, or None when the person has them switched off,
    cannot sign in, or was already celebrated for this ``key``. An unknown ``kind`` is a programming error and raises.
    """
    if kind not in KINDS:
        raise ValueError(f'Unknown celebration kind: {kind}')
    if user is None or not getattr(user, 'is_active', False) or not is_enabled(user):
        return None
    spec = KINDS[kind]
    title = render(spec.title, context)[:120]
    message = render(spec.message, context)[:300]
    celebration, created = Celebration.objects.get_or_create(
        user=user, key=key[:120],
        defaults={
            'kind': kind, 'size': size or spec.size, 'title': title, 'message': message, 'link_url': link_url[:300],
            'payload': {k: v for k, v in context.items() if isinstance(v, (str, int, float, bool))},
            'expires_at': timezone.now() + LIFETIME,
        },
    )
    return celebration if created else None


def award_after_commit(users, kind: str, key: str, **kwargs) -> None:
    """
    Celebrate for several people once the surrounding transaction has committed, so a rolled-back action celebrates
    nothing. Any failure is logged and swallowed: a missing party must never fail the real action.
    """
    people = [u for u in users if u is not None]
    if not people:
        return

    def run():
        for person in people:
            try:
                with transaction.atomic():
                    award(person, kind, key, **dict(kwargs))
            except Exception:  # noqa: BLE001 - the action that earned the celebration has already happened
                logger.warning('Could not award celebration %s to user %s', kind, getattr(person, 'pk', None), exc_info=True)

    transaction.on_commit(run)


def retract(key: str, users=None) -> int:
    """Take back celebrations nobody has seen yet (the moment was undone, e.g. the team was withdrawn). Returns how many."""
    unseen = Celebration.objects.filter(key=key, seen_at__isnull=True)
    if users is not None:
        unseen = unseen.filter(user__in=list(users))
    deleted, _ = unseen.delete()
    return deleted


def pending(user) -> list[Celebration]:
    """What the site should celebrate for this person now: unseen and not expired, the biggest first, then the oldest."""
    if not is_enabled(user):
        return []
    rows = (
        Celebration.objects.filter(user=user, seen_at__isnull=True, expires_at__gt=timezone.now())
        .annotate(priority=_PRIORITY).order_by('priority', 'created_at', 'id')
    )
    return list(rows[:PENDING_LIMIT])


def mark_seen(user, ids) -> int:
    """The site showed these. Only the person's own are touched."""
    wanted = [int(i) for i in ids if str(i).isdigit()]
    return Celebration.objects.filter(user=user, pk__in=wanted, seen_at__isnull=True).update(seen_at=timezone.now())


def sweep(now=None) -> dict[str, int]:
    """Delete celebrations that expired unseen and seen ones older than ``KEEP_SEEN``."""
    now = now or timezone.now()
    expired, _ = Celebration.objects.filter(seen_at__isnull=True, expires_at__lt=now).delete()
    old, _ = Celebration.objects.filter(seen_at__lt=now - KEEP_SEEN).delete()
    return {'expired': expired, 'old_seen': old}
