"""Condition actions that now take effect (set_value, restrict_options, value_field comparisons)
and the publish-time checks on conditions."""

from django.test import TestCase

from apps.forms.models import FieldType
from apps.forms.validation import codes, validate_form_definition, validate_submission
from .factories import add_field, answers, make_form


def _codes(report):
    return {e.code for e in report.errors}


def _rule(field, op="equals", value="x", **extra):
    return {"field": field.id, "operator": op, "value": value, **extra}


class SetValueTests(TestCase):
    def setUp(self):
        self.form = make_form()
        self.role = add_field(self.form, FieldType.RADIO, label="Role", options=["student", "staff"], order=1)
        self.dept = add_field(self.form, FieldType.TEXT, label="Dept", required=True, order=2, conditional_logic={
            "logic": "AND", "action": "set_value", "action_value": "N/A", "rules": [_rule(self.role, value="staff")],
        })

    def test_the_server_assigns_the_value_even_if_the_client_sent_another(self):
        report = validate_submission(self.form, answers((self.role, "staff"), (self.dept, "hacked")))
        self.assertTrue(report.ok, [e.message for e in report.errors])
        self.assertEqual(report.cleaned_answers[self.dept.id], "N/A")

    def test_an_assigned_value_satisfies_a_required_question(self):
        report = validate_submission(self.form, answers((self.role, "staff")))
        self.assertTrue(report.ok, [e.message for e in report.errors])
        self.assertEqual(report.cleaned_answers[self.dept.id], "N/A")

    def test_the_clients_own_answer_is_kept_when_the_rule_does_not_hold(self):
        report = validate_submission(self.form, answers((self.role, "student"), (self.dept, "CSE")))
        self.assertEqual(report.cleaned_answers[self.dept.id], "CSE")
        missing = validate_submission(self.form, answers((self.role, "student")))
        self.assertIn(codes.REQUIRED, _codes(missing))


class RestrictOptionsTests(TestCase):
    def setUp(self):
        self.form = make_form()
        self.level = add_field(self.form, FieldType.RADIO, label="Level", options=["btech", "mtech"], order=1)
        self.branch = add_field(self.form, FieldType.DROPDOWN, label="Branch", options=["CSE", "ECE", "VLSI"], order=2,
                                conditional_logic={"logic": "AND", "action": "restrict_options",
                                                   "action_value": ["CSE", "ECE"], "rules": [_rule(self.level, value="btech")]})
        self.tags = add_field(self.form, FieldType.CHECKBOX, label="Tags", options=["a", "b", "c"], order=3,
                              conditional_logic={"logic": "AND", "action": "restrict_options",
                                                 "action_value": ["a"], "rules": [_rule(self.level, value="btech")]})

    def test_an_option_outside_the_allowed_list_is_refused(self):
        report = validate_submission(self.form, answers((self.level, "btech"), (self.branch, "VLSI")))
        self.assertIn(codes.OPTION_NOT_ALLOWED, _codes(report))

    def test_allowed_options_pass_and_unrestricted_cases_allow_everything(self):
        self.assertTrue(validate_submission(self.form, answers((self.level, "btech"), (self.branch, "ece"))).ok)
        self.assertTrue(validate_submission(self.form, answers((self.level, "mtech"), (self.branch, "VLSI"))).ok)

    def test_every_selected_checkbox_option_must_be_allowed(self):
        bad = validate_submission(self.form, answers((self.level, "btech"), (self.tags, ["a", "c"])))
        self.assertIn(codes.OPTION_NOT_ALLOWED, _codes(bad))
        self.assertTrue(validate_submission(self.form, answers((self.level, "btech"), (self.tags, ["a"]))).ok)


class CompareWithAnotherQuestionTests(TestCase):
    def test_a_question_can_depend_on_how_two_answers_compare(self):
        form = make_form()
        start = add_field(form, FieldType.DATE, label="Start", order=1)
        end = add_field(form, FieldType.DATE, label="End", order=2)
        why = add_field(form, FieldType.TEXT, label="Why is it backwards", order=3, conditional_logic={
            "logic": "AND", "action": "require", "rules": [_rule(end, "before", None, value_field=start.id)],
        })
        backwards = validate_submission(form, answers((start, "2024-02-01"), (end, "2024-01-01")))
        self.assertIn(codes.REQUIRED, _codes(backwards))
        fine = validate_submission(form, answers((start, "2024-01-01"), (end, "2024-02-01")))
        self.assertTrue(fine.ok, [e.message for e in fine.errors])
        self.assertIsNotNone(why)


