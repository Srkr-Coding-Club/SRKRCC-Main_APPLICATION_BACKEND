"""Multi-round shortlisting, the details-form gate, and targeted announcements."""
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.forms.models import FieldType, FormStatus, Response
from apps.forms.tests.factories import add_field, answers, make_form
from apps.hackathons.models import (
    AnnouncementAudience, EntryStatus, HackathonAnnouncement, Round, RoundEntry, TeamStatus,
)
from .helpers import make_hackathon, make_team, make_user


class RoundFlowTests(APITestCase):
    def setUp(self):
        self.hack = make_hackathon()
        self.admin = make_user(role='ADMIN')
        self.a = make_team(self.hack, make_user(), make_user(), name='Alpha')
        self.b = make_team(self.hack, make_user(), make_user(), name='Beta')
        self.forming = make_team(self.hack, make_user(), name='Forming', status=TeamStatus.FORMING)
        self.client.force_authenticate(self.admin)

    def _url(self, suffix=''):
        return f'/api/hackathons/{self.hack.slug}/rounds/{suffix}'

    def test_first_round_populates_registered_teams_only(self):
        resp = self.client.post(self._url(), {'name': 'Idea Submission'}, format='json')
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data['order'], 1)
        self.assertEqual(resp.data['entry_counts']['total'], 2)

    def test_shortlist_then_next_round_gets_only_shortlisted(self):
        r1 = self.client.post(self._url(), {'name': 'R1'}, format='json').data
        resp = self.client.post(self._url(f"{r1['id']}/decide/"),
                                {'team_ids': [self.a.id], 'status': 'SHORTLISTED', 'feedback': 'Great idea'}, format='json')
        self.assertEqual(resp.data['updated'], 1)
        self.client.post(self._url(f"{r1['id']}/decide/"), {'team_ids': [self.b.id], 'status': 'REJECTED'}, format='json')

        r2 = self.client.post(self._url(), {'name': 'R2'}, format='json').data
        self.assertEqual(r2['order'], 2)
        entries = self.client.get(self._url(f"{r2['id']}/entries/")).data
        self.assertEqual([e['team_name'] for e in entries], ['Alpha'])

    def test_results_hidden_from_team_until_published(self):
        r1 = self.client.post(self._url(), {'name': 'R1'}, format='json').data
        self.client.post(self._url(f"{r1['id']}/decide/"),
                         {'team_ids': [self.b.id], 'status': 'REJECTED', 'feedback': 'Sorry'}, format='json')

        self.client.force_authenticate(self.b.leader)
        mine = self.client.get(f'/api/hackathons/{self.hack.slug}/my-team/').data['rounds'][0]['entry']
        self.assertEqual((mine['status'], mine['feedback']), ('PENDING', ''))

        self.client.force_authenticate(self.admin)
        self.client.post(self._url(f"{r1['id']}/publish/"), {}, format='json')
        self.client.force_authenticate(self.b.leader)
        mine = self.client.get(f'/api/hackathons/{self.hack.slug}/my-team/').data['rounds'][0]['entry']
        self.assertEqual((mine['status'], mine['feedback']), ('REJECTED', 'Sorry'))

    def test_non_admin_cannot_manage_rounds(self):
        self.client.force_authenticate(self.a.leader)
        self.assertEqual(self.client.post(self._url(), {'name': 'X'}, format='json').status_code, 403)
        self.assertEqual(self.client.get(self._url()).status_code, 403)

    def test_invalid_decision_status(self):
        r1 = self.client.post(self._url(), {'name': 'R1'}, format='json').data
        resp = self.client.post(self._url(f"{r1['id']}/decide/"), {'team_ids': [self.a.id], 'status': 'WINNER'}, format='json')
        self.assertEqual(resp.data['code'], 'INVALID_STATUS')

    def test_cannot_delete_round_with_later_rounds(self):
        r1 = self.client.post(self._url(), {'name': 'R1'}, format='json').data
        self.client.post(self._url(), {'name': 'R2'}, format='json')
        self.assertEqual(self.client.delete(self._url(f"{r1['id']}/")).data['code'], 'ROUND_HAS_LATER')

    def test_populate_adds_late_registered_teams(self):
        r1 = self.client.post(self._url(), {'name': 'R1'}, format='json').data
        self.forming.status = TeamStatus.REGISTERED
        self.forming.save()
        resp = self.client.post(self._url(f"{r1['id']}/populate/"), {}, format='json')
        self.assertEqual(resp.data['added'], 1)


