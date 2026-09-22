from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.hackathons.models import Hackathon, Team
from apps.forms.models import Form, FormStatus, Response

User = get_user_model()


def _dates():
    start = timezone.now() + timezone.timedelta(days=14)
    return start, start + timezone.timedelta(days=1)


class HackathonsApiTests(TestCase):
    """POST/GET /api/hackathons/ — admin panel hackathon creation + form linking + metrics."""

    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            username='admin1', email='admin1@srkr.ac.in', password='pw12345!', role='ADMIN',
        )
        self.member = User.objects.create_user(
            username='member1', email='member1@srkr.ac.in', password='pw12345!', role='NON_AFFILIATE',
        )
        self.form = Form.objects.create(
            title='Hackathon Registration', slug='hackathon-registration', status=FormStatus.PUBLISHED,
        )

    def _payload(self, **overrides):
        start, end = _dates()
        payload = dict(
            title='IconCoders 2026 Flagship',
            slug='iconcoders-2026-flagship',
            theme='Advanced DSA',
            description='## Rules\n- Solo only\n- 36 hours',
            prize_pool='₹1,00,000',
            is_flagship=True,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            registration_form=self.form.id,
        )
        payload.update(overrides)
        return payload

    def test_anonymous_cannot_create_hackathon(self):
        resp = self.client.post('/api/hackathons/', self._payload(), format='json')
        self.assertIn(resp.status_code, (401, 403))
        self.assertEqual(Hackathon.objects.count(), 0)

    def test_regular_member_cannot_create_hackathon(self):
        self.client.force_authenticate(self.member)
        resp = self.client.post('/api/hackathons/', self._payload(), format='json')
        self.assertEqual(resp.status_code, 403)

    def test_admin_can_create_hackathon_with_linked_form(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.post('/api/hackathons/', self._payload(), format='json')
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data['form_slug'], self.form.slug)
        self.assertEqual(resp.data['registration_count'], 0)
        self.assertEqual(resp.data['team_count'], 0)

    def test_metrics_reflect_real_responses_and_teams_without_fanout(self):
        """Two independent reverse-FK Count() annotations (registration_form__responses
        and teams) in one query must use distinct=True, or each count multiplies by
        the other relation's row count. This pins that regression."""
        hackathon = Hackathon.objects.create(
            title='H', slug='h', theme='t', description='d', registration_form=self.form,
            start_date=_dates()[0], end_date=_dates()[1],
        )
        Response.objects.create(form=self.form, is_test_submission=False)
        Response.objects.create(form=self.form, is_test_submission=False)
        Response.objects.create(form=self.form, is_test_submission=True)  # excluded

        leader = User.objects.create_user(
            username='leader1', email='leader1@srkr.ac.in', password='pw12345!', role='NON_AFFILIATE',
        )
        Team.objects.create(hackathon=hackathon, name='Team A', leader=leader)
        Team.objects.create(hackathon=hackathon, name='Team B', leader=leader)
        Team.objects.create(hackathon=hackathon, name='Team C', leader=leader)

        self.client.force_authenticate(self.admin)
        resp = self.client.get(f'/api/hackathons/{hackathon.slug}/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['registration_count'], 2)
        self.assertEqual(resp.data['team_count'], 3)

    # --- Lifecycle: close / reopen ------------------------------------------

    def test_new_hackathon_defaults_to_live(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.post('/api/hackathons/', self._payload(), format='json')
        self.assertEqual(resp.data['status'], 'LIVE')

    def test_admin_can_close_and_reopen_hackathon(self):
        hackathon = Hackathon.objects.create(
            title='H2', slug='h2', theme='t', description='d',
            start_date=_dates()[0], end_date=_dates()[1],
        )
        self.client.force_authenticate(self.admin)

        resp = self.client.post(f'/api/hackathons/{hackathon.slug}/close/')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['status'], 'CLOSED')
        hackathon.refresh_from_db()
        self.assertEqual(hackathon.status, 'CLOSED')

        resp = self.client.post(f'/api/hackathons/{hackathon.slug}/reopen/')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['status'], 'LIVE')

    def test_regular_member_cannot_close_hackathon(self):
        hackathon = Hackathon.objects.create(
            title='H3', slug='h3', theme='t', description='d',
            start_date=_dates()[0], end_date=_dates()[1],
        )
        self.client.force_authenticate(self.member)
        resp = self.client.post(f'/api/hackathons/{hackathon.slug}/close/')
        self.assertEqual(resp.status_code, 403)
        hackathon.refresh_from_db()
        self.assertEqual(hackathon.status, 'LIVE')

    # --- Visibility: hide / show ---------------------------------------------

    def test_hidden_hackathon_is_excluded_from_public_list_and_detail(self):
        hackathon = Hackathon.objects.create(
            title='Hidden', slug='hidden-hack', theme='t', description='d',
            start_date=_dates()[0], end_date=_dates()[1],
        )
        self.client.force_authenticate(self.admin)
        resp = self.client.post(f'/api/hackathons/{hackathon.slug}/hide/')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(resp.data['is_hidden'])

        anon = APIClient()
        list_resp = anon.get('/api/hackathons/')
        self.assertNotIn(hackathon.slug, [h['slug'] for h in list_resp.data])
        self.assertEqual(anon.get(f'/api/hackathons/{hackathon.slug}/').status_code, 404)

    def test_admin_still_sees_hidden_hackathon(self):
        hackathon = Hackathon.objects.create(
            title='Hidden', slug='hidden-hack-2', theme='t', description='d',
            start_date=_dates()[0], end_date=_dates()[1],
            visible_until=timezone.now() - timezone.timedelta(hours=1),
        )
        self.client.force_authenticate(self.admin)
        resp = self.client.get('/api/hackathons/')
        self.assertIn(hackathon.slug, [h['slug'] for h in resp.data])

    def test_show_undoes_hide(self):
        hackathon = Hackathon.objects.create(
            title='H', slug='show-me-again-hack', theme='t', description='d',
            start_date=_dates()[0], end_date=_dates()[1],
        )
        self.client.force_authenticate(self.admin)
        self.client.post(f'/api/hackathons/{hackathon.slug}/hide/')
        resp = self.client.post(f'/api/hackathons/{hackathon.slug}/show/')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertFalse(resp.data['is_hidden'])

        anon = APIClient()
        list_resp = anon.get('/api/hackathons/')
        self.assertIn(hackathon.slug, [h['slug'] for h in list_resp.data])

    def test_regular_member_cannot_hide_hackathon(self):
        hackathon = Hackathon.objects.create(
            title='H', slug='cant-touch-this-hack', theme='t', description='d',
            start_date=_dates()[0], end_date=_dates()[1],
        )
        self.client.force_authenticate(self.member)
        resp = self.client.post(f'/api/hackathons/{hackathon.slug}/hide/')
        self.assertEqual(resp.status_code, 403)
        hackathon.refresh_from_db()
        self.assertIsNone(hackathon.visible_until)
