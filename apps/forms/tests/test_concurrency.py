"""Many people submitting at the same instant: the limits must hold (real threads, real commits)."""

from concurrent.futures import ThreadPoolExecutor

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import TransactionTestCase
from rest_framework.test import APIClient

from apps.forms.models import FieldType, FormStatus, Response
from .factories import add_field, make_form

User = get_user_model()
RACERS = 8


class SimultaneousSubmissions(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def _race(self, form, answers_for):
        """Submit ``RACERS`` times at once (each as a different signed-in person); returns the status codes."""
        users = [User.objects.create_user(username=f"racer{i}", email=f"racer{i}@srkr.ac.in", password="x") for i in range(RACERS)]

        def go(i):
            try:
                client = APIClient()
                client.force_authenticate(users[i])
                return client.post("/api/forms/submissions/", {"form": form.id, "answers": answers_for(i)}, format="json").status_code
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=RACERS) as pool:
            return list(pool.map(go, range(RACERS)))

    def test_the_form_wide_cap_is_never_exceeded(self):
        form = make_form(status=FormStatus.PUBLISHED, max_total_responses=3)
        name = add_field(form, FieldType.TEXT, label="Name", required=True)
        codes = self._race(form, lambda i: [{"field": name.id, "value": f"P{i}"}])
        self.assertEqual(Response.objects.filter(form=form).count(), 3, codes)
        self.assertEqual(codes.count(201), 3, codes)
        self.assertTrue(all(c in (201, 400, 429) for c in codes), codes)

    def test_a_limited_option_is_never_oversold(self):
        form = make_form(status=FormStatus.PUBLISHED)
        slot = add_field(form, FieldType.RADIO, label="Slot", options=["Morning", "Evening"], required=True,
                         validation_rules={"optionLimits": {"Morning": 2}})
        self._race(form, lambda i: [{"field": slot.id, "value": "Morning"}])
        taken = sum(1 for r in Response.objects.filter(form=form) for a in r.answers.all() if a.value == "Morning")
        self.assertEqual(taken, 2)

    def test_a_unique_answer_is_stored_once(self):
        form = make_form(status=FormStatus.PUBLISHED)
        code = add_field(form, FieldType.TEXT, label="Code", required=True, validation_rules={"unique": True})
        self._race(form, lambda i: [{"field": code.id, "value": "SAME-CODE"}])
        self.assertEqual(Response.objects.filter(form=form).count(), 1)

    def test_one_person_double_submitting_a_single_response_form_gets_one_response(self):
        form = make_form(status=FormStatus.PUBLISHED)
        name = add_field(form, FieldType.TEXT, label="Name", required=True)
        user = User.objects.create_user(username="twice", email="twice@srkr.ac.in", password="x")

        def go(_):
            try:
                client = APIClient()
                client.force_authenticate(user)
                return client.post("/api/forms/submissions/", {"form": form.id, "answers": [{"field": name.id, "value": "x"}]}, format="json").status_code
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=RACERS) as pool:
            list(pool.map(go, range(RACERS)))
        self.assertEqual(Response.objects.filter(form=form, user=user).count(), 1)
