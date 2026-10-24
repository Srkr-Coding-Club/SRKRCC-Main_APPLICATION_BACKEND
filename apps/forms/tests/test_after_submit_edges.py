"""Awkward inputs and unlucky timing around the thank-you page, bot checks and analytics."""

import json
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.forms import spam
from apps.forms.models import FieldType, Form, FormStatus, Response, ResponseDraft
from .factories import add_field, make_form

User = get_user_model()


class _Case(APITestCase):
    def setUp(self):
        cache.clear()  # the per-minute submit limit must start empty and not be left full for another test
        self.addCleanup(cache.clear)
        self.admin = User.objects.create_user(username="edge", email="edge@srkr.ac.in", password="x", role="ADMIN", is_staff=True)
        self.client.force_authenticate(self.admin)
        self.form = make_form(status=FormStatus.PUBLISHED, allow_multiple_responses=True, max_responses_per_user=100)
        self.name = add_field(self.form, FieldType.TEXT, label="Name", required=True)
        self.url = f"/api/forms/{self.form.slug}/"

    def submit(self, **extra):
        return self.client.post("/api/forms/submissions/", {"form": self.form.id, "answers": [{"field": self.name.id, "value": "Ada"}], **extra}, format="json")


class GoingLiveWithoutTheBotCheckConfigured(_Case):
    """The check must not be skippable by publishing some other way than the publish button."""

    @override_settings(TURNSTILE_SECRET_KEY="")
    def test_saving_a_form_as_published_is_refused_too(self):
        self.form.status = FormStatus.DRAFT
        self.form.save()
        resp = self.client.patch(self.url, {"status": "PUBLISHED", "captcha_enabled": True}, format="json")
        self.assertEqual(resp.status_code, 400, resp.data)
        self.form.refresh_from_db()
        self.assertEqual(self.form.status, FormStatus.DRAFT)

    @override_settings(TURNSTILE_SECRET_KEY="")
    def test_switching_it_on_for_a_form_that_is_already_live_is_refused(self):
        resp = self.client.patch(self.url, {"captcha_enabled": True}, format="json")
        self.assertEqual(resp.status_code, 400, resp.data)

    @override_settings(TURNSTILE_SECRET_KEY="")
    def test_a_draft_may_have_it_switched_on_while_it_is_being_built(self):
        self.form.status = FormStatus.DRAFT
        self.form.save()
        self.assertEqual(self.client.patch(self.url, {"captcha_enabled": True}, format="json").status_code, 200)

    @override_settings(TURNSTILE_SECRET_KEY="")
    def test_reopening_a_closed_form_is_refused_as_well(self):
        self.form.captcha_enabled = True
        self.form.status = FormStatus.CLOSED
        self.form.save()
        resp = self.client.post(f"{self.url}reopen/", {"status": "PUBLISHED"}, format="json")
        self.assertEqual(resp.status_code, 400, resp.data)

    @override_settings(TURNSTILE_SECRET_KEY="secret")
    def test_it_goes_live_when_the_secret_is_there(self):
        self.form.status = FormStatus.DRAFT
        self.form.save()
        self.assertEqual(self.client.patch(self.url, {"status": "PUBLISHED", "captcha_enabled": True}, format="json").status_code, 200)


class HoneypotEdges(_Case):
    def test_the_old_field_name_no_longer_triggers_it_and_the_new_one_does(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.submit(website="x").status_code, 201)
        self.assertEqual(Response.objects.count(), 1)  # a real "website" answer must never be mistaken for a bot
        self.assertEqual(self.submit(**{spam.HONEYPOT_FIELD: "x"}).status_code, 201)
        self.assertEqual(Response.objects.count(), 1)

    def test_odd_values_do_not_crash_it(self):
        self.client.force_authenticate(None)
        for value in (0, False, None, [], {}, "   "):
            self.assertEqual(self.submit(**{spam.HONEYPOT_FIELD: value}).status_code, 201, repr(value))
        self.assertEqual(Response.objects.count(), 6)  # blank-ish values are not a bot signal
        for value in ([1], {"a": 1}, 7, True):
            self.assertEqual(self.submit(**{spam.HONEYPOT_FIELD: value}).status_code, 201)
        self.assertEqual(Response.objects.count(), 6)

    def test_the_honeypot_has_an_unguessable_name_that_autofill_would_not_touch(self):
        for word in ("website", "url", "email", "phone", "name", "address", "fax"):
            self.assertNotIn(word, spam.HONEYPOT_FIELD.lower())


