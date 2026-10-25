"""A teammate's mobile number is shown to people on the same team, and never in the team finder."""

from django.core.cache import cache
from rest_framework.test import APIClient, APITestCase

from apps.hackathons.models import TeamMember, TeamStatus
from .helpers import make_hackathon, make_team, make_user

NUMBER = "9111111111"


def api(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


class MemberPhoneTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.hack = make_hackathon(min_team_size=2, max_team_size=3, status="LIVE", features={"team_finder": True})
        self.base = f"/api/hackathons/{self.hack.slug}"
        self.leader = make_user()
        self.leader.phone_number = None
        self.leader.save()
        self.mate = make_user(first_name="Mia")
        self.mate.phone_number = NUMBER
        self.mate.save()
        self.team = make_team(self.hack, self.leader, name="Open Door", status=TeamStatus.FORMING)
        self.team.looking_for_members, self.team.looking_for = True, "a designer"
        self.team.save()
        TeamMember.objects.create(team=self.team, user=self.mate, role="MEMBER")

    def numbers(self, user):
        members = api(user).get(f"{self.base}/my-team/").data["team"]["members"]
        return {m["name"]: m["phone_number"] for m in members}

    def test_the_leader_sees_a_member_number_on_the_dashboard(self):
        self.assertEqual(self.numbers(self.leader)["Mia Last"], NUMBER)

    def test_a_member_sees_their_teammates_numbers(self):
        self.assertIn(NUMBER, self.numbers(self.mate).values())

    def test_a_person_without_a_number_shows_none(self):
        self.assertIsNone(next(v for k, v in self.numbers(self.leader).items() if k != "Mia Last"))

    def test_the_team_finder_never_contains_a_number(self):
        outsider = make_user(first_name="Olu")
        outsider.phone_number = "9123456780"
        outsider.save()
        reply = api(outsider).get(f"{self.base}/finder/")
        self.assertEqual(reply.status_code, 200)
        body = reply.content.decode()
        self.assertNotIn(NUMBER, body)
        self.assertNotIn("9123456780", body)
        self.assertNotIn("phone", body)

    def test_someone_outside_the_team_gets_no_team_payload(self):
        outsider = make_user(first_name="Olu")
        self.assertIsNone(api(outsider).get(f"{self.base}/my-team/").data["team"])


class LeaderSeesNumberBeforeDecidingTests(APITestCase):
    """A leader may call someone before inviting them or accepting their request; nobody else sees that number."""

    def setUp(self):
        from apps.hackathons.models import TeamSeeker
        cache.clear()
        self.hack = make_hackathon(min_team_size=2, max_team_size=3, status="LIVE", features={"team_finder": True})
        self.base = f"/api/hackathons/{self.hack.slug}"
        self.leader = make_user()
        self.team = make_team(self.hack, self.leader, name="Open Door", status=TeamStatus.FORMING)
        self.team.looking_for_members, self.team.looking_for = True, "a designer"
        self.team.save()
        self.seeker = make_user(first_name="Sita")
        self.seeker.phone_number = "9222222222"
        self.seeker.save()
        TeamSeeker.objects.create(hackathon=self.hack, user=self.seeker, skills="design", is_active=True)

    def seekers_seen_by(self, user):
        return api(user).get(f"{self.base}/finder/").data["seekers"]

    def test_a_team_leader_sees_a_listed_persons_number(self):
        self.assertEqual(self.seekers_seen_by(self.leader)[0]["phone"], "9222222222")

    def test_someone_who_leads_no_team_does_not(self):
        other = make_user(first_name="Omar")
        self.assertNotIn("phone", self.seekers_seen_by(other)[0])
        self.assertNotIn("9222222222", api(other).get(f"{self.base}/finder/").content.decode())

    def test_the_leader_sees_the_requesters_number_on_an_incoming_request(self):
        sent = api(self.seeker).post(f"{self.base}/finder/teams/{self.team.pk}/request/", {"message": "hi"}, format="json")
        self.assertEqual(sent.status_code, 200, sent.data)
        incoming = api(self.leader).get(f"{self.base}/finder/requests/").data["incoming"]
        self.assertEqual(incoming[0]["phone"], "9222222222")

    def test_the_requester_does_not_get_a_number_back_in_their_own_requests(self):
        api(self.seeker).post(f"{self.base}/finder/teams/{self.team.pk}/request/", {"message": "hi"}, format="json")
        mine = api(self.seeker).get(f"{self.base}/finder/").data["my_requests"]
        self.assertTrue(mine)
        self.assertTrue(all("phone" not in row for row in mine))
