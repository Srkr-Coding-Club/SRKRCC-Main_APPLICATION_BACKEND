"""Day-of check-in: each participant's rotating signed QR code, scanning or manual check-in, the live roster and no-shows."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone as dt_timezone

from django.core import signing
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import PermissionDenied

from apps.audit.utils import log_audit_event

from .access import can_check_in, has_permission
from .errors import HackathonError
from .models import CheckIn, Hackathon, TeamMember, TeamStatus

TOKEN_SALT = 'hackathon-check-in'
# A QR code changes every bucket so a forwarded screenshot soon stops working. A scan is accepted when the code's
# bucket is within TOKEN_BUCKET_TOLERANCE of the scan time's bucket (slow refreshes, clock drift, offline replays).
TOKEN_BUCKET_SECONDS = 60
TOKEN_BUCKET_TOLERANCE = 3
# A scan made offline is replayed later; its recorded time is trusted only this far back.
MAX_SCAN_AGE = timedelta(hours=24)
DEFAULT_ROSTER_LIMIT = 100
MAX_ROSTER_LIMIT = 500
HELD_BACK_LIMIT = 25
MAX_OVERRIDE_REASON = 200
TIMELINE_BUCKET_MINUTES = 15
# Past this many hours of arrivals the timeline switches to hourly buckets so it never grows unbounded.
TIMELINE_FINE_SPAN_HOURS = 48
UNKNOWN_VOLUNTEER = 'Removed account'
NO_SHOW_WITHDRAW_REASON = 'No-show at check-in.'
# A walk-in override may bring in a team that is still forming or waitlisted, never one that was removed.
REMOVED_TEAM_STATUSES = (TeamStatus.DISQUALIFIED, TeamStatus.WITHDRAWN)


def window_state(hackathon: Hackathon, at=None) -> str:
    """NOT_OPEN, OPEN or CLOSED for the check-in window (no dates set = always OPEN)."""
    at = at or timezone.now()
    if hackathon.check_in_opens_at and at < hackathon.check_in_opens_at:
        return 'NOT_OPEN'
    if hackathon.check_in_closes_at and at >= hackathon.check_in_closes_at:
        return 'CLOSED'
    return 'OPEN'


def scan_time(raw):
    """When a queued scan really happened: the client's timestamp, kept within the last day and never in the future."""
    now = timezone.now()
    when = parse_datetime(str(raw)) if raw else None
    if when is None or timezone.is_naive(when):
        return now
    return min(max(when, now - MAX_SCAN_AGE), now)


