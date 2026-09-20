from django.test import SimpleTestCase, TestCase

from apps.forms.models import FieldType
from apps.forms.validation.conditional import evaluate_operator, compute_layout
from apps.forms.validation.schema import normalize_conditional_logic
from apps.forms.validation import validate_submission
from .factories import make_form, add_field, answers


class OperatorTests(SimpleTestCase):
    def test_equality_numeric_and_string(self):
        self.assertTrue(evaluate_operator("equals", "3", 3))
        self.assertTrue(evaluate_operator("equals", "Yes", "yes"))
        self.assertTrue(evaluate_operator("not_equals", "a", "b"))

    def test_numeric_operators(self):
        self.assertTrue(evaluate_operator("gt", "5", "3"))
        self.assertTrue(evaluate_operator("gte", 3, 3))
        self.assertTrue(evaluate_operator("lt", "2", "10"))
        self.assertFalse(evaluate_operator("lte", "11", "10"))
        self.assertTrue(evaluate_operator("between", 5, [1, 10]))
        self.assertTrue(evaluate_operator("not_between", 50, [1, 10]))

    def test_text_operators(self):
        self.assertTrue(evaluate_operator("contains", "hello world", "world"))
        self.assertTrue(evaluate_operator("not_contains", "abc", "z"))
        self.assertTrue(evaluate_operator("starts_with", "abcdef", "abc"))
        self.assertTrue(evaluate_operator("ends_with", "abcdef", "def"))
        self.assertTrue(evaluate_operator("matches_regex", "AB12", r"^[A-Z]{2}\d{2}$"))
        self.assertTrue(evaluate_operator("is_empty", "", None))
        self.assertTrue(evaluate_operator("is_not_empty", "x", None))

    def test_selection_operators(self):
        self.assertTrue(evaluate_operator("includes", ["a", "b"], "a"))
        self.assertTrue(evaluate_operator("not_includes", ["a", "b"], "z"))
        self.assertTrue(evaluate_operator("includes_any", ["a", "b"], ["z", "b"]))
        self.assertTrue(evaluate_operator("includes_all", ["a", "b", "c"], ["a", "b"]))
        self.assertFalse(evaluate_operator("includes_all", ["a"], ["a", "b"]))

    def test_date_operators(self):
        self.assertTrue(evaluate_operator("before", "2024-01-01", "2025-01-01"))
        self.assertTrue(evaluate_operator("after", "2025-06-01", "2025-01-01"))
        self.assertTrue(evaluate_operator("on", "2025-01-01", "2025-01-01"))

    def test_unevaluable_comparison_is_false_not_error(self):
        self.assertFalse(evaluate_operator("gt", "abc", "def"))
        self.assertFalse(evaluate_operator("matches_regex", "x", "([a"))


class NormalizeTests(SimpleTestCase):
    def test_legacy_single_rule(self):
        n = normalize_conditional_logic({"if": 12, "equals": "Yes"})
        self.assertEqual(n["logic"], "AND")
        self.assertEqual(n["rules"][0], {"field": 12, "operator": "equals", "value": "Yes"})
        self.assertEqual(n["action"], "show")

    def test_legacy_multi_rule_operator_aliases(self):
        n = normalize_conditional_logic(
            {"logic": "OR", "rules": [{"if": 1, "operator": "greater_than", "value": "3"}]}
        )
        self.assertEqual(n["rules"][0]["operator"], "gt")

    def test_broken_parent_placeholder_yields_unevaluable_rule(self):
        n = normalize_conditional_logic({"if": "parent", "equals": "Yes"})
        self.assertIsNone(n["rules"][0]["field"])
        self.assertEqual(n["rules"][0]["field_raw"], "parent")

    def test_empty_is_empty(self):
        self.assertEqual(normalize_conditional_logic({}), {})
        self.assertEqual(normalize_conditional_logic(None), {})

    def test_nested_groups(self):
        n = normalize_conditional_logic({
            "logic": "OR",
            "rules": [
                {"logic": "AND", "rules": [
                    {"field": 1, "operator": "equals", "value": "Yes"},
                    {"field": 2, "operator": "gt", "value": "18"},
                ]},
                {"field": 3, "operator": "equals", "value": "Admin"},
            ],
        })
        self.assertEqual(len(n["rules"]), 2)
        self.assertIn("rules", n["rules"][0])


