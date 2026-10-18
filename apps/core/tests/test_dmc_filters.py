import datetime as dt

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.career.models import JobListing
from apps.core.dmc.adapters.forms import FormIndividualAdapter
from apps.core.dmc.contracts import FilterClause
from apps.core.dmc.registry import get_dataset_registry
from apps.events.models import Event, EventStatus
from apps.forms.models import Form, FormField, FieldType, FormStatus, Response
from apps.hackathons.models import (
    EntryStatus, Hackathon, HackathonStatus, Round, RoundEntry, Submission, Team, TeamMember, TeamStatus,
)

User = get_user_model()

SAMPLE_VALUES = {
    "text": "a", "select": "1", "boolean": "true", "number_range": 1, "date_range": "2025-01-01",
}


class DMCFilterTestCase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="dmc_filter_admin", email="dmc_filter_admin@srkr.ac.in",
            first_name="Admin", last_name="User", password="StrongAdminPassword123!", role="ADMIN",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def query(self, dataset, *filters, expect=200):
        body = {"page_size": 100, "filters": [{"field": f, "operator": o, "value": v} for f, o, v in filters]}
        response = self.client.post(f"/api/admin/dmc/datasets/{dataset}/query/", data=body, format="json")
        self.assertEqual(response.status_code, expect, response.content)
        return response.json()

    def ids(self, dataset, *filters, key="id"):
        return {record[key]["value"] for record in self.query(dataset, *filters)["records"]}

    def user(self, name, **extra):
        first, _, last = name.partition(" ")
        return User.objects.create_user(
            username=name.replace(" ", "_").lower(), email=f"{first.lower()}@srkr.ac.in",
            first_name=first, last_name=last, password="StrongPassword123!", **extra,
        )


class FilterContractTests(DMCFilterTestCase):
    """Every filter a dataset advertises must be accepted by the backend with every operator it lists."""

    def test_every_advertised_operator_is_accepted(self):
        for dataset_id in get_dataset_registry():
            schema = self.client.get(f"/api/admin/dmc/datasets/{dataset_id}/schema/")
            self.assertEqual(schema.status_code, 200, dataset_id)
            for definition in schema.json()["filters"]:
                for operator in definition["operators"]:
                    if operator in ("empty", "not_empty"):
                        value = ""
                    elif operator == "in":
                        value = ["1"]
                    elif definition["type"] == "select" and definition["options"]:
                        value = definition["options"][0]["value"]
                    else:
                        value = SAMPLE_VALUES[definition["type"]]
                    with self.subTest(dataset=dataset_id, filter=definition["key"], operator=operator):
                        self.query(dataset_id, (definition["key"], operator, value))

    def test_unknown_filter_is_rejected_not_ignored(self):
        response = self.query("users", ("password", "eq", "x"), expect=400)
        self.assertIn("cannot be filtered", response["error"])

    def test_unsupported_operator_is_rejected(self):
        self.query("users", ("role", "contains", "ADM"), expect=400)

    def test_unparseable_values_are_rejected(self):
        self.query("users", ("year", "gte", "abc"), expect=400)
        self.query("users", ("registered_at", "gte", "not-a-date"), expect=400)
        self.query("forms_all", ("is_manual_entry", "eq", "maybe"), expect=400)
        self.query("users", ("name", "contains", ""), expect=400)

    def test_export_rejects_bad_filters(self):
        response = self.client.post(
            "/api/admin/dmc/datasets/users/export/",
            data={"format": "csv", "filters": [{"field": "nope", "operator": "eq", "value": "x"}]},
            format="json",
        )
        self.assertEqual(response.status_code, 400)


class UserFilterTests(DMCFilterTestCase):
    def test_name_matches_full_name_and_registration_date_is_inclusive(self):
        asha = self.user("Asha Rao", year=2, branch="CSE")
        self.user("Ravi Kumar", year=3, branch="IT")
        User.objects.filter(pk=asha.pk).update(registered_at=timezone.localtime().replace(hour=23, minute=30))
        today = timezone.localdate().isoformat()

        self.assertEqual(self.ids("users", ("name", "contains", "asha rao")), {str(asha.pk)})
        self.assertEqual(self.ids("users", ("year", "gte", 3), ("branch", "in", ["IT", "ECE"])).isdisjoint({str(asha.pk)}), True)
        self.assertIn(str(asha.pk), self.ids("users", ("registered_at", "lte", today)))
        self.assertIn(str(asha.pk), self.ids("users", ("registered_at", "eq", today)))