def _def_field(**kw):
    base = {"id": None, "label": "Q", "type": "TEXT", "options": [], "rows": [], "validation_rules": {},
            "conditional_logic": {}, "order": 0, "is_required": False}
    base.update(kw)
    return base


class ConditionDefinitionChecks(TestCase):
    """Field 1 is the one watched; field 2 carries the condition under test."""

    def _check(self, rule_or_logic, target_type="TEXT", target_options=None, watcher=None):
        logic = rule_or_logic if "rules" in rule_or_logic else {"logic": "AND", "action": "show", "rules": [rule_or_logic]}
        fields = [
            _def_field(id=1, label="Watched", type=target_type, options=target_options or []),
            _def_field(id=2, label="Dependent", order=1, conditional_logic=logic, **(watcher or {})),
        ]
        report = validate_form_definition({"fields": fields})
        return {e.code for e in report.errors}, {w.code for w in report.warnings}

    def test_operator_must_suit_the_watched_question(self):
        errors, _ = self._check({"field": 1, "operator": "before", "value": "2024-01-01"}, "TEXT")
        self.assertIn(codes.DEF_CONDITION_OPERATOR_MISMATCH, errors)
        errors, _ = self._check({"field": 1, "operator": "includes", "value": "a"}, "NUMBER")
        self.assertIn(codes.DEF_CONDITION_OPERATOR_MISMATCH, errors)
        errors, _ = self._check({"field": 1, "operator": "matches_regex", "value": "a"}, "DATE")
        self.assertIn(codes.DEF_CONDITION_OPERATOR_MISMATCH, errors)

    def test_sensible_operator_and_question_pairs_pass(self):
        for rule, kind, options in [
            ({"field": 1, "operator": "gt", "value": 18}, "NUMBER", None),
            ({"field": 1, "operator": "between", "value": "1,5"}, "RATING", None),
            ({"field": 1, "operator": "after", "value": "2024-01-01"}, "DATE", None),
            ({"field": 1, "operator": "includes_any", "value": ["a", "b"]}, "CHECKBOX", ["a", "b", "c"]),
            ({"field": 1, "operator": "equals", "value": "a"}, "RADIO", ["a", "b"]),
            ({"field": 1, "operator": "contains", "value": "srkr"}, "EMAIL", None),
            ({"field": 1, "operator": "is_empty"}, "FILE", None),
        ]:
            errors, warnings = self._check(rule, kind, options)
            self.assertEqual(errors, set(), (rule, errors))

    def test_a_rule_needs_something_to_compare_with(self):
        errors, _ = self._check({"field": 1, "operator": "equals", "value": ""})
        self.assertIn(codes.DEF_CONDITION_MISSING_VALUE, errors)
        errors, _ = self._check({"field": 1, "operator": "includes_all", "value": []}, "CHECKBOX", ["a"])
        self.assertIn(codes.DEF_CONDITION_MISSING_VALUE, errors)
        errors, _ = self._check({"field": 1, "operator": "is_empty"})
        self.assertEqual(errors, set())

    def test_value_shapes(self):
        bad_values = [
            ({"field": 1, "operator": "between", "value": "5"}, "NUMBER"),
            ({"field": 1, "operator": "between", "value": "a,b"}, "NUMBER"),
            ({"field": 1, "operator": "date_between", "value": "2024-01-01,tomorrow"}, "DATE"),
            ({"field": 1, "operator": "before", "value": "next week"}, "DATE"),
            ({"field": 1, "operator": "gt", "value": "lots"}, "NUMBER"),
            ({"field": 1, "operator": "matches_regex", "value": "(["}, "TEXT"),
            ({"field": 1, "operator": "matches_regex", "value": "(a+)+$"}, "TEXT"),
        ]
        for rule, kind in bad_values:
            errors, _ = self._check(rule, kind)
            self.assertIn(codes.DEF_CONDITION_BAD_VALUE, errors, rule)

    def test_a_value_that_is_not_an_option_warns(self):
        errors, warnings = self._check({"field": 1, "operator": "equals", "value": "Mars"}, "RADIO", ["Earth", "Venus"])
        self.assertEqual(errors, set())
        self.assertIn(codes.WARN_CONDITION_VALUE_NOT_AN_OPTION, warnings)
        _, ok = self._check({"field": 1, "operator": "equals", "value": "earth"}, "RADIO", ["Earth", "Venus"])
        self.assertNotIn(codes.WARN_CONDITION_VALUE_NOT_AN_OPTION, ok)

    def test_value_field_must_exist_and_not_be_the_question_itself(self):
        errors, _ = self._check({"field": 1, "operator": "equals", "value_field": 99})
        self.assertIn(codes.DEF_CONDITION_UNKNOWN_FIELD, errors)
        errors, _ = self._check({"field": 1, "operator": "equals", "value_field": 2})
        self.assertIn(codes.DEF_CONDITION_SELF_REFERENCE, errors)
        errors, _ = self._check({"field": 1, "operator": "equals", "value_field": 1})
        self.assertEqual(errors, set())

    def test_action_values(self):
        def with_action(action, payload, **watcher):
            logic = {"logic": "AND", "action": action, "action_value": payload, "rules": [{"field": 1, "operator": "is_not_empty"}]}
            return self._check(logic, watcher=watcher)

        self.assertIn(codes.DEF_CONDITION_BAD_ACTION_VALUE, with_action("set_value", "", )[0])
        self.assertIn(codes.DEF_CONDITION_BAD_ACTION_VALUE, with_action("set_value", "abc", type="NUMBER")[0])
        self.assertIn(codes.DEF_CONDITION_BAD_ACTION_VALUE, with_action("set_value", "Mars", type="RADIO", options=["A", "B"])[0])
        self.assertEqual(with_action("set_value", "hello")[0], set())
        self.assertIn(codes.DEF_CONDITION_BAD_ACTION_VALUE, with_action("restrict_options", ["A"])[0])  # TEXT
        self.assertIn(codes.DEF_CONDITION_BAD_ACTION_VALUE, with_action("restrict_options", ["Z"], type="RADIO", options=["A", "B"])[0])
        self.assertEqual(with_action("restrict_options", ["A"], type="RADIO", options=["A", "B"])[0], set())
        self.assertIn(codes.DEF_CONDITION_BAD_ACTION_VALUE, with_action("display_message", "x" * 501)[0])
        self.assertEqual(with_action("display_message", "Heads up")[0], set())

    def test_the_legacy_skip_action_warns_instead_of_silently_doing_nothing(self):
        errors, warnings = self._check(
            {"logic": "AND", "action": "skip", "rules": [{"field": 1, "operator": "is_not_empty"}]})
        self.assertEqual(errors, set())
        self.assertIn(codes.WARN_ACTION_NOT_ACTIVE, warnings)

    def test_page_routing_actions_are_refused_on_ordinary_questions(self):
        for action in ("skip_to_section", "end_form"):
            errors, _ = self._check({"logic": "AND", "action": action, "action_value": 5,
                                     "rules": [{"field": 1, "operator": "is_not_empty"}]})
            self.assertIn(codes.DEF_ROUTING_ON_QUESTION, errors, action)

    def test_comparing_two_questions_counts_for_cycle_detection(self):
        fields = [
            _def_field(id=1, label="A", conditional_logic={"logic": "AND", "action": "show",
                                                          "rules": [{"field": 3, "operator": "equals", "value": "x"}]}),
            _def_field(id=2, label="B"),
            _def_field(id=3, label="C", conditional_logic={"logic": "AND", "action": "show",
                                                          "rules": [{"field": 2, "operator": "equals", "value_field": 1}]}),
        ]
        errors = {e.code for e in validate_form_definition({"fields": fields}).errors}
        self.assertIn(codes.DEF_CONDITION_CYCLE, errors)
