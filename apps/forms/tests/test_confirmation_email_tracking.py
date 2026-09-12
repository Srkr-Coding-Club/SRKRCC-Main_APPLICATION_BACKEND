import time

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from rest_framework.test import APITransactionTestCase

from apps.core.models import EmailTemplate
from apps.forms.models import FieldType, FormStatus
from .factories import make_form, add_field

User = get_user_model()


def _wait_until(predicate, timeout=2.0, interval=0.02):
    """Polls `predicate()` until it's truthy or `timeout` seconds elapse.

    The confirmation-email auto-fire path now runs on a real background thread
    (apps.core.tasks.run_in_background) instead of executing inline, so tests
    that assert on its effects (mail.outbox, DB status) need to wait for it —
    it usually finishes in well under a millisecond against the in-memory test
    email backend, but must never be assumed synchronous.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class ConfirmationEmailTrackingTests(APITransactionTestCase):
    """
    A form's confirmation email must be trackable per-response (sent/failed/
    pending) and resendable by an admin from the responses viewer.

    Uses APITransactionTestCase (real commits) rather than APITestCase (each
    test wrapped in a rolled-back transaction) because the confirmation email
    now sends on a background thread with its own DB connection — that thread
    can only see rows this test has actually committed, not rows still inside
    an uncommitted per-test transaction.
    """

    def setUp(self):
        # The anon 'form_submit' throttle scope is keyed in the shared cache by
        # client IP and persists across tests in the same process — clear it so
        # this file's several submissions never bleed into (or get starved by)
        # other test files' own submission counts.
        cache.clear()
        self.template = EmailTemplate.objects.create(
            name="confirmation_test",
            subject_template="Thanks for registering, {{full_name}}!",
            html_template="<p>Hi {{first_name}}, we got your submission.</p>",
            allowed_parameters=["full_name", "first_name", "email", "club_id", "branch",
                                 "membership_status", "roll_number", "portal_url", "login_url"],
        )
        self.form = make_form(
            status=FormStatus.PUBLISHED,
            confirmation_email_enabled=True,
            confirmation_email_template=self.template,
        )
        self.email_field = add_field(self.form, FieldType.EMAIL, label="Email", required=True, order=1)
        # Confirmation-email's recipient-resolution fallback (no club-id automation
        # running) reads the recipient from `club_id_field_mapping['email']` — set
        # it directly so the test doesn't have to enable full club-id automation.
        self.form.club_id_field_mapping = {"email": self.email_field.id}
        self.form.save(update_fields=["club_id_field_mapping"])

        self.admin = User.objects.create_user(
            username="admin1", email="admin1@srkr.ac.in", password="pw12345!", role="ADMIN",
        )
        self.member = User.objects.create_user(
            username="member1", email="member1@srkr.ac.in", password="pw12345!", role="MEMBER",
        )

    def tearDown(self):
        cache.clear()

    def _submit(self):
        body = {
            "form": self.form.id,
            "answers": [{"field": self.email_field.id, "value": self.member.email}],
            "idempotency_key": "confirm-test-1",
        }
        return self.client.post("/api/forms/submissions/", body, format="json")

    def test_submission_sends_and_tracks_confirmation_email(self):
        resp = self._submit()
        self.assertEqual(resp.status_code, 201, resp.data)
        response_id = resp.data["id"]

        # Sent on a background thread. `mail.outbox` gets its entry appended
        # slightly BEFORE the delivery/job rows are saved (send, then persist),
        # so poll the actual persisted status rather than the outbox alone —
        # otherwise the GET below can race the thread's own DB writes.
        self.client.force_authenticate(self.admin)

        def _confirmation_sent():
            r = self.client.get(f"/api/forms/submissions/{response_id}/")
            return r.status_code == 200 and (r.data.get("confirmation_email") or {}).get("status") == "SENT"

        self.assertTrue(_wait_until(_confirmation_sent), "confirmation email was never marked SENT")

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.member.email, mail.outbox[0].to)

        detail = self.client.get(f"/api/forms/submissions/{response_id}/")
        self.assertEqual(detail.status_code, 200)
        self.assertTrue(detail.data["confirmation_email_enabled"])
        self.assertIsNotNone(detail.data["confirmation_email"])
        self.assertEqual(detail.data["confirmation_email"]["status"], "SENT")
        self.assertEqual(detail.data["confirmation_email"]["recipient_email"], self.member.email)

    def test_admin_can_resend_confirmation_email(self):
        resp = self._submit()
        response_id = resp.data["id"]
        self.client.force_authenticate(self.admin)

        # Wait for the auto-fire background send to fully persist (not just hit
        # the outbox) before resending, so the final delivery-count assertion
        # below can't race the first send's own DB writes.
        from apps.forms.models import Response as ResponseModel

        def _first_delivery_persisted():
            return ResponseModel.objects.get(pk=response_id).confirmation_email_deliveries.filter(status="SENT").exists()

        self.assertTrue(_wait_until(_first_delivery_persisted), "confirmation email was never marked SENT")
        self.assertEqual(len(mail.outbox), 1)

        resend = self.client.post(f"/api/forms/submissions/{response_id}/resend-confirmation-email/")
        self.assertEqual(resend.status_code, 200, resend.data)
        self.assertTrue(resend.data["success"])
        self.assertEqual(resend.data["status"], "SENT")

        # A second email was actually sent, and the response now has 2 tracked deliveries.
        self.assertEqual(len(mail.outbox), 2)
        from apps.forms.models import Response
        r = Response.objects.get(pk=response_id)
        self.assertEqual(r.confirmation_email_deliveries.count(), 2)

    def test_non_admin_cannot_resend(self):
        resp = self._submit()
        response_id = resp.data["id"]

        self.client.force_authenticate(self.member)
        resend = self.client.post(f"/api/forms/submissions/{response_id}/resend-confirmation-email/")
        self.assertEqual(resend.status_code, 403)

    def test_unauthenticated_cannot_resend(self):
        resp = self._submit()
        response_id = resp.data["id"]

        resend = self.client.post(f"/api/forms/submissions/{response_id}/resend-confirmation-email/")
        self.assertIn(resend.status_code, (401, 403))

    def test_resend_on_unconfigured_form_returns_clean_400(self):
        plain_form = make_form(status=FormStatus.PUBLISHED)
        name_field = add_field(plain_form, FieldType.TEXT, label="Name", required=True, order=1)
        body = {
            "form": plain_form.id,
            "answers": [{"field": name_field.id, "value": "No Email Form"}],
            "idempotency_key": "confirm-test-2",
        }
        resp = self.client.post("/api/forms/submissions/", body, format="json")
        response_id = resp.data["id"]

        self.client.force_authenticate(self.admin)
        resend = self.client.post(f"/api/forms/submissions/{response_id}/resend-confirmation-email/")
        self.assertEqual(resend.status_code, 400)
        self.assertIn("error", resend.data)

    def test_response_without_confirmation_email_shows_null_status(self):
        plain_form = make_form(status=FormStatus.PUBLISHED)
        name_field = add_field(plain_form, FieldType.TEXT, label="Name", required=True, order=1)
        body = {
            "form": plain_form.id,
            "answers": [{"field": name_field.id, "value": "No Email Form"}],
            "idempotency_key": "confirm-test-3",
        }
        resp = self.client.post("/api/forms/submissions/", body, format="json")
        response_id = resp.data["id"]

        self.client.force_authenticate(self.admin)
        detail = self.client.get(f"/api/forms/submissions/{response_id}/")
        self.assertFalse(detail.data["confirmation_email_enabled"])
        self.assertIsNone(detail.data["confirmation_email"])
