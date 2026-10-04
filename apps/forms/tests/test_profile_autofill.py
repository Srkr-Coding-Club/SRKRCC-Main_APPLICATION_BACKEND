"""
FormField.profile_field - a question mapped to a profile attribute is never
asked of the user; the server resolves and writes its value from the
authenticated submitter's own profile at submission time, discarding
whatever (if anything) the client sent for it. See
apps/forms/serializers.py ResponseSerializer._resolve_profile_autofill.
"""
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.forms.models import FieldType, FormStatus, Response, Answer
from .factories import make_form, add_field, answers

User = get_user_model()


class ProfileAutofillSubmissionTests(APITestCase):
    def setUp(self):
        self.member = User.objects.create_user(
            username="member1", email="member1@srkr.ac.in", password="x",
            role="NON_AFFILIATE", first_name="Ravi", last_name="Kumar",
            phone_number="9876543210", branch="CSE", roll_number="21B91A0501",
        )
        self.client.force_authenticate(self.member)

        self.form = make_form(status=FormStatus.PUBLISHED)
        self.phone_field = add_field(
            self.form, FieldType.PHONE, label="Phone Number", required=True,
            order=1, profile_field="phone_number",
        )
        self.team_name_field = add_field(
            self.form, FieldType.TEXT, label="Team Name", required=True, order=2,
        )

    def _submit(self, phone_value, team_name, idempotency_key):
        body = {
            "form": self.form.id,
            "answers": answers(
                (self.phone_field, phone_value),
                (self.team_name_field, team_name),
            ),
            "idempotency_key": idempotency_key,
        }
        return self.client.post("/api/forms/submissions/", body, format="json")

    def test_profile_value_is_stored_regardless_of_submitted_value(self):
        """The client sends a bogus phone number - the server ignores it and
        stores the submitter's real profile phone number instead."""
        resp = self._submit("0000000000", "The Bugslayers", "autofill-1")
        self.assertEqual(resp.status_code, 201, resp.data)
        response_obj = Response.objects.get(form=self.form)
        answer = Answer.objects.get(response=response_obj, field=self.phone_field)
        self.assertEqual(answer.value, "9876543210")

    def test_profile_value_is_stored_when_client_omits_the_field_entirely(self):
        body = {
            "form": self.form.id,
            "answers": answers((self.team_name_field, "The Bugslayers")),
            "idempotency_key": "autofill-2",
        }
        resp = self.client.post("/api/forms/submissions/", body, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        response_obj = Response.objects.get(form=self.form)
        answer = Answer.objects.get(response=response_obj, field=self.phone_field)
        self.assertEqual(answer.value, "9876543210")

    def test_missing_required_profile_data_is_rejected_with_actionable_code(self):
        self.member.phone_number = ""
        self.member.save(update_fields=["phone_number"])
        resp = self._submit("9999999999", "The Bugslayers", "autofill-3")
        self.assertEqual(resp.status_code, 400)
        codes = [e.get("code") for e in resp.data.get("errors", [])]
        self.assertIn("PROFILE_FIELD_MISSING", codes)
        # Not doubled up with the engine's own generic REQUIRED error for the same field.
        self.assertNotIn("REQUIRED", [
            e["code"] for e in resp.data["errors"] if e.get("field_id") == self.phone_field.id
        ])

    def test_anonymous_submission_is_rejected_when_form_has_profile_fields(self):
        self.client.force_authenticate(None)
        resp = self._submit("9999999999", "The Bugslayers", "autofill-4")
        self.assertEqual(resp.status_code, 400)
        codes = [e.get("code") for e in resp.data.get("errors", [])]
        self.assertIn("PROFILE_FIELD_REQUIRES_LOGIN", codes)

    def test_optional_profile_field_left_unresolvable_is_silently_skipped(self):
        """An optional (not required) profile-mapped field with no profile
        data doesn't block submission - it's just left unanswered."""
        self.phone_field.is_required = False
        self.phone_field.save(update_fields=["is_required"])
        self.member.phone_number = ""
        self.member.save(update_fields=["phone_number"])

        resp = self._submit("9999999999", "The Bugslayers", "autofill-5")
        self.assertEqual(resp.status_code, 201, resp.data)
        response_obj = Response.objects.get(form=self.form)
        self.assertFalse(Answer.objects.filter(response=response_obj, field=self.phone_field).exists())

    def test_admin_manual_entry_is_unaffected_by_profile_mapping(self):
        """PARTIAL mode (admin manual entry) treats a profile-mapped field as
        an ordinary answer supplied directly in the payload - the person
        being entered usually isn't the authenticated caller (the admin)."""
        admin = User.objects.create_user(
            username="admin1", email="admin1@srkr.ac.in", password="x",
            role="ADMIN", is_staff=True,
        )
        self.client.force_authenticate(admin)
        resp = self.client.post(
            f"/api/forms/{self.form.slug}/manual-entry/",
            {"answers": answers((self.phone_field, "8888888888"), (self.team_name_field, "Manual Team"))},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        response_obj = Response.objects.get(form=self.form, is_manual_entry=True)
        answer = Answer.objects.get(response=response_obj, field=self.phone_field)
        self.assertEqual(answer.value, "8888888888")
