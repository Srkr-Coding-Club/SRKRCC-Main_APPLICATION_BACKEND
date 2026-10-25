"""Direct-to-S3 file uploads for file questions: /api/forms/{slug}/uploads/. S3 is replaced by an in-memory stand-in."""

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.forms import upload_service
from apps.forms.models import FieldType, FormStatus, FormUpload, UploadState
from apps.forms.upload_storage import StoredObject, get_upload_storage
from .factories import add_field, make_form

User = get_user_model()
MB = 1024 * 1024
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class FakeStorage:
    """Remembers what would be in the bucket and the signed rules it was asked for."""

    def __init__(self):
        self.objects = {}
        self.signed = []
        self.deleted = []

    def presign_post(self, *, key, content_type, min_bytes, max_bytes, expires):
        self.signed.append({"key": key, "content_type": content_type, "min": min_bytes, "max": max_bytes, "expires": expires})
        return {"url": "https://bucket.example/", "fields": {"key": key, "Content-Type": content_type}}

    def put(self, key, body, content_type="image/png"):
        self.objects[key] = (body, content_type)

    def head(self, key):
        found = self.objects.get(key)
        return StoredObject(size=len(found[0]), content_type=found[1]) if found else None

    def read_start(self, key, length=32):
        return self.objects[key][0][:length]

    def presign_get(self, *, key, filename, content_type, inline, expires):
        self.links = getattr(self, "links", []) + [{"key": key, "filename": filename, "content_type": content_type, "inline": inline, "expires": expires}]
        return f"https://bucket.example/{key}?signed=1"

    def scan_result(self, key):
        return getattr(self, "verdicts", {}).get(key)

    def delete(self, key):
        self.deleted.append(key)
        self.objects.pop(key, None)


