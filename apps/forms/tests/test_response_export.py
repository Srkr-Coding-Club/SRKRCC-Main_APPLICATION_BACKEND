import csv
import io
import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APITestCase

from apps.core.dmc.adapters.forms import FormIndividualAdapter
from apps.core.dmc.contracts import QueryRequest
from apps.forms.models import Answer, FieldType, Form, FormField, FormStatus, Response

User = get_user_model()

RESPONSE_COUNT = 45


class ResponseExportTests(APITestCase):
    """
    The responses viewer pages 20 rows at a time, so its export must come from the
    server and cover every matching response, not just the rows currently on screen.
    """

    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="export_admin", email="export_admin@srkr.ac.in", first_name="Export", last_name="Admin",
            password="StrongAdminPassword123!", role="ADMIN",
        )
        self.student = User.objects.create_user(
            username="export_student", email="export_student@srkr.ac.in",
            password="StrongStudentPassword123!", role="NON_AFFILIATE",
        )
        self.form = Form.objects.create(title="Export Form", slug="export-form", status=FormStatus.PUBLISHED)
        self.language_field = FormField.objects.create(form=self.form, label="Favourite Language", type=FieldType.TEXT, order=1)
        self.responses = []
        for i in range(RESPONSE_COUNT):
            response = Response.objects.create(
                form=self.form, user=None, is_manual_entry=(i % 5 == 0), created_by_admin=self.admin,
            )
            Answer.objects.create(response=response, field=self.language_field, value=f"lang-{i}")
            self.responses.append(response)
        self.client.force_authenticate(self.admin)

    def _export(self, **params):
        return self.client.get(f"/api/forms/{self.form.slug}/responses/export/", params)

    def _csv_rows(self, response):
        return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))

    def test_export_covers_every_response_not_just_the_first_page(self):
        response = self._export()

        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment; filename=", response["Content-Disposition"])
        self.assertEqual(len(self._csv_rows(response)) - 1, RESPONSE_COUNT)

    def test_export_includes_every_answer_as_a_column(self):
        rows = self._csv_rows(self._export())

        column = rows[0].index("Favourite Language")
        values = {row[column] for row in rows[1:]}
        self.assertIn("lang-0", values)
        self.assertIn(f"lang-{RESPONSE_COUNT - 1}", values)

    def test_export_respects_the_viewer_filters(self):
        rows = self._csv_rows(self._export(manual_only="true"))

        # Every fifth response (indexes 0, 5, ..., 40) is a manual entry.
        self.assertEqual(len(rows) - 1, 9)

    def test_export_limits_to_selected_ids_even_across_pages(self):
        picked = [self.responses[0].pk, self.responses[30].pk, self.responses[44].pk]

        rows = self._csv_rows(self._export(ids=",".join(str(pk) for pk in picked)))

        self.assertEqual(len(rows) - 1, 3)

    def test_empty_selection_exports_no_rows(self):
        rows = self._csv_rows(self._export(ids=""))

        self.assertEqual(len(rows) - 1, 0)

    def test_json_export_returns_every_response(self):
        response = self._export(export_format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(json.loads(response.content)), RESPONSE_COUNT)

    def test_xlsx_export_is_a_zip_container(self):
        response = self._export(export_format="xlsx")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"PK"))

    def test_unsupported_format_is_rejected(self):
        self.assertEqual(self._export(export_format="pdf").status_code, 400)

    def test_export_can_be_limited_to_chosen_columns(self):
        rows = self._csv_rows(self._export(columns=f"id,q_{self.language_field.id}"))

        self.assertEqual(rows[0], ["Response ID", "Favourite Language"])
        self.assertEqual(len(rows) - 1, RESPONSE_COUNT)

    def test_column_order_follows_the_form_not_the_request(self):
        rows = self._csv_rows(self._export(columns=f"q_{self.language_field.id},id"))

        self.assertEqual(rows[0], ["Response ID", "Favourite Language"])

    def test_unknown_column_keys_are_ignored(self):
        rows = self._csv_rows(self._export(columns="id,not_a_column"))

        self.assertEqual(rows[0], ["Response ID"])

    def test_an_empty_column_selection_is_rejected(self):
        self.assertEqual(self._export(columns="").status_code, 400)
        self.assertEqual(self._export(columns="not_a_column").status_code, 400)

    def test_students_cannot_export_responses(self):
        self.client.force_authenticate(self.student)

        self.assertEqual(self._export().status_code, 403)


class FormIndividualAdapterStreamTests(TestCase):
    """Chunked streaming must yield each response exactly once, across chunk boundaries."""

    def test_stream_yields_every_response_exactly_once(self):
        admin = User.objects.create_superuser(
            username="stream_admin", email="stream_admin@srkr.ac.in", password="StrongAdminPassword123!", role="ADMIN",
        )
        form = Form.objects.create(title="Stream Form", slug="stream-form", status=FormStatus.PUBLISHED)
        for _ in range(450):
            Response.objects.create(form=form, user=None, is_manual_entry=True, created_by_admin=admin)

        adapter = FormIndividualAdapter(form.pk)
        seen_ids = [record["id"].value for record in adapter.stream_records(QueryRequest(), admin)]

        self.assertEqual(len(seen_ids), 450)
        self.assertEqual(len(set(seen_ids)), 450)
