from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.audit.models import AuditLog
from apps.codequest.models import Problem, Submission

User = get_user_model()


class AuditLogPaginationTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="page_admin", email="page_admin@srkr.ac.in", first_name="Page", last_name="Admin",
            password="StrongAdminPassword123!", role="ADMIN",
        )
        AuditLog.objects.bulk_create([
            AuditLog(actor=self.admin, action="BULK", target_model="Event", target_id=str(i)) for i in range(60)
        ])
        AuditLog.objects.create(actor=self.admin, action="NEEDLE-ACTION", target_model="Hackathon", target_id="77")
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def test_audit_list_returns_one_page_with_total_count(self):
        response = self.client.get("/api/audit/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 61)
        self.assertEqual(len(response.data["results"]), 50)
        self.assertIsNotNone(response.data["next"])

    def test_audit_list_second_page_holds_the_remainder(self):
        response = self.client.get("/api/audit/", {"page": 2})

        self.assertEqual(len(response.data["results"]), 11)
        self.assertIsNone(response.data["next"])

    def test_audit_search_matches_across_the_whole_log(self):
        response = self.client.get("/api/audit/", {"search": "needle-action"})

        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["action"], "NEEDLE-ACTION")


class CodeQuestSubmissionPaginationTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="cq_admin", email="cq_admin@srkr.ac.in", password="StrongAdminPassword123!", role="ADMIN",
        )
        self.solver = User.objects.create_user(
            username="cq_solver", email="cq_solver@srkr.ac.in", password="StrongStudentPassword123!", role="NON_AFFILIATE",
        )
        self.other = User.objects.create_user(
            username="cq_other", email="cq_other@srkr.ac.in", password="StrongStudentPassword123!", role="NON_AFFILIATE",
        )
        today = timezone.localdate()
        self.problems = [
            Problem.objects.create(title=f"CQ {i}", slug=f"cq-{i}", statement="s", scheduled_date=today - timedelta(days=i))
            for i in range(3)
        ]
        Submission.objects.create(problem=self.problems[0], user=self.solver, code="a", is_correct=True)
        Submission.objects.create(problem=self.problems[1], user=self.solver, code="b", is_correct=True)
        Submission.objects.create(problem=self.problems[2], user=self.solver, code="c", is_correct=False)
        Submission.objects.create(problem=self.problems[2], user=self.other, code="d", is_correct=True)
        self.client = APIClient()

    def test_admin_listing_is_paginated_with_an_accepted_total(self):
        self.client.force_authenticate(self.admin)

        response = self.client.get("/api/codequest/submissions/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 4)
        self.assertEqual(response.data["accepted_count"], 3)
        self.assertEqual(len(response.data["results"]), 4)

    def test_solved_dates_lists_only_the_callers_accepted_problems(self):
        self.client.force_authenticate(self.solver)

        response = self.client.get("/api/codequest/submissions/solved-dates/")

        self.assertEqual(response.status_code, 200)
        expected = sorted([self.problems[0].scheduled_date, self.problems[1].scheduled_date])
        self.assertEqual(response.data["dates"], expected)

    def test_solved_dates_requires_authentication(self):
        response = self.client.get("/api/codequest/submissions/solved-dates/")

        self.assertIn(response.status_code, (401, 403))
