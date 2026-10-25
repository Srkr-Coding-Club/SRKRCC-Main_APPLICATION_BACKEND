"""Team finder: asking a recruiting team to take you in, and the leader's accept / decline (also from the notification)."""

from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.audit.models import AuditLog
from apps.hackathons.models import HackathonStaff, JoinRequestStatus, TeamInvite, TeamJoinRequest, TeamMember, TeamStatus
from apps.notifications.models import Notification
from .helpers import make_hackathon, make_team, make_user

HOUR = timedelta(hours=1)


def api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


class JoinCase(APITestCase):
    def setUp(self):
        cache.clear()
        self.hack = make_hackathon(min_team_size=2, max_team_size=3, status="LIVE", features={"team_finder": True})
        self.base = f"/api/hackathons/{self.hack.slug}"
        self.leader = make_user()
        self.team = make_team(self.hack, self.leader, name="Open Door", status=TeamStatus.FORMING)
        self.team.looking_for_members, self.team.looking_for = True, "a designer"
        self.team.save()
        self.seeker = make_user(first_name="Sita")

    def ask(self, user=None, team=None, **body):
        return api(user or self.seeker).post(f"{self.base}/finder/teams/{(team or self.team).pk}/request/", {"message": "hi", **body}, format="json")

    def decide(self, request_id, action, user=None):
        return api(user or self.leader).post(f"{self.base}/finder/requests/{request_id}/{action}/")

    def first_request(self):
        return TeamJoinRequest.objects.get()

    def action_notification(self):
        return Notification.objects.get(recipient=self.leader, action_kind="JOIN_REQUEST")


class AskingToJoin(JoinCase):
    def test_a_request_is_stored_and_the_leader_gets_a_notification_with_actions(self):
        with self.captureOnCommitCallbacks(execute=True):
            reply = self.ask()
        self.assertEqual(reply.status_code, 200, reply.data)
        request = self.first_request()
        self.assertEqual((request.status, request.team_id, request.user_id, request.message), (JoinRequestStatus.PENDING, self.team.pk, self.seeker.pk, "hi"))
        note = self.action_notification()
        self.assertEqual(note.action_ref, f"{self.hack.slug}:{request.pk}")
        self.assertEqual(note.action_outcome, "")
        self.assertIn("Sita", note.title)
        self.assertNotIn(self.seeker.email, note.message)          # emails are never shown
        self.assertTrue(AuditLog.objects.filter(action="Asked To Join Hackathon Team").exists())

    def test_nothing_changes_for_the_team_until_the_leader_answers(self):
        self.ask()
        self.assertEqual(self.team.memberships.count(), 1)
        self.assertFalse(TeamMember.objects.filter(user=self.seeker).exists())

    def test_the_finder_listing_tells_the_requester_what_they_asked(self):
        self.ask()
        mine = api(self.seeker).get(f"{self.base}/finder/").data["my_requests"]
        self.assertEqual([(r["team_id"], r["status"]) for r in mine], [(self.team.pk, "PENDING")])

    def test_asking_twice_the_same_team_is_refused(self):
        self.ask()
        reply = self.ask()
        self.assertEqual((reply.status_code, reply.data["code"]), (409, "ALREADY_REQUESTED"))
        self.assertEqual(TeamJoinRequest.objects.count(), 1)

    def test_at_most_five_requests_can_wait_at_once(self):
        for number in range(5):
            other = make_team(self.hack, make_user(), name=f"Team {number}", status=TeamStatus.FORMING)
            other.looking_for_members = True
            other.save()
            self.assertEqual(self.ask(team=other).status_code, 200)
        reply = self.ask()
        self.assertEqual((reply.status_code, reply.data["code"]), (429, "TOO_MANY_REQUESTS"))

    def test_someone_already_on_a_team_cannot_ask(self):
        make_team(self.hack, self.seeker, name="Mine")
        self.assertEqual(self.ask().data["code"], "ALREADY_IN_TEAM")

    def test_a_team_that_is_not_recruiting_full_or_unknown_is_not_askable(self):
        self.team.looking_for_members = False
        self.team.save()
        self.assertEqual(self.ask().status_code, 404)
        self.team.looking_for_members = True
        self.team.save()
        TeamMember.objects.create(team=self.team, user=make_user())
        TeamMember.objects.create(team=self.team, user=make_user())
        self.assertEqual(self.ask().data["code"], "TEAM_FULL")
        self.assertEqual(api(self.seeker).post(f"{self.base}/finder/teams/999999/request/", {}, format="json").status_code, 404)

    def test_closed_registration_and_a_disabled_finder_are_refused(self):
        type(self.hack).objects.filter(pk=self.hack.pk).update(registration_closes_at=timezone.now() - HOUR)
        self.assertEqual(self.ask().data["code"], "REGISTRATION_CLOSED")
        type(self.hack).objects.filter(pk=self.hack.pk).update(registration_closes_at=None, features={})
        self.assertEqual(self.ask().data["code"], "FEATURE_OFF")

    def test_the_message_is_limited_and_signing_in_is_required(self):
        self.assertEqual(self.ask(message="x" * 301).data["field"], "message")
        self.assertEqual(self.ask(message="").status_code, 200)
        self.assertEqual(api().post(f"{self.base}/finder/teams/{self.team.pk}/request/", {}, format="json").status_code, 401)

    def test_what_the_hackathon_needs_to_know_is_checked_before_the_request_is_sent(self):
        type(self.hack).objects.filter(pk=self.hack.pk).update(
            require_rules_acceptance=True, rules="Be kind.",
            registration_questions=[{"key": "size", "label": "T-shirt size", "type": "text", "required": True}],
        )
        self.assertEqual(self.ask().data["code"], "RULES_NOT_ACCEPTED")
        self.assertEqual(self.ask(accept_rules=True).data["code"], "ANSWER_REQUIRED")
        self.assertEqual(self.ask(accept_rules=True, answers={"size": "M"}).status_code, 200)
        self.assertEqual(self.first_request().registration_data, {"accept_rules": True, "answers": {"size": "M"}})

    def test_someone_who_does_not_meet_the_eligibility_rules_cannot_ask(self):
        type(self.hack).objects.filter(pk=self.hack.pk).update(eligibility={"branches": ["ECE"]})
        self.assertEqual(self.ask().data["code"], "NOT_ELIGIBLE")


