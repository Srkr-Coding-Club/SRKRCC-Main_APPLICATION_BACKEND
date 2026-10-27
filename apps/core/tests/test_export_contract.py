"""The form-response and Data Management Center exports follow the same file contract as every other download."""
import csv
import io
import re

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.forms.models import Answer, FieldType, Form, FormField, FormStatus, Response

User = get_user_model()
DATE_TIME = re.compile(r"^\d{2} [A-Z][a-z]{2} \d{4}, \d{2}:\d{2}$")


class SharedExportContractTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="contract_admin", email="contract_admin@srkr.ac.in", first_name="తెలుగు", last_name="Admin",
            password="StrongAdminPassword123!", role="ADMIN",
        )
        self.form = Form.objects.create(title="Contract Form", slug="contract-form", status=FormStatus.PUBLISHED)
        self.field = FormField.objects.create(form=self.form, label="Comment", type=FieldType.TEXT, order=1)
        response = Response.objects.create(form=self.form, user=None, created_by_admin=self.admin)
        Answer.objects.create(response=response, field=self.field, value="=SUM(1+1)")
        self.client.force_authenticate(self.admin)

    def _rows(self, response):
        return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))

    def test_form_responses_export_uses_the_shared_name_bom_dates_and_formula_protection(self):
        response = self.client.get(f"/api/forms/{self.form.slug}/responses/export/")
        self.assertRegex(response["Content-Disposition"], r'filename="contract-form-responses-\d{8}_\d{6}\.csv"')
        self.assertTrue(response.content.decode("utf-8").startswith("﻿"))
        rows = self._rows(response)
        self.assertTrue(rows[1][rows[0].index("Comment")].startswith("'="))
        submitted = rows[1][rows[0].index("Submitted At")]
        self.assertRegex(submitted, DATE_TIME)

    def test_data_center_export_uses_the_shared_name_bom_and_dates(self):
        response = self.client.post(
            "/api/admin/dmc/datasets/users/export/",
            {"format": "csv", "row_scope": "all_filtered", "column_scope": "all_permitted"}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertRegex(response["Content-Disposition"], r'filename="users-export-\d{8}_\d{6}\.csv"')
        self.assertTrue(response.content.decode("utf-8").startswith("﻿"))
        rows = self._rows(response)
        self.assertIn("తెలుగు", rows[1][rows[0].index("First Name")] if "First Name" in rows[0] else "".join(rows[1]))
        for value in rows[1]:
            self.assertFalse(re.match(r"^\d{4}-\d{2}-\d{2}T", value), f"ISO date left in the file: {value}")
