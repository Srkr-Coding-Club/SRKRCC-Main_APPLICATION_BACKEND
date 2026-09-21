from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.forms.models import FieldType, FormStatus, Response, Answer
from .factories import make_form, add_field

User = get_user_model()


class SubmissionEndpointTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="student", email="student@srkr.ac.in", password="x", role="NON_AFFILIATE",
        )
        self.form = make_form(status=FormStatus.PUBLISHED, allow_multiple_responses=True,
                              max_responses_per_user=3)
        self.name = add_field(self.form, FieldType.TEXT, label="Name", required=True, order=1,
                              validation_rules={"format": "alpha"})
        self.age = add_field(self.form, FieldType.NUMBER, label="Age", required=True, order=2,
                             validation_rules={"minValue": 1, "maxValue": 120, "integerOnly": True})
        self.track = add_field(self.form, FieldType.RADIO, label="Track", required=True,
                               options=["AI", "Web"], order=3)
        self.lic = add_field(self.form, FieldType.TEXT, label="License No", order=4,
                             conditional_logic={"logic": "AND",
                                                "rules": [{"field": self.age.id, "operator": "gte", "value": "18"}],
                                                "action": "show"})
        self.other_form = make_form(status=FormStatus.PUBLISHED)
        self.foreign_field = add_field(self.other_form, FieldType.TEXT, label="Foreign", order=1)
        # Response attribution comes strictly from the authenticated caller now
        # (a client-supplied `user` ID in the body used to be trusted for
        # anonymous requests — an IDOR that let anyone submit as any user ID).
        self.client.force_authenticate(self.user)

    def _submit(self, answers, **extra):
        body = {"form": self.form.id, "answers": answers,
                "idempotency_key": f"k-{id(answers)}", **extra}
        return self.client.post("/api/forms/submissions/", body, format="json")

    def test_valid_submission_created(self):
        resp = self._submit([
            {"field": self.name.id, "value": "Ada"},
            {"field": self.age.id, "value": "25"},
            {"field": self.track.id, "value": "AI"},
            {"field": self.lic.id, "value": "DL-1"},
        ])
        self.assertEqual(resp.status_code, 201, resp.data)
        r = Response.objects.get(form=self.form)
        stored = {a.field_id: a.value for a in r.answers.all()}
        self.assertEqual(stored[self.age.id], 25)          # coerced to int
        self.assertIn(self.lic.id, stored)                 # visible (age >= 18)

    def test_all_errors_returned_at_once(self):
        resp = self._submit([
            {"field": self.name.id, "value": "Ada123"},        # bad format
            {"field": self.age.id, "value": "2.5"},            # not integer
            {"field": self.track.id, "value": "Blockchain"},   # not an option
        ])
        self.assertEqual(resp.status_code, 400)
        codes = {e["code"] for e in resp.data["errors"]}
        self.assertEqual(codes, {"INVALID_FORMAT", "MUST_BE_INTEGER", "OPTION_NOT_ALLOWED"})
        self.assertEqual(resp.data["code"], "VALIDATION_FAILED")

    def test_hidden_conditional_field_answer_dropped(self):
        resp = self._submit([
            {"field": self.name.id, "value": "Ada"},
            {"field": self.age.id, "value": "10"},              # < 18 -> License hidden
            {"field": self.track.id, "value": "AI"},
            {"field": self.lic.id, "value": "SHOULD-BE-DROPPED"},
        ])
        self.assertEqual(resp.status_code, 201, resp.data)
        stored = {a.field_id for a in Response.objects.get(form=self.form).answers.all()}
        self.assertNotIn(self.lic.id, stored)

    def test_unknown_field_id_rejected(self):
        resp = self._submit([
            {"field": self.name.id, "value": "Ada"},
            {"field": self.age.id, "value": "20"},
            {"field": self.track.id, "value": "AI"},
            {"field": 999999, "value": "x"},
        ])
        self.assertEqual(resp.status_code, 400)
        self.assertIn("UNKNOWN_FIELD", {e["code"] for e in resp.data["errors"]})

    def test_field_from_other_form_rejected(self):
        resp = self._submit([
            {"field": self.name.id, "value": "Ada"},
            {"field": self.age.id, "value": "20"},
            {"field": self.track.id, "value": "AI"},
            {"field": self.foreign_field.id, "value": "x"},
        ])
        self.assertEqual(resp.status_code, 400)
        self.assertIn("UNKNOWN_FIELD", {e["code"] for e in resp.data["errors"]})

    def test_duplicate_answer_rejected(self):
        resp = self._submit([
            {"field": self.name.id, "value": "Ada"},
            {"field": self.name.id, "value": "Ada again"},
            {"field": self.age.id, "value": "20"},
            {"field": self.track.id, "value": "AI"},
        ])
        self.assertEqual(resp.status_code, 400)
        self.assertIn("DUPLICATE_ANSWER", {e["code"] for e in resp.data["errors"]})

    def test_deleted_field_answer_rejected(self):
        self.lic.is_deleted = True
        self.lic.save()
        resp = self._submit([
            {"field": self.name.id, "value": "Ada"},
            {"field": self.age.id, "value": "20"},
            {"field": self.track.id, "value": "AI"},
            {"field": self.lic.id, "value": "x"},
        ])
        self.assertEqual(resp.status_code, 400)
        self.assertIn("DELETED_FIELD", {e["code"] for e in resp.data["errors"]})

    def test_max_responses_per_user_enforced(self):
        for i in range(3):
            r = self._submit([
                {"field": self.name.id, "value": "Ada"},
                {"field": self.age.id, "value": "20"},
                {"field": self.track.id, "value": "AI"},
            ], idempotency_key=f"multi-{i}")
            self.assertEqual(r.status_code, 201, r.data)
        r4 = self._submit([
            {"field": self.name.id, "value": "Ada"},
            {"field": self.age.id, "value": "20"},
            {"field": self.track.id, "value": "AI"},
        ], idempotency_key="multi-4")
        self.assertEqual(r4.status_code, 400)
        self.assertEqual(r4.data.get("code"), "MAX_RESPONSES_REACHED")

    def test_draft_form_rejects_submission(self):
        self.form.status = FormStatus.DRAFT
        self.form.save()
        r = self._submit([{"field": self.name.id, "value": "Ada"}])
        self.assertEqual(r.status_code, 400)


class PublishGateApiTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin", email="admin@srkr.ac.in", password="x",
            role="ADMIN", is_staff=True,
        )
        self.client.force_authenticate(self.admin)

    def test_publish_blocked_on_bad_definition(self):
        form = make_form(status=FormStatus.DRAFT)
        add_field(form, FieldType.TEXT, label="T", order=1, validation_rules={"pattern": "([a"})
        resp = self.client.post(f"/api/forms/{form.slug}/publish/")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["code"], "FORM_DEFINITION_INVALID")
        self.assertIn("DEF_INVALID_REGEX", {e["code"] for e in resp.data["errors"]})

    def test_publish_succeeds_with_warnings(self):
        form = make_form(status=FormStatus.DRAFT)
        add_field(form, FieldType.TEXT, label="T", order=1)  # no required field -> warning
        resp = self.client.post(f"/api/forms/{form.slug}/publish/")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(any(w["code"] == "WARN_NO_REQUIRED_FIELDS" for w in resp.data["warnings"]))

    def test_validate_endpoint(self):
        form = make_form(status=FormStatus.DRAFT)
        add_field(form, FieldType.RADIO, label="R", options=[], order=1)
        resp = self.client.get(f"/api/forms/{form.slug}/validate/")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data["publishable"])