class LayoutTests(TestCase):
    def setUp(self):
        self.form = make_form()
        self.trigger = add_field(self.form, FieldType.RADIO, label="Has experience?",
                                 options=["Yes", "No"], order=1)
        self.dependent = add_field(
            self.form, FieldType.NUMBER, label="Years", order=2,
            conditional_logic={"logic": "AND",
                               "rules": [{"field": self.trigger.id, "operator": "equals", "value": "Yes"}],
                               "action": "show"},
        )

    def test_show_when_condition_met(self):
        layout = compute_layout([self.trigger, self.dependent], {self.trigger.id: "Yes"})
        self.assertIn(self.dependent.id, layout.visible_ids)

    def test_hidden_when_condition_not_met(self):
        layout = compute_layout([self.trigger, self.dependent], {self.trigger.id: "No"})
        self.assertNotIn(self.dependent.id, layout.visible_ids)

    def test_require_action_toggles_required(self):
        self.dependent.conditional_logic = {
            "logic": "AND",
            "rules": [{"field": self.trigger.id, "operator": "equals", "value": "Yes"}],
            "action": "require",
        }
        self.dependent.save()
        layout = compute_layout([self.trigger, self.dependent], {self.trigger.id: "Yes"})
        self.assertTrue(layout.effective_required(self.dependent))
        layout2 = compute_layout([self.trigger, self.dependent], {self.trigger.id: "No"})
        self.assertFalse(layout2.effective_required(self.dependent))

    def test_show_rule_is_dormant_until_trigger_is_answered(self):
        # No answer for the trigger yet -> a positive-match `show` rule must not
        # reveal the dependent field on first render.
        layout = compute_layout([self.trigger, self.dependent], {})
        self.assertNotIn(self.dependent.id, layout.visible_ids)
        layout_blank = compute_layout([self.trigger, self.dependent], {self.trigger.id: ""})
        self.assertNotIn(self.dependent.id, layout_blank.visible_ids)

    def test_not_equals_show_rule_also_waits_for_an_answer(self):
        self.dependent.conditional_logic = {
            "logic": "AND",
            "rules": [{"field": self.trigger.id, "operator": "not_equals", "value": "Yes"}],
            "action": "show",
        }
        self.dependent.save()
        # Blank trigger -> still hidden (was previously shown because "" != "Yes").
        self.assertNotIn(self.dependent.id, compute_layout([self.trigger, self.dependent], {}).visible_ids)
        # Answered and not "Yes" -> shown.
        self.assertIn(self.dependent.id, compute_layout([self.trigger, self.dependent], {self.trigger.id: "No"}).visible_ids)

    def test_require_rule_is_dormant_until_trigger_is_answered(self):
        self.dependent.conditional_logic = {
            "logic": "AND",
            "rules": [{"field": self.trigger.id, "operator": "equals", "value": "Yes"}],
            "action": "require",
        }
        self.dependent.save()
        layout = compute_layout([self.trigger, self.dependent], {})
        self.assertFalse(layout.effective_required(self.dependent))

    def test_is_empty_operator_still_fires_on_blank_trigger(self):
        self.dependent.conditional_logic = {
            "logic": "AND",
            "rules": [{"field": self.trigger.id, "operator": "is_empty", "value": None}],
            "action": "show",
        }
        self.dependent.save()
        self.assertIn(self.dependent.id, compute_layout([self.trigger, self.dependent], {}).visible_ids)

    def test_cycle_guard_terminates(self):
        a = add_field(self.form, FieldType.TEXT, label="A", order=3)
        b = add_field(self.form, FieldType.TEXT, label="B", order=4)
        a.conditional_logic = {"logic": "AND", "rules": [{"field": b.id, "operator": "is_not_empty", "value": None}], "action": "show"}
        b.conditional_logic = {"logic": "AND", "rules": [{"field": a.id, "operator": "is_not_empty", "value": None}], "action": "show"}
        a.save(); b.save()
        layout = compute_layout(list(self.form.fields.all()), {})
        self.assertIsInstance(layout.visible_ids, set)


class HiddenFieldSubmissionTests(TestCase):
    def test_hidden_required_field_is_not_enforced_and_its_answer_is_dropped(self):
        form = make_form()
        trig = add_field(form, FieldType.RADIO, label="Employed?", options=["Yes", "No"], required=True, order=1)
        company = add_field(
            form, FieldType.TEXT, label="Company", required=True, order=2,
            conditional_logic={"logic": "AND",
                               "rules": [{"field": trig.id, "operator": "equals", "value": "Yes"}],
                               "action": "show"},
        )
        # Answer "No" -> Company hidden -> not required, and any stray answer for it is dropped.
        report = validate_submission(form, answers((trig, "No"), (company, "stale value")))
        self.assertTrue(report.ok, [e.message for e in report.errors])
        self.assertNotIn(company.id, report.cleaned_answers)

        # Answer "Yes" -> Company now required and empty -> error.
        report2 = validate_submission(form, answers((trig, "Yes")))
        self.assertFalse(report2.ok)
        self.assertEqual(report2.errors[0].field_id, company.id)
