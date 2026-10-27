"""Day-of check-in: each participant's rotating signed QR code, scanning or manual check-in, the live roster and no-shows."""
from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone as dt_timezone

from django.core import signing
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import PermissionDenied

from apps.audit.utils import log_audit_event

from . import check_in_sessions
from .access import can_check_in, has_permission
from .errors import HackathonError
from .models import CheckIn, CheckInScanMode, CheckInSession, CheckInSlot, CheckInVia, Hackathon, TeamMember, TeamStatus

TOKEN_SALT = 'hackathon-check-in'
TEAM_TOKEN_SALT = 'hackathon-check-in-team'
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


def window_state(hackathon: Hackathon, at=None, session: CheckInSession | None = None) -> str:
    """NOT_OPEN, OPEN or CLOSED for a session's window (the main session when none is given; no times set = always OPEN)."""
    return check_in_sessions.window_state(session or hackathon.main_check_in_session(), at)


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


def make_team_token(hackathon: Hackathon, team, at=None) -> str:
    """A team's code: the same rotation as a member's, but its own signature so the two can never be mistaken for each other."""
    return signing.dumps({'h': hackathon.id, 't': team.id, 'b': _bucket(at or timezone.now())}, salt=TEAM_TOKEN_SALT, compress=True)


def _decode(token, salt):
    try:
        return signing.loads(str(token or '').strip(), salt=salt)
    except signing.BadSignature:
        return None


def _verify(payload, hackathon: Hackathon, when) -> None:
    """The code belongs to this hackathon and is recent enough for the moment it was scanned."""
    if payload.get('h') != hackathon.id:
        raise HackathonError('That QR code is for a different hackathon.', 'WRONG_HACKATHON')
    if abs(_bucket(when) - payload.get('b', -10**9)) > TOKEN_BUCKET_TOLERANCE:
        raise HackathonError('That QR code has expired. Ask the participant to reopen their code, or check them in from the list.', 'QR_EXPIRED')


def _user_from_token(hackathon: Hackathon, token: str, when):
    payload = _decode(token, TOKEN_SALT)
    if payload is None:
        if _decode(token, TEAM_TOKEN_SALT) is not None:
            raise HackathonError('That is a team code. Open it with the scanner, then pick who is present.', 'TEAM_CODE')
        raise HackathonError('That QR code is not valid.', 'INVALID_QR')
    _verify(payload, hackathon, when)
    return payload.get('u')


def _team_from_token(hackathon: Hackathon, token: str, when) -> int:
    payload = _decode(token, TEAM_TOKEN_SALT)
    if payload is None:
        if _decode(token, TOKEN_SALT) is not None:
            raise HackathonError("That is a person's own code, not a team code.", 'NOT_A_TEAM_CODE')
        raise HackathonError('That QR code is not valid.', 'INVALID_QR')
    _verify(payload, hackathon, when)
    return payload.get('t')


