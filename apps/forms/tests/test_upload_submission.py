"""Submitting a form whose file answers are uploads already sitting in S3 (stand-in storage)."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.forms import upload_storage
from apps.forms.models import Answer, FieldType, FormStatus, FormUpload, Response, UploadState
from apps.forms.validation import validate_form_definition
from .factories import add_field, make_form
from .test_uploads import MB, PNG, FakeStorage

User = get_user_model()


class CopyingStorage(FakeStorage):
    def __init__(self):
        super().__init__()
        self.copied = []

    def put(self, key, body, content_type="image/png"):
        self.objects[key] = (body, content_type)

    def copy(self, source, target):
        self.copied.append((source, target))
        self.objects[target] = self.objects[source]


@override_settings(FORM_UPLOADS_BACKEND="s3", FORM_UPLOAD_BUCKET="test-bucket")
class UploadSubmissionTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.storage = CopyingStorage()
        patcher = mock.patch("apps.forms.upload_storage.get_upload_storage", return_value=self.storage)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.user = User.objects.create_user(username="uma", email="uma@example.org", password="x", role="NON_AFFILIATE")
        self.other = User.objects.create_user(username="otto", email="otto@example.org", password="x", role="NON_AFFILIATE")
        self.client.force_authenticate(self.user)
        self.form = make_form(status=FormStatus.PUBLISHED, allow_multiple_responses=True, max_responses_per_user=5)
        self.photo = add_field(self.form, FieldType.FILE, label="Photo", order=1)
        self.docs = add_field(self.form, FieldType.MULTI_FILE, label="Docs", order=2, validation_rules={"maxFiles": 3})
        self.uploads_url = f"/api/forms/{self.form.slug}/uploads/"

    def upload(self, field=None, name="me.png", body=PNG, user=None):
        field = field or self.photo
        if user is not None:
            self.client.force_authenticate(user)
        asked = self.client.post(self.uploads_url, {"field_id": field.id, "name": name, "size": len(body), "type": "image/png"}, format="json")
        self.assertEqual(asked.status_code, 201, asked.data)
        upload_id = asked.data["upload"]["id"]
        self.storage.put(FormUpload.objects.get(pk=upload_id).key, body)
        self.assertEqual(self.client.post(f"{self.uploads_url}{upload_id}/complete/").status_code, 200)
        return upload_id

    def submit(self, answers, **extra):
        return self.client.post("/api/forms/submissions/", {"form": self.form.id, "answers": answers, **extra}, format="json")

    def test_the_answer_stores_a_reference_and_the_file_is_moved_out_of_tmp(self):
        photo = self.upload()
        with self.captureOnCommitCallbacks(execute=True):
            reply = self.submit([{"field": self.photo.id, "value": {"upload_id": photo, "name": "lie.exe", "size": 1, "url": "https://evil.example/x"}}])
        self.assertEqual(reply.status_code, 201, reply.data)
        response = Response.objects.get()
        stored = Answer.objects.get(response=response, field=self.photo).value
        # the server's facts replace whatever the browser claimed, and no client link is kept
        self.assertEqual(stored, [{"name": "me.png", "size": len(PNG), "type": "image/png", "upload_id": photo}])
        row = FormUpload.objects.get(pk=photo)
        self.assertEqual((row.state, row.response_id, row.expires_at), (UploadState.ATTACHED, response.pk, None))
        self.assertEqual(row.key, upload_storage.final_key(self.form.pk, photo))
        self.assertIn(row.key, self.storage.objects)
        self.assertEqual(len(self.storage.objects), 1)  # the tmp copy is gone

    def test_several_files_in_one_answer(self):
        ids = [self.upload(self.docs, name=f"{n}.png") for n in range(3)]
        reply = self.submit([{"field": self.docs.id, "value": [{"upload_id": i} for i in ids]}])
        self.assertEqual(reply.status_code, 201, reply.data)
        self.assertEqual(FormUpload.objects.filter(state=UploadState.ATTACHED).count(), 3)

    def test_too_many_files_for_the_question_are_refused(self):
        self.docs.validation_rules = {"maxFiles": 5}
        self.docs.save()
        ids = [self.upload(self.docs, name=f"{n}.png") for n in range(4)]
        self.docs.validation_rules = {"maxFiles": 2}
        self.docs.save()
        reply = self.submit([{"field": self.docs.id, "value": [{"upload_id": i} for i in ids]}])
        self.assertEqual(reply.status_code, 400)
        self.assertEqual(Response.objects.count(), 0)
        self.assertEqual(FormUpload.objects.filter(state=UploadState.UPLOADED).count(), 4)  # still there to retry

    def test_nobody_else_s_upload_can_be_used(self):
        theirs = self.upload(user=self.other)
        self.client.force_authenticate(self.user)
        reply = self.submit([{"field": self.photo.id, "value": {"upload_id": theirs}}])
        self.assertEqual((reply.status_code, reply.data["errors"][0]["code"]), (400, "UPLOAD_NOT_FOUND"))
        self.assertEqual(FormUpload.objects.get(pk=theirs).state, UploadState.UPLOADED)
        self.assertEqual(Response.objects.count(), 0)

    def test_an_upload_cannot_be_put_in_a_different_question_or_form(self):
        photo = self.upload()
        reply = self.submit([{"field": self.docs.id, "value": [{"upload_id": photo}]}])
        self.assertEqual(reply.data["errors"][0]["code"], "UPLOAD_NOT_FOUND")
        other = make_form(status=FormStatus.PUBLISHED)
        field = add_field(other, FieldType.FILE, label="F")
        reply = self.client.post("/api/forms/submissions/", {"form": other.id, "answers": [{"field": field.id, "value": {"upload_id": photo}}]}, format="json")
        self.assertEqual(reply.data["errors"][0]["code"], "UPLOAD_NOT_FOUND")

    def test_unknown_malformed_and_repeated_references(self):
        for junk in ("nope", "11111111-1111-1111-1111-111111111111", 5):
            reply = self.submit([{"field": self.photo.id, "value": {"upload_id": junk}}])
            self.assertEqual(reply.status_code, 400, junk)
        photo = self.upload(self.docs)
        reply = self.submit([{"field": self.docs.id, "value": [{"upload_id": photo}, {"upload_id": photo}]}])
        self.assertEqual(reply.status_code, 400)

    def test_an_upload_still_waiting_for_its_file_cannot_be_submitted(self):
        asked = self.client.post(self.uploads_url, {"field_id": self.photo.id, "name": "a.png", "size": 10, "type": "image/png"}, format="json")
        reply = self.submit([{"field": self.photo.id, "value": {"upload_id": asked.data["upload"]["id"]}}])
        self.assertEqual(reply.data["errors"][0]["code"], "UPLOAD_NOT_FOUND")

    def test_an_upload_is_used_once(self):
        photo = self.upload()
        self.assertEqual(self.submit([{"field": self.photo.id, "value": {"upload_id": photo}}]).status_code, 201)
        again = self.submit([{"field": self.photo.id, "value": {"upload_id": photo}}])
        self.assertEqual(again.status_code, 400)
        self.assertEqual(Response.objects.count(), 1)

    def test_a_storage_failure_while_saving_rolls_everything_back(self):
        photo = self.upload()
        with mock.patch.object(self.storage, "copy", side_effect=RuntimeError("down")):
            reply = self.submit([{"field": self.photo.id, "value": {"upload_id": photo}}])
        self.assertEqual((reply.status_code, reply.data["code"]), (400, "UPLOAD_STORAGE_ERROR"))
        self.assertEqual(Response.objects.count(), 0)
        self.assertEqual(FormUpload.objects.get(pk=photo).state, UploadState.UPLOADED)
        # and trying again works
        self.assertEqual(self.submit([{"field": self.photo.id, "value": {"upload_id": photo}}]).status_code, 201)

    def test_a_required_file_question_needs_a_file(self):
        self.photo.is_required = True
        self.photo.save()
        self.assertEqual(self.submit([]).status_code, 400)

    def test_anonymous_callers_cannot_use_uploads(self):
        photo = self.upload()
        self.client.force_authenticate(None)
        reply = self.submit([{"field": self.photo.id, "value": {"upload_id": photo}}])
        self.assertEqual(reply.status_code, 400)

    # --- editing, replacing, deleting --------------------------------------------------------------

    def test_editing_keeps_a_file_and_swapping_it_releases_the_old_one(self):
        single = make_form(status=FormStatus.PUBLISHED, allow_multiple_responses=False, allow_response_editing=True)
        field = add_field(single, FieldType.FILE, label="Photo")
        url = f"/api/forms/{single.slug}/uploads/"

        def up(name):
            asked = self.client.post(url, {"field_id": field.id, "name": name, "size": len(PNG), "type": "image/png"}, format="json")
            self.storage.put(FormUpload.objects.get(pk=asked.data["upload"]["id"]).key, PNG)
            self.client.post(f"{url}{asked.data['upload']['id']}/complete/")
            return asked.data["upload"]["id"]

        def send(upload_id):
            return self.client.post("/api/forms/submissions/", {"form": single.id, "answers": [{"field": field.id, "value": {"upload_id": upload_id}}]}, format="json")

        first = up("one.png")
        self.assertEqual(send(first).status_code, 201)
        self.assertEqual(send(first).status_code, 200)  # resending the same attached file on an edit is fine
        second = up("two.png")
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(send(second).status_code, 200)
        self.assertEqual(FormUpload.objects.get(pk=first).state, UploadState.DELETED)
        self.assertEqual(FormUpload.objects.get(pk=second).state, UploadState.ATTACHED)
        self.assertNotIn(upload_storage.final_key(single.pk, first), self.storage.objects)
        self.assertIn(upload_storage.final_key(single.pk, second), self.storage.objects)

    def test_deleting_a_response_removes_its_files(self):
        photo = self.upload()
        self.submit([{"field": self.photo.id, "value": {"upload_id": photo}}])
        key = FormUpload.objects.get(pk=photo).key
        with self.captureOnCommitCallbacks(execute=True):
            Response.objects.get().delete()
        self.assertNotIn(key, self.storage.objects)

    def test_deleting_a_form_removes_its_files(self):
        photo = self.upload()
        self.submit([{"field": self.photo.id, "value": {"upload_id": photo}}])
        loose = self.upload(self.docs, name="loose.png")
        keys = list(FormUpload.objects.values_list("key", flat=True))
        with self.captureOnCommitCallbacks(execute=True):
            self.form.delete()
        for key in keys:
            self.assertNotIn(key, self.storage.objects)
        self.assertFalse(FormUpload.objects.filter(pk=loose).exists())

    # --- limits ------------------------------------------------------------------------------------

    def test_a_question_without_a_limit_gets_the_two_megabyte_default_on_submit_too(self):
        photo = self.upload()
        FormUpload.objects.filter(pk=photo).update(size=3 * MB)
        reply = self.submit([{"field": self.photo.id, "value": {"upload_id": photo}}])
        self.assertEqual((reply.status_code, reply.data["errors"][0]["code"]), (400, "FILE_TOO_LARGE"))

    def test_the_admins_own_limit_replaces_the_default(self):
        self.photo.validation_rules = {"maxFileSizeMB": 5}
        self.photo.save()
        photo = self.upload()
        FormUpload.objects.filter(pk=photo).update(size=3 * MB)
        self.assertEqual(self.submit([{"field": self.photo.id, "value": {"upload_id": photo}}]).status_code, 201)

    def test_the_form_definition_may_not_exceed_the_platform_maximum(self):
        self.photo.validation_rules = {"maxFileSizeMB": 26}
        self.photo.save()
        report = validate_form_definition(self.form)
        self.assertIn("platform limit of 25 MB", " ".join(e.message for e in report.errors))
        self.photo.validation_rules = {"maxFileSizeMB": 25}
        self.photo.save()
        self.assertEqual([e for e in validate_form_definition(self.form).errors if "platform limit" in e.message], [])

    def test_the_config_endpoint(self):
        self.client.force_authenticate(None)
        data = self.client.get("/api/forms/upload-config/").data
        self.assertEqual(data, {"direct_upload": True, "default_mb": 2.0, "max_mb": 25.0})
        with override_settings(FORM_UPLOADS_BACKEND="inline"):
            self.assertFalse(self.client.get("/api/forms/upload-config/").data["direct_upload"])


@override_settings(FORM_UPLOADS_BACKEND="s3", FORM_UPLOAD_BUCKET="test-bucket")
class UploadDraftTests(APITestCase):
    """A draft remembers files already uploaded to S3, by reference."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.storage = CopyingStorage()
        patcher = mock.patch("apps.forms.upload_storage.get_upload_storage", return_value=self.storage)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.user = User.objects.create_user(username="uma", email="uma@example.org", password="x", role="NON_AFFILIATE")
        self.client.force_authenticate(self.user)
        self.form = make_form(status=FormStatus.PUBLISHED)
        self.photo = add_field(self.form, FieldType.FILE, label="Photo", order=1)
        self.docs = add_field(self.form, FieldType.MULTI_FILE, label="Docs", order=2)
        self.draft_url = f"/api/forms/{self.form.slug}/draft/"

    def upload(self, field, name="a.png"):
        url = f"/api/forms/{self.form.slug}/uploads/"
        asked = self.client.post(url, {"field_id": field.id, "name": name, "size": len(PNG), "type": "image/png"}, format="json")
        upload_id = asked.data["upload"]["id"]
        self.storage.put(FormUpload.objects.get(pk=upload_id).key, PNG)
        self.client.post(f"{url}{upload_id}/complete/")
        return upload_id

    def test_uploaded_files_survive_in_the_draft_and_come_back_as_references(self):
        photo, doc = self.upload(self.photo), self.upload(self.docs, "d.png")
        saved = self.client.put(self.draft_url, {"answers": {
            str(self.photo.id): {"upload_id": photo, "url": "https://evil.example/x", "name": "lie"},
            str(self.docs.id): [{"upload_id": doc}],
        }}, format="json")
        self.assertEqual((saved.status_code, saved.data["dropped"]), (200, []))
        answers = self.client.get(self.draft_url).data["draft"]["answers"]
        self.assertEqual(answers[str(self.photo.id)], {"upload_id": photo, "name": "a.png", "size": len(PNG), "type": "image/png"})
        self.assertEqual(answers[str(self.docs.id)], [{"upload_id": doc, "name": "d.png", "size": len(PNG), "type": "image/png"}])

    def test_inline_files_and_other_peoples_uploads_are_still_dropped(self):
        other = User.objects.create_user(username="otto", email="otto@example.org", password="x", role="NON_AFFILIATE")
        self.client.force_authenticate(other)
        theirs = self.upload(self.photo)
        self.client.force_authenticate(self.user)
        saved = self.client.put(self.draft_url, {"answers": {
            str(self.photo.id): {"upload_id": theirs},
            str(self.docs.id): [{"name": "x.png", "url": "data:image/png;base64,AAAA"}],
        }}, format="json")
        self.assertEqual(sorted(saved.data["dropped"]), sorted([self.photo.id, self.docs.id]))

    def test_a_file_that_was_removed_or_expired_disappears_from_the_restored_draft(self):
        photo = self.upload(self.photo)
        self.client.put(self.draft_url, {"answers": {str(self.photo.id): {"upload_id": photo}}}, format="json")
        FormUpload.objects.filter(pk=photo).update(state=UploadState.DELETED)
        self.assertEqual(self.client.get(self.draft_url).data["draft"]["answers"], {})

    def test_with_the_feature_off_files_are_never_kept(self):
        photo = self.upload(self.photo)
        with override_settings(FORM_UPLOADS_BACKEND="inline"):
            saved = self.client.put(self.draft_url, {"answers": {str(self.photo.id): {"upload_id": photo}}}, format="json")
        self.assertEqual(saved.data["dropped"], [self.photo.id])
