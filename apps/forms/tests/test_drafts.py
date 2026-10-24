"""Server-side autosave of unfinished responses: /api/forms/{slug}/draft/."""

from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.forms.models import FieldType, FormStatus, ResponseDraft
from .factories import add_field, make_form

User = get_user_model()


class DraftApiTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user(username="dana", email="dana@srkr.ac.in", password="x", role="NON_AFFILIATE")
        self.client.force_authenticate(self.user)
        self.form = make_form(status=FormStatus.PUBLISHED)
        self.name = add_field(self.form, FieldType.TEXT, label="Name", order=1)
        self.brk = add_field(self.form, FieldType.PAGE_BREAK, label="Page 2", order=2)
        self.age = add_field(self.form, FieldType.NUMBER, label="Age", order=3)
        self.url = f"/api/forms/{self.form.slug}/draft/"

    def _save(self, answers, page=0, url=None):
        return self.client.put(url or self.url, {"answers": answers, "current_page": page}, format="json")

    def test_nothing_saved_yet(self):
        resp = self.client.get(self.url)
        self.assertEqual((resp.status_code, resp.data), (200, {"draft": None}))

    def test_saves_and_returns_the_draft_with_the_page(self):
        resp = self._save({str(self.name.id): "Dana", str(self.age.id): "19"}, page=1)
        self.assertEqual(resp.status_code, 200, resp.data)
        draft = self.client.get(self.url).data["draft"]
        self.assertEqual(draft["answers"], {str(self.name.id): "Dana", str(self.age.id): "19"})
        self.assertEqual(draft["current_page"], 1)
        self.assertFalse(draft["form_changed"])

    def test_a_new_save_replaces_the_old_one(self):
        self._save({str(self.name.id): "Dana"})
        self._save({str(self.age.id): "20"}, page=1)
        draft = self.client.get(self.url).data["draft"]
        self.assertEqual(draft["answers"], {str(self.age.id): "20"})
        self.assertEqual(ResponseDraft.objects.filter(form=self.form, user=self.user).count(), 1)

    def test_only_answerable_questions_are_kept(self):
        profile = add_field(self.form, FieldType.TEXT, label="Email", order=4, profile_field="email")
        gone = add_field(self.form, FieldType.TEXT, label="Gone", order=5)
        gone.is_deleted = True
        gone.save()
        resp = self._save({
            str(self.name.id): "Dana", str(self.brk.id): "x", str(profile.id): "x", str(gone.id): "x",
            "99999": "x", "not-an-id": "x", str(self.age.id): "",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.get(self.url).data["draft"]["answers"], {str(self.name.id): "Dana"})

    def test_files_and_signatures_are_not_stored_and_are_reported(self):
        upload = add_field(self.form, FieldType.FILE, label="CV", order=4)
        sign = add_field(self.form, FieldType.SIGNATURE, label="Sig", order=5)
        resp = self._save({str(upload.id): [{"name": "a.pdf", "size": 1}], str(sign.id): "data:image/png;base64,AAAA",
                           str(self.name.id): "Dana"})
        self.assertEqual(sorted(resp.data["dropped"]), sorted([upload.id, sign.id]))
        self.assertEqual(self.client.get(self.url).data["draft"]["answers"], {str(self.name.id): "Dana"})

    def test_values_a_question_could_never_hold_are_dropped(self):
        resp = self._save({str(self.age.id): "lots", str(self.name.id): "bad\x00name"})
        self.assertEqual(sorted(resp.data["dropped"]), sorted([self.age.id, self.name.id]))
        self.assertEqual(self.client.get(self.url).data["draft"]["answers"], {})

    def test_an_oversized_draft_is_refused(self):
        essay = add_field(self.form, FieldType.PARAGRAPH, label="Essay", order=4)
        extra = [add_field(self.form, FieldType.PARAGRAPH, label=f"E{i}", order=5 + i) for i in range(25)]
        answers = {str(f.id): "x" * 9000 for f in [essay, *extra]}
        resp = self._save(answers)
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(resp.data["code"], "DRAFT_TOO_LARGE")

    def test_the_page_index_is_clamped(self):
        self._save({str(self.name.id): "Dana"}, page=99999)
        self.assertEqual(self.client.get(self.url).data["draft"]["current_page"], 100)
        self._save({str(self.name.id): "Dana"}, page=-4)
        self.assertEqual(self.client.get(self.url).data["draft"]["current_page"], 0)

    def test_sign_in_is_required(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.assertEqual(self._save({}).status_code, 401)

    def test_each_user_has_their_own_draft(self):
        self._save({str(self.name.id): "Dana"})
        self.client.force_authenticate(User.objects.create_user(
            username="eli", email="eli@srkr.ac.in", password="x", role="NON_AFFILIATE"))
        self.assertIsNone(self.client.get(self.url).data["draft"])
        self._save({str(self.name.id): "Eli"})
        self.assertEqual(ResponseDraft.objects.filter(form=self.form).count(), 2)

    def test_forms_that_are_not_taking_responses_do_not_accept_drafts(self):
        for status_, extra in [
            (FormStatus.CLOSED, {}),
            (FormStatus.SCHEDULED, {"open_at": timezone.now() + timedelta(days=2)}),
            (FormStatus.PUBLISHED, {"close_at": timezone.now() - timedelta(hours=1)}),
        ]:
            form = make_form(status=status_, **extra)
            field = add_field(form, FieldType.TEXT, label="Q", order=1)
            resp = self._save({str(field.id): "x"}, url=f"/api/forms/{form.slug}/draft/")
            self.assertEqual(resp.status_code, 409, (status_, resp.data))
            self.assertEqual(resp.data["code"], "FORM_NOT_OPEN")

    def test_an_unpublished_form_is_not_reachable_at_all(self):
        hidden = make_form(status=FormStatus.DRAFT)
        self.assertEqual(self.client.get(f"/api/forms/{hidden.slug}/draft/").status_code, 404)

    def test_questions_removed_since_the_draft_was_saved_are_left_out(self):
        self._save({str(self.name.id): "Dana", str(self.age.id): "19"})
        self.age.is_deleted = True
        self.age.save()
        self.form.version += 1
        self.form.save()
        draft = self.client.get(self.url).data["draft"]
        self.assertEqual(draft["answers"], {str(self.name.id): "Dana"})
        self.assertTrue(draft["form_changed"])

    def test_old_drafts_are_not_offered_and_can_be_purged(self):
        self._save({str(self.name.id): "Dana"})
        ResponseDraft.objects.update(updated_at=timezone.now() - timedelta(days=45))
        self.assertIsNone(self.client.get(self.url).data["draft"])
        out = StringIO()
        call_command("purge_form_drafts", "--dry-run", stdout=out)
        self.assertEqual(ResponseDraft.objects.count(), 1)
        call_command("purge_form_drafts", stdout=out)
        self.assertEqual(ResponseDraft.objects.count(), 0)

    def test_the_draft_can_be_discarded(self):
        self._save({str(self.name.id): "Dana"})
        self.assertEqual(self.client.delete(self.url).status_code, 204)
        self.assertIsNone(self.client.get(self.url).data["draft"])

    def test_submitting_the_form_clears_the_draft(self):
        self._save({str(self.name.id): "Dana"})
        resp = self.client.post("/api/forms/submissions/", {
            "form": self.form.id, "idempotency_key": "draft-submit",
            "answers": [{"field": self.name.id, "value": "Dana"}, {"field": self.age.id, "value": "19"}],
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertFalse(ResponseDraft.objects.filter(form=self.form, user=self.user).exists())
