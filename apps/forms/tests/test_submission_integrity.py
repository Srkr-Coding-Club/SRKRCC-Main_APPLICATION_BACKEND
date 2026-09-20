from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework.test import APITestCase

from apps.forms.models import FieldType, FormStatus, Response
from .factories import make_form, add_field

User = get_user_model()


class EditInPlaceTests(APITestCase):
    """PATCH /api/forms/submissions/{id}/ must update the same row, even when
    the form allows multiple responses — it must never create a new one."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="editor", email="editor@srkr.ac.in", password="pw12345!", role="NON_AFFILIATE",
        )
        self.form = make_form(
            status=FormStatus.PUBLISHED,
            allow_multiple_responses=True,
            allow_response_editing=True,
        )
        self.name = add_field(self.form, FieldType.TEXT, label="Name", required=True, order=1)

    def tearDown(self):
        cache.clear()

    def test_patch_updates_existing_response_not_creates_new_one(self):
        self.client.force_authenticate(self.user)
        create_resp = self.client.post("/api/forms/submissions/", {
            "form": self.form.id,
            "answers": [{"field": self.name.id, "value": "Ada"}],
            "idempotency_key": "edit-test-1",
        }, format="json")
        self.assertEqual(create_resp.status_code, 201, create_resp.data)
        response_id = create_resp.data["id"]

        patch_resp = self.client.patch(f"/api/forms/submissions/{response_id}/", {
            "answers": [{"field": self.name.id, "value": "Ada Lovelace"}],
        }, format="json")
        self.assertEqual(patch_resp.status_code, 200, patch_resp.data)
        self.assertEqual(patch_resp.data["id"], response_id)

        self.assertEqual(Response.objects.filter(form=self.form, user=self.user).count(), 1)
        updated = Response.objects.get(pk=response_id)
        self.assertEqual(updated.answers.get(field=self.name).value, "Ada Lovelace")


class ResponseCapAutoCloseTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.form = make_form(status=FormStatus.PUBLISHED, max_total_responses=2)
        self.name = add_field(self.form, FieldType.TEXT, label="Name", required=True, order=1)

    def tearDown(self):
        cache.clear()

    def _submit(self, name):
        return self.client.post("/api/forms/submissions/", {
            "form": self.form.id,
            "answers": [{"field": self.name.id, "value": name}],
            "idempotency_key": f"cap-test-{name}",
        }, format="json")

    def test_form_auto_closes_after_reaching_cap(self):
        r1 = self._submit("First")
        self.assertEqual(r1.status_code, 201, r1.data)
        self.form.refresh_from_db()
        self.assertEqual(self.form.status, FormStatus.PUBLISHED)

        r2 = self._submit("Second")
        self.assertEqual(r2.status_code, 201, r2.data)
        self.form.refresh_from_db()
        self.assertEqual(self.form.status, FormStatus.CLOSED)

        r3 = self._submit("Third")
        self.assertEqual(r3.status_code, 400)
        self.assertIn("closed", r3.data.get("error", "").lower())

    def test_no_cap_means_unlimited(self):
        uncapped_form = make_form(status=FormStatus.PUBLISHED)
        field = add_field(uncapped_form, FieldType.TEXT, label="Name", required=True, order=1)
        for i in range(5):
            resp = self.client.post("/api/forms/submissions/", {
                "form": uncapped_form.id,
                "answers": [{"field": field.id, "value": f"Person {i}"}],
                "idempotency_key": f"uncapped-{i}",
            }, format="json")
            self.assertEqual(resp.status_code, 201, resp.data)
        uncapped_form.refresh_from_db()
        self.assertEqual(uncapped_form.status, FormStatus.PUBLISHED)


class DuplicateEmailAnswerTests(APITestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_duplicate_email_rejected_when_enabled(self):
        form = make_form(status=FormStatus.PUBLISHED, prevent_duplicate_email_answers=True)
        email_field = add_field(form, FieldType.EMAIL, label="Email", required=True, order=1)

        r1 = self.client.post("/api/forms/submissions/", {
            "form": form.id,
            "answers": [{"field": email_field.id, "value": "same@srkr.ac.in"}],
            "idempotency_key": "dup-test-1",
        }, format="json")
        self.assertEqual(r1.status_code, 201, r1.data)

        r2 = self.client.post("/api/forms/submissions/", {
            "form": form.id,
            "answers": [{"field": email_field.id, "value": "same@srkr.ac.in"}],
            "idempotency_key": "dup-test-2",
        }, format="json")
        self.assertEqual(r2.status_code, 400)
        errors = r2.data.get("errors", [])
        self.assertTrue(any(e.get("code") == "DUPLICATE_EMAIL" for e in errors), r2.data)

    def test_same_email_allowed_by_default(self):
        form = make_form(status=FormStatus.PUBLISHED)  # prevent_duplicate_email_answers defaults False
        email_field = add_field(form, FieldType.EMAIL, label="Email", required=True, order=1)

        for i in range(2):
            resp = self.client.post("/api/forms/submissions/", {
                "form": form.id,
                "answers": [{"field": email_field.id, "value": "shared@srkr.ac.in"}],
                "idempotency_key": f"nodup-test-{i}",
            }, format="json")
            self.assertEqual(resp.status_code, 201, resp.data)

    def test_editing_own_response_does_not_trip_duplicate_check_against_itself(self):
        form = make_form(status=FormStatus.PUBLISHED, prevent_duplicate_email_answers=True, allow_response_editing=True)
        email_field = add_field(form, FieldType.EMAIL, label="Email", required=True, order=1)
        user = User.objects.create_user(
            username="dupeditor", email="dupeditor@srkr.ac.in", password="pw12345!", role="NON_AFFILIATE",
        )
        self.client.force_authenticate(user)

        create_resp = self.client.post("/api/forms/submissions/", {
            "form": form.id,
            "answers": [{"field": email_field.id, "value": "me@srkr.ac.in"}],
            "idempotency_key": "dup-self-1",
        }, format="json")
        self.assertEqual(create_resp.status_code, 201, create_resp.data)
        response_id = create_resp.data["id"]

        patch_resp = self.client.patch(f"/api/forms/submissions/{response_id}/", {
            "answers": [{"field": email_field.id, "value": "me@srkr.ac.in"}],
        }, format="json")
        self.assertEqual(patch_resp.status_code, 200, patch_resp.data)
