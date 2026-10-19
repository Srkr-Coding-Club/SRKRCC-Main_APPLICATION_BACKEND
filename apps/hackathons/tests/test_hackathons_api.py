from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.audit.models import AuditLog
from apps.hackathons.models import Hackathon, Round, Team, TeamMember, TeamRole
from apps.forms.models import Form, FormStatus, Response

User = get_user_model()


def _dates():
    start = timezone.now() + timezone.timedelta(days=14)
    return start, start + timezone.timedelta(days=1)


class HackathonsApiTests(TestCase):
    """POST/GET /api/hackathons/ - admin panel hackathon creation + form linking + metrics."""

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
            title='Hackoverflow 2026',
            slug='hackoverflow-2026',
            theme='Advanced DSA',
            description='## Rules\n- Solo only\n- 36 hours',
            prize_pool='₹1,00,000',
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

    def test_new_hackathon_starts_as_a_private_draft(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.post('/api/hackathons/', self._payload(), format='json')
        self.assertEqual(resp.data['status'], 'DRAFT')

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


class HackathonVisibilityAndDeletionTests(TestCase):
    """Manual hide/show is independent of the scheduled window; hard delete is admin-only and slug-confirmed."""

    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(username='vis_admin', email='vis_admin@srkr.ac.in', password='pw12345!', role='ADMIN')
        self.club_lead = User.objects.create_user(username='vis_lead', email='vis_lead@srkr.ac.in', password='pw12345!', role='CLUB_LEAD')
        self.hackathon = Hackathon.objects.create(
            title='Deletable', slug='deletable-hack', theme='t', description='d',
            start_date=_dates()[0], end_date=_dates()[1],
        )
        team = Team.objects.create(hackathon=self.hackathon, name='Doomed', leader=self.admin)
        TeamMember.objects.create(team=team, hackathon=self.hackathon, user=self.admin, role=TeamRole.LEADER)
        Round.objects.create(hackathon=self.hackathon, order=1, name='Round 1')

    def test_hide_and_show_keep_the_scheduled_window(self):
        window_end = timezone.now() + timezone.timedelta(days=30)
        self.hackathon.visible_until = window_end
        self.hackathon.save()
        self.client.force_authenticate(self.admin)

        hidden = self.client.post(f'/api/hackathons/{self.hackathon.slug}/hide/')
        self.assertTrue(hidden.data['is_hidden'])
        self.assertTrue(hidden.data['is_manually_hidden'])
        self.assertIsNone(APIClient().get(f'/api/hackathons/{self.hackathon.slug}/').data.get('slug'))

        shown = self.client.post(f'/api/hackathons/{self.hackathon.slug}/show/')
        self.assertFalse(shown.data['is_hidden'])
        self.assertFalse(shown.data['is_manually_hidden'])
        self.hackathon.refresh_from_db()
        self.assertEqual(self.hackathon.visible_until, window_end)
        self.assertEqual(APIClient().get(f'/api/hackathons/{self.hackathon.slug}/').status_code, 200)

    def test_show_does_not_override_an_expired_window(self):
        self.hackathon.visible_until = timezone.now() - timezone.timedelta(hours=1)
        self.hackathon.save()
        self.client.force_authenticate(self.admin)
        resp = self.client.post(f'/api/hackathons/{self.hackathon.slug}/show/')
        self.assertTrue(resp.data['is_hidden'])
        self.assertFalse(resp.data['is_manually_hidden'])

    def test_delete_impact_counts_what_will_be_removed(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.get(f'/api/hackathons/{self.hackathon.slug}/delete-impact/')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['teams'], 1)
        self.assertEqual(resp.data['members'], 1)
        self.assertEqual(resp.data['rounds'], 1)

    def test_delete_requires_the_matching_slug(self):
        self.client.force_authenticate(self.admin)
        for query in ('', '?confirm_slug=wrong'):
            resp = self.client.delete(f'/api/hackathons/{self.hackathon.slug}/{query}')
            self.assertEqual(resp.status_code, 400, resp.data)
            self.assertEqual(resp.data['code'], 'CONFIRM_SLUG_MISMATCH')
        self.assertTrue(Hackathon.objects.filter(pk=self.hackathon.pk).exists())

    def test_admin_delete_cascades_keeps_the_form_and_writes_an_audit_entry(self):
        form = Form.objects.create(title='Reg', slug='reg-form', status=FormStatus.PUBLISHED)
        self.hackathon.registration_form = form
        self.hackathon.save()
        self.client.force_authenticate(self.admin)

        resp = self.client.delete(f'/api/hackathons/{self.hackathon.slug}/?confirm_slug={self.hackathon.slug}')
        self.assertEqual(resp.status_code, 204, getattr(resp, 'data', None))
        self.assertFalse(Hackathon.objects.filter(pk=self.hackathon.pk).exists())
        self.assertEqual(Team.objects.filter(name='Doomed').count(), 0)
        self.assertTrue(Form.objects.filter(pk=form.pk).exists())
        entry = AuditLog.objects.get(action='Deleted Hackathon', target_id='deletable-hack')
        self.assertEqual(entry.details['deleted']['teams'], 1)

    def test_club_lead_cannot_delete_or_read_impact_but_can_hide(self):
        self.client.force_authenticate(self.club_lead)
        slug = self.hackathon.slug
        self.assertEqual(self.client.delete(f'/api/hackathons/{slug}/?confirm_slug={slug}').status_code, 403)
        self.assertEqual(self.client.get(f'/api/hackathons/{slug}/delete-impact/').status_code, 403)
        self.assertEqual(self.client.post(f'/api/hackathons/{slug}/hide/').status_code, 200)
        self.assertTrue(Hackathon.objects.filter(pk=self.hackathon.pk).exists())


class HackathonBannerCropTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(username='crop_admin', email='crop_admin@srkr.ac.in', password='pw12345!', role='ADMIN')
        self.hackathon = Hackathon.objects.create(
            title='Cropped', slug='cropped-hack', theme='t', description='d',
            start_date=_dates()[0], end_date=_dates()[1], banner_image='https://example.com/banner.jpg',
        )
        self.client.force_authenticate(self.admin)
        self.url = f'/api/hackathons/{self.hackathon.slug}/'

    def test_banner_crop_defaults_to_empty(self):
        self.assertEqual(self.client.get(self.url).data['banner_crop'], {})

    def test_admin_can_save_and_clear_banner_crop(self):
        resp = self.client.patch(self.url, {'banner_crop': {'x': 30, 'y': 72.5, 'zoom': 1.5}}, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['banner_crop'], {'x': 30.0, 'y': 72.5, 'zoom': 1.5})
        cleared = self.client.patch(self.url, {'banner_crop': {}}, format='json')
        self.assertEqual(cleared.data['banner_crop'], {})

    def test_invalid_banner_crop_is_rejected(self):
        for bad in ({'x': 120, 'y': 50, 'zoom': 1}, {'x': 50, 'y': 50, 'zoom': 5}, {'x': 'a', 'y': 1, 'zoom': 1},
                    {'x': 50, 'y': 50}, {'x': 50, 'y': 50, 'zoom': 1, 'rotate': 90}, [1, 2, 3]):
            resp = self.client.patch(self.url, {'banner_crop': bad}, format='json')
            self.assertEqual(resp.status_code, 400, bad)
        self.hackathon.refresh_from_db()
        self.assertEqual(self.hackathon.banner_crop, {})
