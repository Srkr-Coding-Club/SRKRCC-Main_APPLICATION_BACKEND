from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.forms.models import FieldType, FormStatus, Response, Answer
from apps.forms.validation import validate_submission, PARTIAL, codes
from .factories import make_form, add_field, answers

User = get_user_model()


class EditRevalidationTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="s", email="s@srkr.ac.in", password="x", role="NON_AFFILIATE",
        )
        self.form = make_form(status=FormStatus.PUBLISHED, allow_response_editing=True)
        self.name = add_field(self.form, FieldType.TEXT, label="Name", required=True, order=1,
                              validation_rules={"format": "alpha"})
        self.age = add_field(self.form, FieldType.NUMBER, label="Age", required=True, order=2,
                             validation_rules={"minValue": 1, "maxValue": 120})
        self.response = Response.objects.create(form=self.form, user=self.user, form_version=self.form.version)
        Answer.objects.create(response=self.response, field=self.name, value="Ada")
        Answer.objects.create(response=self.response, field=self.age, value=30)
        self.client.force_authenticate(self.user)

    def test_edit_revalidates_full_response(self):
        # change one field to something invalid -> edit is rejected (previously 200)
        resp = self.client.patch(
            f"/api/forms/submissions/{self.response.id}/",
            {"answers": [{"field": self.age.id, "value": "999"}]}, format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("MAX_VALUE", {e["code"] for e in resp.data["errors"]})

    def test_valid_edit_persists(self):
        resp = self.client.patch(
            f"/api/forms/submissions/{self.response.id}/",
            {"answers": [{"field": self.name.id, "value": "Grace"},
                         {"field": self.age.id, "value": "45"}]}, format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.response.refresh_from_db()
        stored = {a.field_id: a.value for a in self.response.answers.all()}
        self.assertEqual(stored[self.name.id], "Grace")
        self.assertEqual(stored[self.age.id], 45)

    def test_edit_blocked_when_editing_disabled(self):
        self.form.allow_response_editing = False
        self.form.save()
        resp = self.client.patch(
            f"/api/forms/submissions/{self.response.id}/",
            {"answers": [{"field": self.name.id, "value": "Grace"}]}, format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["code"], "EDITING_DISABLED")


class PartialModeTests(APITestCase):
    def setUp(self):
        self.form = make_form(status=FormStatus.PUBLISHED)
        self.name = add_field(self.form, FieldType.TEXT, label="Name", required=True, order=1)
        self.email = add_field(self.form, FieldType.EMAIL, label="Email", required=True, order=2)
        self.track = add_field(self.form, FieldType.RADIO, label="Track", required=True,
                               options=["AI", "Web"], order=3)

    def test_partial_mode_downgrades_required_to_warning(self):
        # missing required "email" and "track" -> warnings, not errors, still cleaned
        report = validate_submission(self.form, answers((self.name, "Legacy Person")), mode=PARTIAL)
        self.assertTrue(report.ok)  # no hard errors
        warn_codes = {w.code for w in report.warnings}
        self.assertIn(codes.REQUIRED, warn_codes)

    def test_partial_mode_still_blocks_bad_option(self):
        report = validate_submission(
            self.form,
            answers((self.name, "P"), (self.email, "p@x.com"), (self.track, "Nonexistent")),
            mode=PARTIAL,
        )
        self.assertFalse(report.ok)
        self.assertIn(codes.OPTION_NOT_ALLOWED, {e.code for e in report.errors})

    def test_manual_entry_endpoint_partial(self):
        admin = User.objects.create_user(
            username="a", email="a@srkr.ac.in", password="x", role="ADMIN", is_staff=True,
        )
        self.client.force_authenticate(admin)
        resp = self.client.post(
            f"/api/forms/{self.form.slug}/manual-entry/",
            {"answers": [{"field": self.name.id, "value": "Walk-in"}]}, format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertTrue(len(resp.data.get("warnings", [])) >= 1)
        self.assertEqual(Response.objects.filter(form=self.form).count(), 1)

    def test_manual_entry_blocks_unknown_field(self):
        admin = User.objects.create_user(
            username="a2", email="a2@srkr.ac.in", password="x", role="ADMIN", is_staff=True,
        )
        self.client.force_authenticate(admin)
        resp = self.client.post(
            f"/api/forms/{self.form.slug}/manual-entry/",
            {"answers": [{"field": 987654, "value": "x"}]}, format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("UNKNOWN_FIELD", {e["code"] for e in resp.data["errors"]})