def identify(hackathon: Hackathon, token: str, when) -> tuple[str, int]:
    """What a scanned code is: ``('member', user_id)`` or ``('team', team_id)``."""
    payload = _decode(token, TOKEN_SALT)
    if payload is not None:
        _verify(payload, hackathon, when)
        return 'member', payload.get('u')
    payload = _decode(token, TEAM_TOKEN_SALT)
    if payload is not None:
        _verify(payload, hackathon, when)
        return 'team', payload.get('t')
    raise HackathonError('That QR code is not valid.', 'INVALID_QR')


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
        hackathon: Hackathon, actor, *, user_id=None, token=None, scanned_at=None, override=False, reason=None, session=None,
    ) -> tuple[CheckIn, bool]:
        """Idempotent: scanning twice reports ``created=False`` instead of failing.

        ``session`` is the session to check in to (the main one when omitted). The session's window gates QR scans only:
        staff can still tick a late arrival off the list by hand. ``override`` (organizers only, with a reason) lets in
        someone whose team is still forming or waitlisted, or who has not arrived at the main session yet.
        """
        CheckInService._require_feature(hackathon, actor)
        session = check_in_sessions.resolve(hackathon, session)
        if not session.is_scannable:
            raise HackathonError(f'{session.label} is switched off.', 'SESSION_OFF')
        when = scan_time(scanned_at)
        override_reason = CheckInService._clean_override(hackathon, actor, reason) if override and not token else ''
        if token:
            if session.scan_mode == CheckInScanMode.TEAMS:
                raise HackathonError(f'{session.label} checks teams in together. Scan the team code instead.', 'TEAM_SCAN_ONLY')
            state = check_in_sessions.window_state(session, when)
            if state == 'NOT_OPEN':
                raise HackathonError(f'{session.label} has not opened yet.', 'CHECK_IN_NOT_OPEN')
            if state == 'CLOSED':
                raise HackathonError(f'{session.label} has closed. Tick the person off the list if they are allowed in.', 'CHECK_IN_CLOSED')
            user_id = _user_from_token(hackathon, token, when)
        membership = _membership(hackathon, user_id, allow_unplaced=bool(override_reason))
        via = CheckInVia.MEMBER_QR if token else CheckInVia.OVERRIDE if override_reason else CheckInVia.LIST
        return CheckInService._record(hackathon, actor, session, membership, when, via=via, override_reason=override_reason)

    @staticmethod
    def _record(
        hackathon: Hackathon, actor, session: CheckInSession, membership: TeamMember, when, *,
        via: str, override_reason: str = '', batch_id=None, audit: bool = True,
    ) -> tuple[CheckIn, bool]:
        """
        One person into one session: the arrival rule, then the row. Returns ``(record, created)``; a second call is
        harmless. ``override_reason`` waives the rule and, when it was needed, is kept on the row.
        """
        arrival_waived = False
        if not session.is_main and session.needs_arrival and not check_in_sessions.has_arrived(hackathon, membership.user_id):
            if not override_reason:
                who = membership.user.first_name or membership.user.email
                raise HackathonError(f'{who} has not checked in on arrival yet.', 'NOT_ARRIVED')
            arrival_waived = True
        kept_reason = override_reason if (_is_unplaced(membership) or arrival_waived) else ''
        try:
            with transaction.atomic():
                record, created = CheckIn.objects.get_or_create(
                    session=session, user=membership.user,
                    defaults={
                        'hackathon': hackathon, 'team': membership.team, 'checked_in_by': actor, 'checked_in_at': when,
                        'via': CheckInVia.OVERRIDE if kept_reason else via, 'override_reason': kept_reason, 'batch_id': batch_id,
                    },
                )
        except IntegrityError:
            record, created = CheckIn.objects.get(session=session, user=membership.user), False
        if created and audit:
            details = {'user': membership.user.email, 'team': membership.team.name, 'session': session.label}
            if record.override_reason:
                details['override_reason'] = record.override_reason
            log_audit_event(
                actor=actor, action='Checked In Participant', target_model='Hackathon', target_id=hackathon.slug, details=details,
            )
        return record, created

    # -- scanning a team's code ---------------------------------------------------------------------------------

    @staticmethod
    def _team_for_scan(hackathon: Hackathon, actor, token, session: CheckInSession, when):
        """The team a code names, after the checks a code scan needs (session mode, window, team standing)."""
        if session.scan_mode == CheckInScanMode.MEMBERS:
            raise HackathonError(f'{session.label} checks people in one by one. Each person shows their own code.', 'MEMBER_SCAN_ONLY')
        state = check_in_sessions.window_state(session, when)
        if state == 'NOT_OPEN':
            raise HackathonError(f'{session.label} has not opened yet.', 'CHECK_IN_NOT_OPEN')
        if state == 'CLOSED':
            raise HackathonError(f'{session.label} has closed. Tick people off the list if they are allowed in.', 'CHECK_IN_CLOSED')
        team = hackathon.teams.select_related('problem_statement').filter(pk=_team_from_token(hackathon, token, when)).first()
        if team is None:
            raise HackathonError('That team is not in this hackathon.', 'TEAM_NOT_FOUND', status=404)
        if team.status in REMOVED_TEAM_STATUSES:
            raise HackathonError(f"{team.name} was removed from this hackathon.", 'TEAM_REMOVED')
        placed = team.status == TeamStatus.REGISTERED and not team.is_waitlisted
        if not placed and not has_permission(hackathon, actor, 'teams'):
            raise HackathonError(f'{team.name} is not registered (still forming or waitlisted). Ask an organizer.', 'TEAM_NOT_REGISTERED')
        return team, placed

    @staticmethod
    def _prepare_team_scan(hackathon: Hackathon, actor, token, session, scanned_at):
        CheckInService._require_feature(hackathon, actor)
        session = check_in_sessions.resolve(hackathon, session)
        if not session.is_scannable:
            raise HackathonError(f'{session.label} is switched off.', 'SESSION_OFF')
        when = scan_time(scanned_at)
        team, placed = CheckInService._team_for_scan(hackathon, actor, token, session, when)
        return session, when, team, placed

    @staticmethod
    def team_roster(hackathon: Hackathon, actor, *, token, session=None, scanned_at=None) -> dict:
        """
        What a team's code opens: the team and each member's standing in the session. It checks nobody in; the volunteer
        confirms who is there and sends those names to ``check_in_team``.
        """
        session, when, team, placed = CheckInService._prepare_team_scan(hackathon, actor, token, session, scanned_at)
        memberships = list(team.memberships.select_related('user').order_by('role', 'user__first_name', 'id'))
        ids = [m.user_id for m in memberships]
        records = {c.user_id: c for c in CheckIn.objects.filter(session=session, user_id__in=ids).select_related('checked_in_by')}
        arrived = set(check_in_sessions.main_check_ins(hackathon).filter(user_id__in=ids).values_list('user_id', flat=True))
        needs_arrival = not session.is_main and session.needs_arrival
        members = []
        for membership in memberships:
            record = records.get(membership.user_id)
            members.append({
                'user_id': membership.user_id, 'name': _full_name(membership.user), 'role': membership.role,
                'checked_in': record is not None, 'checked_in_at': record.checked_in_at if record else None,
                'checked_in_by': _full_name(record.checked_in_by) if record and record.checked_in_by else None,
                'via': record.via if record else None,
                'blocked': 'NOT_ARRIVED' if (needs_arrival and record is None and membership.user_id not in arrived) else None,
            })
        return {
            'kind': 'team',
            'team': {
                'id': team.id, 'name': team.name, 'status': team.status, 'placed': placed,
                'problem_code': team.problem_statement.code if team.problem_statement_id else (team.open_innovation_code if team.is_open_innovation else None),
                'problem_title': team.problem_statement.title if team.problem_statement_id else (team.custom_problem_title or None),
            },
            'session': session_summary(session), 'members': members,
            'all_checked_in': bool(members) and all(m['checked_in'] for m in members),
            'can_override': has_permission(hackathon, actor, 'teams'),
        }

    @staticmethod
    @transaction.atomic
    def check_in_team(
        hackathon: Hackathon, actor, *, token, user_ids, session=None, scanned_at=None, override=False, reason=None,
    ) -> dict:
        """
        Check in the team members the volunteer ticked, all under one batch id. Each person gets their own result:
        one refusal (not arrived, not on the team) never stops the others. Needs the team's code, so a ticked list
        alone can never be recorded as a team scan.
        """
        session, when, team, placed = CheckInService._prepare_team_scan(hackathon, actor, token, session, scanned_at)
        wanted = list(dict.fromkeys(int(u) for u in (user_ids or []) if str(u).lstrip('-').isdigit()))
        if not wanted:
            raise HackathonError('Pick at least one person.', 'NOBODY_SELECTED', field='user_ids')
        override_reason = CheckInService._clean_override(hackathon, actor, reason) if override else ''
        if not placed and not override_reason:
            raise HackathonError(f'{team.name} is not registered. Say why they are being let in.', 'REASON_REQUIRED', field='reason')
        members = {m.user_id: m for m in team.memberships.select_related('user', 'team')}
        batch_id = uuid.uuid4()
        results = []
        for user_id in wanted:
            membership = members.get(user_id)
            if membership is None:
                results.append({'user_id': user_id, 'name': None, 'status': 'refused', 'code': 'NOT_IN_TEAM', 'detail': 'That person is not on this team.'})
                continue
            name = _full_name(membership.user)
            try:
                with transaction.atomic():
                    record, created = CheckInService._record(
                        hackathon, actor, session, membership, when, via=CheckInVia.TEAM_QR,
                        override_reason=override_reason, batch_id=batch_id, audit=False,
                    )
            except HackathonError as exc:
                results.append({'user_id': user_id, 'name': name, 'status': 'refused', 'code': exc.detail['code'], 'detail': exc.detail['detail']})
                continue
            results.append({
                'user_id': user_id, 'name': name, 'status': 'created' if created else 'already', 'checked_in_at': record.checked_in_at,
                'via': record.via,
            })
        created = [r for r in results if r['status'] == 'created']
        if created:
            log_audit_event(
                actor=actor, action='Checked In Team Members', target_model='Hackathon', target_id=hackathon.slug,
                details={
                    'team': team.name, 'session': session.label, 'batch': str(batch_id),
                    'people': [members[r['user_id']].user.email for r in created], 'override_reason': override_reason,
                },
            )
        return {
            'batch_id': str(batch_id) if created else None, 'team': {'id': team.id, 'name': team.name}, 'session_id': session.id,
            'results': results, 'created': len(created),
            'already': sum(1 for r in results if r['status'] == 'already'), 'refused': sum(1 for r in results if r['status'] == 'refused'),
        }

    @staticmethod
    @transaction.atomic
    def undo_batch(hackathon: Hackathon, actor, batch_id, cascade: bool = False) -> int:
        """Take back everyone one team scan checked in. Returns how many were removed."""
        CheckInService._require_feature(hackathon, actor)
        try:
            batch = uuid.UUID(str(batch_id))
        except ValueError:
            raise HackathonError('That batch does not exist.', 'INVALID', field='batch_id')
        records = CheckIn.objects.filter(hackathon=hackathon, batch_id=batch).select_related('session')
        first = records.first()
        if first is None:
            return 0
        people = list(records.values_list('user_id', flat=True))
        if first.session.is_main:
            later = CheckIn.objects.filter(hackathon=hackathon, user_id__in=people).exclude(session=first.session)
            if later.exists():
                if not cascade:
                    raise HackathonError(
                        'Some of these people are checked in to later sessions too. Undo those first, or remove them all together.',
                        'HAS_LATER_CHECKINS', later=later.count(),
                    )
                later.delete()
        removed, _ = records.delete()
        log_audit_event(
            actor=actor, action='Undid Team Check-in', target_model='Hackathon', target_id=hackathon.slug,
            details={'batch': str(batch), 'session': first.session.label, 'people': removed},
        )
        return removed

    @staticmethod
    @transaction.atomic
    def undo(hackathon: Hackathon, actor, user_id: int, session=None, cascade: bool = False) -> None:
        """
        Take one person off a session. Undoing the main (arrival) check-in while they are checked in to later
        sessions is refused unless ``cascade`` is set, which removes those too.
        """
        CheckInService._require_feature(hackathon, actor)
        session = check_in_sessions.resolve(hackathon, session)
        records = CheckIn.objects.filter(hackathon=hackathon, user_id=user_id)
        later = records.exclude(session=session)
        if session.is_main and later.exists():
            if not cascade:
                raise HackathonError(
                    'This person is checked in to later sessions too. Undo those first, or remove them all together.',
                    'HAS_LATER_CHECKINS', later=later.count(),
                )
            later.delete()
        deleted, _ = records.filter(session=session).delete()
        if deleted:
            log_audit_event(
                actor=actor, action='Undid Participant Check-in', target_model='Hackathon', target_id=hackathon.slug,
                details={'user_id': user_id, 'session': session.label, 'with_later_sessions': bool(cascade and session.is_main)},
            )

    @staticmethod
    def roster(
        hackathon: Hackathon, *, query='', missing=False, limit=DEFAULT_ROSTER_LIMIT, offset=0, with_held_back=False, session=None,
    ) -> dict:
        """Everyone on a placed team, with who has been checked in to ``session`` (the main one by default): counts for
        the whole event plus one filtered page.

        ``held_back`` (organizers, search only) lists matching people whose team is not placed, so they can be let in.
        """
        session = check_in_sessions.resolve(hackathon, session)
        limit = max(1, min(int(limit), MAX_ROSTER_LIMIT))
        offset = max(0, int(offset))
        arrived = {c.user_id: c for c in CheckIn.objects.filter(session=session).select_related('checked_in_by')}
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
                'via': record.via if record else None,
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
            'count': count, 'limit': limit, 'offset': offset, 'held_back': held_back, 'session': session_summary(session),
        }

    @staticmethod
    def stats(hackathon: Hackathon, actor, session=None) -> dict:
        """Who scanned how many people, and when people arrived (gaps between the first and last arrival are zero-filled)."""
        CheckInService._require_organizer(hackathon, actor)
        session = check_in_sessions.resolve(hackathon, session)
        records = list(CheckIn.objects.filter(session=session).select_related('checked_in_by'))
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
            'volunteers': volunteers, 'bucket_minutes': bucket_minutes, 'timeline': timeline, 'session': session_summary(session),
            'by_via': dict(Counter(r.via for r in records)),
            'team_scans': len({r.batch_id for r in records if r.batch_id}),
        }

    @staticmethod
    def _require_organizer(hackathon: Hackathon, actor):
        if not hackathon.feature('check_in'):
            raise HackathonError('Check-in is not enabled for this hackathon.', 'FEATURE_OFF')
        if not has_permission(hackathon, actor, 'teams'):
            raise PermissionDenied('Only organizers can follow up on no-shows.')

    @staticmethod
    def _no_show_teams(hackathon: Hackathon, session: CheckInSession | None = None):
        """Placed teams where nobody has been checked in to the session (the arrival session when none is given)."""
        session = session or hackathon.main_check_in_session()
        return (
            hackathon.teams.filter(status=TeamStatus.REGISTERED, is_waitlisted=False)
            .exclude(id__in=CheckIn.objects.filter(session=session).values('team_id'))
            .prefetch_related('memberships__user').order_by('name')
        )

    @staticmethod
    def no_shows(hackathon: Hackathon, actor, session=None) -> dict:
        CheckInService._require_organizer(hackathon, actor)
        session = check_in_sessions.resolve(hackathon, session)
        teams = list(CheckInService._no_show_teams(hackathon, session))
        absent_members = _placed_members(hackathon).exclude(user_id__in=CheckIn.objects.filter(session=session).values('user_id')).count()
        return {
            'window': window_state(hackathon, session=session),
            'session': session_summary(session),
            'teams': [{'team_id': t.id, 'name': t.name, 'members': t.memberships.count()} for t in teams],
            'absent_participants': absent_members,
            'waitlisted_teams': hackathon.teams.filter(is_waitlisted=True, status__in=(TeamStatus.FORMING, TeamStatus.REGISTERED)).count(),
        }

    @staticmethod
    def remind_absent(hackathon: Hackathon, actor, session=None) -> int:
        """Nudge every placed participant who has not been checked in to the session (the arrival session when none is given)."""
        from .services import _notify_users

        CheckInService._require_organizer(hackathon, actor)
        session = check_in_sessions.resolve(hackathon, session)
        if not session.is_scannable:
            raise HackathonError('That session is switched off, so there is nobody to remind.', 'SESSION_OFF', field='session')
        arrived = CheckIn.objects.filter(session=session).values('user_id')
        people = [m.user for m in _placed_members(hackathon).exclude(user_id__in=arrived).select_related('user')]
        where = f' at {hackathon.venue}' if hackathon.venue else ''
        if session.is_main:
            title = f'{hackathon.title}: please check in'
            body = f'We have not seen you yet{where}. Open your dashboard and show your QR code at the entrance.'
        else:
            title = f'{hackathon.title}: {session.label} check-in'
            body = f'We have not seen you at {session.label} yet{where}. Open your dashboard and show your QR code to a volunteer.'
        _notify_users(people, title, body, hackathon, actor)
        details = {'people': len(people)}
        if not session.is_main:
            details['session'] = session.label
        log_audit_event(actor=actor, action='Reminded Absent Participants To Check In', target_model='Hackathon', target_id=hackathon.slug, details=details)
        return len(people)

    @staticmethod
    def withdraw_no_shows(hackathon: Hackathon, actor, team_ids, session=None) -> int:
        """Withdraw the chosen no-show teams; the waitlist is then promoted by the normal status change."""
        from .services import TeamService

        CheckInService._require_organizer(hackathon, actor)
        if not check_in_sessions.resolve(hackathon, session).is_main:
            raise HackathonError('Teams are withdrawn for missing the arrival check-in only.', 'ARRIVAL_ONLY', field='session')
        if window_state(hackathon) == 'OPEN' and hackathon.check_in_closes_at:
            raise HackathonError('Check-in is still open. Withdraw no-shows after it closes.', 'CHECK_IN_STILL_OPEN')
        wanted = {int(t) for t in team_ids or [] if str(t).isdigit()}
        teams = [t for t in CheckInService._no_show_teams(hackathon) if t.id in wanted]
        for team in teams:
            TeamService.admin_set_status(team, actor, TeamStatus.WITHDRAWN, NO_SHOW_WITHDRAW_REASON)
        return len(teams)


