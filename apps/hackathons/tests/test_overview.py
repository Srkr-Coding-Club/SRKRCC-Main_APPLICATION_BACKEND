"""GET /overview/: the whole organizer Overview in one response, each part only for people allowed to see it."""
from unittest import mock

from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.hackathons.models import HackathonStaff, PaymentStage, TeamStatus
from .helpers import make_hackathon, make_team, make_user


def _api(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


class OverviewEndpointTests(APITestCase):
    def setUp(self):
        self.hack = make_hackathon(min_team_size=1, features={'ideas': True, 'team_finder': True}, payment_stage=PaymentStage.REGISTRATION, idea_fee_amount=200)
        self.url = f'/api/hackathons/{self.hack.slug}/overview/'
        self.admin = _api(make_user(role='ADMIN'))
        make_team(self.hack, make_user(), name='Alpha')

    def test_an_organizer_gets_every_part_in_one_call(self):
        data = self.admin.get(self.url).data
        self.assertEqual(data['hidden'], [])
        self.assertEqual(data['errors'], {})
        self.assertEqual(data['stats']['teams_by_status']['REGISTERED'], 1)
        self.assertEqual(data['analytics']['capacity']['placed_teams'], 1)
        self.assertEqual(data['ideas']['missing'][0]['name'], 'Alpha')
        self.assertEqual(data['ideas']['counts']['SUBMITTED'], 0)
        self.assertEqual(data['payments']['fee_amount'], 200)
        self.assertEqual(data['payments']['payments'], [])
        self.assertTrue(data['finder']['enabled'])
        self.assertEqual(data['withdrawals'], {'pending': 0})

    def test_payment_rows_carry_only_what_the_money_block_needs(self):
        from apps.hackathons.models import TeamPayment
        team = make_team(self.hack, make_user(), name='Payer', status=TeamStatus.PAYMENT_PENDING)
        TeamPayment.objects.create(hackathon=self.hack, team=team, amount=200, transaction_id='T1')
        row = self.admin.get(self.url).data['payments']['payments'][0]
        self.assertEqual(set(row), {'status', 'amount', 'team_status', 'submitted_at'})

    def test_parts_for_modules_that_are_off_are_hidden(self):
        self.hack.features, self.hack.payment_stage = {}, PaymentStage.NONE
        self.hack.save()
        data = self.admin.get(self.url).data
        self.assertEqual(sorted(data['hidden']), ['finder', 'ideas', 'payments'])
        self.assertIsNone(data['ideas'])
        self.assertIsNotNone(data['withdrawals'])

    def test_someone_with_only_insights_sees_the_numbers_and_nothing_private(self):
        viewer = make_user()
        HackathonStaff.objects.create(hackathon=self.hack, user=viewer, role='VOLUNTEER', permissions=['insights'])
        data = _api(viewer).get(self.url).data
        self.assertIn('stats', data)
        self.assertEqual(sorted(data['hidden']), ['finder', 'ideas', 'payments', 'withdrawals'])
        self.assertIsNone(data['payments'])

    def test_people_without_insights_are_refused(self):
        self.assertEqual(_api(make_user()).get(self.url).status_code, 403)
        self.assertEqual(APIClient().get(self.url).status_code, 401)

    def test_one_failing_part_is_reported_and_the_rest_still_load(self):
        with mock.patch('apps.hackathons.overview.finder_admin.overview', side_effect=RuntimeError('boom')):
            data = self.admin.get(self.url).data
        self.assertEqual(data['errors'], {'finder': 'Could not load this part.'})
        self.assertIsNone(data['finder'])
        self.assertIsNotNone(data['ideas'])
        self.assertIsNotNone(data['stats'])
