"""Invite emails, emailing round results, and the round-results DMC export."""
from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings
from rest_framework.test import APITestCase

from apps.core.dmc.contracts import QueryRequest
from apps.core.dmc.registry import get_dataset
from apps.hackathons.models import (
    AnnouncementAudience, EntryStatus, HackathonAnnouncement, InviteStatus, Round, RoundEntry, TeamInvite, TeamStatus,
)
from apps.hackathons.services import send_invite_email
from .helpers import make_hackathon, make_ps, make_team, make_user


def _sync(fn):
    fn()


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend', FRONTEND_URL='https://srkrcc.test')
class InviteEmailTests(APITestCase):
    def setUp(self):
        self.hack = make_hackathon(title='Spring Hack')
        self.leader = make_user(first_name='Alice')
        self.friend = make_user(first_name='Bob')
        self.team = make_team(self.hack, self.leader, name='Bug Slayers', status=TeamStatus.FORMING)

    def test_inviting_emails_the_invitee_after_commit(self):
        self.client.force_authenticate(self.leader)
        with mock.patch('apps.core.tasks.run_in_background', _sync), self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(f'/api/hackathons/teams/{self.team.id}/invite/', {'email': self.friend.email}, format='json')
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, [self.friend.email])
        self.assertIn('Bug Slayers', msg.subject)
        self.assertIn(f'https://srkrcc.test/hackathons/{self.hack.slug}/dashboard', msg.body)

    def test_failed_invite_sends_nothing(self):
        self.client.force_authenticate(self.leader)
        with mock.patch('apps.core.tasks.run_in_background', _sync), self.captureOnCommitCallbacks(execute=True):
            self.client.post(f'/api/hackathons/teams/{self.team.id}/invite/', {'email': 'nobody@x.com'}, format='json')
        self.assertEqual(mail.outbox, [])

    def test_no_email_for_an_invite_no_longer_pending(self):
        invite = TeamInvite.objects.create(team=self.team, hackathon=self.hack, invited_user=self.friend,
                                           invited_by=self.leader, status=InviteStatus.CANCELLED)
        self.assertFalse(send_invite_email(invite.id))
        self.assertEqual(mail.outbox, [])

    def test_team_name_is_html_escaped(self):
        self.team.name = '<b>x</b>'
        self.team.save()
        invite = TeamInvite.objects.create(team=self.team, hackathon=self.hack, invited_user=self.friend, invited_by=self.leader)
        self.assertTrue(send_invite_email(invite.id))
        html = mail.outbox[0].alternatives[0][0]
        self.assertIn('&lt;b&gt;x&lt;/b&gt;', html)
        self.assertNotIn('<b>x</b>', html)


class PublishEmailTests(APITestCase):
    def setUp(self):
        self.hack = make_hackathon()
        self.round = Round.objects.create(hackathon=self.hack, order=1, name='R1')
        self.client.force_authenticate(make_user(role='ADMIN'))

    def _publish(self, **body):
        return self.client.post(f'/api/hackathons/{self.hack.slug}/rounds/{self.round.id}/publish/', body, format='json')

    def test_publish_with_email_flags_the_announcement(self):
        self.assertEqual(self._publish(announce=True, email=True).status_code, 200)
        a = HackathonAnnouncement.objects.get(round=self.round)
        self.assertEqual(a.audience, AnnouncementAudience.ROUND_SHORTLISTED)
        self.assertTrue(a.send_email)

    def test_publish_without_email(self):
        self._publish(announce=True)
        self.assertFalse(HackathonAnnouncement.objects.get(round=self.round).send_email)


class RoundEntriesDatasetTests(TestCase):
    def setUp(self):
        self.hack = make_hackathon(title='Spring Hack')
        ps = make_ps(self.hack, 'PS-01')
        self.alpha = make_team(self.hack, make_user(), name='Alpha', ps=ps)
        self.beta = make_team(self.hack, make_user(), name='Beta')
        r1 = Round.objects.create(hackathon=self.hack, order=1, name='Ideas', results_published=True)
        RoundEntry.objects.create(round=r1, team=self.alpha, status=EntryStatus.SHORTLISTED, feedback='Nice')
        RoundEntry.objects.create(round=r1, team=self.beta, status=EntryStatus.REJECTED)
        other = make_hackathon()
        RoundEntry.objects.create(round=Round.objects.create(hackathon=other, order=1, name='X'),
                                  team=make_team(other, make_user(), name='Gamma'))
        self.adapter = get_dataset('hackathon_round_entries').adapter_class()

    def _query(self, **kw):
        return self.adapter.query(QueryRequest(**kw), None)

    def test_rows_and_values(self):
        result = self._query(filters=[], search='', page=1, page_size=50)
        self.assertEqual(result.total, 3)
        alpha = next(r for r in result.records if r['team_name'].value == 'Alpha')
        self.assertEqual(alpha['status'].value, 'Shortlisted')
        self.assertEqual(alpha['problem_statement'].value, 'PS-01 — Problem PS-01')
        self.assertTrue(alpha['results_published'].value)
        self.assertFalse(alpha['details_submitted'].value)

    def test_search(self):
        result = self._query(filters=[], search='beta', page=1, page_size=50)
        self.assertEqual([r['team_name'].value for r in result.records], ['Beta'])