@override_settings(FORM_UPLOADS_BACKEND="s3", FORM_UPLOAD_BUCKET="test-bucket")
class UploadTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.storage = FakeStorage()
        patcher = mock.patch("apps.forms.upload_storage.get_upload_storage", return_value=self.storage)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.user = User.objects.create_user(username="uma", email="uma@example.org", password="x", role="NON_AFFILIATE")
        self.other = User.objects.create_user(username="otto", email="otto@example.org", password="x", role="NON_AFFILIATE")
        self.client.force_authenticate(self.user)
        self.form = make_form(status=FormStatus.PUBLISHED)
        self.single = add_field(self.form, FieldType.FILE, label="Photo", order=1)
        self.many = add_field(self.form, FieldType.MULTI_FILE, label="Docs", order=2, validation_rules={"maxFiles": 2, "maxFileSizeMB": 5})
        self.text = add_field(self.form, FieldType.TEXT, label="Name", order=3)
        self.url = f"/api/forms/{self.form.slug}/uploads/"

    def ask(self, field=None, name="me.png", size=1000, type_="image/png", url=None, client=None):
        field = field or self.single
        return (client or self.client).post(url or self.url, {"field_id": field.id, "name": name, "size": size, "type": type_}, format="json")

    def arrive(self, response, body=PNG, content_type="image/png"):
        self.storage.put(response.data["upload"]["id"] and FormUpload.objects.get(pk=response.data["upload"]["id"]).key, body, content_type)

    def finish(self, response, client=None):
        return (client or self.client).post(f"{self.url}{response.data['upload']['id']}/complete/")

    # --- asking for permission ------------------------------------------------------------------

    def test_a_signed_form_is_returned_with_the_limits_baked_in(self):
        reply = self.ask()
        self.assertEqual(reply.status_code, 201, reply.data)
        upload = FormUpload.objects.get()
        self.assertEqual((upload.state, upload.uploader, upload.original_name), (UploadState.PENDING, self.user, "me.png"))
        self.assertEqual(self.storage.signed[0]["key"], upload.key)
        self.assertEqual(self.storage.signed[0]["max"], 2 * MB)  # the default when the admin set no limit
        self.assertEqual(reply.data["max_bytes"], 2 * MB)
        self.assertIn(str(self.form.pk), upload.key)
        self.assertNotIn("me.png", upload.key)  # the key never contains the person's file name

    def test_the_admins_limit_is_used_and_capped_at_the_platform_ceiling(self):
        self.ask(self.many, name="a.pdf", type_="application/pdf")
        self.assertEqual(self.storage.signed[-1]["max"], 5 * MB)
        self.many.validation_rules = {"maxFileSizeMB": 500}
        self.many.save()
        self.ask(self.many, name="b.pdf", type_="application/pdf")
        self.assertEqual(self.storage.signed[-1]["max"], 25 * MB)

    @override_settings(FORM_UPLOAD_DEFAULT_MB=1)
    def test_the_default_is_a_setting(self):
        self.assertEqual(self.ask(size=2 * MB).status_code, 413)

    def test_a_file_over_the_limit_is_refused_before_anything_is_signed(self):
        reply = self.ask(size=2 * MB + 1)
        self.assertEqual((reply.status_code, reply.data["code"]), (413, "FILE_TOO_LARGE"))
        self.assertEqual(self.storage.signed, [])
        self.assertEqual(FormUpload.objects.count(), 0)

    def test_file_type_rules(self):
        self.single.validation_rules = {"allowedFileTypes": ".pdf"}
        self.single.save()
        self.assertEqual(self.ask(name="a.png").data["code"], "FILE_TYPE_NOT_ALLOWED")
        self.assertEqual(self.ask(name="a.pdf", type_="application/pdf").status_code, 201)
        self.single.validation_rules = {"blockedFileTypes": ".zip"}
        self.single.save()
        self.assertEqual(self.ask(name="a.zip", type_="application/zip").data["code"], "FILE_TYPE_BLOCKED")

    def test_risky_types_are_refused_unless_the_admin_allowed_them(self):
        for name, kind in (("run.exe", "application/octet-stream"), ("page.html", "text/html"), ("pic.svg", "image/svg+xml")):
            self.assertEqual(self.ask(name=name, type_=kind).data["code"], "FILE_TYPE_BLOCKED", name)
        self.single.validation_rules = {"allowedFileTypes": ".svg"}
        self.single.save()
        self.assertEqual(self.ask(name="pic.svg", type_="image/svg+xml").status_code, 201)

    def test_hostile_file_names_are_refused(self):
        for name in ("", "  ", "../x.png", "a/b.png", "a\\b.png", "a\x00.png", "a" * 300 + ".png"):
            self.assertEqual(self.ask(name=name).data["code"], "INVALID_FILE_PAYLOAD", repr(name))

    def test_junk_sizes_and_types(self):
        self.assertEqual(self.ask(size="abc").data["code"], "INVALID_FILE_PAYLOAD")
        self.assertEqual(self.ask(size=None).data["code"], "INVALID_FILE_PAYLOAD")
        self.assertEqual(self.ask(size=0).data["code"], "FILE_TOO_SMALL")
        self.assertEqual(self.ask(type_="not a type").status_code, 201)
        self.assertEqual(self.storage.signed[-1]["content_type"], "application/octet-stream")

    def test_only_file_questions_of_this_form_are_accepted(self):
        self.assertEqual(self.ask(self.text).data["code"], "NOT_A_FILE_QUESTION")
        other_form = make_form(status=FormStatus.PUBLISHED)
        foreign = add_field(other_form, FieldType.FILE, label="Other")
        self.assertEqual(self.ask(foreign).data["code"], "NOT_A_FILE_QUESTION")
        reply = self.client.post(self.url, {"field_id": "abc", "name": "a.png", "size": 10}, format="json")
        self.assertEqual(reply.data["code"], "NOT_A_FILE_QUESTION")
        self.single.is_deleted = True
        self.single.save()
        self.assertEqual(self.ask().data["code"], "NOT_A_FILE_QUESTION")

    def test_a_closed_or_unpublished_form_takes_no_uploads_from_a_member(self):
        self.form.status = FormStatus.CLOSED
        self.form.save()
        self.assertEqual(self.ask().data["code"], "FORM_NOT_OPEN")

    def test_an_admin_can_upload_to_a_draft_form_to_preview_it(self):
        self.form.status = FormStatus.DRAFT
        self.form.save()
        admin = User.objects.create_user(username="ada", email="ada@example.org", password="x", role="ADMIN")
        self.client.force_authenticate(admin)
        self.assertEqual(self.ask().status_code, 201)

    def test_signing_in_is_required(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.ask().status_code, 401)
        self.assertEqual(self.client.post(f"{self.url}{'0' * 32}/complete/").status_code, 401)

    def test_nothing_is_offered_while_the_feature_is_off(self):
        with override_settings(FORM_UPLOADS_BACKEND="inline"):
            reply = self.ask()
        self.assertEqual((reply.status_code, reply.data["code"]), (503, "UPLOADS_UNAVAILABLE"))

    def test_one_file_per_single_file_question_a_new_pick_replaces_the_old(self):
        first = self.ask()
        self.arrive(first)
        self.finish(first)
        second = self.ask(name="new.png")
        self.assertEqual(second.status_code, 201)
        old = FormUpload.objects.get(pk=first.data["upload"]["id"])
        self.assertEqual(old.state, UploadState.DELETED)
        self.assertIn(old.key, self.storage.deleted)

    def test_multi_file_question_stops_at_its_limit(self):
        for index in range(2):
            self.assertEqual(self.ask(self.many, name=f"{index}.pdf", type_="application/pdf").status_code, 201)
        reply = self.ask(self.many, name="3.pdf", type_="application/pdf")
        self.assertEqual((reply.status_code, reply.data["code"]), (400, "TOO_MANY_FILES"))
        # removing one makes room
        first = FormUpload.objects.filter(field=self.many).first()
        self.assertEqual(self.client.delete(f"{self.url}{first.pk}/").status_code, 204)
        self.assertEqual(self.ask(self.many, name="3.pdf", type_="application/pdf").status_code, 201)

    @override_settings(FORM_UPLOAD_MAX_PENDING_PER_FORM=3)
    def test_a_cap_on_files_waiting_on_one_form(self):
        self.many.validation_rules = {"maxFiles": 10}
        self.many.save()
        for index in range(3):
            self.assertEqual(self.ask(self.many, name=f"{index}.pdf", type_="application/pdf").status_code, 201)
        reply = self.ask(self.many, name="x.pdf", type_="application/pdf")
        self.assertEqual((reply.status_code, reply.data["code"]), (429, "TOO_MANY_PENDING"))
        # someone else is not affected
        self.client.force_authenticate(self.other)
        self.assertEqual(self.ask(self.many, name="y.pdf", type_="application/pdf").status_code, 201)

    def test_abandoned_requests_stop_counting(self):
        self.many.validation_rules = {"maxFiles": 1}
        self.many.save()
        self.ask(self.many, name="a.pdf", type_="application/pdf")
        FormUpload.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.ask(self.many, name="b.pdf", type_="application/pdf").status_code, 201)

    # --- finishing --------------------------------------------------------------------------------

    def test_a_real_upload_is_confirmed_with_its_real_size(self):
        reply = self.ask(size=10)
        self.arrive(reply)
        done = self.finish(reply)
        self.assertEqual(done.status_code, 200, done.data)
        self.assertEqual((done.data["upload"]["state"], done.data["upload"]["size"]), ("UPLOADED", len(PNG)))
        self.assertEqual(self.finish(reply).status_code, 200)  # asking again is harmless

    def test_confirming_before_the_file_arrived_says_so_and_can_be_retried(self):
        reply = self.ask()
        self.assertEqual(self.finish(reply).data["code"], "UPLOAD_MISSING")
        self.arrive(reply)
        self.assertEqual(self.finish(reply).status_code, 200)

    def test_a_file_that_is_not_what_its_name_says_is_removed(self):
        reply = self.ask()
        self.arrive(reply, body=b"MZ this is not a png at all......")
        refused = self.finish(reply)
        self.assertEqual((refused.status_code, refused.data["code"]), (400, "FILE_CONTENT_MISMATCH"))
        upload = FormUpload.objects.get()
        self.assertEqual(upload.state, UploadState.REJECTED)  # the refusal is remembered
        self.assertIn(upload.key, self.storage.deleted)
        self.assertEqual(self.finish(reply).status_code, 404)

    def test_a_file_bigger_than_signed_is_removed(self):
        reply = self.ask()
        self.arrive(reply, body=PNG + b"0" * (2 * MB))
        refused = self.finish(reply)
        self.assertEqual((refused.status_code, refused.data["code"]), (413, "FILE_TOO_LARGE"))
        self.assertEqual(self.storage.objects, {})

    def test_the_limit_is_rechecked_if_the_admin_lowered_it_meanwhile(self):
        reply = self.ask(size=100)
        self.arrive(reply, body=PNG + b"0" * 5000)
        self.single.validation_rules = {"maxFileSizeMB": 0.001}
        self.single.save()
        self.assertEqual(self.finish(reply).data["code"], "FILE_TOO_LARGE")

    def test_an_expired_request_cannot_be_completed(self):
        reply = self.ask()
        self.arrive(reply)
        FormUpload.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual((self.finish(reply).status_code, self.finish(reply).data["code"]), (410, "UPLOAD_EXPIRED"))

    # --- whose upload is it -----------------------------------------------------------------------

    def test_nobody_else_can_confirm_or_remove_my_upload(self):
        reply = self.ask()
        self.arrive(reply)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.finish(reply).status_code, 404)
        self.assertEqual(self.client.delete(f"{self.url}{reply.data['upload']['id']}/").status_code, 404)
        self.assertEqual(FormUpload.objects.get().state, UploadState.PENDING)

    def test_an_upload_is_only_reachable_through_its_own_form(self):
        reply = self.ask()
        other_form = make_form(status=FormStatus.PUBLISHED)
        wrong = f"/api/forms/{other_form.slug}/uploads/{reply.data['upload']['id']}/complete/"
        self.assertEqual(self.client.post(wrong).status_code, 404)

    def test_junk_ids(self):
        for junk in ("zzzz", "1" * 32, "not-a-uuid-at-all-but-36-chars-long!!"):
            self.assertIn(self.client.post(f"{self.url}{junk}/complete/").status_code, (404,), junk)

    # --- removing ---------------------------------------------------------------------------------

    def test_removing_a_picked_file_deletes_it_from_the_bucket(self):
        reply = self.ask()
        self.arrive(reply)
        self.finish(reply)
        self.assertEqual(self.client.delete(f"{self.url}{reply.data['upload']['id']}/").status_code, 204)
        upload = FormUpload.objects.get()
        self.assertEqual((upload.state, upload.deleted_by), (UploadState.DELETED, self.user))
        self.assertEqual(self.storage.objects, {})
        self.assertEqual(self.client.delete(f"{self.url}{upload.pk}/").status_code, 404)

    def test_a_file_already_in_a_response_cannot_be_removed_this_way(self):
        reply = self.ask()
        self.arrive(reply)
        self.finish(reply)
        FormUpload.objects.update(state=UploadState.ATTACHED)
        self.assertEqual(self.client.delete(f"{self.url}{reply.data['upload']['id']}/").status_code, 404)

    def test_a_storage_outage_while_deleting_does_not_fail_the_request(self):
        reply = self.ask()
        with mock.patch.object(self.storage, "delete", side_effect=RuntimeError("down")):
            self.assertEqual(self.client.delete(f"{self.url}{reply.data['upload']['id']}/").status_code, 204)
        self.assertEqual(FormUpload.objects.get().state, UploadState.DELETED)

    def test_limits_helpers(self):
        self.assertEqual(upload_service.max_files_for(self.single), 1)
        self.assertEqual(upload_service.max_files_for(self.many), 2)
        self.many.validation_rules = {"maxFileSizeMB": "oops", "maxFiles": "x", "minFileSizeKB": "y"}
        self.assertEqual(upload_service.max_bytes_for(self.many), 2 * MB)
        self.assertEqual(upload_service.max_files_for(self.many), 10)
        self.assertEqual(upload_service.min_bytes_for(self.many), 1)


