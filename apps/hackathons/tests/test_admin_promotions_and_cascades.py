from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.hackathons.models import (
    EntryStatus, Hackathon, ProblemStatement, Round, RoundEntry, Team, TeamMember, TeamRole, TeamStatus,
)
from apps.hackathons.tests.helpers import make_user

User = get_user_model()


class AdminPromotionsAndCascadesTests(APITestCase):

    def setUp(self):
        self.admin = make_user(role='ADMIN')
        self.client.force_authenticate(user=self.admin)
        self.now = timezone.now()
        self.hackathon = Hackathon.objects.create(
            title='IconCoders 2026', slug='iconcoders-2026',
            start_date=self.now, end_date=self.now + timezone.timedelta(days=2),
            theme='AI', description='Flagship hackathon',
        )
        self.ps1 = ProblemStatement.objects.create(
            hackathon=self.hackathon, code='PS-001', title='Waste Management',
            description='Smart bins', domain='IoT', is_active=True,
        )
        self.user1 = make_user(email='u1@srkr.ac.in', username='u1')
        self.user2 = make_user(email='u2@srkr.ac.in', username='u2')
        self.user3 = make_user(email='u3@srkr.ac.in', username='u3')

    def test_admin_create_team_with_leader_email(self):
        url = f'/api/hackathons/{self.hackathon.slug}/teams/'
        resp = self.client.post(url, {
            'name': 'Alpha Builders',
            'leader_email': self.user1.email,
            'problem_statement': self.ps1.id,
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        team = Team.objects.get(name='Alpha Builders')
        self.assertEqual(team.leader, self.user1)
        self.assertTrue(TeamMember.objects.filter(team=team, user=self.user1, role=TeamRole.LEADER).exists())
        self.assertEqual(team.problem_statement, self.ps1)

    def test_admin_delete_team_cascades(self):
        team = Team.objects.create(hackathon=self.hackathon, name='Beta Coders', leader=self.user1, status=TeamStatus.REGISTERED)
        TeamMember.objects.create(team=team, hackathon=self.hackathon, user=self.user1, role=TeamRole.LEADER)
        TeamMember.objects.create(team=team, hackathon=self.hackathon, user=self.user2, role=TeamRole.MEMBER)
        round1 = Round.objects.create(hackathon=self.hackathon, order=1, name='Round 1')
        RoundEntry.objects.create(round=round1, team=team, status=EntryStatus.PENDING)

        url = f'/api/hackathons/teams/{team.id}/'
        resp = self.client.delete(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(Team.objects.filter(id=team.id).exists())
        self.assertFalse(TeamMember.objects.filter(team_id=team.id).exists())
        self.assertFalse(RoundEntry.objects.filter(team_id=team.id).exists())

    def test_force_delete_problem_statement_unassigns_teams(self):
        team = Team.objects.create(
            hackathon=self.hackathon, name='Gamma Devs', leader=self.user1,
            problem_statement=self.ps1, status=TeamStatus.REGISTERED,
        )
        url = f'/api/hackathons/{self.hackathon.slug}/problem-statements/{self.ps1.id}/'

        # Without force: blocked
        resp = self.client.delete(url)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data['code'], 'PROBLEM_STATEMENT_IN_USE')

        # With force=true: unassigns teams and deletes
        resp = self.client.delete(f'{url}?force=true')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(ProblemStatement.objects.filter(id=self.ps1.id).exists())
        team.refresh_from_db()
        self.assertIsNone(team.problem_statement)

    def test_round_advance_shortlisted_teams_to_next_round(self):
        team1 = Team.objects.create(hackathon=self.hackathon, name='Team One', leader=self.user1, status=TeamStatus.REGISTERED)
        team2 = Team.objects.create(hackathon=self.hackathon, name='Team Two', leader=self.user2, status=TeamStatus.REGISTERED)
        team3 = Team.objects.create(hackathon=self.hackathon, name='Team Three', leader=self.user3, status=TeamStatus.REGISTERED)

        round1 = Round.objects.create(hackathon=self.hackathon, order=1, name='Round 1')
        RoundEntry.objects.create(round=round1, team=team1, status=EntryStatus.SHORTLISTED)
        RoundEntry.objects.create(round=round1, team=team2, status=EntryStatus.SHORTLISTED)
        RoundEntry.objects.create(round=round1, team=team3, status=EntryStatus.REJECTED)

        url = f'/api/hackathons/{self.hackathon.slug}/rounds/{round1.id}/advance/'
        resp = self.client.post(url, {})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['promoted'], 2)

        # Round 2 was auto-created and populated
        round2 = Round.objects.get(hackathon=self.hackathon, order=2)
        self.assertEqual(round2.name, 'Round 2')
        self.assertEqual(round2.entries.count(), 2)
        self.assertTrue(round2.entries.filter(team=team1).exists())
        self.assertTrue(round2.entries.filter(team=team2).exists())
        self.assertFalse(round2.entries.filter(team=team3).exists())

    def test_round_wildcard_add_and_remove_team(self):
        team = Team.objects.create(hackathon=self.hackathon, name='Delta Squad', leader=self.user1, status=TeamStatus.REGISTERED)
        round1 = Round.objects.create(hackathon=self.hackathon, order=1, name='Round 1')

        # Add team to round
        url_add = f'/api/hackathons/{self.hackathon.slug}/rounds/{round1.id}/add-team/'
        resp = self.client.post(url_add, {'team_id': team.id})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data['added'])
        self.assertTrue(RoundEntry.objects.filter(round=round1, team=team).exists())

        # Remove team from round
        url_remove = f'/api/hackathons/{self.hackathon.slug}/rounds/{round1.id}/remove-team/'
        resp = self.client.post(url_remove, {'team_id': team.id})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data['removed'])
        self.assertFalse(RoundEntry.objects.filter(round=round1, team=team).exists())