class FormFilterTests(DMCFilterTestCase):
    def setUp(self):
        super().setUp()
        self.open_form = Form.objects.create(title="Open Form", slug="open-form", status=FormStatus.PUBLISHED, category="Workshop")
        self.closed_form = Form.objects.create(title="Closed Form", slug="closed-form", status=FormStatus.CLOSED, category="Survey")
        self.open_response = Response.objects.create(form=self.open_form, user=self.user("Open Person"))
        self.closed_response = Response.objects.create(form=self.closed_form, user=self.user("Closed Person"))

    def test_filter_by_form_status(self):
        self.assertEqual(self.ids("forms_all", ("form_status", "eq", "PUBLISHED")), {str(self.open_response.pk)})
        self.assertEqual(self.ids("forms_all", ("form_status", "eq", "CLOSED")), {str(self.closed_response.pk)})
        self.assertEqual(self.ids("forms_all", ("form_status", "neq", "CLOSED")), {str(self.open_response.pk)})
        self.assertEqual(
            self.ids("forms_all", ("form_status", "in", ["PUBLISHED", "CLOSED"])),
            {str(self.open_response.pk), str(self.closed_response.pk)},
        )

    def test_filter_by_form_and_category(self):
        self.assertEqual(self.ids("forms_all", ("form_title", "eq", self.closed_form.pk)), {str(self.closed_response.pk)})
        self.assertEqual(self.ids("forms_all", ("form_category", "eq", "Survey")), {str(self.closed_response.pk)})

    def test_status_is_listed_in_schema_with_open_label(self):
        schema = self.client.get("/api/admin/dmc/datasets/forms_all/schema/").json()
        status_filter = next(f for f in schema["filters"] if f["key"] == "form_status")
        self.assertIn({"label": "Open (Published)", "value": "PUBLISHED"}, status_filter["options"])
        self.assertIn("form_status", {c["key"] for c in schema["columns"]})

    def test_single_form_answer_filters(self):
        field = FormField.objects.create(form=self.open_form, label="City", type=FieldType.TEXT, order=1)
        self.open_response.answers.create(field=field, value="Hyderabad")
        adapter = FormIndividualAdapter(self.open_form.pk)
        self.assertIn(f"q_{field.pk}", {f.key for f in adapter.get_schema(self.admin)[1]})

        def matching(operator, value):
            clause = FilterClause(field=f"q_{field.pk}", operator=operator, value=value)
            return set(adapter._apply_filters(Response.objects.filter(form=self.open_form), [clause]))

        self.assertEqual(matching("contains", "hyder"), {self.open_response})
        self.assertEqual(matching("starts_with", "hyd"), {self.open_response})
        self.assertEqual(matching("eq", "hyderabad"), {self.open_response})
        self.assertEqual(matching("neq", "Hyderabad"), set())
        self.assertEqual(matching("not_empty", ""), {self.open_response})


class EventFilterTests(DMCFilterTestCase):
    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.upcoming = self._event("Upcoming Meetup", EventStatus.LIVE, now + dt.timedelta(days=5), "Talk")
        self.past = self._event("Past Workshop", EventStatus.CLOSED, now - dt.timedelta(days=5), "Workshop")

    def _event(self, title, status, start, category):
        form = Form.objects.create(title=f"{title} form", slug=f"{title.lower().replace(' ', '-')}-form", status=FormStatus.PUBLISHED)
        event = Event.objects.create(
            title=title, slug=title.lower().replace(" ", "-"), description="d", status=status,
            start_time=start, category=category, registration_form=form,
        )
        event.response = Response.objects.create(form=form, user=self.user(title))
        return event

    def test_open_and_closed_events(self):
        self.assertEqual(self.ids("event_registrations", ("event_status", "eq", "LIVE")), {str(self.upcoming.response.pk)})
        self.assertEqual(self.ids("event_registrations", ("event_status", "eq", "CLOSED")), {str(self.past.response.pk)})

    def test_upcoming_and_past(self):
        self.assertEqual(self.ids("event_registrations", ("event_timing", "eq", "upcoming")), {str(self.upcoming.response.pk)})
        self.assertEqual(self.ids("event_registrations", ("event_timing", "eq", "past")), {str(self.past.response.pk)})

    def test_by_event_category_and_negation(self):
        self.assertEqual(self.ids("event_registrations", ("event_title", "eq", self.past.pk)), {str(self.past.response.pk)})
        self.assertEqual(self.ids("event_registrations", ("event_category", "eq", "Talk")), {str(self.upcoming.response.pk)})
        self.assertEqual(self.ids("event_registrations", ("event_title", "neq", self.past.pk)), {str(self.upcoming.response.pk)})