class DetailsFormGateTests(APITestCase):
    def setUp(self):
        self.hack = make_hackathon()
        self.admin = make_user(role='ADMIN')
        self.member = make_user()
        self.team = make_team(self.hack, make_user(), self.member, name='Alpha')
        self.other = make_team(self.hack, make_user(), make_user(), name='Beta')
        self.form = make_form(status=FormStatus.PUBLISHED, allow_multiple_responses=True)
        self.field = add_field(self.form, FieldType.TEXT, label='Repo URL', required=True)
        self.client.force_authenticate(self.admin)
        r = self.client.post(f'/api/hackathons/{self.hack.slug}/rounds/',
                             {'name': 'R1', 'details_form': self.form.id}, format='json').data
        self.round = Round.objects.get(pk=r['id'])
        self.form.refresh_from_db()
        RoundEntry.objects.filter(round=self.round, team=self.team).update(status=EntryStatus.SHORTLISTED)
        RoundEntry.objects.filter(round=self.round, team=self.other).update(status=EntryStatus.REJECTED)

    def _submit(self, user):
        self.client.force_authenticate(user)
        return self.client.post('/api/forms/submissions/', {
            'form': self.form.id, 'answers': answers((self.field, 'https://github.com/x/y')),
        }, format='json')

    def test_attaching_form_forces_single_response(self):
        self.assertFalse(self.form.allow_multiple_responses)

    def test_blocked_until_results_published(self):
        self.assertEqual(self._submit(self.team.leader).status_code, 403)

    def test_only_shortlisted_leader_can_submit_and_entry_is_linked(self):
        self.round.results_published = True
        self.round.save()
        self.assertEqual(self._submit(self.member).status_code, 403)
        self.assertEqual(self._submit(self.other.leader).status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self._submit(None).status_code, 403)

        resp = self._submit(self.team.leader)
        self.assertEqual(resp.status_code, 201, resp.data)
        entry = RoundEntry.objects.get(round=self.round, team=self.team)
        self.assertEqual(entry.details_response, Response.objects.get(form=self.form))

        self.client.force_authenticate(self.team.leader)
        payload = self.client.get(f'/api/hackathons/{self.hack.slug}/my-team/').data['rounds'][0]
        self.assertTrue(payload['entry']['details_submitted'])
        self.assertEqual(payload['details_form']['slug'], self.form.slug)

    def test_unrelated_forms_are_unaffected(self):
        plain = make_form(status=FormStatus.PUBLISHED)
        f = add_field(plain, FieldType.TEXT, label='Name')
        self.client.force_authenticate(None)
        resp = self.client.post('/api/forms/submissions/', {'form': plain.id, 'answers': answers((f, 'Ravi'))}, format='json')
        self.assertEqual(resp.status_code, 201, resp.data)


class AnnouncementAudienceTests(APITestCase):
    def setUp(self):
        self.hack = make_hackathon()
        self.admin = make_user(role='ADMIN')
        self.a = make_team(self.hack, make_user(), make_user(), name='Alpha')
        self.b = make_team(self.hack, make_user(), make_user(), name='Beta')
        self.round = Round.objects.create(hackathon=self.hack, order=1, name='R1', results_published=True)
        RoundEntry.objects.create(round=self.round, team=self.a, status=EntryStatus.SHORTLISTED)
        RoundEntry.objects.create(round=self.round, team=self.b, status=EntryStatus.REJECTED)
        self.client.force_authenticate(self.admin)
        self.url = f'/api/hackathons/{self.hack.slug}/announcements/'

        def post(title, audience, **extra):
            resp = self.client.post(self.url, {'title': title, 'message': 'm', 'audience': audience, **extra}, format='json')
            self.assertEqual(resp.status_code, 201, resp.data)

        post('public', 'PUBLIC')
        post('participants', 'PARTICIPANTS')
        post('round-all', 'ROUND_ALL', round=self.round.id)
        post('shortlisted', 'ROUND_SHORTLISTED', round=self.round.id)
        post('team-b', 'TEAMS', target_teams=[self.b.id])

    def _titles(self, user):
        self.client.force_authenticate(user)
        return sorted(a['title'] for a in self.client.get(self.url).data)

    def test_anonymous_and_outsiders_see_public_only(self):
        self.assertEqual(self._titles(None), ['public'])
        self.assertEqual(self._titles(make_user()), ['public'])

    def test_shortlisted_team(self):
        self.assertEqual(self._titles(self.a.leader), ['participants', 'public', 'round-all', 'shortlisted'])

    def test_rejected_targeted_team(self):
        self.assertEqual(self._titles(self.b.memberships.last().user), ['participants', 'public', 'round-all', 'team-b'])

    def test_shortlisted_announcement_hidden_until_results_published(self):
        self.round.results_published = False
        self.round.save()
        self.assertNotIn('shortlisted', self._titles(self.a.leader))

    def test_scheduled_and_expired_hidden(self):
        now = timezone.now()
        HackathonAnnouncement.objects.create(hackathon=self.hack, title='future', message='m',
                                             publish_at=now + timezone.timedelta(hours=1))
        HackathonAnnouncement.objects.create(hackathon=self.hack, title='expired', message='m',
                                             expires_at=now - timezone.timedelta(minutes=1),
                                             publish_at=now - timezone.timedelta(hours=1))
        self.assertEqual(self._titles(None), ['public'])

    def test_admin_all_listing_and_validation(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(len(self.client.get(self.url, {'all': 'true'}).data), 5)
        resp = self.client.post(self.url, {'title': 't', 'message': 'm', 'audience': 'ROUND_ALL'}, format='json')
        self.assertEqual(resp.data['code'], 'ROUND_REQUIRED')
        resp = self.client.post(self.url, {'title': 't', 'message': 'm', 'audience': 'TEAMS', 'target_teams': []}, format='json')
        self.assertEqual(resp.data['code'], 'TEAMS_REQUIRED')

    def test_withdrawn_team_sees_public_only(self):
        self.a.status = TeamStatus.WITHDRAWN
        self.a.save()
        self.assertEqual(self._titles(self.a.leader), ['public'])

    def test_recipients_resolution(self):
        from apps.hackathons.services import AnnouncementService
        shortlisted = HackathonAnnouncement.objects.get(title='shortlisted')
        emails = set(AnnouncementService.recipients(shortlisted).values_list('email', flat=True))
        self.assertEqual(emails, set(self.a.memberships.values_list('user__email', flat=True)))

    def test_publish_with_announce_creates_shortlisted_announcement(self):
        r2 = Round.objects.create(hackathon=self.hack, order=2, name='Finals')
        self.client.post(f'/api/hackathons/{self.hack.slug}/rounds/{r2.id}/publish/', {'announce': True}, format='json')
        created = HackathonAnnouncement.objects.get(round=r2)
        self.assertEqual(created.audience, AnnouncementAudience.ROUND_SHORTLISTED)
