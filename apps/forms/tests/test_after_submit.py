"""Thank-you page and redirect settings, bot protection and analytics."""

import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.forms import spam
from apps.forms.models import FieldType, Form, FormStatus, Response, ResponseDraft
from .factories import add_field, make_form

User = get_user_model()


def _admin():
    return User.objects.create_user(username="adm5", email="adm5@srkr.ac.in", password="x", role="ADMIN", is_staff=True)


class _Case(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.admin = _admin()
        self.client.force_authenticate(self.admin)
        self.form = make_form(status=FormStatus.PUBLISHED, allow_multiple_responses=True, max_responses_per_user=100)
        self.name = add_field(self.form, FieldType.TEXT, label="Name", required=True)
        self.track = add_field(self.form, FieldType.RADIO, label="Track", options=["AI", "Web"])
        self.score = add_field(self.form, FieldType.NUMBER, label="Score")
        self.url = f"/api/forms/{self.form.slug}/"

    def submit(self, name="Ada", track="AI", score=None, **extra):
        answers = [{"field": self.name.id, "value": name}, {"field": self.track.id, "value": track}]
        if score is not None:
            answers.append({"field": self.score.id, "value": str(score)})
        return self.client.post("/api/forms/submissions/", {"form": self.form.id, "answers": answers, **extra}, format="json")

    def patch(self, **data):
        return self.client.patch(self.url, data, format="json")


class ThankYouSettingsTests(_Case):
    def test_message_redirect_and_delay_are_saved_and_shown_publicly(self):
        resp = self.patch(confirmation_message="  See you, {{%d}}!  " % self.name.id, redirect_url="/events", redirect_delay_seconds=3)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.form.refresh_from_db()
        self.assertEqual(self.form.confirmation_message, "See you, {{%d}}!" % self.name.id)
        public = self.client.get(self.url).data
        self.assertEqual((public["redirect_url"], public["redirect_delay_seconds"]), ("/events", 3))

    def test_only_safe_redirects_are_accepted(self):
        for bad in ("javascript:alert(1)", "//evil.example", "http://plain.example", "data:text/html,x", "/a b", "https://user:pw@x.example", "ftp://x.example", "/\\evil"):
            resp = self.patch(redirect_url=bad)
            self.assertEqual(resp.status_code, 400, bad)
        for good in ("https://example.org/thanks?x=1", "/events/42", ""):
            self.assertEqual(self.patch(redirect_url=good).status_code, 200, good)

    def test_the_form_language_is_one_of_the_supported_ones(self):
        self.assertEqual(self.client.get(self.url).data["language"], "en")
        for code in ("te", "hi", "en"):
            self.assertEqual(self.patch(language=code).status_code, 200, code)
            self.assertEqual(self.client.get(self.url).data["language"], code)
        for bad in ("fr", "", "english"):
            self.assertEqual(self.patch(language=bad).status_code, 400, bad)
        self.assertEqual(self.patch(language=" HI ").status_code, 200)
        self.form.refresh_from_db()
        self.assertEqual(self.form.language, "hi")

    def test_delay_and_message_are_bounded(self):
        self.assertEqual(self.patch(redirect_delay_seconds=31).status_code, 400)
        self.assertEqual(self.patch(confirmation_message="x" * 2001).status_code, 400)

    def test_changes_show_up_in_the_history_and_survive_a_copy(self):
        self.patch(confirmation_message="Thanks!", redirect_url="/events")
        self.form.refresh_from_db()
        latest = self.form.versions.first().snapshot["form"]
        self.assertEqual((latest["confirmation_message"], latest["redirect_url"]), ("Thanks!", "/events"))
        copy = self.client.post(f"{self.url}duplicate/", {}, format="json")
        self.assertEqual(copy.status_code, 201, copy.data)
        self.assertEqual(Form.objects.get(slug=copy.data["slug"]).redirect_url, "/events")

    def test_old_snapshots_without_the_new_settings_do_not_look_changed(self):
        from apps.forms import versions
        old = versions.snapshot_form(self.form)
        old["form"].pop("redirect_url")
        diff = versions.diff_snapshots(old, versions.snapshot_form(self.form))
        self.assertEqual(diff["form"], [])


class BotProtectionTests(_Case):
    def test_a_filled_honeypot_looks_like_success_but_saves_nothing(self):
        self.client.force_authenticate(None)
        resp = self.submit(**{spam.HONEYPOT_FIELD: "http://spam.example"})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(Response.objects.count(), 0)
        self.assertEqual(self.submit(**{spam.HONEYPOT_FIELD: ""}).status_code, 201)
        self.assertEqual(Response.objects.count(), 1)

    @override_settings(TURNSTILE_SECRET_KEY="")
    def test_a_form_asking_for_the_check_without_a_secret_is_not_published_or_submittable(self):
        self.form.captcha_enabled = True
        self.form.status = FormStatus.DRAFT
        self.form.save()
        resp = self.client.post(f"{self.url}publish/")
        self.assertEqual((resp.status_code, resp.data["code"]), (400, "CAPTCHA_NOT_CONFIGURED"))

        self.form.status = FormStatus.PUBLISHED
        self.form.save()
        self.client.force_authenticate(None)
        resp = self.submit()
        self.assertEqual((resp.status_code, resp.data["code"]), (503, "CAPTCHA_NOT_CONFIGURED"))

    @override_settings(TURNSTILE_SECRET_KEY="secret")
    def test_the_token_is_checked_with_turnstile(self):
        self.form.captcha_enabled = True
        self.form.save()
        self.client.force_authenticate(None)
        cases = [(spam.MISSING, 400), (spam.FAILED, 400), (spam.UNAVAILABLE, 503), (spam.OK, 201)]
        for verdict, expected in cases:
            with mock.patch.object(spam, "verify_captcha", return_value=verdict) as check:
                resp = self.submit(**{spam.CAPTCHA_TOKEN_FIELD: "tok"})
            self.assertEqual(resp.status_code, expected, verdict)
            check.assert_called_once()
            self.assertEqual(check.call_args.args[0], "tok")
        self.assertEqual(Response.objects.count(), 1)

    @override_settings(TURNSTILE_SECRET_KEY="secret")
    def test_staff_are_not_asked_and_a_form_without_the_check_never_calls_turnstile(self):
        with mock.patch.object(spam, "verify_captcha") as check:
            self.form.captcha_enabled = True
            self.form.save()
            self.assertEqual(self.submit().status_code, 201)  # the admin
            self.form.captcha_enabled = False
            self.form.save()
            self.client.force_authenticate(None)
            self.assertEqual(self.submit().status_code, 201)
        check.assert_not_called()

    @override_settings(TURNSTILE_SECRET_KEY="secret")
    def test_verify_handles_a_bad_reply_and_an_unreachable_service_without_raising(self):
        class Reply:
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n): return self.body

        with mock.patch("urllib.request.urlopen", return_value=Reply(b'{"success": true}')):
            self.assertEqual(spam.verify_captcha("t"), spam.OK)
        with mock.patch("urllib.request.urlopen", return_value=Reply(b'{"success": false}')):
            self.assertEqual(spam.verify_captcha("t"), spam.FAILED)
        with mock.patch("urllib.request.urlopen", return_value=Reply(b"not json")):
            self.assertEqual(spam.verify_captcha("t"), spam.UNAVAILABLE)
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertEqual(spam.verify_captcha("t"), spam.UNAVAILABLE)
        self.assertEqual(spam.verify_captcha(""), spam.MISSING)
        self.assertEqual(spam.verify_captcha("x" * 5000), spam.FAILED)


