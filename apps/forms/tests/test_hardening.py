"""
Production-hardening regressions for the form validation engine: unsafe numbers,
oversized / control-character input, unverifiable file payloads, rule fail-open,
unanchored patterns, URL domain rules, plus the stricter publish-time checks.
"""

import base64
from unittest import mock

from django.core.cache import cache
from rest_framework.test import APITestCase
from django.test import TestCase

from apps.forms.models import FieldType, FormStatus, Response
from apps.forms.validation import codes, validate_form_definition, validate_submission
from apps.forms.validation import rules as rules_module
from .factories import add_field, answers, make_form

# A real 1x1 transparent PNG.
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
PNG_URL = f"data:image/png;base64,{PNG_B64}"


def _codes(report):
    return {e.code for e in report.errors}


def _data_url(content: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(content).decode()}"


def _single(field_type, value, **kwargs):
    form = make_form()
    field = add_field(form, field_type, label="F", order=1, **kwargs)
    return field, validate_submission(form, answers((field, value)))


class NumberSafetyTests(TestCase):
    def test_non_finite_and_huge_numbers_rejected(self):
        for bad in ("NaN", "Infinity", "-Infinity", "inf", "1e999999", "1e40", float("inf"), float("nan")):
            _, report = _single(FieldType.NUMBER, bad)
            self.assertIn(codes.INVALID_NUMBER, _codes(report), f"value={bad!r}")

    def test_huge_exponent_is_rejected_quickly(self):
        # Previously int(Decimal("1e999999")) took ~20s of CPU on the storage step.
        import time
        start = time.monotonic()
        _single(FieldType.NUMBER, "1e999999")
        self.assertLess(time.monotonic() - start, 1.0)

    def test_comma_grouping_must_be_well_formed(self):
        _, bad = _single(FieldType.NUMBER, "1,2,3")
        self.assertIn(codes.INVALID_NUMBER, _codes(bad))
        field, good = _single(FieldType.NUMBER, "1,234.5")
        self.assertTrue(good.ok)
        self.assertEqual(good.cleaned_answers[field.id], 1234.5)

    def test_excess_precision_rejected(self):
        _, report = _single(FieldType.NUMBER, "0.1234567890123456789")
        self.assertIn(codes.INVALID_NUMBER, _codes(report))

    def test_ordinary_numbers_still_pass(self):
        for ok in ("0", "42", "-7", "12.5", 3, 2.25):
            _, report = _single(FieldType.NUMBER, ok)
            self.assertTrue(report.ok, f"value={ok!r}: {[e.message for e in report.errors]}")


class TextSafetyTests(TestCase):
    def test_null_bytes_and_control_characters_rejected(self):
        for bad in ("a\x00b", "bell\x07", "esc\x1b[0m", "x\x7f"):
            _, report = _single(FieldType.TEXT, bad)
            self.assertIn(codes.INVALID_CHARACTERS, _codes(report), f"value={bad!r}")

    def test_newlines_and_tabs_are_fine_in_paragraphs(self):
        _, report = _single(FieldType.PARAGRAPH, "line one\nline two\tindented")
        self.assertTrue(report.ok)

    def test_default_length_ceilings(self):
        _, text = _single(FieldType.TEXT, "a" * 1001)
        self.assertIn(codes.VALUE_TOO_LONG, _codes(text))
        _, para = _single(FieldType.PARAGRAPH, "a" * 10001)
        self.assertIn(codes.VALUE_TOO_LONG, _codes(para))
        _, ok = _single(FieldType.TEXT, "a" * 1000)
        self.assertTrue(ok.ok)

    def test_checkbox_and_matrix_collections_are_bounded(self):
        _, report = _single(FieldType.CHECKBOX, [str(i) for i in range(201)], options=["a", "b"],
                            validation_rules={"allowOther": True})
        self.assertIn(codes.TOO_MANY_ITEMS, _codes(report))
        _, nested = _single(FieldType.MATRIX_RADIO, {"Row": {"x": 1}}, rows=["Row"], options=["a", "b"])
        self.assertIn(codes.EXPECTED_SCALAR, _codes(nested))

    def test_unicode_names_pass_alpha_format(self):
        field, _ = _single(FieldType.TEXT, "x", validation_rules={"format": "alpha"})
        form = field.form
        for good in ("José", "రాజేష్", "राजेश कुमार", "O Brien"):
            self.assertTrue(validate_submission(form, answers((field, good))).ok, good)
        for bad in ("Ashok1", "a@b", "x_y"):
            self.assertFalse(validate_submission(form, answers((field, bad))).ok, bad)