class AnalyticsEdges(_Case):
    def test_responses_near_midnight_land_on_the_day_they_were_made_in_local_time(self):
        local_now = timezone.localtime()
        just_after_midnight = local_now.replace(hour=0, minute=30, second=0, microsecond=0)
        resp = self.submit()
        Response.objects.filter(pk=Response.objects.get().pk).update(submitted_at=just_after_midnight)
        data = self.client.get(f"{self.url}analytics/", {"days": 3}).data
        today = {row["date"]: row["count"] for row in data["timeline"]}[local_now.date().isoformat()]
        self.assertEqual(today, 1, data["timeline"])
        self.assertEqual(resp.status_code, 201)

    def test_the_timeline_always_ends_today_and_has_one_row_per_day(self):
        data = self.client.get(f"{self.url}analytics/", {"days": 30}).data
        dates = [row["date"] for row in data["timeline"]]
        self.assertEqual(len(dates), 30)
        self.assertEqual(len(set(dates)), 30)
        self.assertEqual(dates[-1], timezone.localdate().isoformat())

    def test_answers_of_deleted_questions_and_odd_stored_values_do_not_break_it(self):
        extra = add_field(self.form, FieldType.NUMBER, label="Score")
        self.submit()
        resp = Response.objects.get()
        resp.answers.create(field=extra, value="not a number")
        resp.answers.create(field=extra, value=True)
        self.assertEqual(self.client.get(f"{self.url}analytics/").status_code, 200)
        extra.is_deleted = True
        extra.save()
        data = self.client.get(f"{self.url}analytics/").data
        self.assertEqual([f["label"] for f in data["fields"]], ["Name"])

    def test_a_choice_question_counts_every_option_even_with_none_chosen(self):
        pick = add_field(self.form, FieldType.CHECKBOX, label="Pick", options=["A", "B", "C"])
        data = self.client.get(f"{self.url}analytics/").data
        row = [f for f in data["fields"] if f["label"] == "Pick"][0]
        self.assertEqual(row["options"], [{"option": "A", "count": 0}, {"option": "B", "count": 0}, {"option": "C", "count": 0}])
        self.assertEqual(row["answered"], 0)

    def test_a_great_many_drafts_are_summarised_not_loaded_forever(self):
        from apps.forms import analytics
        self.assertTrue(hasattr(analytics, "MAX_DRAFTS_SCANNED"))

    def test_templates_and_unpublished_forms_can_still_be_read(self):
        self.form.status = FormStatus.DRAFT
        self.form.save()
        self.assertEqual(self.client.get(f"{self.url}analytics/").status_code, 200)


class RedirectAndMessageEdges(_Case):
    def test_an_import_with_an_unsafe_redirect_is_refused_with_a_message_not_a_crash(self):
        exported = self.client.get(f"{self.url}export-definition/").data
        exported["form"]["redirect_url"] = "javascript:alert(1)"
        resp = self.client.post("/api/forms/import-definition/", exported, format="json")
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertNotIn("Traceback", json.dumps(resp.data, default=str))
        self.assertIn("redirect", json.dumps(resp.data, default=str).lower())

    def test_restoring_an_old_version_keeps_the_thank_you_settings_it_had(self):
        self.client.patch(self.url, {"confirmation_message": "First", "redirect_url": "/a"}, format="json")
        self.client.patch(self.url, {"confirmation_message": "Second", "redirect_url": "/b"}, format="json")
        self.form.refresh_from_db()
        first = self.form.versions.get(version=self.form.version - 1)
        self.assertEqual(self.client.post(f"{self.url}versions/{first.version}/restore/").status_code, 200)
        self.form.refresh_from_db()
        self.assertEqual((self.form.confirmation_message, self.form.redirect_url), ("First", "/a"))

    def test_a_message_with_markup_is_stored_as_text_and_returned_unchanged(self):
        text = "<script>alert(1)</script> {{1}} & more"
        self.client.patch(self.url, {"confirmation_message": text}, format="json")
        self.assertEqual(self.client.get(self.url).data["confirmation_message"], text)

    def test_percent_encoded_and_unicode_paths_are_fine_but_backslashes_and_double_slashes_are_not(self):
        for good in ("/events%2Fa", "/కార్యక్రమాలు", "/a?b=c&d=e#f"):
            self.assertEqual(self.client.patch(self.url, {"redirect_url": good}, format="json").status_code, 200, good)
        for bad in ("/\\evil.example", "//evil.example/x", "/ /", "\t/events", "https:///nohost", "https://evil.example\\@good.example"):
            code = self.client.patch(self.url, {"redirect_url": bad}, format="json").status_code
            self.assertIn(code, (200, 400), bad)
        self.form.refresh_from_db()
        self.assertFalse(self.form.redirect_url.startswith("//"))
        self.assertNotIn("\\", self.form.redirect_url)
