from django.test import TestCase

from apps.forms.models import FieldType
from apps.forms.validation import validate_submission
from apps.forms.validation import codes
from .factories import make_form, add_field, answers


def _codes(report):
    return {e.code for e in report.errors}


class RequiredTests(TestCase):
    def setUp(self):
        self.form = make_form()
        self.name = add_field(self.form, FieldType.TEXT, label="Your name", required=True, order=1)
        self.opt = add_field(self.form, FieldType.TEXT, label="Nickname", required=False, order=2)

    def test_missing_field_rejected(self):
        r = validate_submission(self.form, [])
        self.assertIn(codes.REQUIRED, _codes(r))

    def test_null_empty_and_whitespace_rejected(self):
        for bad in (None, "", "   ", "\t\n"):
            r = validate_submission(self.form, answers((self.name, bad)))
            self.assertIn(codes.REQUIRED, _codes(r), f"value={bad!r}")

    def test_optional_field_may_be_empty(self):
        r = validate_submission(self.form, answers((self.name, "Ada"), (self.opt, "")))
        self.assertTrue(r.ok, [e.message for e in r.errors])

    def test_empty_array_for_multiselect_rejected(self):
        skills = add_field(self.form, FieldType.CHECKBOX, label="Skills",
                           options=["A", "B"], required=True, order=3)
        r = validate_submission(self.form, answers((self.name, "Ada"), (skills, [])))
        self.assertIn(codes.REQUIRED, _codes(r))


class TextTypeTests(TestCase):
    def setUp(self):
        self.form = make_form()

    def _field(self, **rules):
        # not required: each sub-case adds a new field to the shared form, so a
        # submission that only answers one of them must not trip REQUIRED on the rest.
        return add_field(self.form, FieldType.TEXT, label="F", required=False,
                         validation_rules=rules, order=self.form.fields.count() + 1)

    def test_alpha_format(self):
        f = self._field(format="alpha")
        self.assertTrue(validate_submission(self.form, answers((f, "John Doe"))).ok)
        for bad in ("Ashok123", "12345", "Ashok@123"):
            self.assertIn(codes.INVALID_FORMAT, _codes(validate_submission(self.form, answers((f, bad)))))

    def test_alphanumeric_format(self):
        f = self._field(format="alphanumeric")
        self.assertTrue(validate_submission(self.form, answers((f, "EMP123"))).ok)
        for bad in ("EMP@123", "EMP-123"):
            self.assertFalse(validate_submission(self.form, answers((f, bad))).ok)

    def test_min_max_exact_length_boundaries(self):
        f = self._field(minLength=3, maxLength=5)
        self.assertIn(codes.MIN_LENGTH, _codes(validate_submission(self.form, answers((f, "ab")))))
        self.assertTrue(validate_submission(self.form, answers((f, "abc"))).ok)   # exactly min
        self.assertTrue(validate_submission(self.form, answers((f, "abcde"))).ok)  # exactly max
        self.assertIn(codes.MAX_LENGTH, _codes(validate_submission(self.form, answers((f, "abcdef")))))

        g = self._field(exactLength=4)
        self.assertTrue(validate_submission(self.form, answers((g, "abcd"))).ok)
        self.assertIn(codes.EXACT_LENGTH, _codes(validate_submission(self.form, answers((g, "abc")))))

    def test_pattern_and_starts_ends_contains(self):
        f = self._field(pattern=r"^EMP\d+$")
        self.assertTrue(validate_submission(self.form, answers((f, "EMP42"))).ok)
        self.assertIn(codes.PATTERN_MISMATCH, _codes(validate_submission(self.form, answers((f, "X42")))))

        g = self._field(startsWith="AB", endsWith="Z", contains="-")
        self.assertTrue(validate_submission(self.form, answers((g, "AB-Z"))).ok)
        self.assertFalse(validate_submission(self.form, answers((g, "XY-Z"))).ok)

    def test_invalid_regex_does_not_block_submission(self):
        f = self._field(pattern="([a")
        r = validate_submission(self.form, answers((f, "anything")))
        self.assertNotIn(codes.PATTERN_MISMATCH, _codes(r))


