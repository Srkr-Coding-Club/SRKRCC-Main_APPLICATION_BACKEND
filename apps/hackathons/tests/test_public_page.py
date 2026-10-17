from django.test import TestCase
from rest_framework.test import APIClient

from .helpers import make_hackathon, make_user


class PublicPageFieldsTests(TestCase):
    def setUp(self):
        self.admin = make_user(role='ADMIN')
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.hack = make_hackathon()
        self.url = f'/api/hackathons/{self.hack.slug}/'

    def _patch(self, **fields):
        return self.client.patch(self.url, fields, format='json')

    def test_defaults_are_sensible_for_existing_hackathons(self):
        data = APIClient().get(self.url).data
        self.assertEqual((data['mode'], data['venue'], data['organizer_name']), ('ONLINE', '', ''))
        self.assertEqual((data['tags'], data['prizes']), ([], []))

    def test_admin_can_customise_the_presentation(self):
        response = self._patch(
            mode='HYBRID', venue='Main Auditorium', organizer_name='SRKR Coding Club',
            logo_image='https://example.com/logo.png', tags=['AI', ' Web ', 'ai', ''],
            prizes=[{'title': 'Winner', 'amount': '₹25,000', 'description': 'Cash + trophy'}, {'title': 'Runner up'}],
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['mode'], 'HYBRID')
        self.assertEqual(response.data['tags'], ['AI', 'Web'])
        self.assertEqual(response.data['prizes'][1], {'title': 'Runner up', 'amount': '', 'description': ''})
        public = APIClient().get(self.url).data
        self.assertEqual(public['venue'], 'Main Auditorium')

    def test_invalid_values_are_rejected(self):
        for payload in (
            {'mode': 'MOON'},
            {'tags': 'AI'},
            {'tags': ['x' * 41]},
            {'tags': [str(i) for i in range(11)]},
            {'prizes': 'cash'},
            {'prizes': [{'amount': '1'}]},
            {'prizes': [{'title': 'x' * 101}]},
            {'prizes': [{'title': 'P'}] * 13},
        ):
            self.assertEqual(self._patch(**payload).status_code, 400, payload)
