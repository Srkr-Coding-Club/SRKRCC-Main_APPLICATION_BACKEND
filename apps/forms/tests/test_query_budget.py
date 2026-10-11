"""
Query-budget guards: each endpoint must issue the same number of SQL queries
whether a form has 3 responses or 30. A rise with data volume means an N+1
has crept back in.
"""
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from apps.forms.models import Answer, FieldType, Form, FormField, FormStatus, Response

User = get_user_model()


class QueryBudgetTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="budget_admin", email="budget_admin@srkr.ac.in", password="StrongAdminPassword123!", role="ADMIN",
        )
        self.form = Form.objects.create(title="Budget Form", slug="budget-form", status=FormStatus.PUBLISHED)
        self.required = FormField.objects.create(form=self.form, label="Name", type=FieldType.TEXT, order=1, is_required=True)
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.members = []
        self._add_responses(3)

    def _add_responses(self, count):
        for i in range(count):
            member = User.objects.create_user(
                username=f"budget_member_{len(self.members)}",
                email=f"budget_member_{len(self.members)}@srkr.ac.in",
                password="StrongStudentPassword123!",
                role="NON_AFFILIATE",
            )
            self.members.append(member)
            response = Response.objects.create(form=self.form, user=member, is_manual_entry=(i % 2 == 0))
            if i % 3:  # leave every third response's required answer empty
                Answer.objects.create(response=response, field=self.required, value=f"name-{i}")

    def _count_queries(self, method, url, data=None):
        with CaptureQueriesContext(connection) as ctx:
            if method == "GET":
                response = self.client.get(url, data or {})
            else:
                response = self.client.post(url, data or {}, format="json")
        self.assertLess(response.status_code, 400, response.content)
        return len(ctx.captured_queries)

    def _assert_constant_queries(self, method, url, data=None):
        small = self._count_queries(method, url, data)
        self._add_responses(27)
        large = self._count_queries(method, url, data)
        self.assertEqual(small, large, f"{url} query count grew from {small} to {large} with more data")

    def test_data_health_query_count_does_not_grow_with_responses(self):
        self._assert_constant_queries("GET", "/api/forms/data-health/")

    def test_responses_viewer_query_count_does_not_grow_with_responses(self):
        self._assert_constant_queries("GET", f"/api/forms/{self.form.slug}/responses/")

    def test_members_directory_query_count_does_not_grow_with_members(self):
        self._assert_constant_queries("GET", "/api/members/")

    def test_members_directory_has_no_per_member_detail_route(self):
        response = self.client.get(f"/api/members/{self.members[0].pk}/")
        self.assertEqual(response.status_code, 404)

    def test_data_health_counts_responses_with_missing_required_answers(self):
        # setUp: 3 responses; response 0 leaves the required answer empty and is a manual entry.
        response = self.client.get("/api/forms/data-health/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["stats"]["total_responses"], 3)
        self.assertEqual(response.data["stats"]["completion_rate"], 67)
        gap_warnings = [w for w in response.data["warnings"] if w["type"] == "empty_required_fields"]
        self.assertEqual(len(gap_warnings), 1)
        self.assertEqual(gap_warnings[0]["message"], "1 response(s) have empty required fields (manual entries)")