class CareerFilterTests(DMCFilterTestCase):
    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.open_job = self._job("Open Role", now + dt.timedelta(days=3))
        self.expired_job = self._job("Old Role", now - dt.timedelta(days=3))

    def _job(self, title, deadline):
        form = Form.objects.create(title=f"{title} form", slug=f"{title.lower().replace(' ', '-')}-form", status=FormStatus.PUBLISHED)
        job = JobListing.objects.create(
            title=title, slug=title.lower().replace(" ", "-"), company_name="Acme", description="d",
            deadline=deadline, application_form=form,
        )
        job.response = Response.objects.create(form=form, user=self.user(title))
        return job

    def test_open_and_expired_jobs(self):
        self.assertEqual(self.ids("career_jobs", ("job_status", "eq", "open")), {str(self.open_job.pk)})
        self.assertEqual(self.ids("career_jobs", ("job_status", "eq", "expired")), {str(self.expired_job.pk)})

    def test_applications_follow_job_status(self):
        self.assertEqual(self.ids("career_applications", ("job_status", "eq", "open")), {str(self.open_job.response.pk)})
        self.assertEqual(self.ids("career_applications", ("job", "eq", self.expired_job.pk)), {str(self.expired_job.response.pk)})


class HackathonFilterTests(DMCFilterTestCase):
    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.live = self._hackathon("Live Hack", HackathonStatus.LIVE, now)
        self.closed = self._hackathon("Closed Hack", HackathonStatus.CLOSED, now)

    def _hackathon(self, title, status, now):
        hackathon = Hackathon.objects.create(
            title=title, slug=title.lower().replace(" ", "-"), theme="t", description="d", status=status,
            start_date=now, end_date=now + dt.timedelta(days=2),
        )
        leader = self.user(f"{title} Lead")
        hackathon.team = Team.objects.create(hackathon=hackathon, name=f"{title} Team", leader=leader)
        TeamMember.objects.create(team=hackathon.team, user=leader, role="LEADER")
        hackathon.round = Round.objects.create(hackathon=hackathon, name="Screening", order=1)
        hackathon.entry = RoundEntry.objects.create(round=hackathon.round, team=hackathon.team)
        return hackathon

    def test_hackathon_status_on_every_hackathon_dataset(self):
        for dataset, key in (("hackathon_teams", "team_name"), ("hackathon_participants", "team_name"), ("hackathon_round_entries", "team_name")):
            with self.subTest(dataset=dataset):
                self.assertEqual(self.ids(dataset, ("hackathon_status", "eq", "CLOSED"), key=key), {"Closed Hack Team"})
                self.assertEqual(self.ids(dataset, ("hackathon_status", "eq", "LIVE"), key=key), {"Live Hack Team"})

    def test_filter_by_hackathon_with_negation(self):
        self.assertEqual(self.ids("hackathon_teams", ("hackathon_title", "eq", self.live.pk), key="team_name"), {"Live Hack Team"})
        self.assertEqual(self.ids("hackathon_participants", ("hackathon_title", "neq", self.live.pk), key="team_name"), {"Closed Hack Team"})

    def test_team_status_submission_and_round_result(self):
        Team.objects.filter(pk=self.closed.team.pk).update(status=TeamStatus.DISQUALIFIED)
        Submission.objects.create(team=self.live.team, project_title="P", description="d", repo_url="https://example.com/r")
        RoundEntry.objects.filter(pk=self.live.entry.pk).update(status=EntryStatus.SHORTLISTED)

        self.assertEqual(self.ids("hackathon_teams", ("team_status", "eq", "DISQUALIFIED"), key="team_name"), {"Closed Hack Team"})
        self.assertEqual(self.ids("hackathon_teams", ("has_submission", "eq", "true"), key="team_name"), {"Live Hack Team"})
        self.assertEqual(self.ids("hackathon_teams", ("has_submission", "eq", "false"), key="team_name"), {"Closed Hack Team"})
        self.assertEqual(self.ids("hackathon_round_entries", ("status", "eq", "SHORTLISTED"), key="team_name"), {"Live Hack Team"})
        self.assertEqual(self.ids("hackathon_participants", ("team_role", "eq", "Leader"), key="team_name"), {"Live Hack Team", "Closed Hack Team"})
