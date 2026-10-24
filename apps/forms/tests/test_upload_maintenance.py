"""Housekeeping for form uploads: expiry sweep, malware verdicts, and moving old inline files into the bucket."""

import base64
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
from apps.forms import upload_maintenance, upload_storage
from apps.forms.models import Answer, FieldType, FormStatus, FormUpload, Response, ScanStatus, UploadState
from .factories import add_field, make_form
from .test_upload_submission import CopyingStorage
from .test_uploads import PNG

User = get_user_model()


def data_url(body: bytes, kind="image/png") -> str:
    return f"data:{kind};base64,{base64.b64encode(body).decode()}"


@override_settings(FORM_UPLOADS_BACKEND="s3", FORM_UPLOAD_BUCKET="test-bucket")
class MaintenanceBase(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.storage = CopyingStorage()
        patcher = mock.patch("apps.forms.upload_storage.get_upload_storage", return_value=self.storage)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.user = User.objects.create_user(username="uma", email="uma@example.org", password="x", role="NON_AFFILIATE")
        self.admin = User.objects.create_user(username="ada", email="ada@example.org", password="x", role="ADMIN")
        self.form = make_form(status=FormStatus.PUBLISHED, allow_multiple_responses=True, max_responses_per_user=9)
        self.photo = add_field(self.form, FieldType.FILE, label="Photo", order=1)
        self.docs = add_field(self.form, FieldType.MULTI_FILE, label="Docs", order=2)

    def make_upload(self, state=UploadState.UPLOADED, key=None, field=None, response=None, **extra):
        upload = FormUpload(form=self.form, field=field or self.photo, uploader=self.user, response=response, original_name="me.png",
                            content_type="image/png", size=len(PNG), state=state, **extra)
        upload.key = key or upload_storage.object_key(self.form.pk, upload.pk)
        upload.save()
        self.storage.put(upload.key, PNG)
        return upload

    def attached(self, **extra):
        response = Response.objects.create(form=self.form, user=self.user)
        upload = self.make_upload(UploadState.ATTACHED, response=response, **extra)
        Answer.objects.create(response=response, field=self.photo, value=[{"upload_id": str(upload.pk), "name": "me.png", "size": len(PNG), "type": "image/png"}])
        return response, upload


class SweepTests(MaintenanceBase):
    def test_expired_unattached_uploads_are_removed_with_their_objects(self):
        old = self.make_upload(expires_at=timezone.now() - timedelta(minutes=1))
        waiting = self.make_upload(UploadState.PENDING, expires_at=timezone.now() - timedelta(minutes=1))
        fresh = self.make_upload(expires_at=timezone.now() + timedelta(days=1))
        with self.captureOnCommitCallbacks(execute=True):
            result = upload_maintenance.sweep_expired()
        self.assertEqual(result["expired"], 2)
        for gone in (old, waiting):
            gone.refresh_from_db()
            self.assertEqual(gone.state, UploadState.DELETED)
            self.assertNotIn(gone.key, self.storage.objects)
        fresh.refresh_from_db()
        self.assertEqual(fresh.state, UploadState.UPLOADED)
        self.assertIn(fresh.key, self.storage.objects)

    def test_attached_files_are_never_swept(self):
        _, upload = self.attached(expires_at=timezone.now() - timedelta(days=9))
        upload_maintenance.sweep_expired()
        upload.refresh_from_db()
        self.assertEqual(upload.state, UploadState.ATTACHED)
        self.assertIn(upload.key, self.storage.objects)

    def test_old_finished_rows_are_forgotten_recent_ones_kept(self):
        old = self.make_upload(UploadState.DELETED)
        recent = self.make_upload(UploadState.REJECTED)
        FormUpload.objects.filter(pk=old.pk).update(updated_at=timezone.now() - timedelta(days=40))
        self.assertEqual(upload_maintenance.sweep_expired()["rows_forgotten"], 1)
        self.assertFalse(FormUpload.objects.filter(pk=old.pk).exists())
        self.assertTrue(FormUpload.objects.filter(pk=recent.pk).exists())

    def test_the_command_reports_and_survives_a_storage_outage(self):
        self.make_upload(expires_at=timezone.now() - timedelta(minutes=1))
        out = StringIO()
        with mock.patch.object(self.storage, "delete", side_effect=RuntimeError("down")):
            call_command("sweep_form_uploads", stdout=out)
        self.assertIn("1 expired upload(s) removed", out.getvalue())
        self.assertEqual(FormUpload.objects.get().state, UploadState.DELETED)


@override_settings(FORM_UPLOADS_BACKEND="s3", FORM_UPLOAD_BUCKET="test-bucket", FORM_UPLOAD_SCAN="guardduty")
class ScanTests(MaintenanceBase):
    def verdict(self, upload, value):
        self.storage.verdicts = {**getattr(self.storage, "verdicts", {}), upload.key: value}

    def test_a_clean_verdict_marks_the_file_clean(self):
        _, upload = self.attached()
        self.verdict(upload, "NO_THREATS_FOUND")
        self.assertEqual(upload_maintenance.refresh_scan(upload), ScanStatus.CLEAN)
        upload.refresh_from_db()
        self.assertEqual(upload.scan_status, ScanStatus.CLEAN)

    def test_no_verdict_yet_changes_nothing(self):
        _, upload = self.attached()
        self.assertEqual(upload_maintenance.refresh_scan(upload), ScanStatus.NOT_SCANNED)
        self.verdict(upload, "UNSUPPORTED")
        self.assertEqual(upload_maintenance.refresh_scan(upload), ScanStatus.NOT_SCANNED)

    def test_a_scan_that_cannot_be_read_is_tried_again_later(self):
        _, upload = self.attached()
        with mock.patch.object(self.storage, "scan_result", side_effect=RuntimeError("denied")):
            self.assertEqual(upload_maintenance.refresh_scan(upload), ScanStatus.NOT_SCANNED)

    def test_malware_is_removed_from_the_answer_and_the_bucket_and_recorded(self):
        response, upload = self.attached()
        self.verdict(upload, "THREATS_FOUND")
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(upload_maintenance.refresh_scan(upload), ScanStatus.INFECTED)
        upload.refresh_from_db()
        self.assertEqual((upload.state, upload.scan_status), (UploadState.DELETED, ScanStatus.INFECTED))
        self.assertNotIn(upload.key, self.storage.objects)
        self.assertFalse(Answer.objects.filter(response=response, field=self.photo).exists())
        self.assertEqual(AuditLog.objects.get(action="Malware Found In Form File").details["file"], "me.png")

    def test_malware_in_a_file_not_yet_submitted_is_removed_too(self):
        upload = self.make_upload()
        self.verdict(upload, "THREATS_FOUND")
        with self.captureOnCommitCallbacks(execute=True):
            upload_maintenance.refresh_scan(upload)
        self.assertNotIn(upload.key, self.storage.objects)
        client = APIClient()
        client.force_authenticate(self.user)
        reply = client.post("/api/forms/submissions/", {"form": self.form.id, "answers": [{"field": self.photo.id, "value": {"upload_id": str(upload.pk)}}]}, format="json")
        self.assertEqual(reply.status_code, 400)

    def test_the_command_counts_each_outcome(self):
        _, clean = self.attached()
        _, bad = self.attached()
        _, waiting = self.attached()
        self.verdict(clean, "NO_THREATS_FOUND")
        self.verdict(bad, "THREATS_FOUND")
        out = StringIO()
        with self.captureOnCommitCallbacks(execute=True):
            call_command("scan_form_uploads", stdout=out)
        self.assertIn("1 clean, 1 infected (removed), 1 still waiting", out.getvalue())

    def test_the_command_refuses_to_run_with_scanning_off(self):
        with override_settings(FORM_UPLOAD_SCAN="off"), self.assertRaises(CommandError):
            call_command("scan_form_uploads")

    def test_a_file_is_only_served_once_it_is_clean(self):
        _, upload = self.attached()
        admin = APIClient()
        admin.force_authenticate(self.admin)
        url = f"/api/forms/{self.form.slug}/files/{upload.pk}/link/"
        waiting = admin.get(url)
        self.assertEqual((waiting.status_code, waiting.data["code"]), (423, "SCAN_PENDING"))
        self.verdict(upload, "NO_THREATS_FOUND")
        self.assertEqual(admin.get(url).status_code, 200)  # the link request itself checks the verdict

    def test_with_scanning_off_nothing_is_held_back(self):
        _, upload = self.attached()
        admin = APIClient()
        admin.force_authenticate(self.admin)
        with override_settings(FORM_UPLOAD_SCAN="off"):
            self.assertEqual(admin.get(f"/api/forms/{self.form.slug}/files/{upload.pk}/link/").status_code, 200)


class BackfillTests(MaintenanceBase):
    def inline_response(self, files, field=None, user=None):
        field = field or self.docs
        response = Response.objects.create(form=self.form, user=user or self.user)
        Answer.objects.create(response=response, field=field, value=files)
        return response

    def test_inline_files_move_to_the_bucket_and_the_answer_holds_references(self):
        response = self.inline_response([{"name": "a.png", "size": 1, "type": "image/png", "url": data_url(PNG)}, {"name": "b.png", "url": data_url(PNG + b"1")}])
        result = upload_maintenance.backfill_inline()
        self.assertEqual(result, {"answers": 1, "files_moved": 2, "files_skipped": 0})
        value = Answer.objects.get(response=response, field=self.docs).value
        self.assertEqual([i["name"] for i in value], ["a.png", "b.png"])
        self.assertTrue(all("url" not in i and "upload_id" in i for i in value))
        for item in value:
            row = FormUpload.objects.get(pk=item["upload_id"])
            self.assertEqual((row.state, row.response_id, row.uploader_id, row.size), (UploadState.ATTACHED, response.pk, self.user.pk, len(PNG) + (item["name"] == "b.png")))
            self.assertEqual(row.key, upload_storage.final_key(self.form.pk, row.pk))
            self.assertIn(row.key, self.storage.objects)

    def test_it_can_be_run_again(self):
        self.inline_response([{"name": "a.png", "url": data_url(PNG)}])
        upload_maintenance.backfill_inline()
        self.assertEqual(upload_maintenance.backfill_inline()["answers"], 0)
        self.assertEqual(FormUpload.objects.count(), 1)

    def test_dry_run_changes_nothing(self):
        response = self.inline_response([{"name": "a.png", "url": data_url(PNG)}])
        self.assertEqual(upload_maintenance.backfill_inline(dry_run=True)["files_moved"], 1)
        self.assertEqual(FormUpload.objects.count(), 0)
        self.assertIn("url", Answer.objects.get(response=response).value[0])

    def test_files_that_cannot_be_moved_stay_exactly_as_they_were(self):
        junk = {"name": "x.png", "url": "data:image/png;base64,@@@@"}
        liar = {"name": "x.png", "url": data_url(b"MZ not a png at all............")}
        link = {"name": "ext.png", "url": "https://example.org/x.png"}
        response = self.inline_response([junk, liar, link])
        result = upload_maintenance.backfill_inline()
        self.assertEqual((result["files_moved"], result["files_skipped"]), (0, 2))
        self.assertEqual(Answer.objects.get(response=response).value, [junk, liar, link])

    def test_a_mixed_answer_moves_what_it_can(self):
        link = {"name": "ext.png", "url": "https://example.org/x.png"}
        response = self.inline_response([{"name": "a.png", "url": data_url(PNG)}, link])
        upload_maintenance.backfill_inline()
        value = Answer.objects.get(response=response).value
        self.assertIn("upload_id", value[0])
        self.assertEqual(value[1], link)

    def test_a_single_file_answer_keeps_its_shape(self):
        response = self.inline_response({"name": "a.png", "url": data_url(PNG)}, field=self.photo)
        upload_maintenance.backfill_inline()
        value = Answer.objects.get(response=response).value
        self.assertIsInstance(value, dict)
        self.assertIn("upload_id", value)

    def test_a_response_without_a_person_still_moves(self):
        response = Response.objects.create(form=self.form, user=None)
        Answer.objects.create(response=response, field=self.docs, value=[{"name": "a.png", "url": data_url(PNG)}])
        self.assertEqual(upload_maintenance.backfill_inline()["files_moved"], 1)
        self.assertIsNone(FormUpload.objects.get().uploader)

    def test_a_storage_failure_leaves_the_answer_alone(self):
        response = self.inline_response([{"name": "a.png", "url": data_url(PNG)}])
        with mock.patch.object(self.storage, "put", side_effect=RuntimeError("down")):
            result = upload_maintenance.backfill_inline()
        self.assertEqual((result["files_moved"], result["files_skipped"]), (0, 1))
        self.assertIn("url", Answer.objects.get(response=response).value[0])
        self.assertEqual(FormUpload.objects.count(), 0)

    def test_filters_and_limits(self):
        other = make_form(status=FormStatus.PUBLISHED)
        other_field = add_field(other, FieldType.FILE, label="F")
        Answer.objects.create(response=Response.objects.create(form=other, user=self.user), field=other_field, value={"name": "o.png", "url": data_url(PNG)})
        for _ in range(3):
            self.inline_response([{"name": "a.png", "url": data_url(PNG)}])
        self.assertEqual(upload_maintenance.backfill_inline(form_slug=other.slug)["answers"], 1)
        self.assertEqual(upload_maintenance.backfill_inline(limit=2)["answers"], 2)
        self.assertEqual(upload_maintenance.backfill_inline()["answers"], 1)

    def test_the_command_and_its_guard(self):
        self.inline_response([{"name": "a.png", "url": data_url(PNG)}])
        out = StringIO()
        call_command("backfill_form_uploads", "--dry-run", stdout=out)
        self.assertIn("would move 1 file(s)", out.getvalue())
        with override_settings(FORM_UPLOADS_BACKEND="inline"), self.assertRaises(CommandError):
            call_command("backfill_form_uploads")

    def test_moved_files_show_up_for_admins_and_can_be_opened(self):
        self.inline_response([{"name": "a.png", "url": data_url(PNG)}])
        upload_maintenance.backfill_inline()
        admin = APIClient()
        admin.force_authenticate(self.admin)
        listed = admin.get(f"/api/forms/{self.form.slug}/files/").data
        self.assertEqual([f["name"] for f in listed["results"]], ["a.png"])
        self.assertEqual(admin.get(f"/api/forms/{self.form.slug}/files/{listed['results'][0]['id']}/link/").status_code, 200)