class NumberTests(TestCase):
    def setUp(self):
        self.form = make_form()
        self.f = add_field(self.form, FieldType.NUMBER, label="Qty", required=True,
                           validation_rules={"minValue": 1, "maxValue": 5, "integerOnly": True}, order=1)

    def test_non_number_rejected(self):
        self.assertIn(codes.INVALID_NUMBER, _codes(validate_submission(self.form, answers((self.f, "abc")))))

    def test_boundaries(self):
        self.assertIn(codes.MIN_VALUE, _codes(validate_submission(self.form, answers((self.f, "0")))))
        self.assertTrue(validate_submission(self.form, answers((self.f, "1"))).ok)
        self.assertTrue(validate_submission(self.form, answers((self.f, "5"))).ok)
        self.assertIn(codes.MAX_VALUE, _codes(validate_submission(self.form, answers((self.f, "6")))))

    def test_integer_only(self):
        self.assertIn(codes.MUST_BE_INTEGER, _codes(validate_submission(self.form, answers((self.f, "2.5")))))

    def test_number_stored_as_number(self):
        r = validate_submission(self.form, answers((self.f, "3")))
        self.assertEqual(r.cleaned_answers[self.f.id], 3)


class EmailTests(TestCase):
    def setUp(self):
        self.form = make_form()
        self.f = add_field(self.form, FieldType.EMAIL, label="Email", required=True,
                           validation_rules={"allowedDomains": ["srkr.ac.in"], "normalizeCase": True}, order=1)

    def test_invalid_format(self):
        self.assertIn(codes.INVALID_EMAIL, _codes(validate_submission(self.form, answers((self.f, "not-an-email")))))

    def test_domain_allowlist(self):
        self.assertIn(codes.EMAIL_DOMAIN_NOT_ALLOWED,
                      _codes(validate_submission(self.form, answers((self.f, "x@gmail.com")))))
        self.assertTrue(validate_submission(self.form, answers((self.f, "x@srkr.ac.in"))).ok)

    def test_case_normalized_on_store(self):
        r = validate_submission(self.form, answers((self.f, "  Person@SRKR.AC.IN ")))
        self.assertEqual(r.cleaned_answers[self.f.id], "person@srkr.ac.in")


class ChoiceTests(TestCase):
    def setUp(self):
        self.form = make_form()
        self.radio = add_field(self.form, FieldType.RADIO, label="Gender", required=True,
                               options=["Male", "Female", "Other"], order=1)
        self.checks = add_field(self.form, FieldType.CHECKBOX, label="Skills", required=True,
                                options=["Python", "Go", "Rust", "JS", "C++"],
                                validation_rules={"minSelected": 2, "maxSelected": 3}, order=2)

    def test_arbitrary_radio_value_rejected(self):
        r = validate_submission(self.form, answers((self.radio, "InvalidValue"), (self.checks, ["Python", "Go"])))
        self.assertIn(codes.OPTION_NOT_ALLOWED, _codes(r))

    def test_checkbox_membership_and_count(self):
        # unknown option
        r = validate_submission(self.form, answers((self.radio, "Male"), (self.checks, ["Python", "COBOL"])))
        self.assertIn(codes.OPTION_NOT_ALLOWED, _codes(r))
        # too few
        r2 = validate_submission(self.form, answers((self.radio, "Male"), (self.checks, ["Python"])))
        self.assertIn(codes.MIN_SELECTIONS, _codes(r2))
        # too many
        r3 = validate_submission(self.form, answers((self.radio, "Male"), (self.checks, ["Python", "Go", "Rust", "JS"])))
        self.assertIn(codes.MAX_SELECTIONS, _codes(r3))
        # duplicate
        r4 = validate_submission(self.form, answers((self.radio, "Male"), (self.checks, ["Python", "Python", "Go"])))
        self.assertIn(codes.DUPLICATE_SELECTION, _codes(r4))
        # exactly valid
        r5 = validate_submission(self.form, answers((self.radio, "Male"), (self.checks, ["Python", "Go"])))
        self.assertTrue(r5.ok, [e.message for e in r5.errors])

    def test_checkbox_single_string_tolerated_then_count_checked(self):
        r = validate_submission(self.form, answers((self.radio, "Male"), (self.checks, "Python")))
        self.assertIn(codes.MIN_SELECTIONS, _codes(r))  # coerced to ["Python"], fails minSelected 2


