from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from apps.hackathons.models import (
    EmailInviteStatus, EntryStatus, InviteStatus, TeamEmailInvite, TeamInvite, TeamStatus,
)
from apps.hackathons.services import RoundService, TeamService
from apps.notifications.models import Notification

from .helpers import make_hackathon, make_ps, make_team, make_user

User = get_user_model()


class StateChangeNotificationTests(TestCase):
    def setUp(self):
        self.admin = make_user(role='ADMIN')
        self.hack = make_hackathon(min_team_size=1, max_team_size=4)
        self.leader, self.member = make_user(), make_user()
        self.team = make_team(self.hack, self.leader, self.member)

    def _notified(self, user):
        return list(Notification.objects.filter(recipient=user).values_list('title', flat=True))

    def test_removed_member_is_told(self):
        with self.captureOnCommitCallbacks(execute=True):
            TeamService.remove_member(self.team, self.leader, self.member.id)
        self.assertEqual(self._notified(self.member), [f'You were removed from {self.team.name}'])

    def test_new_leader_is_told(self):
        with self.captureOnCommitCallbacks(execute=True):
            TeamService.transfer_leadership(self.team, self.leader, self.member.id)
        self.assertEqual(self._notified(self.member), [f'You now lead {self.team.name}'])
        self.assertEqual(self._notified(self.leader), [])

    def test_disqualification_notifies_every_member(self):
        with self.captureOnCommitCallbacks(execute=True):
            TeamService.admin_set_status(self.team, self.admin, TeamStatus.DISQUALIFIED)
        for user in (self.leader, self.member):
            self.assertEqual(self._notified(user), [f'{self.team.name}: status updated'])
        self.assertIn('disqualified', Notification.objects.filter(recipient=self.member).get().message)

    def test_unchanged_status_notifies_nobody(self):
        with self.captureOnCommitCallbacks(execute=True):
            TeamService.admin_set_status(self.team, self.admin, self.team.status)
        self.assertEqual(Notification.objects.count(), 0)

    def test_admin_added_member_is_told(self):
        newcomer = make_user()
        with self.captureOnCommitCallbacks(execute=True):
            TeamService.admin_add_member(self.team, self.admin, newcomer.email)
        self.assertEqual(self._notified(newcomer), [f'You were added to {self.team.name}'])

    def test_leader_hears_when_an_invite_is_accepted_or_declined(self):
        accepter, decliner = make_user(first_name='Acc'), make_user(first_name='Dec')
        invite_a = TeamService.invite(self.team, self.leader, accepter.email)
        invite_d = TeamService.invite(self.team, self.leader, decliner.email)
        Notification.objects.all().delete()
        with self.captureOnCommitCallbacks(execute=True):
            TeamService.accept_invite(invite_a, accepter)
            TeamService.decline_invite(invite_d, decliner)
        titles = self._notified(self.leader)
        self.assertIn('Acc joined ' + self.team.name, titles)
        self.assertIn('Dec declined your invite', titles)

    def test_a_failing_notification_never_breaks_the_action(self):
        with mock.patch('apps.notifications.services.NotificationService.create_bulk_notifications', side_effect=RuntimeError('boom')), \
                self.assertLogs('apps.hackathons.services', level='ERROR'), self.captureOnCommitCallbacks(execute=True):
            TeamService.remove_member(self.team, self.leader, self.member.id)
        self.assertFalse(self.team.memberships.filter(user=self.member).exists())


