"""
Form.club_id_verification_enabled — the inverse of club_id_enabled (which
*generates* a Club ID for a submitter who doesn't have one). This automation
*verifies* a Club ID the submitter claims to already have: the submission is
rejected if that Club ID isn't registered, or if any other mapped field
(name/email/phone/branch/roll number) doesn't match that member's actual
record. See apps/forms/serializers.py ResponseSerializer._check_club_id_verification.
"""
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.forms.models import FieldType, FormStatus, Response
from .factories import make_form, add_field, answers

User = get_user_model()


class ClubIdVerificationGatingTests(APITestCase):
    """FormSerializer.validate() gating — mirrors test_form_automation_gating.py."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin", email="admin@srkr.ac.in", password="x", role="ADMIN", is_staff=True,
        )
        self.client.force_authenticate(self.admin)
        self.form = make_form(status=FormStatus.DRAFT)
        self.club_id_field = add_field(self.form, FieldType.TEXT, label="Club ID", required=True, order=1)

    def test_draft_save_with_verification_enabled_and_no_mapping_succeeds(self):
        resp = self.client.patch(
            f"/api/forms/{self.form.slug}/",
            {"club_id_verification_enabled": True},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_publishing_with_verification_enabled_and_no_mapping_is_rejected(self):
        resp = self.client.patch(
            f"/api/forms/{self.form.slug}/",
            {"club_id_verification_enabled": True, "status": "PUBLISHED"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("club_id_field_mapping", resp.data)

    def test_publishing_with_verification_enabled_and_mapping_succeeds(self):
        resp = self.client.patch(
            f"/api/forms/{self.form.slug}/",
            {
                "club_id_verification_enabled": True,
                "club_id_field_mapping": {"club_id": self.club_id_field.id},
                "status": "PUBLISHED",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_enabling_both_generation_and_verification_is_rejected(self):
        resp = self.client.patch(
            f"/api/forms/{self.form.slug}/",
            {"club_id_enabled": True, "club_id_verification_enabled": True},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("club_id_verification_enabled", resp.data)

    def test_enabling_verification_while_generation_already_on_is_rejected(self):
        self.form.club_id_enabled = True
        self.form.club_id_field_mapping = {"email": self.club_id_field.id}
        self.form.save(update_fields=["club_id_enabled", "club_id_field_mapping"])

        resp = self.client.patch(
            f"/api/forms/{self.form.slug}/",
            {"club_id_verification_enabled": True},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("club_id_verification_enabled", resp.data)


class ClubIdVerificationSubmissionTests(APITestCase):
    """Submission-time verification — POST /api/forms/submissions/."""

    def setUp(self):
        self.member = User.objects.create_user(
            username="member1", email="member1@srkr.ac.in", password="x",
            role="NON_AFFILIATE", club_id="25SCC277",
            first_name="Ravi", last_name="Kumar",
            phone_number="9876543210", branch="CSE", roll_number="21B91A0501",
        )

        self.form = make_form(status=FormStatus.PUBLISHED)
        self.club_id_field = add_field(self.form, FieldType.TEXT, label="Club ID", required=True, order=1)
        self.name_field = add_field(self.form, FieldType.TEXT, label="Full Name", required=True, order=2)
        self.email_field = add_field(self.form, FieldType.EMAIL, label="Email", required=True, order=3)
        self.form.club_id_verification_enabled = True
        self.form.club_id_field_mapping = {
            "club_id": self.club_id_field.id,
            "full_name": self.name_field.id,
            "email": self.email_field.id,
        }
        self.form.save(update_fields=["club_id_verification_enabled", "club_id_field_mapping"])

    def _submit(self, club_id, name, email, idempotency_key):
        body = {
            "form": self.form.id,
            "answers": answers(
                (self.club_id_field, club_id),
                (self.name_field, name),
                (self.email_field, email),
            ),
            "idempotency_key": idempotency_key,
        }
        return self.client.post("/api/forms/submissions/", body, format="json")

    def test_matching_club_id_and_details_succeeds(self):
        resp = self._submit("25SCC277", "Ravi Kumar", "member1@srkr.ac.in", "verify-ok-1")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertTrue(Response.objects.filter(form=self.form).exists())

    def test_club_id_is_matched_case_insensitively_and_trimmed(self):
        resp = self._submit(" 25scc277 ", "Ravi Kumar", "member1@srkr.ac.in", "verify-ok-2")
        self.assertEqual(resp.status_code, 201, resp.data)

    def test_name_match_tolerates_case_and_extra_whitespace(self):
        resp = self._submit("25SCC277", "  ravi   kumar  ", "member1@srkr.ac.in", "verify-ok-3")
        self.assertEqual(resp.status_code, 201, resp.data)

    def test_unregistered_club_id_is_rejected(self):
        resp = self._submit("25SCC999", "Ravi Kumar", "member1@srkr.ac.in", "verify-bad-1")
        self.assertEqual(resp.status_code, 400)
        codes = [e.get("code") for e in resp.data.get("errors", [])]
        self.assertIn("CLUB_ID_NOT_FOUND", codes)
        self.assertFalse(Response.objects.filter(form=self.form).exists())

    def test_name_mismatch_is_rejected(self):
        resp = self._submit("25SCC277", "Someone Else", "member1@srkr.ac.in", "verify-bad-2")
        self.assertEqual(resp.status_code, 400)
        codes = [e.get("code") for e in resp.data.get("errors", [])]
        self.assertIn("CLUB_ID_MISMATCH", codes)
        self.assertFalse(Response.objects.filter(form=self.form).exists())

    def test_email_mismatch_is_rejected(self):
        resp = self._submit("25SCC277", "Ravi Kumar", "not-ravi@srkr.ac.in", "verify-bad-3")
        self.assertEqual(resp.status_code, 400)
        codes = [e.get("code") for e in resp.data.get("errors", [])]
        self.assertIn("CLUB_ID_MISMATCH", codes)

    def test_error_is_anchored_to_the_mismatched_field(self):
        resp = self._submit("25SCC277", "Someone Else", "member1@srkr.ac.in", "verify-bad-4")
        self.assertEqual(resp.status_code, 400)
        mismatch = next(e for e in resp.data["errors"] if e["code"] == "CLUB_ID_MISMATCH")
        self.assertEqual(mismatch["field_id"], self.name_field.id)

    def test_someone_elses_valid_club_id_with_own_details_is_rejected(self):
        """The exact scenario this feature exists to catch: a valid Club ID
        that belongs to someone else, paired with the submitter's own (truthful
        but non-matching) details."""
        resp = self._submit("25SCC277", "Different Person", "different@srkr.ac.in", "verify-bad-5")
        self.assertEqual(resp.status_code, 400)
        codes = [e.get("code") for e in resp.data.get("errors", [])]
        self.assertIn("CLUB_ID_MISMATCH", codes)


class DedicatedClubIdFieldTests(APITestCase):
    """A CLUB_ID field enables ID verification without the legacy toggle."""

    def setUp(self):
        self.member = User.objects.create_user(
            username="dedicated-member", email="dedicated@srkr.ac.in", password="x",
            role="NON_AFFILIATE", club_id="26SCC101",
        )
        self.form = make_form(status=FormStatus.PUBLISHED)
        self.club_id_field = add_field(self.form, FieldType.CLUB_ID, label="Club ID", required=True, order=1)

    def _submit(self, value, key):
        return self.client.post(
            "/api/forms/submissions/",
            {"form": self.form.id, "answers": answers((self.club_id_field, value)), "idempotency_key": key},
            format="json",
        )

    def test_registered_id_is_accepted_without_form_automation(self):
        response = self._submit("26SCC101", "dedicated-ok-1")
        self.assertEqual(response.status_code, 201, response.data)

    def test_id_lookup_is_trimmed_and_case_insensitive(self):
        response = self._submit(" 26scc101 ", "dedicated-ok-2")
        self.assertEqual(response.status_code, 201, response.data)

    def test_unregistered_id_is_rejected_before_response_is_saved(self):
        response = self._submit("26SCC999", "dedicated-bad-1")
        self.assertEqual(response.status_code, 400)
        self.assertIn("CLUB_ID_NOT_FOUND", [e.get("code") for e in response.data.get("errors", [])])
        self.assertFalse(Response.objects.filter(form=self.form).exists())