class RuleBehaviourTests(TestCase):
    def test_pattern_must_match_the_whole_value(self):
        field, ok = _single(FieldType.TEXT, "1234567890", validation_rules={"pattern": "[0-9]{10}"})
        self.assertTrue(ok.ok)
        report = validate_submission(field.form, answers((field, "abc1234567890xyz")))
        self.assertIn(codes.PATTERN_MISMATCH, _codes(report))

    def test_pattern_error_message_only_applies_to_the_pattern(self):
        field, report = _single(
            FieldType.TEXT, "ab",
            validation_rules={"minLength": 5, "pattern": "[a-z]+", "patternError": "Use lowercase letters"},
        )
        by_code = {e.code: e.message for e in report.errors}
        self.assertEqual(by_code[codes.MIN_LENGTH], "Must be at least 5 character(s).")
        bad = validate_submission(field.form, answers((field, "ABCDE")))
        self.assertEqual({e.code: e.message for e in bad.errors}[codes.PATTERN_MISMATCH], "Use lowercase letters")

    def test_crashing_rule_fails_closed(self):
        form = make_form()
        field = add_field(form, FieldType.TEXT, label="F", order=1, validation_rules={"minLength": 2})
        with mock.patch.dict(rules_module.RULE_VALIDATORS, {"minLength": mock.Mock(side_effect=RuntimeError("boom"))}):
            report = validate_submission(form, answers((field, "hello")))
        self.assertIn(codes.RULE_EVALUATION_FAILED, _codes(report))

    def test_url_domain_rules_are_enforced(self):
        field, ok = _single(FieldType.URL, "https://github.com/me/repo",
                            validation_rules={"allowedDomains": ["github.com"]})
        self.assertTrue(ok.ok)
        form = field.form
        self.assertTrue(validate_submission(form, answers((field, "https://gist.github.com/x"))).ok)
        for bad in ("https://evil.com/github.com", "https://notgithub.com", "https://github.com@evil.com/x"):
            report = validate_submission(form, answers((field, bad)))
            self.assertIn(codes.EMAIL_DOMAIN_NOT_ALLOWED, _codes(report), bad)

    def test_url_blocked_domains(self):
        _, report = _single(FieldType.URL, "https://www.bit.ly/abc", validation_rules={"blockedDomains": ["bit.ly"]})
        self.assertIn(codes.EMAIL_DOMAIN_BLOCKED, _codes(report))

    def test_email_domain_must_contain_a_dot(self):
        _, report = _single(FieldType.EMAIL, "user@localhost")
        self.assertIn(codes.INVALID_EMAIL, _codes(report))

    def test_email_length_ceiling(self):
        _, report = _single(FieldType.EMAIL, "a" * 250 + "@x.com")
        self.assertIn(codes.VALUE_TOO_LONG, _codes(report))

    def test_phone_default_digit_range(self):
        for bad in ("123456", "1234567890123456"):
            _, report = _single(FieldType.PHONE, bad)
            self.assertIn(codes.INVALID_PHONE, _codes(report), bad)
        for good in ("9876543210", "+91 98765 43210", "040-2345-6789"):
            _, report = _single(FieldType.PHONE, good)
            self.assertTrue(report.ok, good)

    def test_dates_outside_a_sane_range_rejected(self):
        for bad in ("0001-01-01", "1899-12-31", "9999-12-31"):
            _, report = _single(FieldType.DATE, bad)
            self.assertIn(codes.DATE_OUT_OF_BOUNDS, _codes(report), bad)
        _, ok = _single(FieldType.DATE, "2004-05-17")
        self.assertTrue(ok.ok)

    def test_club_id_format(self):
        _, bad = _single(FieldType.CLUB_ID, "no spaces!")
        self.assertIn(codes.INVALID_FORMAT, _codes(bad))


