from django.test import TestCase

from apps.forms.models import FieldType, FormStatus
from apps.forms.validation import validate_form_definition, codes
from .factories import make_form, add_field


def _codes(report):
    return {e.code for e in report.errors}


class DefinitionValidationTests(TestCase):
    def test_valid_form_publishable(self):
        form = make_form(status=FormStatus.DRAFT)
        add_field(form, FieldType.TEXT, label="Name", required=True, order=1,
                  validation_rules={"minLength": 2, "maxLength": 80})
        add_field(form, FieldType.RADIO, label="Track", options=["AI", "Web"], order=2)
        report = validate_form_definition(form)
        self.assertTrue(report.publishable, [e.message for e in report.errors])

    def test_choice_field_needs_two_options(self):
        form = make_form()
        add_field(form, FieldType.DROPDOWN, label="Pick", options=["only one"], order=1)
        self.assertIn(codes.DEF_CHOICE_NEEDS_OPTIONS, _codes(validate_form_definition(form)))

    def test_duplicate_and_blank_options(self):
        form = make_form()
        add_field(form, FieldType.RADIO, label="R", options=["A", "a", ""], order=1)
        c = _codes(validate_form_definition(form))
        self.assertIn(codes.DEF_DUPLICATE_OPTION, c)
        self.assertIn(codes.DEF_BLANK_OPTION, c)

    def test_min_greater_than_max(self):
        form = make_form()
        add_field(form, FieldType.TEXT, label="T", order=1,
                  validation_rules={"minLength": 10, "maxLength": 2})
        self.assertIn(codes.DEF_MIN_GREATER_THAN_MAX, _codes(validate_form_definition(form)))

    def test_negative_constraint(self):
        form = make_form()
        add_field(form, FieldType.TEXT, label="T", order=1, validation_rules={"maxLength": -3})
        self.assertIn(codes.DEF_NEGATIVE_CONSTRAINT, _codes(validate_form_definition(form)))

    def test_invalid_and_empty_regex(self):
        form = make_form()
        add_field(form, FieldType.TEXT, label="A", order=1, validation_rules={"pattern": "([a"})
        add_field(form, FieldType.TEXT, label="B", order=2, validation_rules={"pattern": ""})
        c = _codes(validate_form_definition(form))
        self.assertIn(codes.DEF_INVALID_REGEX, c)
        self.assertIn(codes.DEF_EMPTY_REGEX, c)

    def test_unknown_format(self):
        form = make_form()
        add_field(form, FieldType.TEXT, label="T", order=1, validation_rules={"format": "martian"})
        self.assertIn(codes.DEF_UNKNOWN_FORMAT, _codes(validate_form_definition(form)))

    def test_rule_incompatible_with_type(self):
        form = make_form()
        add_field(form, FieldType.RADIO, label="R", options=["A", "B"], order=1,
                  validation_rules={"minLength": 3})
        self.assertIn(codes.DEF_RULE_INCOMPATIBLE, _codes(validate_form_definition(form)))

    def test_scale_range_invalid(self):
        form = make_form()
        add_field(form, FieldType.LINEAR_SCALE, label="S", order=1, min_value=5, max_value=5)
        self.assertIn(codes.DEF_SCALE_RANGE_INVALID, _codes(validate_form_definition(form)))

    def test_conditional_reference_to_missing_field(self):
        form = make_form()
        add_field(form, FieldType.TEXT, label="A", order=1,
                  conditional_logic={"logic": "AND",
                                     "rules": [{"field": 999999, "operator": "equals", "value": "x"}],
                                     "action": "show"})
        self.assertIn(codes.DEF_CONDITION_UNKNOWN_FIELD, _codes(validate_form_definition(form)))

    def test_conditional_self_reference(self):
        form = make_form()
        f = add_field(form, FieldType.TEXT, label="A", order=1)
        f.conditional_logic = {"logic": "AND",
                               "rules": [{"field": f.id, "operator": "equals", "value": "x"}],
                               "action": "show"}
        f.save()
        self.assertIn(codes.DEF_CONDITION_SELF_REFERENCE, _codes(validate_form_definition(form)))

    def test_broken_parent_placeholder_is_warning_not_error(self):
        form = make_form()
        add_field(form, FieldType.TEXT, label="A", order=1,
                  conditional_logic={"if": "parent", "equals": "Yes"})
        report = validate_form_definition(form)
        self.assertTrue(report.publishable)
        self.assertIn(codes.WARN_LEGACY_CONDITION_PLACEHOLDER, {w.code for w in report.warnings})

    def test_cross_field_reference_to_missing_field(self):
        form = make_form()
        add_field(form, FieldType.TEXT, label="A", order=1,
                  validation_rules={"crossField": [{"op": "eq", "field": 424242}]})
        self.assertIn(codes.DEF_CROSS_FIELD_UNKNOWN_FIELD, _codes(validate_form_definition(form)))

    def test_no_required_fields_is_soft_warning(self):
        form = make_form()
        add_field(form, FieldType.TEXT, label="A", order=1)
        report = validate_form_definition(form)
        self.assertTrue(report.publishable)
        self.assertIn(codes.WARN_NO_REQUIRED_FIELDS, {w.code for w in report.warnings})

    def test_payload_shape_also_accepted(self):
        payload = {"fields": [
            {"label": "Name", "type": "TEXT", "is_required": True, "order": 1, "validation_rules": {}},
            {"label": "Bad", "type": "RADIO", "options": [], "order": 2, "validation_rules": {}},
        ]}
        report = validate_form_definition(payload)
        self.assertFalse(report.publishable)
        self.assertIn(codes.DEF_CHOICE_NEEDS_OPTIONS, _codes(report))
