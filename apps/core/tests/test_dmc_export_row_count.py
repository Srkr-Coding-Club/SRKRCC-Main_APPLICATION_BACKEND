import csv
import time
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TransactionTestCase
from rest_framework.test import APIClient

from apps.core.dmc.models import ExportJob
from apps.forms.models import Form, FormField, FieldType, FormStatus, Response

User = get_user_model()

RESPONSE_COUNT = 130


class DMCFormsExportRowCountTests(TransactionTestCase):
    """
    The forms_all export must include every response, and the job's row_count must
    report the number of records written rather than counting newlines (which
    includes the header and any line breaks inside a cell).

    Uses TransactionTestCase because the async path runs on a background thread
    with its own DB connection and can only see committed rows.
    """

    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="dmc_count_admin", email="dmc_count_admin@srkr.ac.in",
            password="StrongAdminPassword123!", role="ADMIN",
        )
        self.form = Form.objects.create(title="Count Form", slug="count-form", status=FormStatus.PUBLISHED)
        FormField.objects.create(form=self.form, label="Notes", type=FieldType.TEXT, order=1)
        for _ in range(RESPONSE_COUNT):
            Response.objects.create(form=self.form, user=None, is_manual_entry=True, created_by_admin=self.admin)
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def _export_payload(self):
        return {
            "format": "csv",
            "row_scope": "all_filtered",
            "column_scope": "visible",
            "visible_column_keys": ["id", "submitted_at"],
        }

    def test_sync_export_includes_every_response(self):
        resp = self.client.post("/api/admin/dmc/datasets/forms_all/export/", self._export_payload(), format="json")

        self.assertEqual(resp.status_code, 200)
        rows = list(csv.reader(resp.content.decode("utf-8-sig").splitlines()))
        self.assertEqual(len(rows) - 1, RESPONSE_COUNT)

    def test_async_export_records_exact_row_count(self):
        with patch("apps.core.dmc.export_service.DMC_SYNC_EXPORT_MAX_ROWS", 0):
            resp = self.client.post("/api/admin/dmc/datasets/forms_all/export/", self._export_payload(), format="json")
        self.assertEqual(resp.status_code, 202, resp.data)

        job = ExportJob.objects.get(pk=resp.data["job_id"])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job.refresh_from_db()
            if job.status in (ExportJob.STATUS_COMPLETED, ExportJob.STATUS_FAILED):
                break
            time.sleep(0.05)

        self.assertEqual(job.status, ExportJob.STATUS_COMPLETED, job.error_message)
        self.assertEqual(job.row_count, RESPONSE_COUNT)