class CanonicalStorageTests(TestCase):
    def test_choice_answers_are_stored_with_the_admins_spelling(self):
        form = make_form()
        radio = add_field(form, FieldType.RADIO, label="Lang", options=["Python", "C++"], order=1)
        boxes = add_field(form, FieldType.CHECKBOX, label="Skills", options=["Django", "React"], order=2)
        grid = add_field(form, FieldType.MATRIX_CHECKBOX, label="Grid", rows=["Speed"], options=["Low", "High"], order=3)
        report = validate_submission(form, answers(
            (radio, "python"), (boxes, ["DJANGO", "react"]), (grid, {"speed": ["high"]}),
        ))
        self.assertTrue(report.ok, [e.message for e in report.errors])
        self.assertEqual(report.cleaned_answers[radio.id], "Python")
        self.assertEqual(report.cleaned_answers[boxes.id], ["Django", "React"])
        self.assertEqual(report.cleaned_answers[grid.id], {"Speed": ["High"]})


class FilePayloadTests(TestCase):
    def setUp(self):
        self.form = make_form()

    def _file(self, field_type=FieldType.FILE, **rules):
        return add_field(self.form, field_type, label=f"File{self.form.fields.count()}", order=self.form.fields.count() + 1,
                         validation_rules=rules)

    def _submit(self, field, value):
        return validate_submission(self.form, answers((field, value)))

    def test_size_is_computed_server_side(self):
        field = self._file(maxFileSizeMB=0.001)  # ~1 KB
        content = b"%PDF-1.4\n" + b"x" * 5000
        item = {"name": "a.pdf", "size": 10, "type": "application/pdf", "url": _data_url(content, "application/pdf")}
        report = self._submit(field, item)
        self.assertIn(codes.FILE_TOO_LARGE, _codes(report))

    def test_stored_size_and_type_come_from_the_payload(self):
        field = self._file()
        content = b"%PDF-1.4\nhello"
        item = {"name": "a.pdf", "size": 1, "type": "text/plain", "url": _data_url(content, "application/pdf")}
        report = self._submit(field, item)
        self.assertTrue(report.ok, [e.message for e in report.errors])
        stored = report.cleaned_answers[field.id][0]
        self.assertEqual(stored["size"], len(content))
        self.assertEqual(stored["type"], "application/pdf")

    def test_content_must_match_extension(self):
        field = self._file()
        item = {"name": "photo.png", "size": 10, "url": _data_url(b"%PDF-1.4 not a png", "image/png")}
        self.assertIn(codes.FILE_CONTENT_MISMATCH, _codes(self._submit(field, item)))

    def test_unsafe_or_malformed_urls_rejected(self):
        field = self._file()
        for url in ("javascript:alert(1)", "file:///etc/passwd", "data:text/html;base64,PGI+", "data:application/pdf;base64,@@@@"):
            item = {"name": "a.pdf", "size": 1, "url": url}
            self.assertTrue({codes.INVALID_FILE_PAYLOAD, codes.FILE_TYPE_BLOCKED, codes.FILE_CONTENT_MISMATCH} & _codes(self._submit(field, item)), url)

    def test_https_link_is_accepted(self):
        field = self._file()
        self.assertTrue(self._submit(field, {"name": "a.pdf", "size": 1, "url": "https://files.example.com/a.pdf"}).ok)

    def test_oversized_inline_payload_rejected(self):
        field = self._file()
        big = _data_url(b"%PDF" + b"0" * (11 * 1024 * 1024), "application/pdf")
        self.assertIn(codes.FILE_TOO_LARGE, _codes(self._submit(field, {"name": "big.pdf", "size": 1, "url": big})))

    def test_executables_and_active_content_blocked_unless_allowed(self):
        field = self._file()
        for name in ("setup.exe", "page.html", "logo.svg"):
            self.assertIn(codes.FILE_TYPE_BLOCKED, _codes(self._submit(field, {"name": name, "size": 1})), name)
        explicit = self._file(allowedFileTypes=".svg")
        self.assertTrue(self._submit(explicit, {"name": "logo.svg", "size": 1}).ok)

    def test_single_file_field_rejects_several(self):
        field = self._file()
        report = self._submit(field, [{"name": "a.pdf", "size": 1}, {"name": "b.pdf", "size": 1}])
        self.assertIn(codes.TOO_MANY_FILES, _codes(report))

    def test_multi_file_default_cap(self):
        field = self._file(FieldType.MULTI_FILE)
        report = self._submit(field, [{"name": f"{i}.pdf", "size": 1} for i in range(11)])
        self.assertIn(codes.TOO_MANY_FILES, _codes(report))