class TheLeaderAnswers(JoinCase):
    def setUp(self):
        super().setUp()
        with self.captureOnCommitCallbacks(execute=True):
            self.ask()
        self.request = self.first_request()

    def test_accepting_adds_the_person_and_registers_the_team(self):
        with self.captureOnCommitCallbacks(execute=True):
            reply = self.decide(self.request.pk, "accept")
        self.assertEqual(reply.status_code, 200, reply.data)
        self.request.refresh_from_db()
        self.assertEqual((self.request.status, self.request.decided_by), (JoinRequestStatus.ACCEPTED, self.leader))
        member = TeamMember.objects.get(user=self.seeker)
        self.assertEqual((member.team_id, member.role), (self.team.pk, "MEMBER"))
        self.team.refresh_from_db()
        self.assertEqual(self.team.status, TeamStatus.REGISTERED)       # it now meets the minimum size
        self.assertTrue(Notification.objects.filter(recipient=self.seeker, title__startswith="You're in").exists())
        self.assertTrue(AuditLog.objects.filter(action="Accepted Hackathon Join Request").exists())

    def test_the_stored_answers_become_the_members_answers(self):
        type(self.hack).objects.filter(pk=self.hack.pk).update(
            require_rules_acceptance=True, rules="Be kind.",
            registration_questions=[{"key": "size", "label": "T-shirt size", "type": "text", "required": True}],
        )
        other_seeker = make_user()
        self.ask(user=other_seeker, accept_rules=True, answers={"size": "L"})
        request = TeamJoinRequest.objects.get(user=other_seeker)
        self.assertEqual(self.decide(request.pk, "accept").status_code, 200)
        member = TeamMember.objects.get(user=other_seeker)
        self.assertEqual(member.answers, {"size": "L"})
        self.assertIsNotNone(member.rules_accepted_at)

    def test_declining_tells_the_person_and_changes_nothing_else(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.decide(self.request.pk, "decline").status_code, 200)
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, JoinRequestStatus.DECLINED)
        self.assertFalse(TeamMember.objects.filter(user=self.seeker).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.seeker, title__contains="declined").exists())

    def test_the_notification_shows_the_outcome_instead_of_buttons_once_answered(self):
        self.decide(self.request.pk, "accept")
        note = self.action_notification()
        self.assertEqual(note.action_outcome, "ACCEPTED")
        self.assertTrue(note.is_read)
        listed = api(self.leader).get("/api/notifications/").data
        rows = listed["results"] if isinstance(listed, dict) else listed
        row = next(r for r in rows if r["action_kind"] == "JOIN_REQUEST")
        self.assertEqual((row["action_outcome"], row["action_ref"]), ("ACCEPTED", f"{self.hack.slug}:{self.request.pk}"))

    def test_answering_twice_is_refused_and_says_what_happened(self):
        self.decide(self.request.pk, "accept")
        reply = self.decide(self.request.pk, "decline")
        self.assertEqual((reply.status_code, reply.data["code"], reply.data["outcome"]), (409, "REQUEST_NOT_PENDING", "ACCEPTED"))
        self.assertTrue(TeamMember.objects.filter(user=self.seeker).exists())

    def test_only_the_leader_or_a_team_organizer_may_answer(self):
        member = make_user()
        TeamMember.objects.create(team=self.team, user=member)
        for outsider in (self.seeker, member, make_user()):
            self.assertEqual(self.decide(self.request.pk, "accept", user=outsider).status_code, 403, outsider.username)
        self.assertEqual(api().post(f"{self.base}/finder/requests/{self.request.pk}/accept/").status_code, 401)
        organizer = make_user()
        HackathonStaff.objects.create(hackathon=self.hack, user=organizer, role="ORGANIZER", permissions=[])
        self.assertEqual(self.decide(self.request.pk, "accept", user=organizer).status_code, 200)

    def test_a_request_of_another_hackathon_or_junk_is_not_found(self):
        other = make_hackathon(features={"team_finder": True})
        self.assertEqual(api(self.leader).post(f"/api/hackathons/{other.slug}/finder/requests/{self.request.pk}/accept/").status_code, 404)
        self.assertEqual(self.decide(999999, "accept").status_code, 404)
        self.assertEqual(self.decide(self.request.pk, "maybe").data["code"], "INVALID")
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, JoinRequestStatus.PENDING)

    def test_a_team_that_filled_up_meanwhile_cannot_accept_and_the_request_stays_open(self):
        TeamMember.objects.create(team=self.team, user=make_user())
        TeamMember.objects.create(team=self.team, user=make_user())
        reply = self.decide(self.request.pk, "accept")
        self.assertEqual((reply.status_code, reply.data["code"]), (400, "TEAM_FULL"))
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, JoinRequestStatus.PENDING)
        listed = api(self.leader).get(f"{self.base}/finder/requests/").data["incoming"][0]
        self.assertEqual(listed["blocked_reason"], "Your team is already full.")
        self.assertEqual(self.decide(self.request.pk, "decline").status_code, 200)   # but it can still be declined

    def test_someone_who_joined_another_team_meanwhile_cannot_be_accepted(self):
        make_team(self.hack, self.seeker, name="Elsewhere")
        reply = self.decide(self.request.pk, "accept")
        self.assertEqual((reply.status_code, reply.data["code"]), (400, "ALREADY_IN_TEAM"))

    def test_registration_closing_or_locked_teams_block_acceptance(self):
        type(self.hack).objects.filter(pk=self.hack.pk).update(team_edits_locked=True)
        self.assertEqual(self.decide(self.request.pk, "accept").data["code"], "TEAM_EDITS_LOCKED")
        type(self.hack).objects.filter(pk=self.hack.pk).update(team_edits_locked=False, registration_closes_at=timezone.now() - HOUR)
        self.assertEqual(self.decide(self.request.pk, "accept").data["code"], "REGISTRATION_CLOSED")

    def test_a_withdrawn_team_cannot_accept(self):
        type(self.team).objects.filter(pk=self.team.pk).update(status=TeamStatus.WITHDRAWN)
        self.assertEqual(self.decide(self.request.pk, "accept").data["code"], "TEAM_INACTIVE")

    def test_joining_a_team_withdraws_every_other_open_request_and_invite(self):
        other_team = make_team(self.hack, make_user(), name="Second", status=TeamStatus.FORMING)
        other_team.looking_for_members = True
        other_team.save()
        with self.captureOnCommitCallbacks(execute=True):
            self.ask(team=other_team)
        invite = TeamInvite.objects.create(team=other_team, hackathon=self.hack, invited_user=self.seeker, invited_by=other_team.leader)
        with self.captureOnCommitCallbacks(execute=True):
            self.decide(self.request.pk, "accept")
        second = TeamJoinRequest.objects.get(team=other_team)
        self.assertEqual(second.status, JoinRequestStatus.CANCELLED)
        invite.refresh_from_db()
        self.assertEqual(invite.status, "CANCELLED")
        note = Notification.objects.get(recipient=other_team.leader, action_kind="JOIN_REQUEST")
        self.assertEqual(note.action_outcome, "CANCELLED")    # that leader's buttons give way to the outcome too

    def test_accepting_an_invite_elsewhere_cancels_the_open_request_too(self):
        other_team = make_team(self.hack, make_user(), name="Second", status=TeamStatus.FORMING)
        invite = TeamInvite.objects.create(team=other_team, hackathon=self.hack, invited_user=self.seeker, invited_by=other_team.leader)
        self.assertEqual(api(self.seeker).post(f"/api/hackathons/invites/{invite.pk}/accept/", {}, format="json").status_code, 200)
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, JoinRequestStatus.CANCELLED)

    def test_a_declined_request_cannot_be_sent_again_for_a_day(self):
        self.decide(self.request.pk, "decline")
        reply = self.ask()
        self.assertEqual((reply.status_code, reply.data["code"]), (409, "DECLINED_RECENTLY"))
        TeamJoinRequest.objects.update(decided_at=timezone.now() - timedelta(hours=25))
        self.assertEqual(self.ask().status_code, 200)

    def test_the_new_leader_sees_requests_after_leadership_moves(self):
        member = make_user()
        TeamMember.objects.create(team=self.team, user=member)
        type(self.team).objects.filter(pk=self.team.pk).update(leader=member)
        self.assertEqual(len(api(member).get(f"{self.base}/finder/requests/").data["incoming"]), 1)
        self.assertEqual(api(self.leader).get(f"{self.base}/finder/requests/").data["incoming"], [])


