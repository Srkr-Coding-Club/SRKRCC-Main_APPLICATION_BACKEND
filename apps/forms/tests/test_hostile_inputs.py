"""A sweep with nasty input over every question type and over malformed definitions (Phases 0-3 of the form builder).

Nothing here may crash, hang or let junk through: a bad answer is a tidy validation error, never a 500.
"""

import itertools
import json
import time

from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework.test import APITestCase

from apps.forms.models import FieldType, Form, FormField, FormStatus, Response
from apps.forms.validation import PARTIAL, STRICT, validate_form_definition, validate_submission
from .factories import add_field, make_form

User = get_user_model()

NASTY = [
    None, "", " ", "   \t\n", 0, -1, 1, 2 ** 70, -2 ** 70, 1.5, float("inf"), float("-inf"), float("nan"), True, False,
    "0", "-0", "1e999", "NaN", "Infinity", "٣", "１２３", "12abc", "0x10", "1_000", " 12 ", "12,5", "١٢٣",
    "a" * 100_000, "\u0000", "a\u0000b", "‮evil", "😀" * 2000, "<script>alert(1)</script>", "'; DROP TABLE forms_form; --",
    "{{1}}", "{1}", "${7*7}", "../../etc/passwd", "javascript:alert(1)", "data:text/html;base64,PHNjcmlwdD4=",
    [], [None], ["a", "a"], ["a"] * 5000, [[]], [{}], {}, {"a": 1}, {"": ""}, {"a": {"b": {"c": []}}}, {"__proto__": 1},
    "2026-02-30", "2026-13-01", "0000-00-00", "9999-12-31", "24:00", "23:60", "99:99", "12:30:45.123456", "2026-10-22T10:00:00Z",
]

# Every question type with a plausible configuration.
CASES = [
    (FieldType.TEXT, {}), (FieldType.TEXT, {"validation_rules": {"minLength": 2, "maxLength": 5, "pattern": "[a-z]+"}}),
    (FieldType.PARAGRAPH, {"validation_rules": {"maxLength": 50}}), (FieldType.EMAIL, {}), (FieldType.PHONE, {}),
    (FieldType.URL, {"validation_rules": {"allowedDomains": ["example.org"]}}),
    (FieldType.NUMBER, {"min_value": 1, "max_value": 10}), (FieldType.NUMBER, {"validation_rules": {"integerOnly": True, "decimals": 2}}),
    (FieldType.DROPDOWN, {"options": ["A", "B"]}), (FieldType.RADIO, {"options": ["A", "B"]}), (FieldType.CHECKBOX, {"options": ["A", "B"]}),
    (FieldType.DATE, {}), (FieldType.TIME, {}), (FieldType.RATING, {"max_value": 5}), (FieldType.LINEAR_SCALE, {"min_value": 1, "max_value": 7}),
    (FieldType.MATRIX_RADIO, {"options": ["Lo", "Hi"], "rows": ["r1", "r2"]}), (FieldType.MATRIX_CHECKBOX, {"options": ["Lo", "Hi"], "rows": ["r1", "r2"]}),
    (FieldType.FILE, {}), (FieldType.MULTI_FILE, {}), (FieldType.SIGNATURE, {}), (FieldType.CLUB_ID, {}),
    (FieldType.HIDDEN, {}), (FieldType.CALCULATED, {"validation_rules": {"formula": "1 + 1"}}),
]


