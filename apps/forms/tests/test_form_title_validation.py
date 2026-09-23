"""
FormSerializer.validate_title — DRF's CharField only rejects a literal ""
("may not be blank"), so a whitespace-only title like "   " used to pass
straight through and get persisted. That form then rendered as a blank row
everywhere its title is listed (the admin's "Select a form" dropdown in the
responses viewer, the registration-form pickers on Event/Hackathon panels).
Regression coverage for a real blank-titled form found in production.
"""
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from .factories import make_form
from apps.forms.models import Form, FormStatus

User = get_user_model()


class FormTitleValidationTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin", email="admin@srkr.ac.in", password="x", role="ADMIN", is_staff=True,
        )
        self.client.force_authenticate(self.admin)

    def test_creating_a_form_with_blank_title_is_rejected(self):
        resp = self.client.post(
            "/api/forms/",
            {"title": "", "slug": "blank-title-form"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("title", resp.data)
        self.assertFalse(Form.objects.filter(slug="blank-title-form").exists())

    def test_creating_a_form_with_whitespace_only_title_is_rejected(self):
        resp = self.client.post(
            "/api/forms/",
            {"title": "   ", "slug": "whitespace-title-form"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("title", resp.data)
        self.assertFalse(Form.objects.filter(slug="whitespace-title-form").exists())

    def test_title_is_trimmed_on_save(self):
        resp = self.client.post(
            "/api/forms/",
            {"title": "  Padded Title  ", "slug": "padded-title-form"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(Form.objects.get(slug="padded-title-form").title, "Padded Title")

    def test_updating_an_existing_form_to_blank_title_is_rejected(self):
        form = make_form(status=FormStatus.DRAFT, title="Original Title")
        resp = self.client.patch(
            f"/api/forms/{form.slug}/",
            {"title": "   "},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("title", resp.data)
        form.refresh_from_db()
        self.assertEqual(form.title, "Original Title")
