from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.forms.models import FieldType, FormStatus
from apps.forms.tests.factories import add_field, make_form

from apps.attendance.models import AttendanceBadge, AttendanceRecord, AttendanceSession, SessionLabel
from apps.attendance.services import (
    generate_sessions,
    issue_badge,
    resolve_display_name,
    scan_time_allowed,
)

User = get_user_model()


def make_attendance_form(**kwargs):
    defaults = dict(
        status=FormStatus.PUBLISHED,
        attendance_enabled=True,
        attendance_start_date=timezone.now().date(),
        attendance_days=2,
        attendance_sessions_per_day=2,
    )
    defaults.update(kwargs)
    return make_form(**defaults)


class GenerateSessionsTests(TestCase):
    def test_creates_expected_day_by_session_grid(self):
        form = make_attendance_form(attendance_days=2, attendance_sessions_per_day=2)
        undeletable = generate_sessions(form)

        self.assertEqual(undeletable, [])
        sessions = AttendanceSession.objects.filter(form=form).order_by('day_index', 'session_label')
        self.assertEqual(sessions.count(), 4)  # 2 days x 2 sessions/day
        labels_by_day = {}
        for s in sessions:
            labels_by_day.setdefault(s.day_index, set()).add(s.session_label)
        self.assertEqual(labels_by_day[0], {SessionLabel.MORNING, SessionLabel.AFTERNOON})
        self.assertEqual(labels_by_day[1], {SessionLabel.MORNING, SessionLabel.AFTERNOON})

    def test_single_session_per_day_uses_morning_only(self):
        form = make_attendance_form(attendance_days=1, attendance_sessions_per_day=1)
        generate_sessions(form)
        sessions = AttendanceSession.objects.filter(form=form)
        self.assertEqual(sessions.count(), 1)
        self.assertEqual(sessions.first().session_label, SessionLabel.MORNING)

    def test_three_sessions_per_day(self):
        form = make_attendance_form(attendance_days=1, attendance_sessions_per_day=3)
        generate_sessions(form)
        labels = set(AttendanceSession.objects.filter(form=form).values_list('session_label', flat=True))
        self.assertEqual(labels, {SessionLabel.MORNING, SessionLabel.AFTERNOON, SessionLabel.EVENING})

    def test_idempotent_no_duplicate_rows_on_repeat_call(self):
        form = make_attendance_form(attendance_days=3, attendance_sessions_per_day=2)
        generate_sessions(form)
        first_count = AttendanceSession.objects.filter(form=form).count()
        first_ids = set(AttendanceSession.objects.filter(form=form).values_list('id', flat=True))

        generate_sessions(form)
        generate_sessions(form)

        second_count = AttendanceSession.objects.filter(form=form).count()
        second_ids = set(AttendanceSession.objects.filter(form=form).values_list('id', flat=True))
        self.assertEqual(first_count, second_count)
        self.assertEqual(first_ids, second_ids)

    def test_disabled_attendance_removes_unscanned_sessions(self):
        form = make_attendance_form(attendance_days=2, attendance_sessions_per_day=1)
        generate_sessions(form)
        self.assertEqual(AttendanceSession.objects.filter(form=form).count(), 2)

        form.attendance_enabled = False
        form.save(update_fields=['attendance_enabled'])
        undeletable = generate_sessions(form)

        self.assertEqual(undeletable, [])
        self.assertEqual(AttendanceSession.objects.filter(form=form).count(), 0)

    def test_shrinking_day_count_deletes_only_unscanned_sessions(self):
        form = make_attendance_form(attendance_days=3, attendance_sessions_per_day=1)
        generate_sessions(form)
        self.assertEqual(AttendanceSession.objects.filter(form=form).count(), 3)

        # Record a scan against the last day's session before shrinking.
        response = _make_response(form)
        badge = issue_badge(response)
        day2_session = AttendanceSession.objects.get(form=form, day_index=2)
        AttendanceRecord.objects.create(badge=badge, session=day2_session)

        form.attendance_days = 1
        form.save(update_fields=['attendance_days'])
        undeletable = generate_sessions(form)

        # The scanned session must survive even though it's no longer in the
        # desired (day_index=0 only) set.
        self.assertEqual(len(undeletable), 1)
        self.assertEqual(undeletable[0].id, day2_session.id)
        self.assertTrue(AttendanceSession.objects.filter(id=day2_session.id).exists())

        # The other now-obsolete, never-scanned session (day_index=1) IS removed.
        self.assertFalse(AttendanceSession.objects.filter(form=form, day_index=1).exists())
        # And the still-desired day_index=0 session remains.
        self.assertTrue(AttendanceSession.objects.filter(form=form, day_index=0).exists())

    def test_start_date_change_updates_existing_session_dates(self):
        form = make_attendance_form(attendance_days=1, attendance_sessions_per_day=1)
        generate_sessions(form)
        original_session = AttendanceSession.objects.get(form=form)
        original_date = original_session.date

        new_start = original_date + timedelta(days=5)
        form.attendance_start_date = new_start
        form.save(update_fields=['attendance_start_date'])
        generate_sessions(form)

        original_session.refresh_from_db()
        self.assertEqual(original_session.date, new_start)


