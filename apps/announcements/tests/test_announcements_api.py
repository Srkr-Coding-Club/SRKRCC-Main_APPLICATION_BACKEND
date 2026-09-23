from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.announcements.models import Announcement

User = get_user_model()


class AnnouncementsApiTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username='admin1', email='admin1@srkr.ac.in', password='pw12345!', role='ADMIN',
        )
        self.club_lead = User.objects.create_user(
            username='lead1', email='lead1@srkr.ac.in', password='pw12345!', role='CLUB_LEAD',
        )
        self.member = User.objects.create_user(
            username='member1', email='member1@srkr.ac.in', password='pw12345!', role='NON_AFFILIATE',
        )

    def _payload(self, **overrides):
        payload = dict(title='Workshop Tomorrow', message='Join us at 10 AM in the seminar hall.', type='INFO')
        payload.update(overrides)
        return payload

    # --- Public / read access -----------------------------------------------

    def test_anonymous_can_list_active_announcements(self):
        Announcement.objects.create(title='Active One', message='Visible', is_active=True)
        resp = self.client.get('/api/announcements/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 1)

    def test_anonymous_does_not_see_inactive_announcements(self):
        Announcement.objects.create(title='Hidden One', message='Not visible', is_active=False)
        resp = self.client.get('/api/announcements/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 0)

    def test_admin_sees_both_active_and_inactive(self):
        Announcement.objects.create(title='Active', message='m', is_active=True)
        Announcement.objects.create(title='Inactive', message='m', is_active=False)
        self.client.force_authenticate(self.admin)
        resp = self.client.get('/api/announcements/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 2)

    def test_member_read_is_also_filtered_to_active_only(self):
        # Read access is open to anyone (SAFE_METHODS), but a regular member
        # is not admin/club-lead, so they get the same public-filtered view
        # as an anonymous caller — not the admin's full list.
        Announcement.objects.create(title='Active', message='m', is_active=True)
        Announcement.objects.create(title='Inactive', message='m', is_active=False)
        self.client.force_authenticate(self.member)
        resp = self.client.get('/api/announcements/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 1)

    def test_newest_first_ordering(self):
        first = Announcement.objects.create(title='First', message='m')
        second = Announcement.objects.create(title='Second', message='m')
        self.client.force_authenticate(self.admin)
        resp = self.client.get('/api/announcements/')
        self.assertEqual(resp.data[0]['id'], second.id)
        self.assertEqual(resp.data[1]['id'], first.id)

    # --- Write authorization -------------------------------------------------

    def test_anonymous_cannot_create(self):
        resp = self.client.post('/api/announcements/', self._payload(), format='json')
        self.assertIn(resp.status_code, (401, 403))
        self.assertEqual(Announcement.objects.count(), 0)

    def test_regular_member_cannot_create(self):
        self.client.force_authenticate(self.member)
        resp = self.client.post('/api/announcements/', self._payload(), format='json')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(Announcement.objects.count(), 0)

    def test_admin_can_create(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.post('/api/announcements/', self._payload(), format='json')
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data['is_active'], True)  # default

    def test_club_lead_can_create(self):
        self.client.force_authenticate(self.club_lead)
        resp = self.client.post('/api/announcements/', self._payload(), format='json')
        self.assertEqual(resp.status_code, 201, resp.data)

    def test_regular_member_cannot_toggle(self):
        a = Announcement.objects.create(title='T', message='m', is_active=True)
        self.client.force_authenticate(self.member)
        resp = self.client.patch(f'/api/announcements/{a.id}/', {'is_active': False}, format='json')
        self.assertEqual(resp.status_code, 403)
        a.refresh_from_db()
        self.assertTrue(a.is_active)

    def test_admin_can_toggle_off_and_on(self):
        a = Announcement.objects.create(title='T', message='m', is_active=True)
        self.client.force_authenticate(self.admin)

        resp = self.client.patch(f'/api/announcements/{a.id}/', {'is_active': False}, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        a.refresh_from_db()
        self.assertFalse(a.is_active)

        resp = self.client.patch(f'/api/announcements/{a.id}/', {'is_active': True}, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        a.refresh_from_db()
        self.assertTrue(a.is_active)

    def test_admin_can_delete(self):
        a = Announcement.objects.create(title='T', message='m')
        self.client.force_authenticate(self.admin)
        resp = self.client.delete(f'/api/announcements/{a.id}/')
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Announcement.objects.filter(id=a.id).exists())

    def test_regular_member_cannot_delete(self):
        a = Announcement.objects.create(title='T', message='m')
        self.client.force_authenticate(self.member)
        resp = self.client.delete(f'/api/announcements/{a.id}/')
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(Announcement.objects.filter(id=a.id).exists())

    # --- Validation ------------------------------------------------------------

    def test_blank_title_is_rejected(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.post('/api/announcements/', self._payload(title=''), format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('title', resp.data)

    def test_whitespace_only_title_is_rejected(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.post('/api/announcements/', self._payload(title='   '), format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('title', resp.data)

    def test_blank_message_is_rejected(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.post('/api/announcements/', self._payload(message=''), format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('message', resp.data)

    def test_invalid_type_is_rejected(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.post('/api/announcements/', self._payload(type='NOT_A_REAL_TYPE'), format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('type', resp.data)

    def test_default_type_is_info(self):
        self.client.force_authenticate(self.admin)
        payload = self._payload()
        del payload['type']
        resp = self.client.post('/api/announcements/', payload, format='json')
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data['type'], 'INFO')

    def test_can_create_with_each_type(self):
        self.client.force_authenticate(self.admin)
        for t in ('INFO', 'SUCCESS', 'WARNING', 'URGENT'):
            resp = self.client.post(
                '/api/announcements/', self._payload(title=f'{t} announcement', type=t), format='json'
            )
            self.assertEqual(resp.status_code, 201, resp.data)
            self.assertEqual(resp.data['type'], t)
