from unittest import mock

from django.core import mail
from django.test import TestCase
from rest_framework.test import APIClient

from apps.hackathons import team_emails
from apps.hackathons.models import TeamInvite, TeamStatus
from apps.hackathons.services import TeamService

from .helpers import make_hackathon, make_user


def _sync(fn):
    fn()


class TeamEmailTestCase(TestCase):
    """Runs team changes with on-commit hooks and the background thread executed inline."""

    def setUp(self):
        self.hackathon = make_hackathon(min_team_size=2, max_team_size=3)
        self.leader = make_user(first_name='Lena')
        self.second = make_user(first_name='Sam')
        self.third = make_user(first_name='Tia')

    def run_changes(self, fn):
        with mock.patch('apps.core.tasks.run_in_background', _sync), self.captureOnCommitCallbacks(execute=True):
            return fn()

    def create_team(self, name='Rockets'):
        return self.run_changes(lambda: TeamService.create_team(self.hackathon, self.leader, name))

    def join(self, team, user):
        invite = TeamInvite.objects.create(team=team, hackathon=self.hackathon, invited_user=user, invited_by=self.leader)
        return self.run_changes(lambda: TeamService.accept_invite(invite, user))

    def recipients(self):
        return sorted(address for message in mail.outbox for address in message.to)


class TeamLifecycleEmailTests(TeamEmailTestCase):
    def test_creating_a_team_below_minimum_emails_the_leader_only(self):
        team = self.create_team()
        self.assertEqual(team.status, TeamStatus.FORMING)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [self.leader.email])
        self.assertIn('Rockets', message.subject)
        self.assertIn('not registered yet', message.body)

    def test_reaching_minimum_size_emails_every_member_once(self):
        team = self.create_team()
        mail.outbox.clear()
        self.join(team, self.second)

        team.refresh_from_db()
        self.assertEqual(team.status, TeamStatus.REGISTERED)
        self.assertEqual(self.recipients(), sorted([self.leader.email, self.second.email]))
        self.assertTrue(all('is registered' in m.subject for m in mail.outbox))
        self.assertIn('Lena', mail.outbox[0].body + mail.outbox[1].body)

    def test_creating_a_team_that_is_already_big_enough_sends_registered_not_created(self):
        self.hackathon.min_team_size = 1
        self.hackathon.save()
        self.create_team()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('is registered', mail.outbox[0].subject)

    def test_later_joiner_emails_everyone_including_the_joiner(self):
        team = self.create_team()
        self.join(team, self.second)
        mail.outbox.clear()
        self.join(team, self.third)
        self.assertEqual(self.recipients(), sorted([self.leader.email, self.second.email, self.third.email]))
        self.assertTrue(all('joined' in m.subject and 'Tia' in m.subject for m in mail.outbox))

    def test_leaving_emails_the_remaining_members(self):
        team = self.create_team()
        self.join(team, self.second)
        mail.outbox.clear()
        self.run_changes(lambda: TeamService.leave_team(team, self.second))
        self.assertEqual(self.recipients(), [self.leader.email])
        self.assertIn('no longer on', mail.outbox[0].subject)
        self.assertIn('not registered yet', mail.outbox[0].body)

    def test_removing_a_member_emails_the_removed_person_and_the_rest(self):
        team = self.create_team()
        self.join(team, self.second)
        self.join(team, self.third)
        mail.outbox.clear()
        self.run_changes(lambda: TeamService.remove_member(team, self.leader, self.third.pk))
        by_recipient = {m.to[0]: m for m in mail.outbox}
        self.assertEqual(set(by_recipient), {self.leader.email, self.second.email, self.third.email})
        self.assertIn('You were removed', by_recipient[self.third.email].subject)
        self.assertIn('no longer on', by_recipient[self.second.email].subject)

    def test_admin_status_change_emails_all_members(self):
        team = self.create_team()
        self.join(team, self.second)
        mail.outbox.clear()
        admin = make_user(role='ADMIN')
        self.run_changes(lambda: TeamService.admin_set_status(team, admin, TeamStatus.DISQUALIFIED))
        self.assertEqual(self.recipients(), sorted([self.leader.email, self.second.email]))
        self.assertIn('disqualified', mail.outbox[0].body)


class TeamEmailCustomizationTests(TeamEmailTestCase):
    def test_custom_subject_and_body_are_used_with_placeholders(self):
        self.hackathon.team_email_config = team_emails.normalize_config({
            'team_created': {'subject': 'Welcome to {{hackathon_title}}, {{first_name}}!', 'body': 'Team {{team_name}} - see {{dashboard_url}}'},
        })
        self.hackathon.save()
        self.create_team()
        message = mail.outbox[0]
        self.assertEqual(message.subject, f'Welcome to {self.hackathon.title}, Lena!')
        self.assertTrue(message.body.startswith('Team Rockets - see http'))

    def test_disabled_email_is_not_sent(self):
        self.hackathon.team_email_config = team_emails.normalize_config({'team_created': {'enabled': False}})
        self.hackathon.save()
        self.create_team()
        self.assertEqual(mail.outbox, [])

    def test_team_name_is_html_escaped(self):
        self.create_team(name='<b>Hack</b>')
        html = mail.outbox[0].alternatives[0][0]
        self.assertNotIn('<b>Hack</b>', html)
        self.assertIn('&lt;b&gt;Hack&lt;/b&gt;', html)

    def test_unknown_placeholder_is_rejected(self):
        with self.assertRaises(team_emails.TeamEmailConfigError):
            team_emails.normalize_config({'team_created': {'body': 'Your password is {{password}}'}})
        with self.assertRaises(team_emails.TeamEmailConfigError):
            team_emails.normalize_config({'nope': {}})


class TeamEmailSettingsApiTests(TeamEmailTestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_user(role='ADMIN')
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.url = f'/api/hackathons/{self.hackathon.slug}/team-emails/'

    def test_get_lists_every_email_with_defaults_and_placeholders(self):
        data = self.client.get(self.url).json()
        self.assertEqual({e['key'] for e in data['emails']}, set(team_emails.EVENTS))
        first = data['emails'][0]
        self.assertTrue(first['enabled'])
        self.assertIn('{{team_name}}', first['default_body'])
        self.assertIn('team_name', {p['key'] for p in data['placeholders']})

    def test_put_saves_overrides_and_rejects_bad_placeholders(self):
        good = self.client.put(self.url, {'emails': {'team_registered': {'enabled': True, 'subject': 'Hi {{first_name}}', 'body': ''}}}, format='json')
        self.assertEqual(good.status_code, 200, good.content)
        self.hackathon.refresh_from_db()
        self.assertEqual(self.hackathon.team_email_config['team_registered']['subject'], 'Hi {{first_name}}')

        bad = self.client.put(self.url, {'emails': {'team_registered': {'body': '{{secret}}'}}}, format='json')
        self.assertEqual(bad.status_code, 400)

    def test_participants_cannot_read_or_change_settings(self):
        self.client.force_authenticate(self.leader)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.put(self.url, {'emails': {}}, format='json').status_code, 403)

    def test_test_send_goes_to_the_admin_only(self):
        response = self.client.post(
            f'{self.url}test/', {'email': 'team_registered', 'subject': 'Check {{team_name}}', 'body': 'Members:\n{{members_list}}'}, format='json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.admin.email])
        self.assertEqual(mail.outbox[0].subject, '[TEST] Check Sample Squad')
