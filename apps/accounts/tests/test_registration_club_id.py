from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

User = get_user_model()


class RegistrationClubIdTests(TestCase):
    """POST /api/auth/register/ — optional 'Affiliate ID' (club_id) field.

    A club representative sometimes hands a prospective member their Club ID
    before they ever touch the site. Signup should accept it if given, and
    leave club_id unset (assigned later, same as today) if not.
    """

    def setUp(self):
        self.client = APIClient()

    def _payload(self, **overrides):
        payload = {
            'username': 'newmember',
            'email': 'newmember@srkr.ac.in',
            'password': 'StrongPass123!',
            'first_name': 'New',
            'last_name': 'Member',
            'roll_number': '22B91A0599',
            'branch': 'CSE',
            'year': 2,
        }
        payload.update(overrides)
        return payload

    def test_signup_without_affiliate_id_leaves_club_id_unset(self):
        resp = self.client.post('/api/auth/register/', self._payload(), format='json')
        self.assertEqual(resp.status_code, 201)
        user = User.objects.get(email='newmember@srkr.ac.in')
        self.assertIsNone(user.club_id)

    def test_signup_with_blank_affiliate_id_is_fine(self):
        resp = self.client.post('/api/auth/register/', self._payload(club_id=''), format='json')
        self.assertEqual(resp.status_code, 201)
        user = User.objects.get(email='newmember@srkr.ac.in')
        self.assertIsNone(user.club_id)

    def test_signup_with_valid_unclaimed_affiliate_id_attaches_it(self):
        resp = self.client.post('/api/auth/register/', self._payload(club_id='25scc301'), format='json')
        self.assertEqual(resp.status_code, 201)
        user = User.objects.get(email='newmember@srkr.ac.in')
        self.assertEqual(user.club_id, '25SCC301')  # normalized to canonical uppercase form

    def test_signup_with_already_taken_affiliate_id_is_rejected(self):
        User.objects.create_user(
            username='existing', email='existing@srkr.ac.in', password='pw12345!',
            club_id='25SCC277',
        )
        resp = self.client.post('/api/auth/register/', self._payload(club_id='25SCC277'), format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('club_id', resp.data)
        self.assertFalse(User.objects.filter(email='newmember@srkr.ac.in').exists())

    def test_signup_with_malformed_affiliate_id_is_rejected(self):
        resp = self.client.post('/api/auth/register/', self._payload(club_id='not-a-club-id'), format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('club_id', resp.data)
