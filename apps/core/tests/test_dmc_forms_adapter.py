from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.forms.models import Form, FormField, Response, FieldType, FormStatus

User = get_user_model()


class DMCFormsAdapterIdentityFallbackTests(TestCase):
    """
    apps/core/dmc/adapters/forms.py's FormsAllAdapter must resolve name/email
    the same way apps/forms/serializers.py's ResponseDetailSerializer does,
    since Response.user is nullable for manual-entry/CSV-imported responses.
    """

    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="dmc_admin",
            email="dmc_admin@srkr.ac.in",
            first_name="Admin",
            last_name="User",
            password="StrongAdminPassword123!",
            role="ADMIN",
        )
        self.club_admin = User.objects.create_user(
            username="dmc_club_admin",
            email="dmc_club_admin@srkr.ac.in",
            first_name="Club",
            last_name="Admin",
            password="StrongAdminPassword123!",
            role="ADMIN",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

        self.form = Form.objects.create(title="Test Form", slug="test-form", status=FormStatus.PUBLISHED)
        self.name_field = FormField.objects.create(form=self.form, label="Full Name", type=FieldType.TEXT, order=1)
        self.email_field = FormField.objects.create(form=self.form, label="Email Address", type=FieldType.EMAIL, order=2)

    def _query_forms_all(self, sort_field="submitted_at", direction="asc"):
        resp = self.client.post(
            "/api/admin/dmc/datasets/forms_all/query/",
            data={"sort": {"field": sort_field, "direction": direction}, "page_size": 50},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def test_manual_entry_response_resolves_identity_via_admin_fallback(self):
        Response.objects.create(
            form=self.form,
            user=None,
            is_manual_entry=True,
            created_by_admin=self.club_admin,
        )

        data = self._query_forms_all()
        record = next(r for r in data["records"] if r["is_manual_entry"]["value"] is True)

        self.assertEqual(record["name"]["value"], f"Admin: {self.club_admin.email}")
        self.assertEqual(record["email"]["value"], self.club_admin.email)

    def test_csv_imported_response_resolves_identity_via_answer_fields(self):
        response = Response.objects.create(form=self.form, user=None, is_manual_entry=False)
        response.answers.create(field=self.name_field, value="Imported Student")
        response.answers.create(field=self.email_field, value="imported@srkr.ac.in")

        data = self._query_forms_all()
        record = next(r for r in data["records"] if r["id"]["value"] == str(response.pk))

        self.assertEqual(record["name"]["value"], "Imported Student")
        self.assertEqual(record["email"]["value"], "imported@srkr.ac.in")

    def test_csv_imported_response_without_matching_answers_falls_back_to_defaults(self):
        Response.objects.create(form=self.form, user=None, is_manual_entry=False)

        data = self._query_forms_all()
        record = next(r for r in data["records"] if r["email"]["value"] == "offline@srkr.ac.in")

        self.assertEqual(record["name"]["value"], "Anonymous Student")
        self.assertEqual(record["email"]["value"], "offline@srkr.ac.in")


class DMCFormsAdapterSortingTests(TestCase):
    """The 'Manual Entry' column is sortable=True and must actually reorder results."""

    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="dmc_sort_admin",
            email="dmc_sort_admin@srkr.ac.in",
            first_name="Admin",
            last_name="Sort",
            password="StrongAdminPassword123!",
            role="ADMIN",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

        self.form = Form.objects.create(title="Sort Form", slug="sort-form", status=FormStatus.PUBLISHED)
        self.manual_response = Response.objects.create(form=self.form, user=None, is_manual_entry=True, created_by_admin=self.admin)
        self.regular_response = Response.objects.create(form=self.form, user=None, is_manual_entry=False)

    def test_sorting_by_is_manual_entry_changes_result_order(self):
        resp = self.client.post(
            "/api/admin/dmc/datasets/forms_all/query/",
            data={"sort": {"field": "is_manual_entry", "direction": "asc"}, "page_size": 50},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        asc_order = [r["id"]["value"] for r in resp.json()["records"]]

        resp_desc = self.client.post(
            "/api/admin/dmc/datasets/forms_all/query/",
            data={"sort": {"field": "is_manual_entry", "direction": "desc"}, "page_size": 50},
            format="json",
        )
        self.assertEqual(resp_desc.status_code, 200, resp_desc.content)
        desc_order = [r["id"]["value"] for r in resp_desc.json()["records"]]

        self.assertEqual(asc_order, list(reversed(desc_order)))
        self.assertEqual(asc_order[0], str(self.regular_response.pk))
        self.assertEqual(desc_order[0], str(self.manual_response.pk))
