import uuid

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.models import ImportJob, ImportJobStatus
from apps.core.dmc.models import ExportJob
from apps.core.models import BackupJob, EmailJob, EmailJobStatus, EmailTemplate, ImportAttempt, ImportAttemptStatus

User = get_user_model()


class BackgroundJobsOverviewTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="jobsadmin", email="jobsadmin@srkr.ac.in", password="pw12345!", role="ADMIN",
        )
        self.member = User.objects.create_user(
            username="jobsmember", email="jobsmember@srkr.ac.in", password="pw12345!", role="NON_AFFILIATE",
        )

        template = EmailTemplate.objects.create(
            name="overview_test", subject_template="Hi", html_template="<p>Hi</p>",
        )
        EmailJob.objects.create(
            template=template, campaign_name="September Newsletter",
            total_recipients=10, sent_count=8, failed_count=2,
            status=EmailJobStatus.PARTIALLY_FAILED, created_by=self.admin,
        )
        ExportJob.objects.create(
            dataset_id="users", format="csv", status=ExportJob.STATUS_COMPLETED,
            row_count=42, file_size=1024, created_by=self.admin,
        )
        ImportJob.objects.create(
            source_filename="legacy_members.csv", status=ImportJobStatus.COMMITTED,
            total_rows=50, valid_rows=48, new_users_count=30, updated_users_count=18,
            created_by=self.admin, expires_at=timezone.now(),
        )
        backup_job = BackupJob.objects.create(
            original_filename="backup_2025.xlsx", file_sha256="a" * 64,
            file_size_bytes=2048, file_format="XLSX", created_by=self.admin,
        )
        ImportAttempt.objects.create(
            backup_job=backup_job, target_domain="USERS",
            total_records=20, valid_records=19, inserted_records=15, updated_records=4,
            status=ImportAttemptStatus.COMMITTED, committed_by=self.admin,
        )

    def test_requires_admin_or_club_lead(self):
        resp = self.client.get("/api/admin/jobs/")
        self.assertIn(resp.status_code, (401, 403))

        self.client.force_authenticate(self.member)
        resp = self.client.get("/api/admin/jobs/")
        self.assertEqual(resp.status_code, 403)

    def test_aggregates_all_four_job_types(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.get("/api/admin/jobs/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["count"], 4)
        types_seen = {row["type"] for row in resp.data["results"]}
        self.assertEqual(types_seen, {"email", "export", "member_import", "backup_import"})

    def test_results_sorted_newest_first(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.get("/api/admin/jobs/")
        timestamps = [row["created_at"] for row in resp.data["results"]]
        self.assertEqual(timestamps, sorted(timestamps, reverse=True))

    def test_type_filter(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.get("/api/admin/jobs/?type=email")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["count"], 1)
        self.assertEqual(resp.data["results"][0]["type"], "email")

    def test_invalid_type_filter_rejected(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.get("/api/admin/jobs/?type=not_a_real_type")
        self.assertEqual(resp.status_code, 400)

    def test_status_group_filter(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.get("/api/admin/jobs/?status=SUCCESS")
        self.assertEqual(resp.status_code, 200)
        # export (COMPLETED), member_import (COMMITTED), backup_import (COMMITTED) all map to SUCCESS.
        self.assertEqual(resp.data["count"], 3)
        for row in resp.data["results"]:
            self.assertEqual(row["status_group"], "SUCCESS")

    def test_stats_bucket_counts(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.get("/api/admin/jobs/")
        self.assertEqual(resp.data["stats"].get("PARTIAL"), 1)
        self.assertEqual(resp.data["stats"].get("SUCCESS"), 3)

    def test_email_job_summary_shape(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.get("/api/admin/jobs/?type=email")
        row = resp.data["results"][0]
        self.assertEqual(row["summary"], {"total": 10, "sent": 8, "failed": 2})
        self.assertEqual(row["created_by"]["email"], self.admin.email)