class TheSenderTakesItBack(JoinCase):
    def setUp(self):
        super().setUp()
        with self.captureOnCommitCallbacks(execute=True):
            self.ask()
        self.request = self.first_request()

    def test_cancelling_works_and_the_notification_follows(self):
        reply = self.decide(self.request.pk, "cancel", user=self.seeker)
        self.assertEqual(reply.status_code, 200, reply.data)
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, JoinRequestStatus.CANCELLED)
        self.assertEqual(self.action_notification().action_outcome, "CANCELLED")
        self.assertEqual(self.decide(self.request.pk, "accept").data["code"], "REQUEST_NOT_PENDING")

    def test_only_the_sender_can_cancel_and_not_after_it_was_answered(self):
        self.assertEqual(self.decide(self.request.pk, "cancel", user=self.leader).status_code, 404)
        self.assertEqual(self.decide(self.request.pk, "cancel", user=make_user()).status_code, 404)
        self.decide(self.request.pk, "decline")
        self.assertEqual(self.decide(self.request.pk, "cancel", user=self.seeker).data["code"], "REQUEST_NOT_PENDING")

    def test_a_cancelled_request_can_be_sent_again(self):
        self.decide(self.request.pk, "cancel", user=self.seeker)
        self.assertEqual(self.ask().status_code, 200)

    def test_the_lists_show_each_side_only_what_is_theirs(self):
        mine = api(self.seeker).get(f"{self.base}/finder/requests/").data
        self.assertEqual((mine["incoming"], [r["status"] for r in mine["mine"]]), ([], ["PENDING"]))
        theirs = api(self.leader).get(f"{self.base}/finder/requests/").data
        incoming = theirs["incoming"][0]
        self.assertEqual((incoming["name"], incoming["skills"], theirs["mine"]), ("Sita Last", "", []))
        self.assertNotIn(self.seeker.email, str(theirs))
        self.assertEqual(api(make_user()).get(f"{self.base}/finder/requests/").data, {"incoming": [], "mine": []})

    def test_one_person_cannot_hold_two_pending_requests_to_the_same_team_even_by_racing(self):
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            TeamJoinRequest.objects.create(hackathon=self.hack, team=self.team, user=self.seeker)
