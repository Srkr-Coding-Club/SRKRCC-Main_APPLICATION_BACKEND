"""
Check-in sessions: the days and slots of an event people can be scanned into (Day 1 Morning, Day 2 Afternoon, ...).

The hackathon's ``check_in_days`` and ``check_in_slots_per_day`` generate them; organizers can also add sessions by hand
(``is_custom``: their own title, date and time), which the schedule never touches. The main session is the arrival check-in
and what the rest of the app means by "checked in" (certificates, round rules, no-shows, analytics). Sessions that have
people checked in are never deleted: when the schedule shrinks they are kept as ``outside_schedule``.
"""
from __future__ import annotations

import datetime
from datetime import timedelta

from django.db import transaction
from django.db.models import F
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.audit.utils import log_audit_event

from .errors import HackathonError
from .models import CheckIn, CheckInScanMode, CheckInSession, CheckInSlot, Hackathon

MAX_DAYS = 14
SLOT_ORDER = (CheckInSlot.MORNING, CheckInSlot.AFTERNOON, CheckInSlot.EVENING)
DEFAULT_NAME = 'Check-in'
MAX_CUSTOM_SESSIONS = 60


def ordered(queryset):
    """Sessions in the order the event happens: by day, then slot, then opening time (hand-made sessions have no slot and follow it)."""
    return queryset.order_by(F('day_index').asc(), F('slot').asc(nulls_last=True), F('opens_at').asc(nulls_last=True), 'id')


def clamp_schedule(days, slots) -> tuple[int, int]:
    """The days and slots per day actually used: whole numbers within what the app supports."""
    def whole(value, default):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    return max(1, min(whole(days, 1), MAX_DAYS)), max(1, min(whole(slots, 1), len(SLOT_ORDER)))


def first_day(hackathon: Hackathon):
    start = hackathon.start_date
    return timezone.localtime(start).date() if start else timezone.localdate()


@transaction.atomic
def sync(hackathon: Hackathon) -> list[CheckInSession]:
    """
    Make the sessions match the hackathon's days and slots: add what is missing, refresh dates, retire sessions that
    left the schedule but have people checked in, and delete the ones that never had anyone. The main session stays.
    """
    days, slots = clamp_schedule(hackathon.check_in_days, hackathon.check_in_slots_per_day)
    main = hackathon.main_check_in_session()
    wanted = {(day, slot) for day in range(days) for slot in SLOT_ORDER[:slots]}
    wanted.add((main.day_index, main.slot))
    existing = {(s.day_index, s.slot): s for s in hackathon.check_in_sessions.filter(is_custom=False)}
    start = first_day(hackathon)

    for day, slot in sorted(wanted, key=lambda key: (key[0], SLOT_ORDER.index(key[1]))):
        date = start + timedelta(days=day)
        session = existing.get((day, slot))
        if session is None:
            hackathon.check_in_sessions.create(day_index=day, slot=slot, date=date, needs_arrival=True, counts_for_certificate=False)
            continue
        changed = []
        if session.date != date:
            session.date = date
            changed.append('date')
        if session.outside_schedule:
            session.outside_schedule = False
            changed.append('outside_schedule')
        if changed:
            session.save(update_fields=changed + ['updated_at'])

    for key, session in existing.items():
        if key in wanted:
            continue
        if session.check_ins.exists():
            if not session.outside_schedule:
                session.outside_schedule = True
                session.save(update_fields=['outside_schedule', 'updated_at'])
        else:
            session.delete()
    return list(ordered(hackathon.check_in_sessions.all()))


MAX_NAME_LENGTH = 80
EDITABLE_FLAGS = ('needs_arrival', 'counts_for_certificate', 'is_active')


def _moment(value, field):
    """An ISO date-time (or empty = none) from a request body."""
    if value in (None, ''):
        return None
    when = parse_datetime(str(value))
    if when is None or timezone.is_naive(when):
        raise HackathonError('Give the time as a full date and time.', 'INVALID', field=field)
    return when


