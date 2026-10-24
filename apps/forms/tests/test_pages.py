"""Multi-page forms: PAGE_BREAK questions, page routing (skip_to_section / end_form) and the checks around them."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APITestCase

from apps.forms.models import FieldType, FormField, FormStatus
from apps.forms.validation import codes, validate_form_definition, validate_submission
from .factories import add_field, answers, make_form

User = get_user_model()


def _route(source, action, value=None, op="equals", expected="done"):
    logic = {"logic": "AND", "action": action, "rules": [{"field": source.id, "operator": op, "value": expected}]}
    if value is not None:
        logic["action_value"] = value
    return logic


def _codes(report):
    return {e.code for e in report.errors}


class PageRoutingSubmissionTests(TestCase):
    def setUp(self):
        self.form = make_form()
        self.path = add_field(self.form, FieldType.RADIO, label="Path", options=["done", "more"], required=True, order=1)
        self.brk1 = add_field(self.form, FieldType.PAGE_BREAK, label="Details", order=2)
        self.detail = add_field(self.form, FieldType.TEXT, label="Detail", required=True, order=3)
        self.brk2 = add_field(self.form, FieldType.PAGE_BREAK, label="Extras", order=4)
        self.extra = add_field(self.form, FieldType.TEXT, label="Extra", required=True, order=5)

    def _route_after_first_page(self, action, value=None):
        self.brk1.conditional_logic = _route(self.path, action, value)
        self.brk1.save()

    def test_page_breaks_are_never_answered_or_required(self):
        report = validate_submission(self.form, answers(
            (self.path, "more"), (self.detail, "d"), (self.extra, "e"), (self.brk1, "ignored")))
        self.assertTrue(report.ok, [e.message for e in report.errors])
        self.assertNotIn(self.brk1.id, report.cleaned_answers)

    def test_every_reachable_page_is_still_required(self):
        report = validate_submission(self.form, answers((self.path, "more")))
        self.assertEqual({e.field_id for e in report.errors}, {self.detail.id, self.extra.id})

    def test_end_form_stops_requiring_later_pages_and_drops_their_answers(self):
        self._route_after_first_page("end_form")
        report = validate_submission(self.form, answers((self.path, "done"), (self.detail, "leftover"), (self.extra, "leftover")))
        self.assertTrue(report.ok, [e.message for e in report.errors])
        self.assertEqual(set(report.cleaned_answers), {self.path.id})

    def test_end_form_does_nothing_when_the_rule_does_not_hold(self):
        self._route_after_first_page("end_form")
        report = validate_submission(self.form, answers((self.path, "more")))
        self.assertEqual({e.field_id for e in report.errors}, {self.detail.id, self.extra.id})

    def test_skip_to_section_skips_only_the_pages_in_between(self):
        self._route_after_first_page("skip_to_section", self.brk2.id)
        report = validate_submission(self.form, answers((self.path, "done"), (self.detail, "skipped"), (self.extra, "kept")))
        self.assertTrue(report.ok, [e.message for e in report.errors])
        self.assertEqual(report.cleaned_answers[self.extra.id], "kept")
        self.assertNotIn(self.detail.id, report.cleaned_answers)

    def test_editing_a_response_follows_the_same_routes(self):
        self._route_after_first_page("end_form")
        report = validate_submission(
            self.form, answers((self.path, "done")), is_edit=True,
            existing_answers={self.detail.id: "old", self.extra.id: "old"})
        self.assertTrue(report.ok, [e.message for e in report.errors])
        self.assertEqual(set(report.cleaned_answers), {self.path.id})


def _def_field(**kw):
    base = {"id": None, "label": "Q", "type": "TEXT", "options": [], "rows": [], "validation_rules": {},
            "conditional_logic": {}, "order": 0, "is_required": False}
    base.update(kw)
    return base


class PageDefinitionTests(TestCase):
    def _check(self, *fields):
        payload = {"fields": [dict(f, order=i) for i, f in enumerate(fields)]}
        report = validate_form_definition(payload)
        return {e.code for e in report.errors}, {w.code for w in report.warnings}

    @staticmethod
    def _route(action, source, value=None):
        logic = {"logic": "AND", "action": action, "rules": [{"field": source, "operator": "is_not_empty"}]}
        if value is not None:
            logic["action_value"] = value
        return logic

    def test_a_tidy_multi_page_form_is_publishable(self):
        errors, warnings = self._check(
            _def_field(id=1, label="Path"),
            _def_field(id=2, label="Page 2", type="PAGE_BREAK", conditional_logic=self._route("skip_to_section", 1, 4)),
            _def_field(id=3, label="Middle"),
            _def_field(id=4, label="Page 3", type="PAGE_BREAK"),
            _def_field(id=5, label="Last"),
        )
        self.assertEqual(errors, set())
        self.assertNotIn(codes.WARN_EMPTY_PAGE, warnings)

    def test_a_page_break_can_only_route(self):
        errors, _ = self._check(
            _def_field(id=1),
            _def_field(id=2, type="PAGE_BREAK", conditional_logic=self._route("hide", 1)),
            _def_field(id=3, label="After"),
        )
        self.assertIn(codes.DEF_PAGE_BREAK_ACTION, errors)

    def test_routing_belongs_on_page_breaks_only(self):
        errors, _ = self._check(
            _def_field(id=1), _def_field(id=2, conditional_logic=self._route("end_form", 1)),
        )
        self.assertIn(codes.DEF_ROUTING_ON_QUESTION, errors)

    def test_skip_target_must_be_a_later_page_break(self):
        for target in (2, 1, 3, 99, None):
            errors, _ = self._check(
                _def_field(id=1),
                _def_field(id=2, type="PAGE_BREAK", conditional_logic=self._route("skip_to_section", 1, target)),
                _def_field(id=3, label="Plain question"),
                _def_field(id=4, type="PAGE_BREAK"),
                _def_field(id=5),
            )
            self.assertIn(codes.DEF_ROUTE_TARGET_INVALID, errors, target)

    def test_a_route_cannot_watch_a_page_that_has_not_been_answered_yet(self):
        errors, _ = self._check(
            _def_field(id=1),
            _def_field(id=2, type="PAGE_BREAK", conditional_logic=self._route("end_form", 3)),
            _def_field(id=3, label="Later"),
        )
        self.assertIn(codes.DEF_ROUTE_WATCHES_LATER_PAGE, errors)

    def test_empty_pages_are_flagged(self):
        for fields in (
            [_def_field(id=1, type="PAGE_BREAK"), _def_field(id=2)],
            [_def_field(id=1), _def_field(id=2, type="PAGE_BREAK"), _def_field(id=3, type="PAGE_BREAK"), _def_field(id=4)],
            [_def_field(id=1), _def_field(id=2, type="PAGE_BREAK")],
        ):
            _, warnings = self._check(*fields)
            self.assertIn(codes.WARN_EMPTY_PAGE, warnings)

    def test_a_page_break_takes_no_validation_rules_and_repeated_titles_are_fine(self):
        errors, warnings = self._check(
            _def_field(id=1),
            _def_field(id=2, label="Next", type="PAGE_BREAK", validation_rules={"minLength": 3}),
            _def_field(id=3, label="A"),
            _def_field(id=4, label="Next", type="PAGE_BREAK"),
            _def_field(id=5, label="B"),
        )
        self.assertIn(codes.DEF_RULE_INCOMPATIBLE, errors)
        self.assertNotIn(codes.WARN_DUPLICATE_LABEL, warnings)


class PageBreakApiTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="pager", email="pager@srkr.ac.in", password="x", role="ADMIN", is_staff=True)
        self.client.force_authenticate(self.admin)

    def test_a_form_with_page_breaks_saves_and_publishes(self):
        resp = self.client.post("/api/forms/", {
            "title": "Paged", "slug": "paged", "status": "PUBLISHED", "fields": [
                {"label": "Name", "type": "TEXT", "order": 1, "is_required": True},
                {"label": "More about you", "type": "PAGE_BREAK", "description": "Two quick questions", "order": 2},
                {"label": "Age", "type": "NUMBER", "order": 3},
            ],
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual([f["type"] for f in resp.data["fields"]], ["TEXT", "PAGE_BREAK", "NUMBER"])
        self.assertEqual(resp.data["fields"][1]["description"], "Two quick questions")
        self.assertEqual(FormField.objects.filter(form__slug="paged", type="PAGE_BREAK").count(), 1)

    def test_publishing_is_blocked_by_a_bad_route(self):
        resp = self.client.post("/api/forms/", {
            "title": "Bad route", "slug": "bad-route", "status": "PUBLISHED", "fields": [
                {"label": "Name", "type": "TEXT", "order": 1},
                {"label": "Page 2", "type": "PAGE_BREAK", "order": 2,
                 "conditional_logic": {"logic": "AND", "action": "skip_to_section", "action_value": 12345,
                                       "rules": [{"field": 1, "operator": "is_not_empty"}]}},
                {"label": "Age", "type": "NUMBER", "order": 3},
            ],
        }, format="json")
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(resp.data["code"], "FORM_DEFINITION_INVALID")
