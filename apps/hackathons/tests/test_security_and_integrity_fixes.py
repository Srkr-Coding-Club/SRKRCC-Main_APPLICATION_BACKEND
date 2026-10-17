from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.hackathons.models import (
    AnnouncementAudience, EntryStatus, HackathonAnnouncement, RoundEntry, Submission,
)
from apps.hackathons.services import RoundService, TeamService

from .helpers import make_hackathon, make_ps, make_team, make_user


class UserLookupAccessTests(TestCase):
    def setUp(self):
        self.hack = make_hackathon(min_team_size=1)
        self.leader = make_user()
        self.team = make_team(self.hack, self.leader)
        self.target = make_user(first_name='Zed', email='zed.target@srkr.ac.in')
        self.client = APIClient()
        self.url = f'/api/hackathons/{self.hack.slug}/user-lookup/'

    def test_user_without_a_team_cannot_search_members(self):
        self.client.force_authenticate(make_user())
        for params in ({'q': 'zed'}, {'email': self.target.email}):
            self.assertEqual(self.client.get(self.url, params).status_code, 403)

    def test_member_who_is_not_the_leader_cannot_search(self):
        member = make_user()
        TeamService.admin_add_member(self.team, make_user(role='ADMIN'), member.email)
        self.client.force_authenticate(member)
        self.assertEqual(self.client.get(self.url, {'q': 'zed'}).status_code, 403)

    def test_leader_cannot_search_on_behalf_of_another_team(self):
        other = make_team(self.hack, make_user())
        self.client.force_authenticate(self.leader)
        self.assertEqual(self.client.get(self.url, {'q': 'zed', 'team_id': other.id}).status_code, 403)

    def test_leader_can_search(self):
        self.client.force_authenticate(self.leader)
        response = self.client.get(self.url, {'q': 'zed'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['results'][0]['email'], self.target.email)

    def test_admin_can_search(self):
        self.client.force_authenticate(make_user(role='ADMIN'))
        self.assertEqual(self.client.get(self.url, {'q': 'zed'}).status_code, 200)

    def test_short_queries_return_nothing(self):
        self.client.force_authenticate(self.leader)
        response = self.client.get(self.url, {'q': 'ze'})
        self.assertEqual(response.data['results'], [])

    def test_results_are_capped(self):
        for _ in range(8):
            make_user(first_name='Capped')
        self.client.force_authenticate(self.leader)
        self.assertEqual(len(self.client.get(self.url, {'q': 'Capped'}).data['results']), 5)


class ScheduledAnnouncementEmailTests(TestCase):
    def setUp(self):
        self.admin = make_user(role='ADMIN')
        self.hack = make_hackathon(min_team_size=1)
        make_team(self.hack, make_user())
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def _post(self, **overrides):
        body = {'title': 'Hello', 'message': 'm', 'audience': 'PARTICIPANTS', 'send_email': True}
        body.update(overrides)
        with mock.patch('apps.hackathons.services.AnnouncementService.notify', return_value=1) as notify, \
                self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f'/api/hackathons/{self.hack.slug}/announcements/', body, format='json')
        return response, notify

    def test_immediate_announcement_is_emailed_and_stamped(self):
        response, notify = self._post()
        self.assertEqual(response.status_code, 201)
        self.assertTrue(notify.called)
        self.assertIsNotNone(HackathonAnnouncement.objects.get().emailed_at)

    def test_future_announcement_is_not_emailed_yet(self):
        future = (timezone.now() + timezone.timedelta(days=3)).isoformat()
        response, notify = self._post(publish_at=future)
        self.assertEqual(response.status_code, 201)
        self.assertFalse(notify.called)
        self.assertIsNone(HackathonAnnouncement.objects.get().emailed_at)

    def test_due_announcement_is_emailed_once_when_listed(self):
        future = (timezone.now() + timezone.timedelta(days=3)).isoformat()
        self._post(publish_at=future)
        HackathonAnnouncement.objects.update(publish_at=timezone.now() - timezone.timedelta(minutes=1))
        url = f'/api/hackathons/{self.hack.slug}/announcements/'
        with mock.patch('apps.hackathons.services.AnnouncementService.notify', return_value=1) as notify:
            self.client.get(url)
            self.client.get(url)
        self.assertEqual(notify.call_count, 1)

    def test_management_command_sends_due_emails(self):
        future = (timezone.now() + timezone.timedelta(days=3)).isoformat()
        self._post(publish_at=future)
        HackathonAnnouncement.objects.update(publish_at=timezone.now() - timezone.timedelta(minutes=1))
        with mock.patch('apps.hackathons.services.AnnouncementService.notify', return_value=2) as notify:
            call_command('send_due_hackathon_emails', stdout=StringIO())
        self.assertTrue(notify.called)
        self.assertIsNotNone(HackathonAnnouncement.objects.get().emailed_at)

    def test_inactive_announcement_is_never_emailed(self):
        future = (timezone.now() + timezone.timedelta(days=3)).isoformat()
        self._post(publish_at=future, is_active=False)
        HackathonAnnouncement.objects.update(publish_at=timezone.now() - timezone.timedelta(minutes=1))
        with mock.patch('apps.hackathons.services.AnnouncementService.notify') as notify:
            call_command('send_due_hackathon_emails', stdout=StringIO())
        self.assertFalse(notify.called)


