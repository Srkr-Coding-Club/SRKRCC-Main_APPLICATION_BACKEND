"""
Query-budget guard for the admin team listing. Members, pending invites and round
entries are prefetched, so the query count must not grow with the number of teams.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from apps.hackathons.models import Hackathon, InviteStatus, Team, TeamInvite, TeamMember

User = get_user_model()


class TeamListQueryBudgetTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="team_budget_admin", email="team_budget_admin@srkr.ac.in",
            password="StrongAdminPassword123!", role="ADMIN",
        )
        now = timezone.now()
        self.hackathon = Hackathon.objects.create(
            title="Budget Hack", slug="budget-hack", theme="AI", description="d",
            start_date=now, end_date=now + timedelta(days=30),
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self._add_teams(2)

    def _new_user(self):
        index = User.objects.count()
        return User.objects.create_user(
            username=f"team_budget_user_{index}", email=f"team_budget_user_{index}@srkr.ac.in",
            password="StrongStudentPassword123!", role="NON_AFFILIATE",
        )

    def _add_teams(self, count):
        for _ in range(count):
            leader = self._new_user()
            team = Team.objects.create(hackathon=self.hackathon, name=f"Team {leader.pk}", leader=leader)
            TeamMember.objects.create(team=team, hackathon=self.hackathon, user=leader)
            invitee = self._new_user()
            TeamInvite.objects.create(
                team=team, hackathon=self.hackathon, invited_user=invitee, invited_by=leader,
                status=InviteStatus.PENDING,
            )

    def _count_queries(self):
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(f"/api/hackathons/{self.hackathon.slug}/teams/")
        self.assertEqual(response.status_code, 200, response.content)
        return len(ctx.captured_queries)

    def test_team_listing_query_count_does_not_grow_with_teams(self):
        small = self._count_queries()
        self._add_teams(10)
        large = self._count_queries()

        self.assertEqual(small, large, f"team listing query count grew from {small} to {large}")

    def test_team_listing_still_returns_pending_invites(self):
        response = self.client.get(f"/api/hackathons/{self.hackathon.slug}/teams/")

        self.assertEqual(len(response.data), 2)
        for team in response.data:
            self.assertEqual(len(team["pending_invites"]), 1)
            self.assertEqual(team["member_count"], 1)