class DateTests(TestCase):
    def setUp(self):
        self.form = make_form()
        self.f = add_field(self.form, FieldType.DATE, label="DOB", required=True,
                           validation_rules={"minDate": "2000-01-01", "maxDate": "2010-12-31"}, order=1)

    def test_invalid_date(self):
        self.assertIn(codes.INVALID_DATE, _codes(validate_submission(self.form, answers((self.f, "31/02/2020")))))

    def test_bounds(self):
        self.assertIn(codes.DATE_TOO_EARLY, _codes(validate_submission(self.form, answers((self.f, "1999-12-31")))))
        self.assertTrue(validate_submission(self.form, answers((self.f, "2005-06-15"))).ok)
        self.assertIn(codes.DATE_TOO_LATE, _codes(validate_submission(self.form, answers((self.f, "2011-01-01")))))

    def test_date_stored_as_iso_string(self):
        r = validate_submission(self.form, answers((self.f, "2005-06-15")))
        self.assertEqual(r.cleaned_answers[self.f.id], "2005-06-15")


class RatingTests(TestCase):
    def test_scale_range_from_model_columns(self):
        form = make_form()
        f = add_field(form, FieldType.RATING, label="Rate us", required=True,
                      min_value=1, max_value=5, order=1)
        self.assertIn(codes.OUT_OF_RANGE, _codes(validate_submission(form, answers((f, "0")))))
        self.assertIn(codes.OUT_OF_RANGE, _codes(validate_submission(form, answers((f, "6")))))
        self.assertIn(codes.MUST_BE_INTEGER, _codes(validate_submission(form, answers((f, "2.5")))))
        self.assertTrue(validate_submission(form, answers((f, "3"))).ok)


class FileTests(TestCase):
    def setUp(self):
        self.form = make_form()
        self.f = add_field(self.form, FieldType.MULTI_FILE, label="Docs", required=True,
                           validation_rules={"allowedFileTypes": ".pdf,.docx",
                                             "maxFileSizeMB": 1, "maxFiles": 2}, order=1)

    def test_extension_size_and_count(self):
        big = {"name": "a.pdf", "size": 5 * 1024 * 1024}
        self.assertIn(codes.FILE_TOO_LARGE, _codes(validate_submission(self.form, answers((self.f, [big])))))
        wrong = {"name": "a.exe", "size": 10}
        self.assertIn(codes.FILE_TYPE_NOT_ALLOWED, _codes(validate_submission(self.form, answers((self.f, [wrong])))))
        many = [{"name": f"{i}.pdf", "size": 10} for i in range(3)]
        self.assertIn(codes.TOO_MANY_FILES, _codes(validate_submission(self.form, answers((self.f, many)))))
        ok = [{"name": "a.pdf", "size": 100}, {"name": "b.docx", "size": 200}]
        self.assertTrue(validate_submission(self.form, answers((self.f, ok))).ok)

    def test_inline_data_url_and_mime_are_preserved(self):
        form = make_form()
        f = add_field(form, FieldType.FILE, label="Photo", required=True, order=1)
        data_url = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
        report = validate_submission(
            form, answers((f, {"name": "me.png", "size": 120, "type": "image/png", "url": data_url}))
        )
        self.assertTrue(report.ok)
        stored = report.cleaned_answers[f.id]
        self.assertEqual(stored[0]["url"], data_url)
        self.assertEqual(stored[0]["type"], "image/png")