class SignatureTests(TestCase):
    def test_png_signature_accepted(self):
        _, report = _single(FieldType.SIGNATURE, PNG_URL)
        self.assertTrue(report.ok, [e.message for e in report.errors])

    def test_non_png_or_non_image_rejected(self):
        for bad in ("scribble", _data_url(b"GIF89a....", "image/gif"), _data_url(b"not a png at all", "image/png"),
                    "data:image/png;base64,abc"):
            _, report = _single(FieldType.SIGNATURE, bad)
            self.assertIn(codes.INVALID_SIGNATURE, _codes(report), bad)

    def test_oversized_signature_rejected(self):
        big = _data_url(b"\x89PNG\r\n\x1a\n" + b"0" * (600 * 1024), "image/png")
        _, report = _single(FieldType.SIGNATURE, big)
        self.assertTrue({codes.FILE_TOO_LARGE, codes.INVALID_SIGNATURE} & _codes(report))


def _def_field(**kw):
    base = {"id": None, "label": "Q", "type": "TEXT", "options": [], "rows": [], "validation_rules": {},
            "conditional_logic": {}, "order": 0, "is_required": False}
    base.update(kw)
    return base


class DefinitionHardeningTests(TestCase):
    def _codes(self, *fields, warnings=False):
        payload = {"fields": [dict(f, id=f.get("id") or i + 1, order=i) for i, f in enumerate(fields)]}
        report = validate_form_definition(payload)
        return {w.code for w in report.warnings} if warnings else {e.code for e in report.errors}

    def test_risky_regex_blocked(self):
        for pat in ("(a+)+$", r"(\w*\s?)*", "(x*)*y"):
            c = self._codes(_def_field(validation_rules={"pattern": pat}))
            self.assertIn(codes.DEF_RISKY_REGEX, c, pat)
        self.assertNotIn(codes.DEF_RISKY_REGEX, self._codes(_def_field(validation_rules={"pattern": r"[A-Z]{2}\d{4}"})))

    def test_overlong_regex_blocked(self):
        c = self._codes(_def_field(validation_rules={"pattern": "a" * 501}))
        self.assertIn(codes.DEF_INVALID_REGEX, c)

    def test_python_only_regex_syntax_warns(self):
        w = self._codes(_def_field(validation_rules={"pattern": r"(?P<year>\d{4})"}), warnings=True)
        self.assertIn(codes.WARN_REGEX_DIALECT, w)

    def test_unparseable_bounds_blocked(self):
        c = self._codes(_def_field(type="DATE", validation_rules={"minDate": "next tuesday"}),
                        _def_field(type="TIME", validation_rules={"maxTime": "late"}))
        self.assertIn(codes.DEF_INVALID_BOUND, c)

    def test_unsatisfiable_combinations_blocked(self):
        cases = [
            _def_field(validation_rules={"exactLength": 10, "maxLength": 5}),
            _def_field(type="NUMBER", validation_rules={"exactValue": 50, "maxValue": 10}),
            _def_field(type="NUMBER", validation_rules={"positiveOnly": True, "maxValue": 0}),
            _def_field(type="CHECKBOX", options=["a", "b"], validation_rules={"minSelected": 3}),
            _def_field(type="DATE", validation_rules={"futureOnly": True, "maxDate": "2000-01-01"}),
            _def_field(type="DATE", validation_rules={"pastOnly": True, "futureOnly": True}),
            _def_field(type="FILE", validation_rules={"allowedFileTypes": ".pdf", "blockedFileTypes": ".pdf"}),
            _def_field(type="PHONE", validation_rules={"minDigits": 20}),
            _def_field(validation_rules={"minLength": 5000}),
        ]
        for case in cases:
            self.assertIn(codes.DEF_RULE_UNSATISFIABLE, self._codes(case), case)

    def test_option_and_matrix_shape_checks(self):
        many = [str(i) for i in range(201)]
        self.assertIn(codes.DEF_TOO_MANY_ITEMS, self._codes(_def_field(type="DROPDOWN", options=many)))
        self.assertIn(codes.DEF_INVALID_OPTION, self._codes(_def_field(type="RADIO", options=["a", {"x": 1}])))
        self.assertIn(codes.DEF_INVALID_OPTION, self._codes(_def_field(type="RADIO", options=["a", "b" * 300])))
        c = self._codes(_def_field(type="MATRIX_RADIO", rows=["r", "R"], options=["a", "a"]))
        self.assertIn(codes.DEF_DUPLICATE_OPTION, c)

    def test_scale_span_capped(self):
        c = self._codes(_def_field(type="LINEAR_SCALE", min_value=1, max_value=1000))
        self.assertIn(codes.DEF_SCALE_RANGE_INVALID, c)

    def test_field_count_capped(self):
        c = self._codes(*[_def_field(label=f"Q{i}") for i in range(201)])
        self.assertIn(codes.DEF_TOO_MANY_ITEMS, c)

    def test_profile_field_type_compatibility(self):
        c = self._codes(_def_field(type="CHECKBOX", options=["a", "b"], profile_field="email"))
        self.assertIn(codes.DEF_PROFILE_FIELD_INCOMPATIBLE, c)
        ok = self._codes(_def_field(type="EMAIL", profile_field="email"))
        self.assertNotIn(codes.DEF_PROFILE_FIELD_INCOMPATIBLE, ok)

    def test_duplicate_labels_warn(self):
        w = self._codes(_def_field(label="Name"), _def_field(label=" name "), warnings=True)
        self.assertIn(codes.WARN_DUPLICATE_LABEL, w)

    def test_conditional_cycles_blocked(self):
        a = _def_field(id=1, label="A", conditional_logic={"logic": "AND", "rules": [{"if": 2, "operator": "equals", "value": "x"}]})
        b = _def_field(id=2, label="B", conditional_logic={"logic": "AND", "rules": [{"if": 1, "operator": "equals", "value": "y"}]})
        self.assertIn(codes.DEF_CONDITION_CYCLE, self._codes(a, b))
        c = _def_field(id=3, label="C")
        a_ok = dict(a, conditional_logic={"logic": "AND", "rules": [{"if": 3, "operator": "equals", "value": "x"}]})
        self.assertNotIn(codes.DEF_CONDITION_CYCLE, self._codes(a_ok, b, c))


