from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase
from django.test import TestCase

from apps.forms.models import FieldType, FormStatus, Response, Answer
from apps.forms.validation import validate_submission, codes
from apps.forms.serializers import ResponseDetailSerializer, check_field_conditional_dependencies
from .factories import make_form, add_field, answers

User = get_user_model()


class SecurityTests(APITestCase):
    def setUp(self):
        self.form = make_form(status=FormStatus.PUBLISHED)
        self.name = add_field(self.form, FieldType.TEXT, label="Name", required=True, order=1)
        self.secret = add_field(self.form, FieldType.TEXT, label="Secret", order=2,
                                conditional_logic={"logic": "AND",
                                                   "rules": [{"field": self.name.id, "operator": "equals", "value": "unlock"}],
                                                   "action": "show"})

    def _post(self, answers, key):
        return self.client.post("/api/forms/submissions/",
                                {"form": self.form.id, "answers": answers, "idempotency_key": key},
                                format="json")

    def test_cannot_bypass_required_by_omitting_field(self):
        r = self._post([], "sec1")
        self.assertEqual(r.status_code, 400)
        self.assertIn(codes.REQUIRED, {e["code"] for e in r.data["errors"]})

    def test_cannot_send_answer_for_conditionally_hidden_field(self):
        # name != "unlock" -> secret hidden -> its answer is silently dropped, not stored
        r = self._post([{"field": self.name.id, "value": "nope"},
                        {"field": self.secret.id, "value": "inject"}], "sec2")
        self.assertEqual(r.status_code, 201, r.data)
        stored = {a.field_id for a in Response.objects.get(form=self.form).answers.all()}
        self.assertNotIn(self.secret.id, stored)

    def test_wrong_types_rejected(self):
        num = add_field(self.form, FieldType.NUMBER, label="N", required=True, order=3)
        r = self._post([{"field": self.name.id, "value": "x"},
                        {"field": num.id, "value": {"nested": "object"}}], "sec3")
        self.assertEqual(r.status_code, 400)
        self.assertIn("EXPECTED_SCALAR", {e["code"] for e in r.data["errors"]})

    def test_malformed_field_id(self):
        r = self._post([{"field": "not-a-number", "value": "x"}], "sec4")
        self.assertEqual(r.status_code, 400)
        self.assertIn("UNKNOWN_FIELD", {e["code"] for e in r.data["errors"]})


class BackwardCompatTests(TestCase):
    def test_legacy_single_rule_conditional_still_works(self):
        form = make_form()
        trigger = add_field(form, FieldType.RADIO, label="T", options=["Yes", "No"], order=1)
        dep = add_field(form, FieldType.TEXT, label="D", required=True, order=2,
                        conditional_logic={"if": trigger.id, "equals": "Yes"})  # legacy shape
        # trigger=No -> dep hidden -> ok without answering it
        self.assertTrue(validate_submission(form, answers((trigger, "No"))).ok)
        # trigger=Yes -> dep required
        r = validate_submission(form, answers((trigger, "Yes")))
        self.assertFalse(r.ok)
        self.assertEqual(r.errors[0].field_id, dep.id)

    def test_legacy_multi_rule_with_if_key(self):
        form = make_form()
        a = add_field(form, FieldType.NUMBER, label="A", order=1)
        b = add_field(form, FieldType.TEXT, label="B", required=True, order=2,
                      conditional_logic={"logic": "AND",
                                         "rules": [{"if": a.id, "operator": "greater_than", "value": "5"}]})
        self.assertTrue(validate_submission(form, answers((a, "3"))).ok)          # b hidden
        self.assertFalse(validate_submission(form, answers((a, "10"))).ok)         # b shown+required

    def test_broken_parent_placeholder_never_blocks(self):
        form = make_form()
        add_field(form, FieldType.TEXT, label="A", required=True, order=1)
        ph = add_field(form, FieldType.TEXT, label="Placeholder", required=True, order=2,
                       conditional_logic={"if": "parent", "equals": "Yes"})
        # placeholder rule is unevaluable -> field stays visible; still required.
        r = validate_submission(form, answers((form.fields.get(order=1), "x"), (ph, "answered")))
        self.assertTrue(r.ok, [e.message for e in r.errors])

    def test_check_field_conditional_dependencies_reads_canonical_and_legacy(self):
        form = make_form()
        target = add_field(form, FieldType.RADIO, label="Target", options=["A", "B"], order=1)
        legacy = add_field(form, FieldType.TEXT, label="L", order=2,
                           conditional_logic={"if": target.id, "equals": "A"})
        canonical = add_field(form, FieldType.TEXT, label="C", order=3,
                              conditional_logic={"logic": "AND",
                                                 "rules": [{"field": target.id, "operator": "equals", "value": "A"}]})
        deps = check_field_conditional_dependencies(target.id, form)
        dep_ids = {d.id for d in deps}
        self.assertEqual(dep_ids, {legacy.id, canonical.id})

    def test_existing_response_still_serializes(self):
        form = make_form()
        f1 = add_field(form, FieldType.TEXT, label="Name", order=1)
        f2 = add_field(form, FieldType.CHECKBOX, label="Skills", options=["A", "B"], order=2)
        resp = Response.objects.create(form=form, form_version=form.version)
        Answer.objects.create(response=resp, field=f1, value="Ada")
        Answer.objects.create(response=resp, field=f2, value=["A", "B"])
        data = ResponseDetailSerializer(resp).data
        self.assertEqual(len(data["answers"]), 2)