class IssueBadgeTests(TestCase):
    def test_get_or_create_returns_same_badge(self):
        form = make_attendance_form()
        response = _make_response(form)

        badge1 = issue_badge(response)
        badge2 = issue_badge(response)

        self.assertEqual(badge1.id, badge2.id)
        self.assertEqual(AttendanceBadge.objects.filter(response=response).count(), 1)

    def test_token_is_generated_and_unique(self):
        form = make_attendance_form()
        r1 = _make_response(form)
        r2 = _make_response(form)
        b1 = issue_badge(r1)
        b2 = issue_badge(r2)
        self.assertTrue(b1.token)
        self.assertNotEqual(b1.token, b2.token)


class ResolveDisplayNameTests(TestCase):
    def test_uses_linked_user_name(self):
        form = make_attendance_form()
        user = User.objects.create_user(
            username="jdoe", email="jdoe@srkr.ac.in", password="x",
            first_name="Jane", last_name="Doe",
        )
        response = _make_response(form, user=user)
        self.assertEqual(resolve_display_name(response), "Jane Doe")

    def test_falls_back_to_answer_heuristic_for_anonymous(self):
        form = make_attendance_form()
        name_field = add_field(form, FieldType.TEXT, label="Full Name", order=99)
        response = _make_response(form)
        from apps.forms.models import Answer
        Answer.objects.create(response=response, field=name_field, value="Ada Lovelace")
        self.assertEqual(resolve_display_name(response), "Ada Lovelace")

    def test_anonymous_fallback(self):
        form = make_attendance_form()
        response = _make_response(form)
        self.assertEqual(resolve_display_name(response), "Anonymous")


class ScanTimeAllowedTests(TestCase):
    def test_no_window_configured_always_allowed(self):
        form = make_attendance_form(attendance_window_minutes=None)
        session = AttendanceSession.objects.create(
            form=form, day_index=0, session_label=SessionLabel.MORNING,
            date=timezone.now().date(), opens_at=timezone.now() - timedelta(days=10),
        )
        self.assertTrue(scan_time_allowed(form, session))

    def test_within_window_allowed(self):
        form = make_attendance_form(attendance_window_minutes=30)
        now = timezone.now()
        session = AttendanceSession.objects.create(
            form=form, day_index=0, session_label=SessionLabel.MORNING,
            date=now.date(), opens_at=now,
        )
        self.assertTrue(scan_time_allowed(form, session, now=now + timedelta(minutes=10)))

    def test_outside_window_rejected(self):
        form = make_attendance_form(attendance_window_minutes=30)
        now = timezone.now()
        session = AttendanceSession.objects.create(
            form=form, day_index=0, session_label=SessionLabel.MORNING,
            date=now.date(), opens_at=now,
        )
        self.assertFalse(scan_time_allowed(form, session, now=now + timedelta(hours=5)))

    def test_no_opens_at_always_allowed(self):
        form = make_attendance_form(attendance_window_minutes=30)
        session = AttendanceSession.objects.create(
            form=form, day_index=0, session_label=SessionLabel.MORNING,
            date=timezone.now().date(), opens_at=None,
        )
        self.assertTrue(scan_time_allowed(form, session))


def _make_response(form, user=None):
    from apps.forms.models import Response
    return Response.objects.create(form=form, user=user, form_version=form.version)