class SubmissionOwnershipTests(TestCase):
    def test_submission_cannot_be_moved_to_another_team(self):
        hack = make_hackathon(min_team_size=1)
        mine, theirs = make_user(), make_user()
        my_team, other_team = make_team(hack, mine), make_team(hack, theirs)
        submission = Submission.objects.create(team=my_team, project_title='t', description='d', repo_url='https://x.io')
        client = APIClient()
        client.force_authenticate(mine)

        response = client.patch(f'/api/hackathons/submissions/{submission.id}/', {'team': other_team.id}, format='json')

        self.assertEqual(response.status_code, 400)
        submission.refresh_from_db()
        self.assertEqual(submission.team_id, my_team.id)

    def test_leader_can_still_edit_their_own_submission(self):
        hack = make_hackathon(min_team_size=1)
        leader = make_user()
        team = make_team(hack, leader)
        submission = Submission.objects.create(team=team, project_title='t', description='d', repo_url='https://x.io')
        client = APIClient()
        client.force_authenticate(leader)
        response = client.patch(f'/api/hackathons/submissions/{submission.id}/', {'project_title': 'New'}, format='json')
        self.assertEqual(response.status_code, 200)


class DeactivatedProblemStatementTests(TestCase):
    def setUp(self):
        self.hack = make_hackathon(min_team_size=1)
        self.ps = make_ps(self.hack)
        self.leader = make_user()
        self.team = TeamService.create_team(self.hack, self.leader, 'Alpha', self.ps)
        self.ps.is_active = False
        self.ps.save()
        self.client = APIClient()
        self.client.force_authenticate(self.leader)

    def test_team_can_be_renamed_while_keeping_its_deactivated_statement(self):
        response = self.client.patch(
            f'/api/hackathons/teams/{self.team.id}/', {'name': 'Alpha 2', 'problem_statement': self.ps.id}, format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.team.refresh_from_db()
        self.assertEqual((self.team.name, self.team.problem_statement_id), ('Alpha 2', self.ps.id))

    def test_team_still_cannot_pick_a_different_inactive_statement(self):
        other = make_ps(self.hack, code='PS2', is_active=False)
        response = self.client.patch(f'/api/hackathons/teams/{self.team.id}/', {'problem_statement': other.id}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'PROBLEM_STATEMENT_INVALID')


class DeleteRoundKeepsAnnouncementsTests(TestCase):
    def setUp(self):
        self.admin = make_user(role='ADMIN')
        self.hack = make_hackathon(min_team_size=1)
        self.winner, self.loser = make_team(self.hack, make_user()), make_team(self.hack, make_user())
        self.round = RoundService.create_round(self.hack, self.admin, name='R1')
        RoundService.decide(self.round, self.admin, [self.winner.id], EntryStatus.SHORTLISTED)
        RoundService.decide(self.round, self.admin, [self.loser.id], EntryStatus.REJECTED)

    def _announce(self, audience):
        return HackathonAnnouncement.objects.create(
            hackathon=self.hack, title='T', message='m', audience=audience, round=self.round,
        )

    def test_shortlisted_announcement_survives_for_the_shortlisted_teams(self):
        announcement = self._announce(AnnouncementAudience.ROUND_SHORTLISTED)
        RoundService.delete_round(self.round, self.admin)
        announcement.refresh_from_db()
        self.assertEqual(announcement.audience, AnnouncementAudience.TEAMS)
        self.assertIsNone(announcement.round)
        self.assertEqual(list(announcement.target_teams.values_list('id', flat=True)), [self.winner.id])

    def test_round_wide_announcement_survives_for_every_team_in_it(self):
        announcement = self._announce(AnnouncementAudience.ROUND_ALL)
        RoundService.delete_round(self.round, self.admin)
        announcement.refresh_from_db()
        self.assertCountEqual(announcement.target_teams.values_list('id', flat=True), [self.winner.id, self.loser.id])

    def test_delete_endpoint_removes_round_but_not_announcement(self):
        announcement = self._announce(AnnouncementAudience.ROUND_ALL)
        client = APIClient()
        client.force_authenticate(self.admin)
        response = client.delete(f'/api/hackathons/{self.hack.slug}/rounds/{self.round.id}/')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(HackathonAnnouncement.objects.filter(pk=announcement.pk).exists())
        self.assertFalse(RoundEntry.objects.filter(round_id=self.round.id).exists())