@transaction.atomic
def update(session: CheckInSession, actor, data: dict) -> CheckInSession:
    """
    Organizer edits to one session: its name, window, switches and scan mode. The main session's window is the
    hackathon's own (where it has always been edited), so it is written there; the main session is never switched off
    and always counts for the certificate and never needs arrival.
    """
    hackathon = session.hackathon
    changes = {}
    if 'name' in data:
        name = ' '.join(str(data['name'] or '').split())
        if len(name) > MAX_NAME_LENGTH:
            raise HackathonError(f'Keep the name under {MAX_NAME_LENGTH} characters.', 'INVALID', field='name')
        changes['name'] = name
    for flag in EDITABLE_FLAGS:
        if flag in data:
            if not isinstance(data[flag], bool):
                raise HackathonError('This must be on or off.', 'INVALID', field=flag)
            changes[flag] = data[flag]
    if 'outside_schedule' in data and session.is_custom:
        if data['outside_schedule'] is not False:
            raise HackathonError('A session can only be brought back; deleting it is what retires it.', 'INVALID', field='outside_schedule')
        changes['outside_schedule'] = False
    if 'date' in data and session.is_custom:
        date = _clean_date(hackathon, data['date'])
        changes['date'], changes['day_index'] = date, _day_index(hackathon, date)
    if 'name' in changes and session.is_custom:
        changes['name'] = _clean_title(hackathon, changes['name'], changes.get('date', session.date), exclude_id=session.pk)
    if 'scan_mode' in data:
        if data['scan_mode'] not in CheckInScanMode.values:
            raise HackathonError(f"Choose one of: {', '.join(CheckInScanMode.values)}.", 'INVALID', field='scan_mode')
        changes['scan_mode'] = data['scan_mode']
    opens, closes = session.window()
    if 'opens_at' in data:
        opens = _moment(data['opens_at'], 'opens_at')
    if 'closes_at' in data:
        closes = _moment(data['closes_at'], 'closes_at')
    if opens and closes and closes <= opens:
        raise HackathonError('It must close after it opens.', 'INVALID', field='closes_at')
    if session.is_main:
        changes.pop('needs_arrival', None)
        if changes.get('is_active') is False:
            raise HackathonError('The arrival check-in cannot be switched off.', 'MAIN_SESSION_ALWAYS_ON', field='is_active')
        changes['counts_for_certificate'] = True
        if 'opens_at' in data or 'closes_at' in data:
            hackathon.check_in_opens_at, hackathon.check_in_closes_at = opens, closes
            hackathon.save(update_fields=['check_in_opens_at', 'check_in_closes_at', 'updated_at'])
    else:
        changes['opens_at'], changes['closes_at'] = opens, closes
    for field, value in changes.items():
        setattr(session, field, value)
    session.save(update_fields=[*changes, 'updated_at'])
    log_audit_event(
        actor=actor, action='Edited Check-in Session', target_model='Hackathon', target_id=hackathon.slug,
        details={'session': session.label, 'changes': sorted(set(changes) | ({'opens_at', 'closes_at'} & set(data)))},
    )
    return session


def _day_index(hackathon: Hackathon, date) -> int:
    return (date - first_day(hackathon)).days


def _clean_date(hackathon: Hackathon, value):
    """A session's date: a real date on a day of the event."""
    if isinstance(value, str):
        try:
            date = datetime.date.fromisoformat(value.strip())
        except ValueError:
            raise HackathonError('Give the date as YYYY-MM-DD.', 'INVALID', field='date')
    elif isinstance(value, datetime.date):
        date = value
    else:
        raise HackathonError('Choose the day this session happens.', 'INVALID', field='date')
    last = timezone.localtime(hackathon.end_date).date() if hackathon.end_date else first_day(hackathon)
    if date < first_day(hackathon) or date > last:
        raise HackathonError('Choose a day the event runs.', 'INVALID', field='date')
    return date


def _clean_title(hackathon: Hackathon, value, date, *, exclude_id=None) -> str:
    title = ' '.join(str(value or '').split())
    if not title:
        raise HackathonError('Give the session a title.', 'INVALID', field='name')
    if len(title) > MAX_NAME_LENGTH:
        raise HackathonError(f'Keep the name under {MAX_NAME_LENGTH} characters.', 'INVALID', field='name')
    same_day = hackathon.check_in_sessions.filter(date=date).exclude(pk=exclude_id)
    if any(item.label.lower() == title.lower() for item in same_day):
        raise HackathonError('A session with that title already exists on that day.', 'DUPLICATE', field='name')
    return title


