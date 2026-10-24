"""Per-question controls: custom error messages, unique answers, option limits, defaults, hidden and calculated
questions, {{answer}} piping, and the publish-time checks on all of them."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APITestCase

from apps.forms.models import Answer, FieldType, FormStatus, Response
from apps.forms.validation import codes, normalize_validation_rules, validate_form_definition, validate_submission
from .factories import add_field, answers, make_form

User = get_user_model()


def _codes(report):
    return {e.code for e in report.errors}


def _messages(report):
    return {e.code: e.message for e in report.errors}


class CustomMessageTests(TestCase):
    def setUp(self):
        self.form = make_form()

    def _field(self, ftype, rules, **kw):
        return add_field(self.form, ftype, label="Q", order=self.form.fields.count() + 1, validation_rules=rules, **kw)

    def test_a_message_replaces_the_built_in_one_for_that_rule_only(self):
        f = self._field(FieldType.TEXT, {"minLength": 5, "maxLength": 8, "messages": {"minLength": "Give us a bit more!"}})
        short = _messages(validate_submission(self.form, answers((f, "ab"))))
        self.assertEqual(short[codes.MIN_LENGTH], "Give us a bit more!")
        long = _messages(validate_submission(self.form, answers((f, "abcdefghij"))))
        self.assertEqual(long[codes.MAX_LENGTH], "Must be at most 8 character(s).")

    def test_required_and_type_messages(self):
        name = self._field(FieldType.TEXT, {"messages": {"required": "We do need your name."}}, required=True)
        email = self._field(FieldType.EMAIL, {"messages": {"type": "That does not look like an email."}})
        report = validate_submission(self.form, answers((email, "nope")))
        by_field = {e.field_id: e.message for e in report.errors}
        self.assertEqual(by_field[name.id], "We do need your name.")
        self.assertEqual(by_field[email.id], "That does not look like an email.")

    def test_options_message(self):
        f = self._field(FieldType.RADIO, {"messages": {"options": "Pick one of the buttons."}}, options=["a", "b"])
        self.assertEqual(_messages(validate_submission(self.form, answers((f, "z"))))[codes.OPTION_NOT_ALLOWED],
                         "Pick one of the buttons.")

    def test_messages_are_cleaned_when_saved(self):
        rules = normalize_validation_rules("TEXT", {"messages": {"minLength": "  Too short  ", "": "x", "maxLength": "", "pattern": 5},
                                                    "bogus": 1})
        self.assertEqual(rules["messages"], {"minLength": "Too short", "pattern": "5"})
        self.assertNotIn("bogus", rules)
        long = normalize_validation_rules("TEXT", {"messages": {"minLength": "x" * 500}})
        self.assertEqual(len(long["messages"]["minLength"]), 200)
        self.assertNotIn("messages", normalize_validation_rules("TEXT", {"messages": "nope"}))


class DefaultAndHiddenTests(TestCase):
    def test_default_values_are_cleaned_and_kept_per_type(self):
        self.assertEqual(normalize_validation_rules("TEXT", {"defaultValue": " hi "}), {"defaultValue": "hi"})
        self.assertEqual(normalize_validation_rules("CHECKBOX", {"defaultValue": ["a", " ", "b"]}), {"defaultValue": ["a", "b"]})
        self.assertEqual(normalize_validation_rules("NUMBER", {"defaultValue": 18}), {"defaultValue": "18"})
        self.assertEqual(normalize_validation_rules("TEXT", {"defaultValue": ""}), {})
        self.assertEqual(normalize_validation_rules("TEXT", {"defaultValue": {"x": 1}}), {})
        self.assertEqual(normalize_validation_rules("FILE", {"defaultValue": "x"}), {})

    def test_a_hidden_question_is_an_optional_text_answer(self):
        form = make_form()
        ref = add_field(form, FieldType.HIDDEN, label="Referrer", required=True, order=1, validation_rules={"queryParam": "ref"})
        report = validate_submission(form, answers())
        self.assertTrue(report.ok, [e.message for e in report.errors])
        ok = validate_submission(form, answers((ref, "poster-7")))
        self.assertEqual(ok.cleaned_answers[ref.id], "poster-7")

    def test_a_hidden_answer_has_the_usual_safety_limits(self):
        form = make_form()
        ref = add_field(form, FieldType.HIDDEN, label="Referrer", order=1)
        self.assertIn(codes.VALUE_TOO_LONG, _codes(validate_submission(form, answers((ref, "x" * 501)))))
        self.assertIn(codes.INVALID_CHARACTERS, _codes(validate_submission(form, answers((ref, "a\x00b")))))


class CalculatedTests(TestCase):
    def setUp(self):
        self.form = make_form()
        self.price = add_field(self.form, FieldType.NUMBER, label="Price", order=1)
        self.qty = add_field(self.form, FieldType.NUMBER, label="Qty", order=2)
        self.total = add_field(self.form, FieldType.CALCULATED, label="Total", order=3,
                               validation_rules={"formula": "{%d} * {%d}" % (self.price.id, self.qty.id), "decimals": 2})

    def test_the_server_stores_the_computed_value(self):
        report = validate_submission(self.form, answers((self.price, "19.99"), (self.qty, "3")))
        self.assertTrue(report.ok, [e.message for e in report.errors])
        self.assertEqual(report.cleaned_answers[self.total.id], 59.97)

    def test_a_value_sent_by_the_client_is_ignored(self):
        report = validate_submission(self.form, answers((self.price, "10"), (self.qty, "2"), (self.total, "1")))
        self.assertEqual(report.cleaned_answers[self.total.id], 20)

    def test_no_result_means_no_answer_and_never_a_required_error(self):
        self.total.is_required = True
        self.total.validation_rules = {"formula": "{%d} / {%d}" % (self.price.id, self.qty.id), "decimals": 2}
        self.total.save()
        report = validate_submission(self.form, answers((self.price, "10"), (self.qty, "0"), (self.total, "999")))
        self.assertNotIn(self.total.id, report.cleaned_answers)
        self.assertNotIn(codes.REQUIRED, _codes(report))

    def test_calculated_questions_can_build_on_each_other(self):
        grand = add_field(self.form, FieldType.CALCULATED, label="With tax", order=4,
                          validation_rules={"formula": "{%d} * 1.18" % self.total.id, "decimals": 2})
        report = validate_submission(self.form, answers((self.price, "100"), (self.qty, "1")))
        self.assertEqual(report.cleaned_answers[grand.id], 118)

    def test_a_hidden_input_counts_as_blank(self):
        gate = add_field(self.form, FieldType.RADIO, label="Bulk?", options=["yes", "no"], order=0)
        self.qty.conditional_logic = {"logic": "AND", "action": "show", "rules": [{"field": gate.id, "operator": "equals", "value": "yes"}]}
        self.qty.save()
        report = validate_submission(self.form, answers((gate, "no"), (self.price, "5"), (self.qty, "4")))
        self.assertEqual(report.cleaned_answers[self.total.id], 0)

    def test_editing_recomputes_from_the_stored_answers(self):
        report = validate_submission(self.form, answers((self.qty, "5")), is_edit=True,
                                     existing_answers={self.price.id: 10, self.qty.id: 2, self.total.id: 20})
        self.assertEqual(report.cleaned_answers[self.total.id], 50)


class UniqueAnswerTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.form = make_form(status=FormStatus.PUBLISHED, allow_multiple_responses=True, max_responses_per_user=5)
        self.roll = add_field(self.form, FieldType.TEXT, label="Roll no", required=True, order=1,
                              validation_rules={"unique": True})
        self.note = add_field(self.form, FieldType.TEXT, label="Note", order=2)

    def _post(self, value, key, extra=None):
        body = [{"field": self.roll.id, "value": value}] + (extra or [])
        return self.client.post("/api/forms/submissions/", {"form": self.form.id, "answers": body, "idempotency_key": key}, format="json")

    def test_the_same_answer_cannot_be_given_twice_ignoring_case(self):
        self.assertEqual(self._post("22B91A0501", "u1").status_code, 201)
        again = self._post("22b91a0501", "u2")
        self.assertEqual(again.status_code, 400)
        self.assertEqual(again.data["errors"][0]["code"], codes.DUPLICATE_VALUE)
        self.assertEqual(self._post("22B91A0502", "u3").status_code, 201)

    def test_other_questions_may_repeat(self):
        self._post("A1", "n1", [{"field": self.note.id, "value": "same"}])
        self.assertEqual(self._post("A2", "n2", [{"field": self.note.id, "value": "same"}]).status_code, 201)

    def test_test_submissions_do_not_count(self):
        response = Response.objects.create(form=self.form, is_test_submission=True)
        Answer.objects.create(response=response, field=self.roll, value="T1")
        self.assertEqual(self._post("T1", "t1").status_code, 201)

    def test_editing_your_own_response_is_not_a_duplicate(self):
        user = User.objects.create_user(username="uq", email="uq@srkr.ac.in", password="x", role="NON_AFFILIATE")
        self.client.force_authenticate(user)
        first = self._post("KEEP", "e1")
        self.assertEqual(first.status_code, 201)
        edit = self.client.patch(f"/api/forms/submissions/{first.data['id']}/", {"answers": [{"field": self.roll.id, "value": "KEEP"}]}, format="json")
        self.assertEqual(edit.status_code, 200, edit.data)

    def test_the_admins_own_message_is_used(self):
        self.roll.validation_rules = {"unique": True, "messages": {"unique": "That roll number is already registered."}}
        self.roll.save()
        self._post("DUP", "m1")
        self.assertEqual(self._post("dup", "m2").data["errors"][0]["message"], "That roll number is already registered.")


class OptionLimitTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.form = make_form(status=FormStatus.PUBLISHED, allow_multiple_responses=True, max_responses_per_user=5)
        self.slot = add_field(self.form, FieldType.RADIO, label="Slot", required=True, order=1, options=["Morning", "Evening"],
                              validation_rules={"optionLimits": {"Morning": 2}})
        self.tags = add_field(self.form, FieldType.CHECKBOX, label="Tracks", order=2, options=["AI", "Web", "IoT"],
                              validation_rules={"optionLimits": {"AI": 1}})

    def _post(self, slot, key, tags=None):
        body = [{"field": self.slot.id, "value": slot}] + ([{"field": self.tags.id, "value": tags}] if tags else [])
        return self.client.post("/api/forms/submissions/", {"form": self.form.id, "answers": body, "idempotency_key": key}, format="json")

    def test_an_option_closes_once_its_limit_is_reached(self):
        self.assertEqual(self._post("Morning", "c1").status_code, 201)
        self.assertEqual(self._post("morning", "c2").status_code, 201)
        full = self._post("Morning", "c3")
        self.assertEqual(full.status_code, 400)
        self.assertEqual(full.data["errors"][0]["code"], codes.OPTION_FULL)
        self.assertEqual(self._post("Evening", "c4").status_code, 201)

    def test_limits_apply_to_every_checked_box(self):
        self.assertEqual(self._post("Evening", "k1", ["AI", "Web"]).status_code, 201)
        resp = self._post("Evening", "k2", ["Web", "AI"])
        self.assertEqual(resp.data["errors"][0]["code"], codes.OPTION_FULL)
        self.assertEqual(self._post("Evening", "k3", ["Web"]).status_code, 201)

    def test_keeping_your_own_seat_while_editing_is_fine(self):
        user = User.objects.create_user(username="ol", email="ol@srkr.ac.in", password="x", role="NON_AFFILIATE")
        self.client.force_authenticate(user)
        mine = self._post("Morning", "o1")
        self.client.force_authenticate(None)
        self._post("Morning", "o2")
        self.client.force_authenticate(user)
        edit = self.client.patch(f"/api/forms/submissions/{mine.data['id']}/", {"answers": [{"field": self.slot.id, "value": "Morning"}]}, format="json")
        self.assertEqual(edit.status_code, 200, edit.data)

    def test_the_public_availability_lists_places_left(self):
        self._post("Morning", "a1")
        self.client.force_authenticate(None)
        resp = self.client.get(f"/api/forms/{self.form.slug}/availability/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data[str(self.slot.id)], {"Morning": 1})
        self.assertEqual(resp.data[str(self.tags.id)], {"AI": 1})

    def test_availability_never_goes_below_zero_and_skips_unlimited_forms(self):
        for key in ("z1", "z2"):
            self._post("Morning", key)
        Answer.objects.create(response=Response.objects.create(form=self.form), field=self.slot, value="Morning")
        self.assertEqual(self.client.get(f"/api/forms/{self.form.slug}/availability/").data[str(self.slot.id)], {"Morning": 0})
        plain = make_form(status=FormStatus.PUBLISHED)
        add_field(plain, FieldType.RADIO, label="Q", options=["a", "b"], order=1)
        self.assertEqual(self.client.get(f"/api/forms/{plain.slug}/availability/").data, {})


def _def_field(**kw):
    base = {"id": None, "label": "Q", "type": "TEXT", "options": [], "rows": [], "validation_rules": {},
            "conditional_logic": {}, "order": 0, "is_required": False}
    base.update(kw)
    return base


class ControlDefinitionChecks(TestCase):
    def _check(self, *fields):
        payload = {"fields": [dict(f, order=i) for i, f in enumerate(fields)]}
        report = validate_form_definition(payload)
        return {e.code for e in report.errors}, {w.code for w in report.warnings}

    def test_option_limits_must_name_real_options_with_sane_numbers(self):
        errors, _ = self._check(_def_field(id=1, type="RADIO", options=["a", "b"], validation_rules={"optionLimits": {"zzz": 5}}))
        self.assertIn(codes.DEF_OPTION_LIMIT_INVALID, errors)
        errors, _ = self._check(_def_field(id=1, type="RADIO", options=["a", "b"], validation_rules={"optionLimits": {"a": 5, "B": 10}}))
        self.assertEqual(errors, set())

    def test_defaults_must_fit_the_question(self):
        for field in (
            _def_field(id=1, type="NUMBER", validation_rules={"defaultValue": "lots"}),
            _def_field(id=1, type="RADIO", options=["a", "b"], validation_rules={"defaultValue": "c"}),
            _def_field(id=1, type="CHECKBOX", options=["a", "b"], validation_rules={"defaultValue": ["a", "z"]}),
            _def_field(id=1, type="DATE", validation_rules={"defaultValue": "tomorrow"}),
        ):
            errors, _ = self._check(field)
            self.assertIn(codes.DEF_DEFAULT_INVALID, errors, field)
        errors, _ = self._check(_def_field(id=1, type="RADIO", options=["a", "b"], validation_rules={"defaultValue": "A"}))
        self.assertEqual(errors, set())

    def test_hidden_questions_need_a_sensible_link_parameter(self):
        errors, _ = self._check(_def_field(id=1, type="HIDDEN", validation_rules={"queryParam": "1 bad param"}))
        self.assertIn(codes.DEF_QUERY_PARAM_INVALID, errors)
        errors, warnings = self._check(_def_field(id=1, type="HIDDEN", validation_rules={"queryParam": "utm_source"}))
        self.assertEqual(errors, set())
        self.assertNotIn(codes.WARN_HIDDEN_FIELD_EMPTY, warnings)
        _, warnings = self._check(_def_field(id=1, type="HIDDEN"))
        self.assertIn(codes.WARN_HIDDEN_FIELD_EMPTY, warnings)

    def test_formulas_are_checked(self):
        num = _def_field(id=1, label="Price", type="NUMBER")
        text = _def_field(id=2, label="Name")
        cases = {
            "missing": _def_field(id=3, type="CALCULATED"),
            "syntax": _def_field(id=3, type="CALCULATED", validation_rules={"formula": "{1} +"}),
            "unknown question": _def_field(id=3, type="CALCULATED", validation_rules={"formula": "{99} + 1"}),
            "itself": _def_field(id=3, type="CALCULATED", validation_rules={"formula": "{3} + 1"}),
            "text question": _def_field(id=3, type="CALCULATED", validation_rules={"formula": "{2} + 1"}),
            "decimals": _def_field(id=3, type="CALCULATED", validation_rules={"formula": "{1}", "decimals": 9}),
        }
        for name, calc in cases.items():
            errors, _ = self._check(num, text, calc)
            self.assertTrue({codes.DEF_FORMULA_INVALID, codes.DEF_FORMULA_BAD_REFERENCE} & errors, name)
        errors, _ = self._check(num, text, _def_field(id=3, type="CALCULATED", validation_rules={"formula": "{1} * 2 + 1", "decimals": 0}))
        self.assertEqual(errors, set())

    def test_calculated_questions_that_depend_on_each_other_are_refused(self):
        errors, _ = self._check(
            _def_field(id=1, type="NUMBER"),
            _def_field(id=2, type="CALCULATED", validation_rules={"formula": "{3} + {1}"}),
            _def_field(id=3, type="CALCULATED", validation_rules={"formula": "{2} + 1"}),
        )
        self.assertIn(codes.DEF_FORMULA_CYCLE, errors)

    def test_piping_into_labels_warns_about_answers_that_cannot_be_known_yet(self):
        _, warnings = self._check(
            _def_field(id=1, label="Your name"),
            _def_field(id=2, label="Hello {{1}}, tell us more"),
        )
        self.assertNotIn(codes.WARN_PIPE_UNKNOWN_FIELD, warnings)
        for fields in (
            [_def_field(id=1, label="Hi {{99}}")],
            [_def_field(id=1, label="Hi {{1}}")],
            [_def_field(id=1, label="Hi {{2}}"), _def_field(id=2, label="Later")],
            [_def_field(id=1, label="Q", description="About {{7}}")],
        ):
            _, warnings = self._check(*fields)
            self.assertIn(codes.WARN_PIPE_UNKNOWN_FIELD, warnings, fields)

    def test_conditions_can_watch_hidden_and_calculated_questions(self):
        errors, _ = self._check(
            _def_field(id=1, type="NUMBER"),
            _def_field(id=2, type="CALCULATED", validation_rules={"formula": "{1} * 2"}),
            _def_field(id=3, label="Big spender", conditional_logic={"logic": "AND", "action": "show",
                                                                     "rules": [{"field": 2, "operator": "gt", "value": 100}]}),
            _def_field(id=4, type="HIDDEN", validation_rules={"queryParam": "ref"}),
            _def_field(id=5, label="Referred", conditional_logic={"logic": "AND", "action": "show",
                                                                  "rules": [{"field": 4, "operator": "contains", "value": "poster"}]}),
        )
        self.assertEqual(errors, set())