class AnalyticsTests(_Case):
    def setUp(self):
        super().setUp()
        self.url_a = f"{self.url}analytics/"

    def test_counts_options_numbers_and_timeline(self):
        self.submit(name="A", track="AI", score=4)
        self.submit(name="B", track="AI", score=8)
        self.submit(name="C", track="Web")
        Response.objects.create(form=self.form, is_test_submission=True)
        data = self.client.get(self.url_a, {"days": 7}).data
        self.assertEqual((data["total_responses"], data["truncated"], data["days"]), (3, False, 7))
        self.assertEqual(sum(d["count"] for d in data["timeline"]), 3)
        self.assertEqual(len(data["timeline"]), 7)
        by_label = {f["label"]: f for f in data["fields"]}
        self.assertEqual(by_label["Track"]["options"], [{"option": "AI", "count": 2}, {"option": "Web", "count": 1}])
        self.assertEqual(by_label["Score"]["answered"], 2)
        self.assertAlmostEqual(by_label["Score"]["answer_rate"], round(2 / 3, 4))
        self.assertEqual((by_label["Score"]["numeric"]["min"], by_label["Score"]["numeric"]["max"], by_label["Score"]["numeric"]["average"]), (4.0, 8.0, 6.0))

    def test_an_empty_form_and_nonsense_days_are_fine(self):
        data = self.client.get(self.url_a, {"days": "abc"}).data
        self.assertEqual((data["total_responses"], data["days"], data["first_response_at"]), (0, 30, None))
        self.assertEqual(self.client.get(self.url_a, {"days": 100000}).data["days"], 365)

    def test_unfinished_drafts_show_where_people_stopped(self):
        users = [User.objects.create_user(username=f"d{i}", email=f"d{i}@srkr.ac.in", password="x") for i in range(3)]
        ResponseDraft.objects.create(form=self.form, user=users[0], answers={str(self.name.id): "x"})
        ResponseDraft.objects.create(form=self.form, user=users[1], answers={str(self.name.id): "x"})
        ResponseDraft.objects.create(form=self.form, user=users[2], answers={str(self.name.id): "x", str(self.track.id): "AI"})
        stopped = self.client.get(self.url_a).data["unfinished"]
        self.assertEqual(stopped["count"], 3)
        self.assertEqual([(s["label"], s["count"]) for s in stopped["stopped_after"]], [("Name", 2), ("Track", 1)])

    def test_only_staff_can_read_it(self):
        member = User.objects.create_user(username="mem6", email="mem6@srkr.ac.in", password="x")
        self.client.force_authenticate(member)
        self.assertIn(self.client.get(self.url_a).status_code, (403, 404))
        self.client.force_authenticate(None)
        self.assertIn(self.client.get(self.url_a).status_code, (401, 403))
