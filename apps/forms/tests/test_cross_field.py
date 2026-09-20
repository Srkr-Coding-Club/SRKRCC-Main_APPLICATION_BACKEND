from django.test import TestCase

from apps.forms.models import FieldType
from apps.forms.validation import validate_submission, codes
from .factories import make_form, add_field, answers


def _codes(r):
    return {e.code for e in r.errors}


class CrossFieldTests(TestCase):
    def test_password_confirmation(self):
        form = make_form()
        pw = add_field(form, FieldType.TEXT, label="Password", required=True, order=1)
        confirm = add_field(form, FieldType.TEXT, label="Confirm Password", required=True, order=2,
                            validation_rules={"crossField": [{"op": "eq", "field": pw.id,
                                                              "message": "Passwords must match."}]})
        r = validate_submission(form, answers((pw, "secret1"), (confirm, "secret2")))
        self.assertIn(codes.CROSS_FIELD_MISMATCH, _codes(r))
        self.assertEqual(r.errors[0].message, "Passwords must match.")

        r2 = validate_submission(form, answers((pw, "secret1"), (confirm, "secret1")))
        self.assertTrue(r2.ok, [e.message for e in r2.errors])

    def test_date_range_order(self):
        form = make_form()
        start = add_field(form, FieldType.DATE, label="Start", required=True, order=1)
        end = add_field(form, FieldType.DATE, label="End", required=True, order=2,
                        validation_rules={"crossField": [{"op": "gte", "field": start.id}]})
        r = validate_submission(form, answers((start, "2025-05-10"), (end, "2025-05-01")))
        self.assertIn(codes.CROSS_FIELD_ORDER, _codes(r))
        r2 = validate_submission(form, answers((start, "2025-05-01"), (end, "2025-05-10")))
        self.assertTrue(r2.ok)

    def test_numeric_min_max_salary(self):
        form = make_form()
        lo = add_field(form, FieldType.NUMBER, label="Min Salary", required=True, order=1)
        hi = add_field(form, FieldType.NUMBER, label="Max Salary", required=True, order=2,
                       validation_rules={"crossField": [{"op": "gte", "field": lo.id}]})
        self.assertIn(codes.CROSS_FIELD_ORDER,
                      _codes(validate_submission(form, answers((lo, "50000"), (hi, "40000")))))
        self.assertTrue(validate_submission(form, answers((lo, "40000"), (hi, "50000"))).ok)

    def test_required_if(self):
        form = make_form()
        status = add_field(form, FieldType.RADIO, label="Employment Status", required=True,
                           options=["Employed", "Student"], order=1)
        company = add_field(form, FieldType.TEXT, label="Company", order=2,
                            validation_rules={"crossField": [{"op": "required_if", "field": status.id,
                                                              "equals": "Employed"}]})
        r = validate_submission(form, answers((status, "Employed")))
        self.assertIn(codes.CROSS_FIELD_REQUIRED, _codes(r))
        self.assertTrue(validate_submission(form, answers((status, "Student"))).ok)
        self.assertTrue(validate_submission(form, answers((status, "Employed"), (company, "SRKRCC"))).ok)