class EmailInviteTests(TestCase):
    def setUp(self):
        self.hack = make_hackathon(min_team_size=1, max_team_size=3)
        self.leader = make_user()
        self.team = make_team(self.hack, self.leader)
        self.client = APIClient()
        self.client.force_authenticate(self.leader)
        self.url = f'/api/hackathons/teams/{self.team.id}/invite-by-email/'

    def _invite(self, email='newcomer@example.com'):
        with mock.patch('apps.hackathons.services.send_email_invite') as send, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.url, {'email': email}, format='json')
        return response, send

    def test_leader_can_invite_an_address_without_an_account(self):
        response, send = self._invite()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['team']['pending_email_invites'][0]['email'], 'newcomer@example.com')
        self.assertTrue(send.called)

    def test_existing_accounts_are_pointed_to_the_normal_invite(self):
        existing = make_user()
        response, _ = self._invite(existing.email)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'ACCOUNT_EXISTS')

    def test_invalid_address_is_rejected(self):
        response, _ = self._invite('not-an-email')
        self.assertEqual(response.data['code'], 'EMAIL_INVALID')

    def test_duplicate_is_rejected(self):
        self._invite()
        response, _ = self._invite('NewComer@Example.com')
        self.assertEqual(response.status_code, 400)

    def test_email_invites_count_toward_the_team_size(self):
        self._invite('a@example.com')
        self._invite('b@example.com')
        response, _ = self._invite('c@example.com')
        self.assertEqual(response.data['code'], 'TEAM_FULL')
        # and a normal invite sees the same limit
        other = make_user()
        with self.assertRaises(Exception) as caught:
            TeamService.invite(self.team, self.leader, other.email)
        self.assertEqual(caught.exception.detail['code'], 'CANNOT_INVITE')

    def test_only_the_leader_may_send_one(self):
        member = make_user()
        TeamService.admin_add_member(self.team, make_user(role='ADMIN'), member.email)
        self.client.force_authenticate(member)
        self.assertEqual(self.client.post(self.url, {'email': 'x@example.com'}, format='json').status_code, 403)

    def test_leader_can_cancel_it(self):
        self._invite()
        invite = TeamEmailInvite.objects.get()
        response = self.client.post(
            f'/api/hackathons/teams/{self.team.id}/cancel-email-invite/', {'invite_id': invite.id}, format='json',
        )
        self.assertEqual(response.status_code, 200)
        invite.refresh_from_db()
        self.assertEqual(invite.status, EmailInviteStatus.CANCELLED)

    def test_signing_up_turns_the_email_invite_into_a_real_invite(self):
        self._invite()
        with self.captureOnCommitCallbacks(execute=True):
            newcomer = User.objects.create_user(username='newcomer', email='NewComer@example.com', password='pw12345!')
        invite = TeamInvite.objects.get(invited_user=newcomer)
        self.assertEqual((invite.team_id, invite.status), (self.team.id, InviteStatus.PENDING))
        self.assertEqual(TeamEmailInvite.objects.get().status, EmailInviteStatus.CLAIMED)
        self.assertTrue(Notification.objects.filter(recipient=newcomer).exists())

    def test_registration_endpoint_claims_invites_too(self):
        self._invite('signup@gmail.com')
        with self.captureOnCommitCallbacks(execute=True):
            response = APIClient().post('/api/auth/register/', {
                'email': 'signup@gmail.com', 'password': 'StrongPass123!', 'first_name': 'Sign', 'last_name': 'Up',
                'roll_number': '22B91A0577', 'branch': 'CSE', 'year': 2,
            }, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertTrue(TeamInvite.objects.filter(invited_user__email='signup@gmail.com').exists())

    def test_invite_is_dropped_when_the_team_can_no_longer_take_them(self):
        self._invite()
        self.hack.team_edits_locked = True
        self.hack.save()
        with self.captureOnCommitCallbacks(execute=True):
            newcomer = User.objects.create_user(username='late', email='newcomer@example.com', password='pw12345!')
        self.assertFalse(TeamInvite.objects.filter(invited_user=newcomer).exists())
        self.assertEqual(TeamEmailInvite.objects.get().status, EmailInviteStatus.CANCELLED)


class FormingTeamsTests(TestCase):
    def setUp(self):
        self.admin = make_user(role='ADMIN')
        self.hack = make_hackathon(min_team_size=3, max_team_size=4)
        self.leader = make_user()
        self.forming = make_team(self.hack, self.leader, make_user(), name='Short', status=TeamStatus.FORMING)
        make_team(self.hack, make_user(), make_user(), make_user(), name='Ready', status=TeamStatus.REGISTERED)
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def test_stats_list_forming_teams_with_what_they_need(self):
        stats = self.client.get(f'/api/hackathons/{self.hack.slug}/stats/').data
        self.assertEqual(len(stats['forming_teams']), 1)
        team = stats['forming_teams'][0]
        self.assertEqual((team['name'], team['members'], team['needed']), ('Short', 2, 1))
        self.assertEqual(team['leader_email'], self.leader.email)

    def test_reminder_notifies_and_emails_only_forming_leaders(self):
        with mock.patch('apps.hackathons.services.send_forming_reminder_email') as send, \
                mock.patch('apps.core.tasks.run_in_background', side_effect=lambda fn: fn()):
            response = self.client.post(f'/api/hackathons/{self.hack.slug}/remind-forming/')
        self.assertEqual(response.data, {'reminded': 1})
        self.assertEqual(Notification.objects.get().recipient, self.leader)
        send.assert_called_once_with(self.forming.id)

    def test_reminder_is_admin_only(self):
        player = APIClient()
        player.force_authenticate(self.leader)
        self.assertEqual(player.post(f'/api/hackathons/{self.hack.slug}/remind-forming/').status_code, 403)

    def test_reminder_email_body_names_what_is_missing(self):
        from apps.hackathons.services import send_forming_reminder_email
        with mock.patch('apps.core.services.email_service.EmailNotificationService.send_email', return_value=True) as send:
            self.assertTrue(send_forming_reminder_email(self.forming.id))
        subject, text = send.call_args.args[1], send.call_args.args[2]
        self.assertIn('1 more member', subject)
        self.assertIn('Short', text)


class ProblemLockedAfterRoundStartsTests(TestCase):
    def setUp(self):
        self.admin = make_user(role='ADMIN')
        self.hack = make_hackathon(min_team_size=1)
        self.ps1, self.ps2 = make_ps(self.hack, code='PS1'), make_ps(self.hack, code='PS2')
        self.leader = make_user()
        self.team = TeamService.create_team(self.hack, self.leader, 'Alpha', self.ps1)
        self.client = APIClient()
        self.client.force_authenticate(self.leader)
        self.url = f'/api/hackathons/teams/{self.team.id}/'

    def test_problem_can_change_before_any_round(self):
        response = self.client.patch(self.url, {'problem_statement': self.ps2.id}, format='json')
        self.assertEqual(response.status_code, 200)

    def test_problem_is_locked_once_the_team_is_in_a_round(self):
        RoundService.create_round(self.hack, self.admin, name='R1')
        response = self.client.patch(self.url, {'problem_statement': self.ps2.id}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'PROBLEM_LOCKED')

    def test_renaming_still_works_and_resending_the_same_problem_is_fine(self):
        RoundService.create_round(self.hack, self.admin, name='R1')
        response = self.client.patch(self.url, {'name': 'Alpha 2', 'problem_statement': self.ps1.id}, format='json')
        self.assertEqual(response.status_code, 200)

    def test_admins_can_still_change_it(self):
        RoundService.create_round(self.hack, self.admin, name='R1')
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.patch(self.url, {'problem_statement': self.ps2.id}, format='json').status_code, 200)


class HackathonFaqTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(make_user(role='ADMIN'))
        self.hack = make_hackathon()

    def _patch(self, faq):
        return self.client.patch(f'/api/hackathons/{self.hack.slug}/', {'faq': faq}, format='json')

    def test_admin_can_set_and_clear_the_faq(self):
        response = self._patch([{'question': '  Q1 ', 'answer': 'A1'}])
        self.assertEqual(response.data['faq'], [{'question': 'Q1', 'answer': 'A1'}])
        self.assertEqual(self._patch([]).data['faq'], [])

    def test_public_payload_carries_the_faq(self):
        self._patch([{'question': 'Q', 'answer': 'A'}])
        self.assertEqual(APIClient().get(f'/api/hackathons/{self.hack.slug}/').data['faq'], [{'question': 'Q', 'answer': 'A'}])

    def test_invalid_entries_are_rejected(self):
        for bad in ('text', [{'question': 'Q'}], [{'question': '', 'answer': 'A'}], [{'question': 'Q', 'answer': 'x' * 2001}], [{'question': 'Q', 'answer': 'A'}] * 21):
            self.assertEqual(self._patch(bad).status_code, 400, bad)


class AdminListQueryBudgetTests(TestCase):
    def setUp(self):
        self.admin = make_user(role='ADMIN')
        self.hack = make_hackathon(min_team_size=1)
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self._add_teams(2)
        self.round = RoundService.create_round(self.hack, self.admin, name='R1')
        self.next_round = RoundService.create_round(self.hack, self.admin, name='R2', populate=False)

    def _add_teams(self, count):
        for _ in range(count):
            leader = make_user()
            make_team(self.hack, leader, make_user())

    def _queries(self, url):
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return len(ctx.captured_queries)

    def test_round_entries_listing_does_not_grow_with_teams(self):
        url = f'/api/hackathons/{self.hack.slug}/rounds/{self.round.id}/entries/'
        RoundService.populate_entries(self.round, self.admin)
        small = self._queries(url)
        self._add_teams(10)
        RoundService.populate_entries(self.round, self.admin)
        self.assertEqual(self._queries(url), small)

    def test_round_entries_report_promotion_and_member_counts(self):
        RoundService.populate_entries(self.round, self.admin)
        first = self.round.entries.first()
        RoundService.decide(self.round, self.admin, [first.team_id], EntryStatus.SHORTLISTED)
        RoundService.add_team_entry(self.next_round, first.team, self.admin)
        data = self.client.get(f'/api/hackathons/{self.hack.slug}/rounds/{self.round.id}/entries/').data
        by_team = {row['team_id']: row for row in data}
        self.assertTrue(by_team[first.team_id]['is_promoted_to_next'])
        self.assertEqual(by_team[first.team_id]['member_count'], 2)
        self.assertFalse(any(row['is_promoted_to_next'] for tid, row in by_team.items() if tid != first.team_id))

    def test_entry_counts_are_correct(self):
        RoundService.populate_entries(self.round, self.admin)
        teams = list(self.round.entries.values_list('team_id', flat=True))
        RoundService.decide(self.round, self.admin, teams[:1], EntryStatus.SHORTLISTED)
        RoundService.decide(self.round, self.admin, teams[1:], EntryStatus.REJECTED)
        counts = self.client.get(f'/api/hackathons/{self.hack.slug}/rounds/').data[0]['entry_counts']
        self.assertEqual((counts['total'], counts['SHORTLISTED'], counts['REJECTED'], counts['PENDING']), (2, 1, 1, 0))
        self.assertEqual(counts['next_round_id'], self.next_round.id)


class TeamCollegeInAdminViewTests(TestCase):
    def test_admin_team_members_carry_their_college(self):
        hack = make_hackathon(min_team_size=1)
        leader = make_user(other_college_name='Tiny Town Institute')
        make_team(hack, leader)
        client = APIClient()
        client.force_authenticate(make_user(role='ADMIN'))
        members = client.get(f'/api/hackathons/{hack.slug}/teams/').data[0]['members']
        self.assertEqual(members[0]['college'], 'Tiny Town Institute')

    def test_participants_do_not_see_member_colleges(self):
        hack = make_hackathon(min_team_size=1)
        leader = make_user()
        make_team(hack, leader)
        client = APIClient()
        client.force_authenticate(leader)
        member = client.get(f'/api/hackathons/{hack.slug}/my-team/').data['team']['members'][0]
        self.assertNotIn('college', member)
