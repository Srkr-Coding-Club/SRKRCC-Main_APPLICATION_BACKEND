import time
from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework.test import APITransactionTestCase

from apps.forms.models import FieldType, FormStatus, Response as ResponseModel
from apps.notifications.models import Notification, NotificationCategory, NotificationType
from .factories import make_form, add_field

User = get_user_model()


def _wait_until(predicate, timeout=2.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class FormNotificationAutomationTests(APITransactionTestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user(
            username="form_admin", email="admin_form@srkr.ac.in", password="pw12345!", role="ADMIN",
        )
        self.member = User.objects.create_user(
            username="form_member", email="member_form@srkr.ac.in", password="pw12345!", role="NON_AFFILIATE",
        )

        self.form = make_form(
            title="Tech Symposium Registration",
            status=FormStatus.PUBLISHED,
            confirmation_notification_enabled=True,
            notify_admin_on_submission=True,
            notify_members_on_publish=True,
            notification_title="Registration Confirmed!",
            notification_message="You are registered for Tech Symposium.",
            created_by=self.admin,
        )
        self.email_field = add_field(self.form, FieldType.EMAIL, label="Email Address", required=True, order=1)
        self.form.club_id_field_mapping = {"email": self.email_field.id}
        self.form.save(update_fields=["club_id_field_mapping"])

    def tearDown(self):
        cache.clear()

    def _submit_as_member(self, idempotency_key="notif-test-1"):
        self.client.force_authenticate(self.member)
        body = {
            "form": self.form.id,
            "answers": [{"field": self.email_field.id, "value": self.member.email}],
            "idempotency_key": idempotency_key,
        }
        return self.client.post("/api/forms/submissions/", body, format="json")

    def test_submission_dispatches_in_app_notification_to_submitter_and_admin(self):
        resp = self._submit_as_member("notif-auto-1")
        self.assertEqual(resp.status_code, 201, resp.data)
        response_id = resp.data["id"]

        # Submitter notification
        def _submitter_notif_exists():
            return Notification.objects.filter(
                recipient=self.member,
                category=NotificationCategory.FORM,
                type=NotificationType.SUCCESS,
                response_id=response_id,
            ).exists()

        self.assertTrue(_wait_until(_submitter_notif_exists), "Submitter notification was never created")
        member_notif = Notification.objects.get(
            recipient=self.member,
            category=NotificationCategory.FORM,
            response_id=response_id,
        )
        self.assertEqual(member_notif.title, "Registration Confirmed!")
        self.assertEqual(member_notif.message, "You are registered for Tech Symposium.")
        self.assertEqual(member_notif.link_url, f"/forms/{self.form.slug}")

        # Admin notification
        def _admin_notif_exists():
            return Notification.objects.filter(
                recipient=self.admin,
                category=NotificationCategory.FORM,
                type=NotificationType.INFO,
                response_id=response_id,
            ).exists()

        self.assertTrue(_wait_until(_admin_notif_exists), "Admin notification was never created")
        admin_notif = Notification.objects.get(
            recipient=self.admin,
            category=NotificationCategory.FORM,
            response_id=response_id,
        )
        self.assertIn("New Response", admin_notif.title)
        self.assertEqual(admin_notif.link_url, f"/admin/responses?form={self.form.slug}")

        # Verify ResponseDetailSerializer displays confirmation_notification
        self.client.force_authenticate(self.admin)
        detail = self.client.get(f"/api/forms/submissions/{response_id}/")
        self.assertEqual(detail.status_code, 200)
        self.assertTrue(detail.data["confirmation_notification_enabled"])
        self.assertIsNotNone(detail.data["confirmation_notification"])
        self.assertEqual(detail.data["confirmation_notification"]["title"], "Registration Confirmed!")

    def test_admin_can_resend_submission_notification(self):
        resp = self._submit_as_member("notif-resend-1")
        response_id = resp.data["id"]

        def _first_persisted():
            return Notification.objects.filter(recipient=self.member, response_id=response_id).exists()

        self.assertTrue(_wait_until(_first_persisted), "First notification was never persisted")
        self.assertEqual(Notification.objects.filter(recipient=self.member, response_id=response_id).count(), 1)

        # Admin calls resend action
        self.client.force_authenticate(self.admin)
        resend_resp = self.client.post(f"/api/forms/submissions/{response_id}/resend-notification/")
        self.assertEqual(resend_resp.status_code, 200, resend_resp.data)
        self.assertTrue(resend_resp.data["success"])
        self.assertEqual(resend_resp.data["status"], "SENT")

        # Now two notifications exist for member linked to this response
        self.assertEqual(Notification.objects.filter(recipient=self.member, response_id=response_id).count(), 2)

    def test_publish_form_broadcasts_in_app_notification(self):
        draft_form = make_form(
            title="Winter Hackathon Registration",
            status=FormStatus.DRAFT,
            notify_members_on_publish=True,
            created_by=self.admin,
        )
        add_field(draft_form, FieldType.TEXT, label="Full Name", required=True)

        self.client.force_authenticate(self.admin)
        pub_resp = self.client.post(f"/api/forms/{draft_form.slug}/publish/")
        self.assertEqual(pub_resp.status_code, 200, pub_resp.data)

        # Check broadcast notification was generated for members
        member_notif = Notification.objects.filter(
            recipient=self.member,
            category=NotificationCategory.FORM,
            link_url=f"/forms/{draft_form.slug}",
        ).first()
        self.assertIsNotNone(member_notif)
        self.assertIn(draft_form.title, member_notif.title)