class EveryQuestionTypeAgainstNastyAnswers(APITestCase):
    def test_no_answer_to_any_question_type_crashes_the_engine(self):
        failures = []
        for (ftype, extra), value in itertools.product(CASES, NASTY):
            form = make_form(status=FormStatus.PUBLISHED)
            field = add_field(form, ftype, required=True, **extra)
            for mode in (STRICT, PARTIAL):
                started = time.perf_counter()
                try:
                    report = validate_submission(form, [{"field": field.id, "value": value}], mode=mode)
                    json.dumps([e.as_dict() for e in report.errors] + [w.as_dict() for w in report.warnings], default=str)
                    json.dumps(report.cleaned_answers, default=str)
                except Exception as ex:  # noqa: BLE001 - the point is that nothing may escape
                    failures.append(f"{ftype} {mode} {str(value)[:30]!r}: {type(ex).__name__}: {ex}")
                else:
                    if time.perf_counter() - started > 2:
                        failures.append(f"{ftype} {mode} {str(value)[:30]!r}: took {time.perf_counter() - started:.1f}s")
        self.assertEqual(failures[:15], [])

    def test_what_gets_through_is_clean(self):
        """If an answer is accepted, what is stored is of the right shape for the question."""
        problems = []
        for (ftype, extra), value in itertools.product(CASES, NASTY):
            if ftype in (FieldType.CALCULATED, FieldType.HIDDEN, FieldType.FILE, FieldType.MULTI_FILE, FieldType.SIGNATURE, FieldType.CLUB_ID):
                continue
            form = make_form(status=FormStatus.PUBLISHED)
            field = add_field(form, ftype, required=False, **extra)
            report = validate_submission(form, [{"field": field.id, "value": value}], mode=STRICT)
            if report.errors or field.id not in report.cleaned_answers:
                continue
            stored = report.cleaned_answers[field.id]
            if ftype in (FieldType.NUMBER, FieldType.RATING, FieldType.LINEAR_SCALE):
                if not isinstance(stored, (int, float)) or isinstance(stored, bool) or stored != stored or stored in (float("inf"), float("-inf")):
                    problems.append(f"{ftype} stored {stored!r} for {str(value)[:30]!r}")
            elif ftype in (FieldType.DROPDOWN, FieldType.RADIO):
                if stored not in field.options:
                    problems.append(f"{ftype} stored {stored!r} which is not an option")
            elif ftype == FieldType.CHECKBOX:
                if not isinstance(stored, list) or any(s not in field.options for s in stored) or len(set(stored)) != len(stored):
                    problems.append(f"{ftype} stored {stored!r}")
            elif ftype in (FieldType.TEXT, FieldType.PARAGRAPH, FieldType.EMAIL, FieldType.PHONE, FieldType.URL):
                if isinstance(stored, str) and "\u0000" in stored:
                    problems.append(f"{ftype} kept a NUL byte")
                if isinstance(stored, str) and len(stored) > 10_000:
                    problems.append(f"{ftype} kept {len(stored)} characters")
        self.assertEqual(problems[:15], [])

    def test_a_required_question_is_never_satisfied_by_emptiness(self):
        empties = [None, "", "   ", [], {}, "\n\t"]
        for (ftype, extra), value in itertools.product(CASES, empties):
            if ftype in (FieldType.HIDDEN, FieldType.CALCULATED):
                continue
            form = make_form(status=FormStatus.PUBLISHED)
            field = add_field(form, ftype, required=True, **extra)
            report = validate_submission(form, [{"field": field.id, "value": value}], mode=STRICT)
            self.assertTrue(report.errors, f"{ftype} accepted {value!r} for a required question")