def _bucket(at) -> int:
    return int(at.timestamp() // TOKEN_BUCKET_SECONDS)


def make_token(hackathon: Hackathon, user, at=None) -> str:
    return signing.dumps({'h': hackathon.id, 'u': user.id, 'b': _bucket(at or timezone.now())}, salt=TOKEN_SALT, compress=True)


def _user_from_token(hackathon: Hackathon, token: str, when):
    try:
        payload = signing.loads(str(token or '').strip(), salt=TOKEN_SALT)
    except signing.BadSignature:
        raise HackathonError('That QR code is not valid.', 'INVALID_QR')
    if payload.get('h') != hackathon.id:
        raise HackathonError('That QR code is for a different hackathon.', 'WRONG_HACKATHON')
    if abs(_bucket(when) - payload.get('b', -10**9)) > TOKEN_BUCKET_TOLERANCE:
        raise HackathonError('That QR code has expired. Ask the participant to reopen their code, or check them in from the list.', 'QR_EXPIRED')
    return payload.get('u')


def _membership(hackathon: Hackathon, user_id, *, allow_unplaced: bool) -> TeamMember:
    membership = (
        TeamMember.objects.select_related('team', 'user')
        .filter(hackathon=hackathon, user_id=user_id).first()
    )
    if membership is None:
        raise HackathonError('That person is not on a team for this hackathon.', 'NOT_A_PARTICIPANT', status=404)
    who = membership.user.first_name or membership.user.email
    if membership.team.status in REMOVED_TEAM_STATUSES:
        raise HackathonError(f"{who}'s team was removed from this hackathon.", 'TEAM_REMOVED')
    if (membership.team.status != TeamStatus.REGISTERED or membership.team.is_waitlisted) and not allow_unplaced:
        raise HackathonError(
            f"{who}'s team is not registered (still forming or waitlisted).", 'TEAM_NOT_REGISTERED',
        )
    return membership


def _full_name(user) -> str:
    return f'{user.first_name} {user.last_name}'.strip() or user.email


def _search_filter(query) -> Q | None:
    """Every word typed must match the name, email or team, so a full name like "Asha Rao" finds its person."""
    words = str(query or '').split()
    search = Q()
    for word in words:
        search &= (
            Q(user__first_name__icontains=word) | Q(user__last_name__icontains=word)
            | Q(user__email__icontains=word) | Q(team__name__icontains=word)
        )
    return search if words else None


def _placed_members(hackathon: Hackathon):
    return hackathon.memberships.filter(team__status=TeamStatus.REGISTERED, team__is_waitlisted=False)


class CheckInService:

    @staticmethod
    def _require_feature(hackathon: Hackathon, actor):
        if not hackathon.feature('check_in'):
            raise HackathonError('Check-in is not enabled for this hackathon.', 'FEATURE_OFF')
        if not can_check_in(hackathon, actor):
            raise PermissionDenied('You are not allowed to check participants in.')

    @staticmethod
    def _clean_override(hackathon: Hackathon, actor, reason) -> str:
        if not has_permission(hackathon, actor, 'teams'):
            raise PermissionDenied('Only organizers can check in someone whose team is not registered.')
        reason = ' '.join(str(reason or '').split())
        if not reason:
            raise HackathonError('Say why this person is being let in.', 'REASON_REQUIRED', field='reason')
        return reason[:MAX_OVERRIDE_REASON]

    @staticmethod
    @transaction.atomic
    def check_in(
        hackathon: Hackathon, actor, *, user_id=None, token=None, scanned_at=None, override=False, reason=None,
    ) -> tuple[CheckIn, bool]:
        """Idempotent: scanning twice reports ``created=False`` instead of failing.

        The check-in window gates QR scans only: staff can still tick a late arrival off the list by hand.
        ``override`` (organizers only, with a reason) lets in someone whose team is still forming or waitlisted.
        """
        CheckInService._require_feature(hackathon, actor)
        when = scan_time(scanned_at)
        override_reason = CheckInService._clean_override(hackathon, actor, reason) if override and not token else ''
        if token:
            state = window_state(hackathon, when)
            if state == 'NOT_OPEN':
                raise HackathonError('Check-in has not opened yet.', 'CHECK_IN_NOT_OPEN')
            if state == 'CLOSED':
                raise HackathonError('Check-in has closed. Tick the person off the list if they are allowed in.', 'CHECK_IN_CLOSED')
            user_id = _user_from_token(hackathon, token, when)
        membership = _membership(hackathon, user_id, allow_unplaced=bool(override_reason))
        try:
            with transaction.atomic():
                record, created = CheckIn.objects.get_or_create(
                    hackathon=hackathon, user=membership.user,
                    defaults={
                        'team': membership.team, 'checked_in_by': actor, 'checked_in_at': when,
                        'override_reason': override_reason if _is_unplaced(membership) else '',
                    },
                )
        except IntegrityError:
            record, created = CheckIn.objects.get(hackathon=hackathon, user=membership.user), False
        if created:
            details = {'user': membership.user.email, 'team': membership.team.name}
            if record.override_reason:
                details['override_reason'] = record.override_reason
            log_audit_event(
                actor=actor, action='Checked In Participant', target_model='Hackathon', target_id=hackathon.slug, details=details,
            )
        return record, created

    @staticmethod
    @transaction.atomic
    def undo(hackathon: Hackathon, actor, user_id: int) -> None:
        CheckInService._require_feature(hackathon, actor)
        deleted, _ = CheckIn.objects.filter(hackathon=hackathon, user_id=user_id).delete()
        if deleted:
            log_audit_event(
                actor=actor, action='Undid Participant Check-in', target_model='Hackathon', target_id=hackathon.slug,
                details={'user_id': user_id},
            )

    @staticmethod
    def roster(hackathon: Hackathon, *, query='', missing=False, limit=DEFAULT_ROSTER_LIMIT, offset=0, with_held_back=False) -> dict:
        """Everyone on a placed team, with who has arrived: counts for the whole event plus one filtered page.

        ``held_back`` (organizers, search only) lists matching people whose team is not placed, so they can be let in.
        """
        limit = max(1, min(int(limit), MAX_ROSTER_LIMIT))
        offset = max(0, int(offset))
        arrived = {c.user_id: c for c in hackathon.check_ins.select_related('checked_in_by')}
        placed = _placed_members(hackathon)
        total = placed.count()
        matching = placed.select_related('user', 'team')
        search = _search_filter(query)
        if search:
            matching = matching.filter(search)
        if missing:
            matching = matching.exclude(user_id__in=arrived.keys())
        count = matching.count()
        rows = []
        for membership in matching.order_by('team__name', 'role', 'user__first_name', 'id')[offset:offset + limit]:
            record = arrived.get(membership.user_id)
            rows.append({
                'user_id': membership.user_id, 'name': _full_name(membership.user), 'email': membership.user.email,
                'team_id': membership.team_id, 'team_name': membership.team.name, 'role': membership.role,
                'checked_in': record is not None, 'checked_in_at': record.checked_in_at if record else None,
                'checked_in_by': (_full_name(record.checked_in_by) if record and record.checked_in_by else None),
            })
        held_back = []
        if with_held_back and search:
            unplaced = (
                hackathon.memberships.select_related('user', 'team')
                .exclude(team__status__in=REMOVED_TEAM_STATUSES)
                .exclude(id__in=placed.values('id'))
                .exclude(user_id__in=arrived.keys())
                .filter(search).order_by('team__name', 'user__first_name', 'id')[:HELD_BACK_LIMIT]
            )
            held_back = [{
                'user_id': m.user_id, 'name': _full_name(m.user), 'email': m.user.email, 'team_name': m.team.name,
                'why': 'Waitlisted' if m.team.is_waitlisted else 'Team still forming',
            } for m in unplaced]
        return {
            'participants': rows, 'total': total, 'checked_in': placed.filter(user_id__in=arrived.keys()).count(),
            'count': count, 'limit': limit, 'offset': offset, 'held_back': held_back,
        }

    @staticmethod
    def stats(hackathon: Hackathon, actor) -> dict:
        """Who scanned how many people, and when people arrived (gaps between the first and last arrival are zero-filled)."""
        CheckInService._require_organizer(hackathon, actor)
        records = list(hackathon.check_ins.select_related('checked_in_by'))
        by_person = Counter(_full_name(r.checked_in_by) if r.checked_in_by else UNKNOWN_VOLUNTEER for r in records)
        volunteers = [{'name': name, 'count': count} for name, count in sorted(by_person.items(), key=lambda kv: (-kv[1], kv[0]))]
        timeline, bucket_minutes = [], TIMELINE_BUCKET_MINUTES
        if records:
            times = [r.checked_in_at for r in records]
            first, last = min(times), max(times)
            if last - first > timedelta(hours=TIMELINE_FINE_SPAN_HOURS):
                bucket_minutes = 60
            step = bucket_minutes * 60
            counts = Counter(int(t.timestamp() // step) for t in times)
            timeline = [
                {'at': datetime.fromtimestamp(b * step, dt_timezone.utc), 'count': counts.get(b, 0)}
                for b in range(min(counts), max(counts) + 1)
            ]
        return {
            'total': len(records), 'overrides': sum(1 for r in records if r.override_reason),
            'volunteers': volunteers, 'bucket_minutes': bucket_minutes, 'timeline': timeline,
        }

    @staticmethod
    def _require_organizer(hackathon: Hackathon, actor):
        if not hackathon.feature('check_in'):
            raise HackathonError('Check-in is not enabled for this hackathon.', 'FEATURE_OFF')
        if not has_permission(hackathon, actor, 'teams'):
            raise PermissionDenied('Only organizers can follow up on no-shows.')

    @staticmethod
    def _no_show_teams(hackathon: Hackathon):
        """Placed teams where nobody has arrived."""
        return (
            hackathon.teams.filter(status=TeamStatus.REGISTERED, is_waitlisted=False)
            .exclude(id__in=hackathon.check_ins.values('team_id'))
            .prefetch_related('memberships__user').order_by('name')
        )

    @staticmethod
    def no_shows(hackathon: Hackathon, actor) -> dict:
        CheckInService._require_organizer(hackathon, actor)
        teams = list(CheckInService._no_show_teams(hackathon))
        absent_members = _placed_members(hackathon).exclude(user_id__in=hackathon.check_ins.values('user_id')).count()
        return {
            'window': window_state(hackathon),
            'teams': [{'team_id': t.id, 'name': t.name, 'members': t.memberships.count()} for t in teams],
            'absent_participants': absent_members,
            'waitlisted_teams': hackathon.teams.filter(is_waitlisted=True, status__in=(TeamStatus.FORMING, TeamStatus.REGISTERED)).count(),
        }

    @staticmethod
    def remind_absent(hackathon: Hackathon, actor) -> int:
        """Nudge every placed participant who has not checked in yet."""
        from .services import _notify_users

        CheckInService._require_organizer(hackathon, actor)
        arrived = hackathon.check_ins.values('user_id')
        people = [m.user for m in _placed_members(hackathon).exclude(user_id__in=arrived).select_related('user')]
        where = f' at {hackathon.venue}' if hackathon.venue else ''
        _notify_users(
            people, f'{hackathon.title}: please check in',
            f'We have not seen you yet{where}. Open your dashboard and show your QR code at the entrance.', hackathon, actor,
        )
        log_audit_event(actor=actor, action='Reminded Absent Participants To Check In', target_model='Hackathon', target_id=hackathon.slug, details={'people': len(people)})
        return len(people)

    @staticmethod
    def withdraw_no_shows(hackathon: Hackathon, actor, team_ids) -> int:
        """Withdraw the chosen no-show teams; the waitlist is then promoted by the normal status change."""
        from .services import TeamService

        CheckInService._require_organizer(hackathon, actor)
        if window_state(hackathon) == 'OPEN' and hackathon.check_in_closes_at:
            raise HackathonError('Check-in is still open. Withdraw no-shows after it closes.', 'CHECK_IN_STILL_OPEN')
        wanted = {int(t) for t in team_ids or [] if str(t).isdigit()}
        teams = [t for t in CheckInService._no_show_teams(hackathon) if t.id in wanted]
        for team in teams:
            TeamService.admin_set_status(team, actor, TeamStatus.WITHDRAWN, NO_SHOW_WITHDRAW_REASON)
        return len(teams)


def _is_unplaced(membership: TeamMember) -> bool:
    return membership.team.status != TeamStatus.REGISTERED or membership.team.is_waitlisted