class FormVisibilityTests(APITestCase):
    """DRAFT forms are unfinished/internal — only ADMIN/CLUB_LEAD may list or
    retrieve them; everyone else (including anonymous) must not be able to
    discover a draft form's existence or field structure, whether by listing
    or by knowing/guessing its slug."""

    def setUp(self):
        self.draft = make_form(status=FormStatus.DRAFT, slug="secret-draft")
        self.published = make_form(status=FormStatus.PUBLISHED, slug="open-form")
        self.scheduled = make_form(status=FormStatus.SCHEDULED, slug="upcoming-form")
        self.admin = User.objects.create_user(
            username="admin", email="admin@srkr.ac.in", password="x", role="ADMIN", is_staff=True,
        )

    def test_anonymous_list_excludes_draft(self):
        resp = self.client.get("/api/forms/")
        slugs = {f["slug"] for f in resp.data}
        self.assertNotIn("secret-draft", slugs)
        self.assertIn("open-form", slugs)
        self.assertIn("upcoming-form", slugs)

    def test_anonymous_retrieve_of_draft_slug_is_404(self):
        resp = self.client.get(f"/api/forms/{self.draft.slug}/")
        self.assertEqual(resp.status_code, 404)

    def test_admin_can_still_list_and_retrieve_draft(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.get("/api/forms/")
        slugs = {f["slug"] for f in resp.data}
        self.assertIn("secret-draft", slugs)
        resp = self.client.get(f"/api/forms/{self.draft.slug}/")
        self.assertEqual(resp.status_code, 200)


class AnonymousSubmissionIdentityTests(APITestCase):
    """An anonymous submitter must never be able to attribute a response to
    an arbitrary user by ID — that was an IDOR letting anyone hijack another
    member's single-submission response or consume their response quota."""

    def setUp(self):
        self.form = make_form(status=FormStatus.PUBLISHED)
        self.name = add_field(self.form, FieldType.TEXT, label="Name", order=1)
        self.victim = User.objects.create_user(
            username="victim", email="victim@srkr.ac.in", password="x", role="NON_AFFILIATE",
        )

    def test_anonymous_post_with_user_id_does_not_attribute_response(self):
        resp = self.client.post(
            "/api/forms/submissions/",
            {"form": self.form.id, "user": self.victim.id,
             "answers": [{"field": self.name.id, "value": "Ada"}],
             "idempotency_key": "spoof-1"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        created = Response.objects.get(form=self.form)
        self.assertIsNone(created.user_id)