class SubmissionApiHardeningTests(APITestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def _post(self, form, field, value, key):
        return self.client.post("/api/forms/submissions/", {
            "form": form.id, "answers": [{"field": field.id, "value": value}], "idempotency_key": key,
        }, format="json")

    def test_duplicate_email_check_ignores_case(self):
        form = make_form(status=FormStatus.PUBLISHED, prevent_duplicate_email_answers=True)
        email = add_field(form, FieldType.EMAIL, label="Email", required=True, order=1)
        self.assertEqual(self._post(form, email, "Ravi@SRKR.ac.in", "ci-1").status_code, 201)
        second = self._post(form, email, "ravi@srkr.ac.in", "ci-2")
        self.assertEqual(second.status_code, 400)
        self.assertTrue(any(e.get("code") == "DUPLICATE_EMAIL" for e in second.data.get("errors", [])), second.data)

    def test_form_wide_cap_is_enforced_even_when_status_is_stale(self):
        form = make_form(status=FormStatus.PUBLISHED, max_total_responses=1)
        name = add_field(form, FieldType.TEXT, label="Name", required=True, order=1)
        Response.objects.create(form=form)
        resp = self._post(form, name, "Late", "cap-late")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("code"), "MAX_TOTAL_RESPONSES_REACHED")
        self.assertEqual(Response.objects.filter(form=form).count(), 1)

    def test_null_byte_returns_400_not_500(self):
        form = make_form(status=FormStatus.PUBLISHED)
        name = add_field(form, FieldType.TEXT, label="Name", required=True, order=1)
        resp = self._post(form, name, "bad\u0000name", "nul-1")
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(any(e.get("code") == codes.INVALID_CHARACTERS for e in resp.data.get("errors", [])), resp.data)
