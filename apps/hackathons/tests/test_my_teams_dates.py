from django.test import TestCase
from rest_framework.test import APIClient

from .helpers import make_hackathon, make_team, make_user


class MyTeamsDatesTests(TestCase):
    """GET /api/hackathons/my-teams/ carries the hackathon's dates so the profile can sort teams into Upcoming and Past."""

    def test_each_team_carries_its_hackathons_start_and_end(self):
        hackathon = make_hackathon()
        leader = make_user()
        make_team(hackathon, leader)
        client = APIClient()
        client.force_authenticate(leader)
        item = client.get('/api/hackathons/my-teams/').json()[0]
        self.assertEqual(item['hackathon_title'], hackathon.title)
        self.assertTrue(item['hackathon_start'])
        self.assertTrue(item['hackathon_end'])
        self.assertLess(item['hackathon_start'], item['hackathon_end'])
