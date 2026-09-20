import time
from unittest.mock import patch

from django.contrib.auth import get_user_model
from rest_framework.test import APITransactionTestCase

from apps.core.dmc.models import ExportJob

User = get_user_model()


def _wait_until(predicate, timeout=2.0, interval=0.02):
    """Polls `predicate()` until truthy or `timeout` elapses — the async export
    path runs on a real background thread (apps.core.tasks.run_in_background),
    so its effects aren't visible the instant the dispatching request returns."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class DMCAsyncExportTests(APITransactionTestCase):
    """
    ExportService's async path previously always failed to even import its own
    background-job function (apps/core/dmc/tasks.py didn't exist) and silently
    ran the full export inline while still telling the client `mode: "async"`.
    This covers: the job function actually exists and works when run directly
    (simulating how run_in_background invokes it), and the real dispatch path
    (a background thread, no task queue) genuinely completes off-request.

    Uses APITransactionTestCase (real commits) rather than APITestCase, since
    the background thread runs on its own DB connection and can only see rows
    this test has actually committed, not rows inside a rolled-back per-test
    transaction.
    """

    def setUp(self):
        self.admin = User.objects.create_user(
            username="exportadmin", email="exportadmin@srkr.ac.in", password="pw12345!", role="ADMIN",
        )
        # A couple of extra users just so the "users" dataset has real rows to export.
        for i in range(3):
            User.objects.create_user(
                username=f"exportuser{i}", email=f"exportuser{i}@srkr.ac.in", password="pw12345!", role="NON_AFFILIATE",
            )

    def test_export_endpoint_dispatches_and_completes_in_background(self):
        self.client.force_authenticate(self.admin)
        # DMC_SYNC_EXPORT_MAX_ROWS is read once at module import time, so
        # @override_settings can't reach it — patch the module constant directly
        # to force the async branch without needing 1000+ real rows.
        with patch("apps.core.dmc.export_service.DMC_SYNC_EXPORT_MAX_ROWS", 0):
            resp = self.client.post(
                "/api/admin/dmc/datasets/users/export/",
                {"format": "csv", "row_scope": "all_filtered", "column_scope": "visible", "visible_column_keys": ["email"]},
                format="json",
            )
        self.assertEqual(resp.status_code, 202, resp.data)
        job_id = resp.data["job_id"]

        # Immediately after dispatch, the job has not necessarily finished yet —
        # it must be QUEUED or already RUNNING/COMPLETED, never left uncreated.
        job = ExportJob.objects.get(pk=job_id)
        self.assertIn(job.status, [ExportJob.STATUS_QUEUED, ExportJob.STATUS_RUNNING, ExportJob.STATUS_COMPLETED])

        self.assertTrue(
            _wait_until(lambda: ExportJob.objects.get(pk=job_id).status == ExportJob.STATUS_COMPLETED),
            "export job never reached COMPLETED",
        )
        job.refresh_from_db()
        self.assertTrue(job.file_path)
        self.assertGreater(job.file_size or 0, 0)

    def test_run_export_job_executes_correctly_when_run_directly(self):
        """Exercises the job function the same way run_in_background's thread
        invokes it — directly, against a job left QUEUED."""
        from apps.core.dmc.tasks import run_export_job

        job = ExportJob.objects.create(
            dataset_id="users",
            created_by=self.admin,
            format="csv",
            row_scope="all_filtered",
            column_scope="visible",
            column_keys=["email", "role"],
            filter_snapshot={"search": "", "sort_field": "created_at", "sort_direction": "desc", "filters": []},
        )

        run_export_job(job.pk)

        job.refresh_from_db()
        self.assertEqual(job.status, ExportJob.STATUS_COMPLETED, job.error_message)
        self.assertTrue(job.file_path)
        self.assertGreaterEqual(job.row_count or 0, 3)

    def test_small_export_stays_synchronous(self):
        """Sanity check the sync path (unaffected by this fix) still works."""
        self.client.force_authenticate(self.admin)
        resp = self.client.post(
            "/api/admin/dmc/datasets/users/export/",
            {"format": "csv", "row_scope": "all_filtered", "column_scope": "visible", "visible_column_keys": ["email"]},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get("X-Export-Mode"), "sync")
