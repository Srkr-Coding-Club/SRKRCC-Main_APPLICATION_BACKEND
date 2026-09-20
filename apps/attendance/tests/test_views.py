from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.forms.models import FormStatus, Response
from apps.forms.tests.factories import make_form

from apps.attendance.models import AttendanceBadge, AttendanceRecord, AttendanceSession, SessionLabel
from apps.attendance.services import generate_sessions, issue_badge

User = get_user_model()


def make_attendance_form(**kwargs):
    defaults = dict(
        status=FormStatus.PUBLISHED,
        attendance_enabled=True,
        attendance_start_date=timezone.now().date(),
        attendance_days=1,
        attendance_sessions_per_day=1,
    )
    defaults.update(kwargs)
    form = make_form(**defaults)
    generate_sessions(form)
    return form


class AttendanceSessionListViewTests(APITestCase):
    def setUp(self):
        self.form = make_attendance_form(attendance_sessions_per_day=2)
        self.admin = User.objects.create_user(username="admin1", email="admin1@srkr.ac.in", password="x", role="ADMIN")
        self.member = User.objects.create_user(username="member1", email="member1@srkr.ac.in", password="x", role="NON_AFFILIATE")

    def test_admin_can_list_sessions(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.get(f"/api/forms/{self.form.id}/attendance/sessions/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(len(resp.data), 2)

    def test_member_forbidden(self):
        self.client.force_authenticate(self.member)
        resp = self.client.get(f"/api/forms/{self.form.id}/attendance/sessions/")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_forbidden(self):
        resp = self.client.get(f"/api/forms/{self.form.id}/attendance/sessions/")
        self.assertIn(resp.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))


class MyBadgeViewTests(APITestCase):
    def setUp(self):
        self.form = make_attendance_form()
        self.user = User.objects.create_user(username="reg1", email="reg1@srkr.ac.in", password="x", role="NON_AFFILIATE")

    def test_returns_badge_for_own_response(self):
        response_obj = Response.objects.create(form=self.form, user=self.user, form_version=self.form.version)
        badge = issue_badge(response_obj)

        self.client.force_authenticate(self.user)
        resp = self.client.get(f"/api/forms/{self.form.id}/attendance/my-badge/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data["token"], badge.token)
        self.assertEqual(resp.data["response_id"], response_obj.id)
        self.assertFalse(resp.data["revoked"])

    def test_404_when_no_response(self):
        self.client.force_authenticate(self.user)
        resp = self.client.get(f"/api/forms/{self.form.id}/attendance/my-badge/")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_404_when_attendance_not_enabled(self):
        plain_form = make_form(status=FormStatus.PUBLISHED, attendance_enabled=False)
        Response.objects.create(form=plain_form, user=self.user, form_version=plain_form.version)
        self.client.force_authenticate(self.user)
        resp = self.client.get(f"/api/forms/{plain_form.id}/attendance/my-badge/")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


class AttendanceScanViewTests(APITestCase):
    def setUp(self):
        self.form = make_attendance_form(attendance_sessions_per_day=1)
        self.session_obj = AttendanceSession.objects.get(form=self.form)
        self.registrant = User.objects.create_user(username="reg2", email="reg2@srkr.ac.in", password="x", role="NON_AFFILIATE")
        self.response_obj = Response.objects.create(
            form=self.form, user=self.registrant, form_version=self.form.version,
        )
        self.badge = issue_badge(self.response_obj)

        self.volunteer = User.objects.create_user(username="vol1", email="vol1@srkr.ac.in", password="x", role="VOLUNTEER")
        self.admin = User.objects.create_user(username="admin2", email="admin2@srkr.ac.in", password="x", role="ADMIN")
        self.member = User.objects.create_user(username="member2", email="member2@srkr.ac.in", password="x", role="NON_AFFILIATE")

    def _scan(self, user, token=None, session_id=None):
        self.client.force_authenticate(user)
        return self.client.post("/api/attendance/scan/", {
            "token": token if token is not None else self.badge.token,
            "session_id": session_id if session_id is not None else self.session_obj.id,
        }, format="json")

    def test_volunteer_can_scan_successfully(self):
        resp = self._scan(self.volunteer)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertTrue(resp.data["new_scan"])
        self.assertFalse(resp.data["already_recorded"])
        self.assertEqual(resp.data["response_id"], self.response_obj.id)
        self.assertTrue(
            AttendanceRecord.objects.filter(badge=self.badge, session=self.session_obj).exists()
        )

    def test_admin_can_scan(self):
        resp = self._scan(self.admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

    def test_member_forbidden_from_scanning(self):
        resp = self._scan(self.member)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(AttendanceRecord.objects.filter(badge=self.badge, session=self.session_obj).exists())

    def test_duplicate_scan_is_idempotent_not_an_error(self):
        first = self._scan(self.volunteer)
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertTrue(first.data["new_scan"])

        second = self._scan(self.volunteer)
        self.assertEqual(second.status_code, status.HTTP_200_OK, second.data)
        self.assertFalse(second.data["new_scan"])
        self.assertTrue(second.data["already_recorded"])

        self.assertEqual(
            AttendanceRecord.objects.filter(badge=self.badge, session=self.session_obj).count(), 1,
        )

    def test_invalid_token_rejected(self):
        resp = self._scan(self.volunteer, token="not-a-real-token")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_revoked_badge_rejected(self):
        self.badge.revoked = True
        self.badge.save(update_fields=["revoked"])
        resp = self._scan(self.volunteer)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data["code"], "BADGE_REVOKED")

    def test_wrong_form_session_rejected(self):
        other_form = make_attendance_form(attendance_sessions_per_day=1)
        other_session = AttendanceSession.objects.get(form=other_form)

        resp = self._scan(self.volunteer, session_id=other_session.id)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data["code"], "FORM_MISMATCH")
        self.assertFalse(AttendanceRecord.objects.filter(badge=self.badge).exists())

    def test_scan_rejected_outside_configured_time_window(self):
        self.form.attendance_window_minutes = 15
        self.form.save(update_fields=["attendance_window_minutes"])
        self.session_obj.opens_at = timezone.now() + timedelta(hours=6)
        self.session_obj.save(update_fields=["opens_at"])

        resp = self._scan(self.volunteer)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data["code"], "OUTSIDE_SCAN_WINDOW")

    def test_scan_allowed_within_configured_time_window(self):
        self.form.attendance_window_minutes = 60
        self.form.save(update_fields=["attendance_window_minutes"])
        self.session_obj.opens_at = timezone.now()
        self.session_obj.save(update_fields=["opens_at"])

        resp = self._scan(self.volunteer)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)


