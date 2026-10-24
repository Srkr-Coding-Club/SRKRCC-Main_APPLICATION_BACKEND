"""
The upload flow against a real S3 API (LocalStack). Skipped unless LOCALSTACK_URL is set, e.g.

    docker run -d -p 4566:4566 -e SERVICES=s3 localstack/localstack:3.8
    LOCALSTACK_URL=http://127.0.0.1:4566 python manage.py test apps.forms.tests.test_uploads_localstack

Nothing is replaced by a stand-in: signed forms are posted to the bucket over HTTP exactly as a browser would.
"""
import os
import urllib.error
import urllib.request
import uuid

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.forms import upload_storage
from apps.forms.models import Answer, FieldType, FormStatus, FormUpload, Response, UploadState
from .factories import add_field, make_form

User = get_user_model()
ENDPOINT = os.getenv("LOCALSTACK_URL", "")
BUCKET = "srkrcc-form-uploads-test"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def multipart(fields: dict, filename: str, body: bytes):
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n\r\n'.encode() + body + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def post_to_bucket(signed: dict, filename: str, body: bytes) -> int:
    data, content_type = multipart(signed["fields"], filename, body)
    request = urllib.request.Request(signed["url"], data=data, headers={"Content-Type": content_type}, method="POST")
    try:
        return urllib.request.urlopen(request, timeout=20).status
    except urllib.error.HTTPError as error:
        return error.code