def session_summary(session: CheckInSession) -> dict:
    """What the screens need to name a session and know whether it can be scanned right now."""
    opens, closes = session.window()
    return {
        'id': session.id, 'name': session.label, 'day_index': session.day_index, 'slot': CheckInSlot(session.slot).name, 'date': session.date,
        'is_main': session.is_main, 'needs_arrival': session.needs_arrival, 'counts_for_certificate': session.counts_for_certificate,
        'scan_mode': session.scan_mode, 'is_active': session.is_active, 'outside_schedule': session.outside_schedule,
        'opens_at': opens, 'closes_at': closes, 'window': check_in_sessions.window_state(session),
    }


def any_session_open(hackathon: Hackathon) -> bool:
    """Whether people could be scanned into some session right now, so a code is worth showing."""
    hackathon.main_check_in_session()
    return any(
        check_in_sessions.window_state(session) == 'OPEN'
        for session in hackathon.check_in_sessions.all() if session.is_scannable
    )


def sessions_overview(hackathon: Hackathon) -> list[dict]:
    """Every session with how many people are checked in to it (people who run the check-in desk see this)."""
    from django.db.models import Count

    hackathon.main_check_in_session()
    rows = hackathon.check_in_sessions.annotate(checked_in=Count('check_ins')).order_by('day_index', 'slot')
    return [{**session_summary(session), 'checked_in': session.checked_in} for session in rows]


def my_sessions(hackathon: Hackathon, user) -> list[dict]:
    """The sessions as one participant sees them: scannable ones, with whether and when they were checked in."""
    mine = {c.session_id: c for c in CheckIn.objects.filter(hackathon=hackathon, user=user)}
    rows = []
    for session in hackathon.check_in_sessions.order_by('day_index', 'slot'):
        record = mine.get(session.id)
        if not session.is_scannable and record is None:
            continue
        summary = session_summary(session)
        rows.append({
            'id': summary['id'], 'name': summary['name'], 'is_main': summary['is_main'], 'date': summary['date'], 'window': summary['window'],
            'opens_at': summary['opens_at'], 'closes_at': summary['closes_at'],
            'checked_in': record is not None, 'checked_in_at': record.checked_in_at if record else None,
        })
    return rows


def _is_unplaced(membership: TeamMember) -> bool:
    return membership.team.status != TeamStatus.REGISTERED or membership.team.is_waitlisted
