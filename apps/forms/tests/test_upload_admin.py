"""Form admins managing uploaded files: list, view/download links, delete, replace."""

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.audit.models import AuditLog
from apps.forms import upload_storage
from apps.forms.models import Answer, FieldType, FormStatus, FormUpload, UploadState
from .factories import add_field, make_form
from .test_upload_submission import CopyingStorage
from .test_uploads import PNG

User = get_user_model()
PDF = b"%PDF-1.4 " + b"0" * 64


@override_settings(FORM_UPLOADS_BACKEND="s3", FORM_UPLOAD_BUCKET="test-bucket")
class UploadAdminTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.storage = CopyingStorage()
        patcher = mock.patch("apps.forms.upload_storage.get_upload_storage", return_value=self.storage)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.admin = User.objects.create_user(username="ada", email="ada@example.org", password="x", role="ADMIN")
        self.lead = User.objects.create_user(username="lee", email="lee@example.org", password="x", role="CLUB_LEAD")
        self.member = User.objects.create_user(username="uma", email="uma@example.org", password="x", role="NON_AFFILIATE", first_name="Uma")
        self.other = User.objects.create_user(username="otto", email="otto@example.org", password="x", role="NON_AFFILIATE")
        self.form = make_form(status=FormStatus.PUBLISHED, allow_multiple_responses=True, max_responses_per_user=5)
        self.photo = add_field(self.form, FieldType.FILE, label="Photo", order=1, required=True)
        self.docs = add_field(self.form, FieldType.MULTI_FILE, label="Docs", order=2)
        self.base = f"/api/forms/{self.form.slug}"

    # --- helpers ---------------------------------------------------------------------------------

    def as_user(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def upload(self, client, field, name="me.png", body=PNG, kind="image/png"):
        asked = client.post(f"{self.base}/uploads/", {"field_id": field.id, "name": name, "size": len(body), "type": kind}, format="json")
        self.assertEqual(asked.status_code, 201, asked.data)
        upload_id = asked.data["upload"]["id"]
        self.storage.put(FormUpload.objects.get(pk=upload_id).key, body, kind)
        self.assertEqual(client.post(f"{self.base}/uploads/{upload_id}/complete/").status_code, 200)
        return upload_id

    def submit(self, client, answers):
        reply = client.post("/api/forms/submissions/", {"form": self.form.id, "answers": answers}, format="json")
        self.assertEqual(reply.status_code, 201, reply.data)
        return reply.data["id"]

    def respond(self, user, photo_name="me.png", docs=()):
        client = self.as_user(user)
        photo = self.upload(client, self.photo, photo_name)
        answers = [{"field": self.photo.id, "value": {"upload_id": photo}}]
        doc_ids = [self.upload(client, self.docs, n, PDF, "application/pdf") for n in docs]
        if doc_ids:
            answers.append({"field": self.docs.id, "value": [{"upload_id": d} for d in doc_ids]})
        return self.submit(client, answers), photo, doc_ids

    # --- listing ---------------------------------------------------------------------------------

    def test_an_admin_sees_every_attached_file_with_who_what_and_where(self):
        response_id, photo, _ = self.respond(self.member, docs=["cv.pdf"])
        data = self.as_user(self.admin).get(f"{self.base}/files/").data
        self.assertEqual(data["count"], 2)
        by_name = {f["name"]: f for f in data["results"]}
        self.assertEqual(by_name["me.png"]["uploader"], {"id": self.member.pk, "name": "Uma", "email": "uma@example.org"})
        self.assertEqual((by_name["me.png"]["is_image"], by_name["cv.pdf"]["is_image"]), (True, False))
        self.assertEqual(by_name["me.png"]["field"], {"id": self.photo.id, "label": "Photo"})
        self.assertEqual(by_name["me.png"]["response_id"], response_id)
        self.assertEqual(data["total_bytes"], len(PNG) + len(PDF))
        self.assertNotIn("key", by_name["me.png"])  # storage keys never leave the server

    def test_files_that_are_not_part_of_a_response_are_not_listed(self):
        self.upload(self.as_user(self.member), self.photo)  # picked, never submitted
        self.assertEqual(self.as_user(self.admin).get(f"{self.base}/files/").data["count"], 0)

    def test_filters(self):
        self.respond(self.member, "alpha.png", docs=["cv.pdf"])
        self.respond(self.other, "beta.png")
        admin = self.as_user(self.admin)
        count = lambda query: admin.get(f"{self.base}/files/?{query}").data["count"]  # noqa: E731
        self.assertEqual(count("kind=images"), 2)
        self.assertEqual(count("kind=documents"), 1)
        self.assertEqual(count(f"field={self.docs.id}"), 1)
        self.assertEqual(count("search=alpha"), 1)
        self.assertEqual(count("search=otto%40example"), 1)
        self.assertEqual(count("search=Uma"), 2)
        self.assertEqual(count("search=nothing-like-this"), 0)
        tomorrow = (timezone.now() + timedelta(days=1)).date().isoformat()
        self.assertEqual(count(f"date_from={tomorrow}"), 0)
        self.assertEqual(count(f"date_to={tomorrow}"), 3)
        self.assertEqual(count("field=abc&date_from=junk"), 3)

    def test_paging(self):
        for n in range(3):
            self.respond(self.member, f"{n}.png")
        page = self.as_user(self.admin).get(f"{self.base}/files/?page_size=2").data
        self.assertEqual((page["count"], len(page["results"]), bool(page["next"])), (3, 2, True))

    def test_only_form_admins_and_leads_may_use_the_files_tab(self):
        self.respond(self.member)
        for user, expected in ((self.admin, 200), (self.lead, 200), (self.member, 403), (self.other, 403)):
            self.assertEqual(self.as_user(user).get(f"{self.base}/files/").status_code, expected, user.role)
        self.assertIn(APIClient().get(f"{self.base}/files/").status_code, (401, 403))

    def test_the_list_is_one_query_per_page_not_one_per_file(self):
        for n in range(4):
            self.respond(self.member, f"{n}.png")
        admin = self.as_user(self.admin)
        with self.assertNumQueries(7) as _:
            admin.get(f"{self.base}/files/")

    # --- links -----------------------------------------------------------------------------------

    def test_an_admin_gets_a_short_link_and_images_may_open_in_the_page(self):
        _, photo, _ = self.respond(self.member)
        reply = self.as_user(self.admin).get(f"{self.base}/files/{photo}/link/?inline=1")
        self.assertEqual((reply.status_code, reply.data["expires_in"], reply.data["inline"]), (200, 60, True))
        self.assertTrue(reply.data["url"].startswith("https://bucket.example/"))
        self.assertEqual(self.storage.links[-1]["filename"], "me.png")

    def test_only_safe_types_open_in_the_page_everything_else_downloads(self):
        _, _, docs = self.respond(self.member, docs=["cv.pdf", "page.txt"])
        admin = self.as_user(self.admin)
        self.assertTrue(admin.get(f"{self.base}/files/{docs[0]}/link/?inline=1").data["inline"])  # pdf
        FormUpload.objects.filter(pk=docs[1]).update(content_type="text/html")
        self.assertFalse(admin.get(f"{self.base}/files/{docs[1]}/link/?inline=1").data["inline"])
        self.assertFalse(admin.get(f"{self.base}/files/{docs[0]}/link/").data["inline"])  # not asked for

    def test_a_person_can_see_their_own_file_but_not_someone_elses(self):
        _, mine, _ = self.respond(self.member)
        _, theirs, _ = self.respond(self.other)
        client = self.as_user(self.member)
        self.assertEqual(client.get(f"{self.base}/files/{mine}/link/").status_code, 200)
        self.assertEqual(client.get(f"{self.base}/files/{theirs}/link/").status_code, 404)
        self.assertIn(APIClient().get(f"{self.base}/files/{mine}/link/").status_code, (401, 403))

    def test_links_for_deleted_or_unknown_files_and_other_forms_are_not_found(self):
        _, photo, _ = self.respond(self.member)
        admin = self.as_user(self.admin)
        self.assertEqual(admin.get(f"{self.base}/files/{'0' * 32}/link/").status_code, 404)
        other_form = make_form(status=FormStatus.PUBLISHED)
        self.assertEqual(admin.get(f"/api/forms/{other_form.slug}/files/{photo}/link/").status_code, 404)
        FormUpload.objects.filter(pk=photo).update(state=UploadState.DELETED)
        self.assertEqual(admin.get(f"{self.base}/files/{photo}/link/").status_code, 404)

    # --- deleting --------------------------------------------------------------------------------

    def test_deleting_removes_the_file_from_the_answer_and_the_bucket(self):
        response_id, photo, docs = self.respond(self.member, docs=["a.pdf", "b.pdf"])
        key = FormUpload.objects.get(pk=docs[0]).key
        with self.captureOnCommitCallbacks(execute=True):
            reply = self.as_user(self.admin).post(f"{self.base}/files/delete/", {"ids": [docs[0]]}, format="json")
        self.assertEqual((reply.status_code, reply.data["removed"], reply.data["responses_missing_required_file"]), (200, 1, []))
        upload = FormUpload.objects.get(pk=docs[0])
        self.assertEqual((upload.state, upload.deleted_by), (UploadState.DELETED, self.admin))
        self.assertNotIn(key, self.storage.objects)
        left = Answer.objects.get(response_id=response_id, field=self.docs).value
        self.assertEqual([i["upload_id"] for i in left], [docs[1]])
        self.assertEqual(self.as_user(self.admin).get(f"{self.base}/files/").data["count"], 2)

    def test_deleting_the_last_file_removes_the_answer_and_flags_a_missing_required_file(self):
        response_id, photo, _ = self.respond(self.member)
        reply = self.as_user(self.admin).post(f"{self.base}/files/delete/", {"ids": [photo]}, format="json")
        self.assertEqual(reply.data["responses_missing_required_file"], [response_id])
        self.assertFalse(Answer.objects.filter(response_id=response_id, field=self.photo).exists())

    def test_deletes_are_audited(self):
        _, photo, _ = self.respond(self.member)
        self.as_user(self.admin).post(f"{self.base}/files/delete/", {"ids": [photo]}, format="json")
        entry = AuditLog.objects.get(action="Deleted Form File")
        self.assertEqual((entry.actor, entry.target_id, entry.details["file"]), (self.admin, self.form.slug, "me.png"))

    def test_bulk_delete_ignores_unknown_repeated_and_foreign_ids(self):
        _, a, _ = self.respond(self.member, "a.png")
        _, b, _ = self.respond(self.member, "b.png")
        other_form = make_form(status=FormStatus.PUBLISHED)
        foreign = FormUpload.objects.create(form=other_form, original_name="x.png", key="k-foreign", state=UploadState.ATTACHED)
        reply = self.as_user(self.admin).post(f"{self.base}/files/delete/", {"ids": [a, a, b, "junk", "0" * 32, str(foreign.pk)]}, format="json")
        self.assertEqual(reply.data["removed"], 2)
        foreign.refresh_from_db()
        self.assertEqual(foreign.state, UploadState.ATTACHED)

    def test_delete_needs_a_list_and_the_right_role(self):
        _, photo, _ = self.respond(self.member)
        self.assertEqual(self.as_user(self.admin).post(f"{self.base}/files/delete/", {"ids": "x"}, format="json").status_code, 400)
        self.assertEqual(self.as_user(self.admin).post(f"{self.base}/files/delete/", {}, format="json").status_code, 400)
        self.assertEqual(self.as_user(self.member).post(f"{self.base}/files/delete/", {"ids": [photo]}, format="json").status_code, 403)
        self.assertEqual(FormUpload.objects.get(pk=photo).state, UploadState.ATTACHED)

    def test_a_storage_outage_while_deleting_still_records_the_delete(self):
        _, photo, _ = self.respond(self.member)
        with mock.patch.object(self.storage, "delete", side_effect=RuntimeError("down")), self.captureOnCommitCallbacks(execute=True):
            reply = self.as_user(self.admin).post(f"{self.base}/files/delete/", {"ids": [photo]}, format="json")
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(FormUpload.objects.get(pk=photo).state, UploadState.DELETED)

    # --- replacing -------------------------------------------------------------------------------

    def test_replacing_swaps_the_file_in_the_answer_and_keeps_the_history(self):
        response_id, photo, _ = self.respond(self.member)
        old_key = FormUpload.objects.get(pk=photo).key
        admin = self.as_user(self.admin)
        fresh = self.upload(admin, self.photo, "fixed.png")
        with self.captureOnCommitCallbacks(execute=True):
            reply = admin.post(f"{self.base}/files/{photo}/replace/", {"upload_id": fresh}, format="json")
        self.assertEqual(reply.status_code, 200, reply.data)
        new = FormUpload.objects.get(pk=fresh)
        self.assertEqual((new.state, new.response_id, new.replaces_id), (UploadState.ATTACHED, response_id, FormUpload.objects.get(pk=photo).pk))
        self.assertEqual(new.key, upload_storage.final_key(self.form.pk, new.pk))
        old = FormUpload.objects.get(pk=photo)
        self.assertEqual((old.state, old.deleted_by), (UploadState.DELETED, self.admin))
        self.assertNotIn(old_key, self.storage.objects)
        self.assertIn(new.key, self.storage.objects)
        value = Answer.objects.get(response_id=response_id, field=self.photo).value
        self.assertEqual(value, [{"upload_id": fresh, "name": "fixed.png", "size": len(PNG), "type": "image/png"}])
        self.assertEqual(AuditLog.objects.get(action="Replaced Form File").details["replaced_with"], "fixed.png")
        listed = self.as_user(self.admin).get(f"{self.base}/files/").data["results"]
        self.assertEqual([(f["name"], f["replaced_a_file"]) for f in listed], [("fixed.png", True)])

    def test_replacing_one_of_several_files_leaves_the_others(self):
        response_id, _, docs = self.respond(self.member, docs=["a.pdf", "b.pdf"])
        admin = self.as_user(self.admin)
        fresh = self.upload(admin, self.docs, "c.pdf", PDF, "application/pdf")
        admin.post(f"{self.base}/files/{docs[0]}/replace/", {"upload_id": fresh}, format="json")
        names = [i["name"] for i in Answer.objects.get(response_id=response_id, field=self.docs).value]
        self.assertEqual(sorted(names), ["b.pdf", "c.pdf"])

    def test_the_replacement_must_be_a_finished_upload_of_the_same_question_made_by_the_admin(self):
        _, photo, docs = self.respond(self.member, docs=["a.pdf"])
        admin = self.as_user(self.admin)
        post = lambda new: admin.post(f"{self.base}/files/{photo}/replace/", {"upload_id": new}, format="json")  # noqa: E731
        self.assertEqual(post(None).data["code"], "NOT_A_REPLACEMENT")
        self.assertEqual(post("junk").data["code"], "NOT_A_REPLACEMENT")
        self.assertEqual(post(self.upload(admin, self.docs, "z.pdf", PDF, "application/pdf")).data["code"], "NOT_A_REPLACEMENT")  # other question
        theirs = self.upload(self.as_user(self.other), self.photo, "o.png")
        self.assertEqual(post(theirs).data["code"], "NOT_A_REPLACEMENT")  # someone else's upload
        pending = admin.post(f"{self.base}/uploads/", {"field_id": self.photo.id, "name": "p.png", "size": 10, "type": "image/png"}, format="json")
        self.assertEqual(post(pending.data["upload"]["id"]).data["code"], "NOT_A_REPLACEMENT")  # not finished
        self.assertEqual(FormUpload.objects.get(pk=photo).state, UploadState.ATTACHED)

    def test_a_replacement_that_breaks_the_questions_rules_never_gets_that_far(self):
        _, photo, _ = self.respond(self.member)
        self.photo.validation_rules = {"allowedFileTypes": ".png"}
        self.photo.save()
        admin = self.as_user(self.admin)
        refused = admin.post(f"{self.base}/uploads/", {"field_id": self.photo.id, "name": "x.pdf", "size": 10, "type": "application/pdf"}, format="json")
        self.assertEqual(refused.data["code"], "FILE_TYPE_NOT_ALLOWED")

    def test_a_storage_failure_during_replace_changes_nothing(self):
        response_id, photo, _ = self.respond(self.member)
        admin = self.as_user(self.admin)
        fresh = self.upload(admin, self.photo, "fixed.png")
        with mock.patch.object(self.storage, "copy", side_effect=RuntimeError("down")):
            reply = admin.post(f"{self.base}/files/{photo}/replace/", {"upload_id": fresh}, format="json")
        self.assertEqual((reply.status_code, reply.data["code"]), (503, "UPLOAD_STORAGE_ERROR"))
        self.assertEqual(FormUpload.objects.get(pk=photo).state, UploadState.ATTACHED)
        self.assertEqual(FormUpload.objects.get(pk=fresh).state, UploadState.UPLOADED)

    def test_members_cannot_replace_and_unknown_files_are_not_found(self):
        _, photo, _ = self.respond(self.member)
        fresh = self.upload(self.as_user(self.member), self.photo, "mine.png")
        self.assertEqual(self.as_user(self.member).post(f"{self.base}/files/{photo}/replace/", {"upload_id": fresh}, format="json").status_code, 403)
        self.assertEqual(self.as_user(self.admin).post(f"{self.base}/files/{'0' * 32}/replace/", {"upload_id": fresh}, format="json").status_code, 404)
