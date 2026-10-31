"""One response for the organizer Overview: everything the page needs, each part only when this person may see it."""
from __future__ import annotations

import logging
from collections.abc import Callable

from django.db.models import Count

from . import access, analytics, finder_admin, payments, withdrawals
from .models import IdeaStatus, IdeaSubmission, PaymentStage
from .ideas import IdeaService

logger = logging.getLogger(__name__)

# The payment fields the money block reads; the full rows (reasons, screenshots, contacts) stay in the Payments tab.
PAYMENT_FIELDS = ('status', 'amount', 'team_status', 'submitted_at')


def _ideas(hackathon) -> dict:
    counts = {value: 0 for value in IdeaStatus.values}
    for row in IdeaSubmission.objects.filter(team__hackathon=hackathon).values('status').annotate(n=Count('id')):
        counts[row['status']] = row['n']
    return {'counts': counts, 'missing': list(IdeaService.teams_without_idea(hackathon).values('id', 'name'))}


def _payments(hackathon, user) -> dict:
    queue = payments.queue(hackathon, user)
    return {
        'counts': queue['counts'], 'fee_amount': queue['fee_amount'], 'stage': queue['stage'],
        'payments': [{key: row[key] for key in PAYMENT_FIELDS} for row in queue['payments']],
    }


def build(hackathon, user, stats: Callable[[object], dict]) -> dict:
    """
    ``stats``, ``analytics`` and ``now`` are always there (the view needs the ``insights`` permission). Every other part is
    ``None`` when its module is off or the person lacks its permission (listed in ``hidden``), or when it failed to load
    (listed in ``errors``): one broken part never blanks the page.
    """
    features = hackathon.features or {}
    allowed = lambda key: access.has_permission(hackathon, user, key)  # noqa: E731
    parts: dict[str, tuple[bool, Callable[[], dict]]] = {
        'ideas': (bool(features.get('ideas')) and allowed('ideas'), lambda: _ideas(hackathon)),
        'payments': (hackathon.payment_stage != PaymentStage.NONE and allowed('teams'), lambda: _payments(hackathon, user)),
        'finder': (bool(features.get('team_finder')) and allowed('teams'), lambda: finder_admin.overview(hackathon)),
        'withdrawals': (allowed('teams'), lambda: {'pending': len(withdrawals.WithdrawalService.overview(hackathon)['pending'])}),
    }
    result: dict = {'stats': stats(hackathon), 'analytics': analytics.overview(hackathon)}
    hidden, errors = [], {}
    for name, (shown, load) in parts.items():
        result[name] = None
        if not shown:
            hidden.append(name)
            continue
        try:
            result[name] = load()
        except Exception:  # noqa: BLE001 - a failing part is reported, not fatal
            logger.exception('Overview part %s failed for %s', name, hackathon.slug)
            errors[name] = 'Could not load this part.'
    result['hidden'], result['errors'] = hidden, errors
    return result