class AttendanceReportViewTests(APITestCase):
    def setUp(self):
        self.form = make_attendance_form(attendance_sessions_per_day=2)
        self.sessions = list(AttendanceSession.objects.filter(form=self.form).order_by('session_label'))
        self.admin = User.objects.create_user(username="admin3", email="admin3@srkr.ac.in", password="x", role="ADMIN")
        self.member = User.objects.create_user(username="member3", email="member3@srkr.ac.in", password="x", role="NON_AFFILIATE")

        self.r1 = Response.objects.create(form=self.form, user=self.member, form_version=self.form.version)
        self.b1 = issue_badge(self.r1)
        AttendanceRecord.objects.create(badge=self.b1, session=self.sessions[0])

        self.other_user = User.objects.create_user(username="member4", email="member4@srkr.ac.in", password="x", role="NON_AFFILIATE")
        self.r2 = Response.objects.create(form=self.form, user=self.other_user, form_version=self.form.version)
        issue_badge(self.r2)  # no scans

    def test_report_shape_and_counts(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.get(f"/api/forms/{self.form.id}/attendance/report/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

        self.assertIn("sessions", resp.data)
        self.assertIn("registrants", resp.data)
        self.assertEqual(len(resp.data["sessions"]), 2)
        self.assertEqual(len(resp.data["registrants"]), 2)

        session0_summary = next(s for s in resp.data["sessions"] if s["id"] == self.sessions[0].id)
        self.assertEqual(session0_summary["attended_count"], 1)
        self.assertEqual(session0_summary["total_registrants"], 2)
        self.assertEqual(session0_summary["percentage"], 50.0)

        row1 = next(r for r in resp.data["registrants"] if r["response_id"] == self.r1.id)
        self.assertTrue(row1["sessions"][self.sessions[0].id])
        self.assertFalse(row1["sessions"][self.sessions[1].id])

    def test_member_forbidden_from_report(self):
        self.client.force_authenticate(self.member)
        resp = self.client.get(f"/api/forms/{self.form.id}/attendance/report/")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