@override_settings(FORM_UPLOAD_BUCKET="srkrcc-dev-form-uploads-745999343357", FORM_UPLOAD_REGION="ap-south-1", FORM_UPLOAD_ENDPOINT_URL="")
class PresignHostTests(SimpleTestCase):
    def test_the_form_posts_to_the_regional_bucket_host(self):
        with mock.patch.dict("os.environ", {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test"}):
            signed = get_upload_storage().presign_post(
                key="form-uploads/tmp/1/abc", content_type="image/png", min_bytes=1, max_bytes=100, expires=60,
            )
        self.assertEqual(signed["url"], "https://srkrcc-dev-form-uploads-745999343357.s3.ap-south-1.amazonaws.com/")

    @override_settings(FORM_UPLOAD_BUCKET="local-bucket", FORM_UPLOAD_REGION="us-east-1", FORM_UPLOAD_ENDPOINT_URL="http://127.0.0.1:4566")
    def test_a_local_endpoint_stays_on_that_host(self):
        with mock.patch.dict("os.environ", {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test"}):
            signed = get_upload_storage().presign_post(
                key="k", content_type="image/png", min_bytes=1, max_bytes=100, expires=60,
            )
        self.assertEqual(signed["url"], "http://127.0.0.1:4566/local-bucket")
