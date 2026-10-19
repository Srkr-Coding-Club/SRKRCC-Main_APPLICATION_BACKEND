from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from apps.hackathons.models import TeamStatus
from apps.hackathons.services import TeamService
from apps.notifications.models import Notification

from .helpers import make_hackathon, make_ps, make_team, make_user


class InviteNotificationTests(TestCase):
    def setUp(self):
        self.hack = make_hackathon(min_team_size=1, max_team_size=4)
        self.leader = make_user()
        self.team = make_team(self.hack, self.leader)
        self.invitee = make_user()

    def test_invite_creates_a_notification_linking_to_the_dashboard(self):
        TeamService.invite(self.team, self.leader, self.invitee.email)
        notification = Notification.objects.get(recipient=self.invitee)
        self.assertEqual(notification.link_url, f'/hackathons/{self.hack.slug}/dashboard')

    def test_a_failing_notification_is_logged_and_does_not_lose_the_invite(self):
        with mock.patch('apps.notifications.services.NotificationService.create_notification', side_effect=RuntimeError('boom')), \
                self.assertLogs('apps.hackathons.services', level='ERROR') as logs:
            invite = TeamService.invite(self.team, self.leader, self.invitee.email)
        self.assertTrue(invite.pk)
        self.assertTrue(any('team invite' in line for line in logs.output))
        self.team.refresh_from_db()
        self.assertEqual(self.team.invites.count(), 1)


class ProblemStatementReorderTests(TestCase):
    def setUp(self):
        self.hack = make_hackathon()
        self.first = make_ps(self.hack, code='PS-001', order=1)
        self.second = make_ps(self.hack, code='PS-002', order=2)
        self.third = make_ps(self.hack, code='PS-003', order=2)  # duplicate order, as CSV imports could leave
        self.client = APIClient()
        self.client.force_authenticate(make_user(role='ADMIN'))
        self.url = f'/api/hackathons/{self.hack.slug}/problem-statements/reorder/'

    def test_reorder_assigns_a_clean_sequence(self):
        response = self.client.post(self.url, {'ids': [self.third.id, self.first.id, self.second.id]}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual([p['id'] for p in response.data], [self.third.id, self.first.id, self.second.id])
        self.assertEqual([p['order'] for p in response.data], [1, 2, 3])

    def test_list_endpoint_follows_the_display_order(self):
        self.first.order, self.second.order, self.third.order = 3, 1, 2
        for statement in (self.first, self.second, self.third):
            statement.save()
        response = APIClient().get(f'/api/hackathons/{self.hack.slug}/problem-statements/')
        self.assertEqual([p['id'] for p in response.data], [self.second.id, self.third.id, self.first.id])

    def test_reorder_requires_every_statement_exactly_once(self):
        for ids in ([self.first.id], [self.first.id, self.first.id, self.second.id], [self.first.id, self.second.id, self.third.id, 9999]):
            response = self.client.post(self.url, {'ids': ids}, format='json')
            self.assertEqual(response.status_code, 400, ids)
            self.assertEqual(response.data['code'], 'REORDER_MISMATCH')

    def test_reorder_is_admin_only(self):
        player = APIClient()
        player.force_authenticate(make_user())
        self.assertEqual(player.post(self.url, {'ids': []}, format='json').status_code, 403)

    def test_failed_reorder_changes_nothing(self):
        self.client.post(self.url, {'ids': [self.third.id]}, format='json')
        self.first.refresh_from_db()
        self.assertEqual(self.first.order, 1)


class RegisteredTeamCountTests(TestCase):
    def test_public_registered_count_excludes_teams_still_forming(self):
        hack = make_hackathon(min_team_size=2, max_team_size=3)
        make_team(hack, make_user(), make_user(), name='Ready', status=TeamStatus.REGISTERED)
        make_team(hack, make_user(), name='Forming', status=TeamStatus.FORMING)
        response = APIClient().get(f'/api/hackathons/{hack.slug}/')
        self.assertEqual(response.data['team_count'], 2)
        self.assertEqual(response.data['registered_team_count'], 1)