@override_settings(
    FORM_UPLOADS_BACKEND="s3", FORM_UPLOAD_BUCKET=BUCKET, FORM_UPLOAD_ENDPOINT_URL=ENDPOINT, FORM_UPLOAD_REGION="us-east-1",
)
class LocalStackUploadTests(APITestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not ENDPOINT:
            return
        os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
        os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
        import boto3
        cls.s3 = boto3.client("s3", region_name="us-east-1", endpoint_url=ENDPOINT)
        try:
            cls.s3.create_bucket(Bucket=BUCKET)
        except cls.s3.exceptions.BucketAlreadyOwnedByYou:
            pass

    def setUp(self):
        if not ENDPOINT:
            self.skipTest("Set LOCALSTACK_URL to run the real-S3 tests.")
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user(username="uma", email="uma@example.org", password="x", role="NON_AFFILIATE")
        self.client.force_authenticate(self.user)
        self.form = make_form(status=FormStatus.PUBLISHED, allow_multiple_responses=True, max_responses_per_user=5)
        self.photo = add_field(self.form, FieldType.FILE, label="Photo", order=1)
        self.url = f"/api/forms/{self.form.slug}/uploads/"

    def exists(self, key):
        try:
            self.s3.head_object(Bucket=BUCKET, Key=key)
            return True
        except Exception:  # noqa: BLE001
            return False

    def ask(self, name="me.png", size=len(PNG), kind="image/png"):
        return self.client.post(self.url, {"field_id": self.photo.id, "name": name, "size": size, "type": kind}, format="json")

    def test_the_whole_journey_against_a_real_bucket(self):
        granted = self.ask()
        self.assertEqual(granted.status_code, 201, granted.data)
        upload = FormUpload.objects.get()

        self.assertEqual(post_to_bucket(granted.data["post"], "me.png", PNG), 204)
        self.assertTrue(self.exists(upload.key))

        done = self.client.post(f"{self.url}{upload.pk}/complete/")
        self.assertEqual((done.status_code, done.data["upload"]["size"]), (200, len(PNG)))

        sent = self.client.post("/api/forms/submissions/", {"form": self.form.id, "answers": [{"field": self.photo.id, "value": {"upload_id": str(upload.pk)}}]}, format="json")
        self.assertEqual(sent.status_code, 201, sent.data)
        upload.refresh_from_db()
        self.assertEqual(upload.state, UploadState.ATTACHED)
        self.assertEqual(upload.key, upload_storage.final_key(self.form.pk, upload.pk))
        self.assertTrue(self.exists(upload.key))
        with self.captureOnCommitCallbacks(execute=True):
            Response.objects.get().delete()
        self.assertFalse(self.exists(upload.key))

    def test_the_bucket_itself_refuses_a_file_bigger_than_signed(self):
        granted = self.ask(size=100)
        self.assertEqual(post_to_bucket(granted.data["post"], "me.png", PNG + b"0" * (3 * 1024 * 1024)), 400)
        self.assertFalse(self.exists(FormUpload.objects.get().key))

    def test_the_bucket_refuses_a_different_key_or_type(self):
        granted = self.ask()
        tampered = dict(granted.data["post"]["fields"], key="form-uploads/tmp/elsewhere")
        self.assertEqual(post_to_bucket({"url": granted.data["post"]["url"], "fields": tampered}, "me.png", PNG), 403)
        wrong_type = dict(granted.data["post"]["fields"], **{"Content-Type": "text/html"})
        self.assertEqual(post_to_bucket({"url": granted.data["post"]["url"], "fields": wrong_type}, "me.png", PNG), 403)

    def test_a_file_that_is_not_what_it_claims_is_removed_from_the_bucket(self):
        granted = self.ask()
        post_to_bucket(granted.data["post"], "me.png", b"MZ not a png at all............")
        upload = FormUpload.objects.get()
        self.assertEqual(self.client.post(f"{self.url}{upload.pk}/complete/").data["code"], "FILE_CONTENT_MISMATCH")
        self.assertFalse(self.exists(upload.key))

    def test_confirming_before_sending_is_reported_and_removing_deletes_the_object(self):
        granted = self.ask()
        upload = FormUpload.objects.get()
        self.assertEqual(self.client.post(f"{self.url}{upload.pk}/complete/").data["code"], "UPLOAD_MISSING")
        post_to_bucket(granted.data["post"], "me.png", PNG)
        self.assertEqual(self.client.delete(f"{self.url}{upload.pk}/").status_code, 204)
        self.assertFalse(self.exists(upload.key))

    def test_editing_a_response_to_swap_the_file_removes_the_old_object(self):
        single = make_form(status=FormStatus.PUBLISHED, allow_multiple_responses=False, allow_response_editing=True)
        field = add_field(single, FieldType.FILE, label="Photo")
        url = f"/api/forms/{single.slug}/uploads/"

        def up():
            asked = self.client.post(url, {"field_id": field.id, "name": "x.png", "size": len(PNG), "type": "image/png"}, format="json")
            post_to_bucket(asked.data["post"], "x.png", PNG)
            self.client.post(f"{url}{asked.data['upload']['id']}/complete/")
            return asked.data["upload"]["id"]

        def send(upload_id):
            return self.client.post("/api/forms/submissions/", {"form": single.id, "answers": [{"field": field.id, "value": {"upload_id": upload_id}}]}, format="json")

        first = up()
        self.assertEqual(send(first).status_code, 201)
        second = up()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(send(second).status_code, 200)
        self.assertFalse(self.exists(upload_storage.final_key(single.pk, first)))
        self.assertTrue(self.exists(upload_storage.final_key(single.pk, second)))
        self.assertEqual(Answer.objects.get(response__form=single).value[0]["upload_id"], second)

    def test_an_admins_link_downloads_the_real_file_with_the_right_headers(self):
        admin = User.objects.create_user(username="ada", email="ada@example.org", password="x", role="ADMIN")
        granted = self.ask(name="my photo.png")
        upload = FormUpload.objects.get()
        post_to_bucket(granted.data["post"], "my photo.png", PNG)
        self.client.post(f"{self.url}{upload.pk}/complete/")
        self.client.post("/api/forms/submissions/", {"form": self.form.id, "answers": [{"field": self.photo.id, "value": {"upload_id": str(upload.pk)}}]}, format="json")

        self.client.force_authenticate(admin)
        base = f"/api/forms/{self.form.slug}/files/{upload.pk}/link/"
        for inline, disposition in ((True, "inline"), (False, "attachment")):
            link = self.client.get(base + ("?inline=1" if inline else "")).data["url"]
            reply = urllib.request.urlopen(link, timeout=20)
            self.assertEqual(reply.read(), PNG)
            self.assertTrue(reply.headers["Content-Disposition"].startswith(disposition), reply.headers["Content-Disposition"])
            self.assertIn("my%20photo.png", reply.headers["Content-Disposition"])
        self.assertEqual(urllib.request.urlopen(self.client.get(base).data["url"], timeout=20).headers["Content-Type"], "application/octet-stream")

        removed = self.client.post(f"/api/forms/{self.form.slug}/files/delete/", {"ids": [str(upload.pk)]}, format="json")
        self.assertEqual(removed.data["removed"], 1)

    # --- housekeeping against the real API -------------------------------------------------------------

    def test_backfill_puts_the_file_in_the_bucket_and_the_admin_link_serves_it(self):
        import base64
        from apps.forms import upload_maintenance
        response = Response.objects.create(form=self.form, user=self.user)
        url = "data:image/png;base64," + base64.b64encode(PNG).decode()
        Answer.objects.create(response=response, field=self.photo, value={"name": "old.png", "type": "image/png", "url": url})
        self.assertEqual(upload_maintenance.backfill_inline()["files_moved"], 1)
        upload = FormUpload.objects.get()
        self.assertEqual(self.s3.get_object(Bucket=BUCKET, Key=upload.key)["Body"].read(), PNG)
        admin = User.objects.create_user(username="ada", email="ada@example.org", password="x", role="ADMIN")
        self.client.force_authenticate(admin)
        link = self.client.get(f"/api/forms/{self.form.slug}/files/{upload.pk}/link/").data["url"]
        self.assertEqual(urllib.request.urlopen(link, timeout=20).read(), PNG)

    def test_the_sweeper_removes_expired_objects_from_the_bucket(self):
        from datetime import timedelta
        from django.utils import timezone
        from apps.forms import upload_maintenance
        granted = self.ask()
        upload = FormUpload.objects.get()
        post_to_bucket(granted.data["post"], "me.png", PNG)
        self.client.post(f"{self.url}{upload.pk}/complete/")
        self.assertTrue(self.exists(upload.key))
        FormUpload.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(upload_maintenance.sweep_expired()["expired"], 1)
        self.assertFalse(self.exists(upload.key))

    @override_settings(FORM_UPLOAD_SCAN="guardduty")
    def test_a_guardduty_style_tag_decides_whether_a_file_is_served_or_removed(self):
        from apps.forms import upload_maintenance
        granted = self.ask()
        upload = FormUpload.objects.get()
        post_to_bucket(granted.data["post"], "me.png", PNG)
        self.client.post(f"{self.url}{upload.pk}/complete/")
        self.client.post("/api/forms/submissions/", {"form": self.form.id, "answers": [{"field": self.photo.id, "value": {"upload_id": str(upload.pk)}}]}, format="json")
        upload.refresh_from_db()

        admin = User.objects.create_user(username="ada", email="ada@example.org", password="x", role="ADMIN")
        self.client.force_authenticate(admin)
        link = f"/api/forms/{self.form.slug}/files/{upload.pk}/link/"
        self.assertEqual(self.client.get(link).status_code, 423)  # no verdict yet: held back

        tag = lambda value: self.s3.put_object_tagging(Bucket=BUCKET, Key=upload.key, Tagging={"TagSet": [{"Key": "GuardDutyMalwareScanStatus", "Value": value}]})  # noqa: E731
        tag("NO_THREATS_FOUND")
        self.assertEqual(self.client.get(link).status_code, 200)

        upload.scan_status = "NOT_SCANNED"
        upload.save()
        tag("THREATS_FOUND")
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(upload_maintenance.scan_pending()["infected"], 1)
        self.assertFalse(self.exists(upload.key))