@transaction.atomic
def create(hackathon: Hackathon, actor, data: dict) -> CheckInSession:
    """An organizer's own session (Lunch, Prize ceremony, ...): title and day required, window and rules optional."""
    hackathon.main_check_in_session()
    if hackathon.check_in_sessions.filter(is_custom=True).count() >= MAX_CUSTOM_SESSIONS:
        raise HackathonError(f'A hackathon can have at most {MAX_CUSTOM_SESSIONS} sessions of your own.', 'TOO_MANY')
    date = _clean_date(hackathon, data.get('date'))
    title = _clean_title(hackathon, data.get('name'), date)
    opens, closes = _moment(data.get('opens_at'), 'opens_at'), _moment(data.get('closes_at'), 'closes_at')
    if opens and closes and closes <= opens:
        raise HackathonError('It must close after it opens.', 'INVALID', field='closes_at')
    scan_mode = data.get('scan_mode', CheckInScanMode.BOTH)
    if scan_mode not in CheckInScanMode.values:
        raise HackathonError(f"Choose one of: {', '.join(CheckInScanMode.values)}.", 'INVALID', field='scan_mode')
    flags = {}
    for flag, default in (('needs_arrival', True), ('counts_for_certificate', False)):
        value = data.get(flag, default)
        if not isinstance(value, bool):
            raise HackathonError('This must be on or off.', 'INVALID', field=flag)
        flags[flag] = value
    session = hackathon.check_in_sessions.create(
        is_custom=True, slot=None, day_index=_day_index(hackathon, date), date=date, name=title,
        opens_at=opens, closes_at=closes, scan_mode=scan_mode, **flags,
    )
    log_audit_event(
        actor=actor, action='Added Check-in Session', target_model='Hackathon', target_id=hackathon.slug,
        details={'session': session.label, 'date': str(date), 'scan_mode': scan_mode},
    )
    return session


@transaction.atomic
def delete(session: CheckInSession, actor) -> str:
    """Remove a session an organizer added. One that already has people checked in is retired instead; returns which."""
    if not session.is_custom:
        raise HackathonError('Only sessions you added yourself can be deleted. Change the days and slots to remove the others.', 'NOT_CUSTOM')
    label, hackathon = session.label, session.hackathon
    if session.check_ins.exists():
        session.outside_schedule = True
        session.save(update_fields=['outside_schedule', 'updated_at'])
        outcome = 'retired'
    else:
        session.delete()
        outcome = 'deleted'
    log_audit_event(
        actor=actor, action='Removed Check-in Session', target_model='Hackathon', target_id=hackathon.slug,
        details={'session': label, 'outcome': outcome},
    )
    return outcome


def main_check_ins(hackathon: Hackathon):
    """Everyone checked in to the arrival session: what "checked in" means for certificates, rounds and no-shows."""
    return CheckIn.objects.filter(hackathon=hackathon, session__is_main=True)


def has_arrived(hackathon: Hackathon, user_id) -> bool:
    return main_check_ins(hackathon).filter(user_id=user_id).exists()


def resolve(hackathon: Hackathon, session=None) -> CheckInSession:
    """The session named by an object, an id (as a string too) or nothing (the main session); it must belong to this hackathon."""
    if session in (None, ''):
        return hackathon.main_check_in_session()
    if isinstance(session, CheckInSession):
        found = session if session.hackathon_id == hackathon.id else None
    else:
        try:
            found = hackathon.check_in_sessions.filter(pk=int(session)).first()
        except (TypeError, ValueError):
            found = None
    if found is None:
        raise HackathonError('That check-in session does not exist.', 'SESSION_NOT_FOUND', field='session', status=404)
    return found


def window_state(session: CheckInSession, at=None) -> str:
    """NOT_OPEN, OPEN or CLOSED for a session's own window (no times set = always OPEN)."""
    at = at or timezone.now()
    opens, closes = session.window()
    if opens and at < opens:
        return 'NOT_OPEN'
    if closes and at >= closes:
        return 'CLOSED'
    return 'OPEN'
