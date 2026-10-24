"""The admin Maintenance page's backend: task states, running a task, run records, and the commands that share them."""

from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.audit.models import AuditLog
from apps.core import maintenance
from apps.core.models import MaintenanceRun, MaintenanceRunStatus
from apps.forms.models import FieldType, FormStatus, FormUpload, ResponseDraft, UploadState
from apps.forms.tests.factories import add_field, make_form

User = get_user_model()
URL = "/api/admin/maintenance/"


def inline(fn):
    """Run background work at once, in the test's own transaction."""
    return mock.patch("apps.core.maintenance.run_in_background", side_effect=lambda work: work())(fn)


class MaintenanceTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user(username="ada", email="ada@example.org", password="x", role="ADMIN")
        self.lead = User.objects.create_user(username="lee", email="lee@example.org", password="x", role="CLUB_LEAD")
        self.client.force_authenticate(self.admin)
        self.form = make_form(status=FormStatus.PUBLISHED)
        self.field = add_field(self.form, FieldType.FILE, label="Photo")

    def task(self, key, response=None):
        return next(t for t in (response or self.client.get(URL)).data["tasks"] if t["key"] == key)

    def stale_draft(self, days=40):
        person = User.objects.create_user(username=f"p{User.objects.count()}", email=f"p{User.objects.count()}@example.org", password="x")
        draft = ResponseDraft.objects.create(form=self.form, user=person, answers={})
        ResponseDraft.objects.filter(pk=draft.pk).update(updated_at=timezone.now() - timedelta(days=days))

    # --- who may see it -------------------------------------------------------------------------

    def test_admins_only(self):
        self.assertEqual(self.client.get(URL).status_code, 200)
        for user in (self.lead, None):
            self.client.force_authenticate(user)
            self.assertIn(self.client.get(URL).status_code, (401, 403))
            self.assertIn(self.client.post(f"{URL}purge_form_drafts/run/").status_code, (401, 403))

    # --- states ---------------------------------------------------------------------------------

    def test_every_task_is_listed_with_a_state(self):
        data = self.client.get(URL).data
        self.assertEqual([t["key"] for t in data["tasks"]], ["sweep_form_uploads", "scan_form_uploads", "backfill_form_uploads", "purge_form_drafts"])
        self.assertEqual(self.task("purge_form_drafts")["last_run"], None)

    def test_never_run_task_with_nothing_waiting_is_due_if_it_has_a_schedule_else_ok(self):
        self.assertEqual(self.task("purge_form_drafts")["state"], "due")   # weekly, never run
        with override_settings(FORM_UPLOADS_BACKEND="s3", FORM_UPLOAD_BUCKET="b"):
            self.assertEqual(self.task("backfill_form_uploads")["state"], "ok")  # one-off: only when work is waiting

    def test_waiting_work_means_needed_and_says_how_much(self):
        self.stale_draft()
        self.stale_draft()
        item = self.task("purge_form_drafts")
        self.assertEqual((item["state"], item["pending"]), ("needed", 2))
        self.assertIn("abandoned draft", item["pending_label"])

    def test_a_recent_successful_run_with_nothing_waiting_is_up_to_date_and_an_old_one_is_due(self):
        maintenance.execute(maintenance.get("purge_form_drafts"), trigger="admin", user=self.admin)
        self.assertEqual(self.task("purge_form_drafts")["state"], "ok")
        MaintenanceRun.objects.update(started_at=timezone.now() - timedelta(days=8))
        self.assertEqual(self.task("purge_form_drafts")["state"], "due")

    def test_a_failed_run_does_not_count_as_up_to_date(self):
        with override_settings(), mock.patch("apps.forms.drafts.purge_stale", side_effect=RuntimeError("db down")):
            with self.assertRaises(RuntimeError):
                maintenance.execute(maintenance.get("purge_form_drafts"), trigger="admin", user=self.admin)
        item = self.task("purge_form_drafts")
        self.assertEqual((item["state"], item["last_run"]["status"], item["last_run"]["error"]), ("due", "FAILED", "db down"))
        self.assertIsNone(item["last_success_at"])

    def test_tasks_for_switched_off_features_say_why(self):
        sweep = self.task("sweep_form_uploads")
        self.assertEqual(sweep["state"], "unavailable")
        self.assertIn("switched off", sweep["unavailable_reason"])
        with override_settings(FORM_UPLOADS_BACKEND="s3", FORM_UPLOAD_BUCKET="b"):
            self.assertEqual(self.task("scan_form_uploads")["state"], "unavailable")   # uploads on, scanning off
            self.assertIn("scanning", self.task("scan_form_uploads")["unavailable_reason"])
            self.assertNotEqual(self.task("sweep_form_uploads")["state"], "unavailable")

    @override_settings(FORM_UPLOADS_BACKEND="s3", FORM_UPLOAD_BUCKET="b", FORM_UPLOAD_SCAN="guardduty")
    def test_upload_counts(self):
        def upload(**extra):
            row = FormUpload(form=self.form, field=self.field, original_name="a.png", **extra)
            row.key = f"k-{row.pk}"
            row.save()
            return row
        upload(state=UploadState.UPLOADED, expires_at=timezone.now() - timedelta(hours=1))
        upload(state=UploadState.UPLOADED, expires_at=timezone.now() + timedelta(hours=1))
        data = self.client.get(URL)
        self.assertEqual(self.task("sweep_form_uploads", data)["pending"], 1)
        self.assertEqual(self.task("scan_form_uploads", data)["pending"], 2)

    @override_settings(FORM_UPLOADS_BACKEND="s3", FORM_UPLOAD_BUCKET="b")
    def test_old_inline_files_are_counted(self):
        from apps.forms.models import Answer, Response
        response = Response.objects.create(form=self.form, user=self.admin)
        Answer.objects.create(response=response, field=self.field, value={"name": "a.png", "url": "data:image/png;base64,AAAA"})
        Answer.objects.create(response=Response.objects.create(form=self.form), field=self.field, value={"upload_id": "x", "name": "b.png"})
        item = self.task("backfill_form_uploads")
        self.assertEqual((item["state"], item["pending"]), ("needed", 1))

    def test_attention_counts_what_needs_doing(self):
        before = self.client.get(URL).data["attention"]
        self.stale_draft()
        self.assertEqual(self.client.get(URL).data["attention"], before)   # already counted: it was due
        maintenance.execute(maintenance.get("purge_form_drafts"), trigger="admin", user=self.admin)
        self.assertEqual(self.client.get(URL).data["attention"], before - 1)

    # --- running --------------------------------------------------------------------------------

    @inline
    def test_run_now_does_the_work_records_it_and_audits_it(self, _):
        self.stale_draft()
        reply = self.client.post(f"{URL}purge_form_drafts/run/")
        self.assertEqual(reply.status_code, 202, reply.data)
        self.assertEqual(ResponseDraft.objects.count(), 0)
        item = self.task("purge_form_drafts")
        self.assertEqual((item["state"], item["last_run"]["status"], item["last_run"]["trigger"], item["last_run"]["by"]), ("ok", "SUCCESS", "admin", "ada"))
        self.assertEqual(item["last_run"]["result"], "1 abandoned draft deleted.")
        self.assertEqual(AuditLog.objects.get(action="Started Maintenance Task").target_id, "purge_form_drafts")

    @inline
    def test_a_failure_is_recorded_and_shown(self, _):
        with mock.patch("apps.forms.drafts.purge_stale", side_effect=RuntimeError("db down")):
            self.assertEqual(self.client.post(f"{URL}purge_form_drafts/run/").status_code, 202)
        run = MaintenanceRun.objects.get()
        self.assertEqual((run.status, run.error), (MaintenanceRunStatus.FAILED, "db down"))

    def test_a_task_already_running_is_not_started_twice_but_a_dead_one_does_not_block(self):
        MaintenanceRun.objects.create(task="purge_form_drafts")
        reply = self.client.post(f"{URL}purge_form_drafts/run/")
        self.assertEqual((reply.status_code, reply.data["code"]), (409, "ALREADY_RUNNING"))
        self.assertTrue(self.task("purge_form_drafts")["running"])
        MaintenanceRun.objects.update(started_at=timezone.now() - timedelta(minutes=30))
        self.assertFalse(self.task("purge_form_drafts")["running"])
        self.assertEqual(MaintenanceRun.objects.get().status, MaintenanceRunStatus.FAILED)
        with mock.patch("apps.core.maintenance.run_in_background"):
            self.assertEqual(self.client.post(f"{URL}purge_form_drafts/run/").status_code, 202)

    def test_unknown_and_unavailable_tasks_are_refused(self):
        self.assertEqual(self.client.post(f"{URL}nonsense/run/").data["code"], "UNKNOWN_TASK")
        reply = self.client.post(f"{URL}scan_form_uploads/run/")
        self.assertEqual((reply.status_code, reply.data["code"]), (409, "TASK_UNAVAILABLE"))
        self.assertEqual(MaintenanceRun.objects.count(), 0)

    def test_the_page_only_gets_a_get_and_the_run_only_a_post(self):
        self.assertEqual(self.client.post(URL).status_code, 405)
        self.assertEqual(self.client.get(f"{URL}purge_form_drafts/run/").status_code, 405)

    # --- commands share the same records ---------------------------------------------------------

    def test_a_command_run_shows_up_as_the_last_run(self):
        self.stale_draft()
        out = StringIO()
        call_command("purge_form_drafts", stdout=out)
        self.assertIn("1 stale draft(s) deleted", out.getvalue())
        item = self.task("purge_form_drafts")
        self.assertEqual((item["last_run"]["trigger"], item["last_run"]["by"], item["state"]), ("command", None, "ok"))

    def test_a_dry_run_leaves_no_record(self):
        self.stale_draft()
        call_command("purge_form_drafts", "--dry-run", stdout=StringIO())
        self.assertEqual((MaintenanceRun.objects.count(), ResponseDraft.objects.count()), (0, 1))

    def test_a_command_refuses_while_the_same_task_is_running(self):
        MaintenanceRun.objects.create(task="purge_form_drafts")
        with self.assertRaises(CommandError):
            call_command("purge_form_drafts", stdout=StringIO())

    @override_settings(FORM_UPLOADS_BACKEND="s3", FORM_UPLOAD_BUCKET="b")
    def test_the_upload_commands_leave_records_too(self):
        with mock.patch("apps.forms.upload_storage.get_upload_storage"):
            call_command("sweep_form_uploads", stdout=StringIO())
            call_command("backfill_form_uploads", stdout=StringIO())
        self.assertEqual(sorted(MaintenanceRun.objects.values_list("task", flat=True)), ["backfill_form_uploads", "sweep_form_uploads"])
        self.assertEqual(self.task("sweep_form_uploads")["last_run"]["result"], "0 unused uploads removed, 0 old records tidied.")