class SubmissionEnvelopeIsHostileToo(APITestCase):
    """The shape around the answers: missing keys, wrong types, huge lists, ids that are not the form's."""

    def setUp(self):
        cache.clear()  # these tests post a lot; the per-minute submit limit must start empty ...
        self.addCleanup(cache.clear)  # ... and must not be left full for whichever test runs next
        self.form = make_form(status=FormStatus.PUBLISHED, allow_multiple_responses=True, max_responses_per_user=50)
        self.name = add_field(self.form, FieldType.TEXT, label="Name", required=True)
        self.other = make_form(status=FormStatus.PUBLISHED)
        self.foreign = add_field(self.other, FieldType.TEXT, label="Foreign")

    def post(self, body):
        return self.client.post("/api/forms/submissions/", body, format="json")

    def test_malformed_bodies_get_a_400_never_a_500(self):
        bodies = [
            {}, {"form": self.form.id}, {"form": self.form.id, "answers": None}, {"form": self.form.id, "answers": "text"},
            {"form": self.form.id, "answers": {"a": 1}}, {"form": self.form.id, "answers": [None]}, {"form": self.form.id, "answers": ["x"]},
            {"form": self.form.id, "answers": [{}]}, {"form": self.form.id, "answers": [{"value": "x"}]},
            {"form": self.form.id, "answers": [{"field": "abc", "value": "x"}]}, {"form": self.form.id, "answers": [{"field": None, "value": "x"}]},
            {"form": self.form.id, "answers": [{"field": -1, "value": "x"}]}, {"form": self.form.id, "answers": [{"field": 2 ** 70, "value": "x"}]},
            {"form": self.form.id, "answers": [{"field": self.foreign.id, "value": "x"}]},
            {"form": "abc", "answers": []}, {"form": -5, "answers": []}, {"form": 2 ** 70, "answers": []}, {"form": [1], "answers": []},
            {"form": self.form.id, "answers": [{"field": self.name.id, "value": "x"}] * 3},
            {"form": self.form.id, "answers": [{"field": self.name.id, "value": "x"}] * 5000},
            {"form": self.form.id, "answers": [{"field": self.name.id, "value": "x", "extra": {"deep": [[[[]]]]}}]},
        ]
        for body in bodies:
            resp = self.post(body)
            self.assertLess(resp.status_code, 500, f"{str(body)[:80]} -> {resp.status_code} {getattr(resp, 'data', '')}")

    def test_a_list_instead_of_an_object_is_rejected_cleanly(self):
        self.assertLess(self.client.post("/api/forms/submissions/", [1, 2, 3], format="json").status_code, 500)
        self.assertLess(self.client.post("/api/forms/submissions/", "plain", content_type="text/plain").status_code, 500)
        self.assertLess(self.client.post("/api/forms/submissions/", "{not json", content_type="application/json").status_code, 500)

    def test_an_answer_to_another_forms_question_is_not_stored(self):
        self.post({"form": self.form.id, "answers": [{"field": self.name.id, "value": "x"}, {"field": self.foreign.id, "value": "leak"}]})
        self.assertEqual(self.foreign.answer_set.count(), 0)

    def test_the_same_question_answered_twice_stores_one_answer(self):
        resp = self.post({"form": self.form.id, "answers": [{"field": self.name.id, "value": "first"}, {"field": self.name.id, "value": "second"}]})
        if resp.status_code == 201:
            self.assertEqual(Response.objects.get().answers.filter(field=self.name).count(), 1)


