"""
Business logic for the QR-code attendance feature — kept separate from
views.py per this codebase's convention (see apps/forms/services.py).
"""
from datetime import datetime, time as dtime, timedelta

from django.utils import timezone

from .models import AttendanceBadge, AttendanceRecord, AttendanceSession, SessionLabel

SESSION_LABELS_BY_COUNT = {
    1: [SessionLabel.MORNING],
    2: [SessionLabel.MORNING, SessionLabel.AFTERNOON],
    3: [SessionLabel.MORNING, SessionLabel.AFTERNOON, SessionLabel.EVENING],
}

# Default scheduled clock time for each session label, used to compute a
# session's opens_at/closes_at when it's (re)generated. Just a sensible
# starting point — nothing elsewhere assumes these exact hours.
SESSION_TIME_DEFAULTS = {
    SessionLabel.MORNING: (dtime(9, 0), dtime(12, 0)),
    SessionLabel.AFTERNOON: (dtime(13, 0), dtime(16, 0)),
    SessionLabel.EVENING: (dtime(17, 0), dtime(20, 0)),
}


def _aware(day, clock_time):
    naive = datetime.combine(day, clock_time)
    return timezone.make_aware(naive) if timezone.is_naive(naive) else naive


def generate_sessions(form):
    """
    (Re)generate `form`'s AttendanceSession rows from its current
    attendance_enabled / attendance_start_date / attendance_days /
    attendance_sessions_per_day config.

    Idempotent: calling this repeatedly with an unchanged config makes no
    writes. Non-destructive of scanned data: an AttendanceSession that already
    has one or more AttendanceRecord scans is NEVER deleted, even if the
    current config no longer calls for it (e.g. attendance_days was reduced,
    or attendance was disabled entirely) — such sessions are left exactly as
    they are.

    Returns the list of AttendanceSession instances that the current config no
    longer calls for but could not be removed because they already have scans
    recorded against them, so the caller can surface a warning.
    """
    labels = []
    day_count = 0
    if form.attendance_enabled and form.attendance_start_date:
        day_count = form.attendance_days or 0
        labels = SESSION_LABELS_BY_COUNT.get(form.attendance_sessions_per_day, [SessionLabel.MORNING])

    desired = {}
    for day_index in range(day_count):
        session_date = form.attendance_start_date + timedelta(days=day_index)
        for label in labels:
            desired[(day_index, label)] = session_date

    existing = {
        (s.day_index, s.session_label): s
        for s in AttendanceSession.objects.filter(form=form)
    }

    undeletable = []
    for key, session in existing.items():
        if key in desired:
            continue
        if AttendanceRecord.objects.filter(session=session).exists():
            undeletable.append(session)
        else:
            session.delete()

    for (day_index, label), session_date in desired.items():
        start_t, end_t = SESSION_TIME_DEFAULTS.get(label, (dtime(9, 0), dtime(12, 0)))
        opens_at = _aware(session_date, start_t)
        closes_at = _aware(session_date, end_t)

        session = existing.get((day_index, label))
        if session is None:
            AttendanceSession.objects.create(
                form=form, day_index=day_index, session_label=label,
                date=session_date, opens_at=opens_at, closes_at=closes_at,
            )
            continue

        changed_fields = []
        if session.date != session_date:
            session.date = session_date
            changed_fields.append('date')
        if session.opens_at != opens_at:
            session.opens_at = opens_at
            changed_fields.append('opens_at')
        if session.closes_at != closes_at:
            session.closes_at = closes_at
            changed_fields.append('closes_at')
        if changed_fields:
            session.save(update_fields=changed_fields + ['updated_at'])

    return undeletable


def issue_badge(response):
    """
    get_or_create the permanent AttendanceBadge for a completed Response.
    Safe to call repeatedly (e.g. once at initial submission and again if the
    submission is later edited in place) — a Response only ever gets one
    badge/token.
    """
    badge, _created = AttendanceBadge.objects.get_or_create(response=response)
    return badge


def resolve_display_name(response) -> str:
    """
    Best-effort registrant display name for a Response, used by the scan
    endpoint's result and the attendance report grid. Mirrors the heuristic
    ResponseDetailSerializer.get_user_name already uses elsewhere in this
    codebase (apps/forms/serializers.py): prefer the linked User's name, then
    an admin-manual-entry attribution, then scan the response's own answers
    for a name-shaped field.
    """
    if response.user:
        name = f'{response.user.first_name} {response.user.last_name}'.strip()
        return name or response.user.username or response.user.email

    if response.is_manual_entry and response.created_by_admin:
        return f'Admin: {response.created_by_admin.email}'

    for ans in response.answers.all():
        if ans.field and any(k in ans.field.label.lower() for k in ['name', 'student', 'candidate', 'applicant']):
            if ans.value and str(ans.value).strip():
                return str(ans.value).strip()

    return 'Anonymous'


def scan_time_allowed(form, session, now=None) -> bool:
    """
    True if `now` (default: current time) falls within the allowed scan
    window for `session`, given `form.attendance_window_minutes`. No time
    restriction applies (always True) when the form has no window configured,
    or the session has no scheduled opens_at to measure against.
    """
    window = form.attendance_window_minutes
    if not window or not session.opens_at:
        return True
    now = now or timezone.now()
    delta = timedelta(minutes=window)
    return (session.opens_at - delta) <= now <= (session.opens_at + delta)
