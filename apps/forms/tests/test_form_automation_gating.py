"""
FormSerializer.validate() gates the automation-completeness checks (Club ID
field mapping, confirmation email template, attendance start date) the same
way it already gates form-definition structural errors: DRAFT saves stay
lenient, PUBLISHED/SCHEDULED must be fully configured.

Regression coverage for a bug where the Form Builder's own UI told admins to
"enable Club ID generation, save once, then come back to map the email field"
— but the very first Save Draft was rejected with 400, because the backend
required the mapping to already exist regardless of target status. Same class
of bug for confirmation_email_template and attendance_start_date.
"""
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from .factories import make_form, add_field
from apps.forms.models import FieldType, FormStatus

User = get_user_model()


class AutomationCompletenessGatingTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin", email="admin@srkr.ac.in", password="x", role="ADMIN", is_staff=True,
        )
        self.client.force_authenticate(self.admin)
        self.form = make_form(status=FormStatus.DRAFT)
        self.email_field = add_field(self.form, FieldType.EMAIL, label="Email", required=True, order=1)

    def test_draft_save_with_club_id_enabled_and_no_mapping_succeeds(self):
        resp = self.client.patch(
            f"/api/forms/{self.form.slug}/",
            {"club_id_enabled": True},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_publishing_with_club_id_enabled_and_no_mapping_is_rejected(self):
        resp = self.client.patch(
            f"/api/forms/{self.form.slug}/",
            {"club_id_enabled": True, "status": "PUBLISHED"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("club_id_field_mapping", resp.data)

    def test_publishing_with_club_id_enabled_and_mapping_succeeds(self):
        resp = self.client.patch(
            f"/api/forms/{self.form.slug}/",
            {
                "club_id_enabled": True,
                "club_id_field_mapping": {"email": self.email_field.id},
                "status": "PUBLISHED",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_draft_save_with_confirmation_email_enabled_and_no_template_succeeds(self):
        resp = self.client.patch(
            f"/api/forms/{self.form.slug}/",
            {"confirmation_email_enabled": True},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_publishing_with_confirmation_email_enabled_and_no_template_is_rejected(self):
        resp = self.client.patch(
            f"/api/forms/{self.form.slug}/",
            {"confirmation_email_enabled": True, "status": "PUBLISHED"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("confirmation_email_template", resp.data)

    def test_draft_save_with_attendance_enabled_and_no_start_date_succeeds(self):
        resp = self.client.patch(
            f"/api/forms/{self.form.slug}/",
            {"attendance_enabled": True},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_publishing_with_attendance_enabled_and_no_start_date_is_rejected(self):
        resp = self.client.patch(
            f"/api/forms/{self.form.slug}/",
            {"attendance_enabled": True, "status": "PUBLISHED"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("attendance_start_date", resp.data)