class MalformedDefinitions(APITestCase):
    """A form definition written by hand (import, API) with every field type and nonsense settings must not crash the publish gate."""

    def setUp(self):
        self.admin = User.objects.create_user(username="hostile", email="hostile@srkr.ac.in", password="x", role="ADMIN", is_staff=True)
        self.client.force_authenticate(self.admin)

    def test_the_definition_check_survives_nonsense_rules(self):
        junk = [None, "", "x", 5, -1, 2 ** 70, float("nan"), [], [None], {}, {"a": {"b": []}}, True, "a" * 10_000]
        keys = ["minLength", "maxLength", "pattern", "decimals", "formula", "optionLimits", "messages", "defaultValue", "queryParam",
                "allowedDomains", "minDate", "maxDate", "step", "unique", "crossFieldRules", "requiredRows", "maxSelections"]
        for ftype in [t for t, _ in CASES]:
            for key in keys:
                for value in junk:
                    payload = {"fields": [{"id": 1, "label": "Q", "type": ftype, "order": 1, "options": ["A"], "rows": ["r"],
                                           "validation_rules": {key: value}, "conditional_logic": {}}]}
                    try:
                        validate_form_definition(payload)
                    except Exception as ex:  # noqa: BLE001
                        self.fail(f"{ftype}.{key}={str(value)[:30]!r}: {type(ex).__name__}: {ex}")

    def test_the_definition_check_survives_nonsense_conditions(self):
        junk = [None, "", "x", 5, [], [None], {}, {"rules": "x"}, {"rules": [None]}, {"rules": [{}]}, {"rules": [{"field": "a", "operator": 5}]},
                {"logic": 7, "rules": []}, {"action": [], "rules": [{"field": 2}]}, {"rules": [{"field": 1, "operator": "equals", "value": {"a": []}}]},
                {"rules": [{"rules": [{"rules": [{"rules": []}]}]}]}]
        for value in junk:
            payload = {"fields": [
                {"id": 1, "label": "A", "type": "TEXT", "order": 1, "validation_rules": {}, "conditional_logic": value},
                {"id": 2, "label": "B", "type": "TEXT", "order": 2, "validation_rules": {}, "conditional_logic": {}},
            ]}
            try:
                validate_form_definition(payload)
            except Exception as ex:  # noqa: BLE001
                self.fail(f"conditional_logic={str(value)[:60]!r}: {type(ex).__name__}: {ex}")

    def test_deeply_nested_conditions_and_huge_forms_do_not_hang(self):
        node = {"field": 1, "operator": "equals", "value": "x"}
        for _ in range(500):
            node = {"logic": "AND", "rules": [node]}
        payload = {"fields": [
            {"id": 1, "label": "A", "type": "TEXT", "order": 1, "validation_rules": {}, "conditional_logic": {}},
            {"id": 2, "label": "B", "type": "TEXT", "order": 2, "validation_rules": {}, "conditional_logic": node},
        ]}
        started = time.perf_counter()
        try:
            validate_form_definition(payload)
        except RecursionError:
            self.fail("a deeply nested condition blew the stack")
        self.assertLess(time.perf_counter() - started, 3)

    def test_saving_a_form_with_nonsense_fields_is_a_400_not_a_500(self):
        bodies = [
            {"title": "T", "slug": "t-1", "fields": "oops"}, {"title": "T", "slug": "t-2", "fields": [None]},
            {"title": "T", "slug": "t-3", "fields": [{"label": "x", "type": "NOPE"}]},
            {"title": "T", "slug": "t-4", "fields": [{"label": "x", "type": "TEXT", "validation_rules": "bad"}]},
            {"title": "T", "slug": "t-5", "fields": [{"label": "x", "type": "TEXT", "conditional_logic": [1, 2]}]},
            {"title": "T", "slug": "t-6", "fields": [{"label": "x" * 5000, "type": "TEXT"}]},
            {"title": "T", "slug": "t-7", "fields": [{"label": "x", "type": "RADIO", "options": "A,B"}]},
            {"title": "T", "slug": "t-8", "fields": [{"label": "x", "type": "RADIO", "options": [None, 5, {}, ["a"]]}]},
            {"title": "T", "slug": "t-9", "fields": [{"label": "x", "type": "TEXT", "id": "abc"}]},
            {"title": "T", "slug": "t-10", "fields": [{"label": "x", "type": "TEXT", "order": "first"}]},
            {"title": "T", "slug": "t-11", "fields": [{"label": "x", "type": "NUMBER", "min_value": "abc"}]},
            {"title": "T", "slug": "t-12", "max_total_responses": -3, "fields": []},
            {"title": "T", "slug": "t-13", "open_at": "not a date", "fields": []},
            {"title": "T", "slug": "t-14", "club_id_field_mapping": "x", "fields": []},
            {"title": "T", "slug": "t-15", "club_id_field_mapping": {"email": {"a": 1}}, "fields": []},
            {"title": "T" * 500, "slug": "t-16", "fields": []},
        ]
        for body in bodies:
            resp = self.client.post("/api/forms/", body, format="json")
            self.assertLess(resp.status_code, 500, f"{json.dumps(body)[:100]} -> {resp.status_code}")
