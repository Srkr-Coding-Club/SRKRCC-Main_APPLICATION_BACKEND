"""Registration payment: the leader pays when creating the team, an organizer verifies, rejects, reopens or removes it."""
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.audit.models import AuditLog
from apps.forms.models import UploadState
from apps.forms.tests.test_upload_submission import CopyingStorage
from apps.hackathons import payments, registration
from apps.hackathons.models import HackathonStaff, IdeaFile, PaymentStatus, Team, TeamMember, TeamPayment, TeamStatus
from .helpers import make_hackathon, make_team, make_user

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


@override_settings(FORM_UPLOADS_BACKEND="s3", FORM_UPLOAD_BUCKET="test-bucket")
class PaymentCase(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.storage = CopyingStorage()
        patcher = mock.patch("apps.forms.upload_storage.get_upload_storage", return_value=self.storage)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.hack = make_hackathon(min_team_size=1, payment_stage="REGISTRATION", idea_fee_amount=200, contacts=[{"name": "Ravi (club lead)", "email": "ravi@club.org"}])
        self.slug = self.hack.slug
        self.base = f"/api/hackathons/{self.slug}"
        self.admin = make_user(role="ADMIN")
        self.leader = make_user(first_name="Asha", last_name="Rao")

    # -- helpers
    def upload(self, user=None, name="pay.png", body=PNG):
        client = api(user or self.leader)
        asked = client.post(f"{self.base}/payment-files/", {"name": name, "size": len(body)}, format="json")
        self.assertEqual(asked.status_code, 201, asked.data)
        file_id = asked.data["file"]["id"]
        self.storage.put(IdeaFile.objects.get(pk=file_id).key, body)
        done = client.post(f"{self.base}/payment-files/{file_id}/")
        self.assertEqual(done.status_code, 200, done.data)
        return file_id

    def create(self, user=None, name="Alpha", reference="UPI 123", file_id="auto", **extra):
        user = user or self.leader
        if file_id == "auto":
            file_id = self.upload(user)
        body = {"name": name, "transaction_id": reference, "payment_file_id": file_id, **extra}
        return api(user).post(f"{self.base}/teams/", body, format="json")

    def pending(self, user=None, name="Alpha", reference="UPI 123"):
        reply = self.create(user, name, reference)
        self.assertEqual(reply.status_code, 201, reply.data)
        return Team.objects.get(pk=reply.data["id"])

    def act(self, team, action, user=None, **body):
        return api(user or self.admin).post(f"{self.base}/payments/{team.payment.pk}/{action}/", body, format="json")

    def notices(self):
        """Run the on-commit notifications and return (recipients, title, message) for each."""
        return self._notify.call_args_list

    def organizer(self, permissions):
        user = make_user()
        HackathonStaff.objects.create(hackathon=self.hack, user=user, role="ORGANIZER", permissions=permissions)
        return user


class SubmittingTests(PaymentCase):
    def test_the_team_is_created_pending_with_its_payment_and_holds_no_seat(self):
        self.hack.max_teams = 1
        self.hack.save()
        team = self.pending()
        self.assertEqual((team.status, team.is_waitlisted, team.leader), (TeamStatus.PAYMENT_PENDING, False, self.leader))
        self.assertEqual(team.memberships.get().role, "LEADER")
        payment = team.payment
        self.assertEqual((payment.amount, payment.status, payment.transaction_id, payment.submitted_by), (200, PaymentStatus.PENDING, "UPI 123", self.leader))
        self.assertEqual((payment.screenshot.state, payment.screenshot.team), (UploadState.ATTACHED, team))
        self.assertEqual(registration.placed_teams(self.hack).count(), 0)
        self.assertFalse(registration.teams_full(self.hack))

    def test_the_amount_is_the_fee_at_the_time_of_submitting(self):
        team = self.pending()
        self.hack.idea_fee_amount = 500
        self.hack.save()
        self.assertEqual(TeamPayment.objects.get(team=team).amount, 200)

    def test_the_transaction_id_and_the_screenshot_are_required(self):
        reply = self.create(reference="  ", file_id=self.upload())
        self.assertEqual((reply.status_code, reply.data["field"]), (400, "transaction_id"))
        reply = self.create(file_id=None)
        self.assertEqual((reply.status_code, reply.data["field"]), (400, "payment_file_id"))
        self.assertIn("₹200", reply.data["detail"])
        reply = self.create(reference="x" * 61, file_id=self.upload())
        self.assertEqual(reply.data["field"], "transaction_id")
        self.assertFalse(Team.objects.exists() or TeamMember.objects.exists())

    def test_a_screenshot_that_is_not_theirs_or_not_finished_is_refused(self):
        other = make_user()
        theirs = self.upload(other)
        reply = self.create(file_id=theirs)
        self.assertEqual((reply.status_code, reply.data["code"], reply.data["field"]), (400, "FILE_NOT_FOUND", "payment_file_id"))
        asked = api(self.leader).post(f"{self.base}/payment-files/", {"name": "p.png", "size": 70}, format="json")
        unfinished = asked.data["file"]["id"]
        self.assertEqual(self.create(file_id=unfinished).data["code"], "FILE_NOT_FOUND")
        self.assertFalse(Team.objects.exists())

    def test_a_used_transaction_id_is_refused_whatever_its_spacing_or_case(self):
        self.pending(reference="UPI 123 456")
        reply = self.create(make_user(), name="Beta", reference="upi123456")
        self.assertEqual((reply.status_code, reply.data["code"], reply.data["field"]), (400, "TRANSACTION_REUSED", "transaction_id"))
        self.assertEqual(Team.objects.count(), 1)
        self.assertEqual(TeamMember.objects.count(), 1)

    def test_sending_the_same_payment_twice_returns_the_same_team(self):
        file_id = self.upload()
        first = self.create(file_id=file_id)
        again = self.create(file_id=file_id)
        self.assertEqual((first.status_code, again.status_code), (201, 201), again.data)
        self.assertEqual(again.data["id"], first.data["id"])
        self.assertEqual((Team.objects.count(), TeamPayment.objects.count()), (1, 1))
        other_reference = self.create(reference="DIFFERENT", file_id=file_id)
        self.assertEqual(other_reference.data["code"], "ALREADY_IN_TEAM")

    def test_a_full_hackathon_without_a_waitlist_refuses_before_taking_a_payment(self):
        self.hack.max_teams = 1
        self.hack.save()
        make_team(self.hack, make_user(), name="Placed")
        reply = self.create()
        self.assertEqual((reply.status_code, reply.data["code"]), (400, "HACKATHON_FULL"))
        self.assertEqual(Team.objects.count(), 1)

    def test_a_full_hackathon_with_a_waitlist_still_takes_the_payment_but_decides_later(self):
        self.hack.max_teams, self.hack.waitlist_enabled = 1, True
        self.hack.save()
        make_team(self.hack, make_user(), name="Placed")
        team = self.pending()
        self.assertEqual((team.status, team.is_waitlisted), (TeamStatus.PAYMENT_PENDING, False))

    def test_nothing_is_asked_when_there_is_no_fee_or_the_fee_is_paid_with_the_idea(self):
        for stage, fee in (("NONE", 0), ("REGISTRATION", 0), ("IDEA", 200)):
            hack = make_hackathon(min_team_size=1, payment_stage=stage, idea_fee_amount=fee)
            reply = api(make_user()).post(f"/api/hackathons/{hack.slug}/teams/", {"name": "Free"}, format="json")
            self.assertEqual((reply.status_code, reply.data["status"]), (201, "REGISTERED"), (stage, fee))

    def test_an_organizer_creating_a_team_does_not_need_a_payment(self):
        reply = api(self.admin).post(f"{self.base}/teams/", {"name": "By hand", "leader_email": self.leader.email}, format="json")
        self.assertEqual((reply.status_code, reply.data["status"]), (201, "REGISTERED"))
        self.assertFalse(TeamPayment.objects.exists())

    def test_the_leader_sees_their_payment_but_never_the_organizers_reason(self):
        team = self.pending()
        self.assertEqual(self.act(team, "reject", reason="Amount does not match").status_code, 200)
        body = api(self.leader).get(f"{self.base}/my-team/").data
        self.assertEqual((body["team"]["status"], body["payment"]["status"], body["payment"]["amount"]), ("PAYMENT_REJECTED", "REJECTED", 200))
        self.assertNotIn("reason", body["payment"])
        self.assertNotIn("Amount does not match", str(body))


class UploadTests(PaymentCase):
    def test_two_leaders_uploading_together_do_not_discard_each_others_screenshot(self):
        other = make_user()
        mine = api(self.leader).post(f"{self.base}/payment-files/", {"name": "a.png", "size": 70}, format="json").data["file"]["id"]
        theirs = api(other).post(f"{self.base}/payment-files/", {"name": "b.png", "size": 70}, format="json").data["file"]["id"]
        self.assertEqual(IdeaFile.objects.get(pk=mine).state, UploadState.PENDING)
        self.assertEqual(IdeaFile.objects.get(pk=theirs).state, UploadState.PENDING)
        api(self.leader).post(f"{self.base}/payment-files/", {"name": "c.png", "size": 70}, format="json")
        self.assertEqual(IdeaFile.objects.get(pk=mine).state, UploadState.DELETED)  # their own earlier pick is replaced
        self.assertEqual(IdeaFile.objects.get(pk=theirs).state, UploadState.PENDING)

    def test_uploads_are_refused_when_no_fee_is_due_or_the_person_is_already_in_a_team(self):
        free = make_hackathon(min_team_size=1)
        self.assertEqual(api(self.leader).post(f"/api/hackathons/{free.slug}/payment-files/", {"name": "a.png", "size": 70}, format="json").data["code"], "PAYMENT_NOT_REQUIRED")
        make_team(self.hack, self.leader, name="Mine")
        reply = api(self.leader).post(f"{self.base}/payment-files/", {"name": "a.png", "size": 70}, format="json")
        self.assertEqual(reply.data["code"], "ALREADY_IN_TEAM")

    def test_someone_elses_upload_cannot_be_confirmed_or_discarded(self):
        mine = api(self.leader).post(f"{self.base}/payment-files/", {"name": "a.png", "size": 70}, format="json").data["file"]["id"]
        stranger = make_user()
        self.assertEqual(api(stranger).post(f"{self.base}/payment-files/{mine}/").status_code, 404)
        self.assertEqual(api(stranger).delete(f"{self.base}/payment-files/{mine}/").status_code, 404)
        self.assertEqual(api(self.leader).delete(f"{self.base}/payment-files/{mine}/").status_code, 204)

    def test_the_hackathons_payment_qr_cannot_be_confirmed_as_a_screenshot(self):
        qr = api(self.admin).post(f"{self.base}/idea-qr/", {"name": "qr.png", "size": 70}, format="json").data["file"]["id"]
        self.assertEqual(api(self.leader).post(f"{self.base}/payment-files/{qr}/").status_code, 404)

    def test_a_wrong_file_type_is_refused(self):
        reply = api(self.leader).post(f"{self.base}/payment-files/", {"name": "pay.exe", "size": 70}, format="json")
        self.assertEqual((reply.status_code, reply.data["code"]), (400, "FILE_TYPE_NOT_ALLOWED"))

    def test_people_paying_to_register_may_see_the_payment_qr_but_strangers_to_a_free_hackathon_may_not(self):
        from apps.hackathons import idea_files

        self.assertTrue(idea_files.can_see_qr(self.hack, self.leader))
        free = make_hackathon(min_team_size=1)
        self.assertFalse(idea_files.can_see_qr(free, self.leader))
        self.assertFalse(idea_files.can_see_qr(self.hack, type("Anon", (), {"is_authenticated": False})()))


class CancelTests(PaymentCase):
    def test_the_leader_can_cancel_a_pending_team_and_start_again(self):
        team = self.pending()
        reply = api(self.leader).post(f"/api/hackathons/teams/{team.pk}/cancel-payment/")
        self.assertEqual((reply.status_code, reply.data["cancelled"]), (200, True))
        self.assertFalse(Team.objects.exists() or TeamMember.objects.exists())
        record = TeamPayment.objects.get()
        self.assertEqual((record.status, record.team_id, record.team_name, record.leader_email, record.amount), (PaymentStatus.CANCELLED, None, "Alpha", self.leader.email, 200))
        again = self.create(name="Alpha again")  # same transaction id: it was freed
        self.assertEqual(again.status_code, 201, again.data)

    def test_cancelling_does_not_count_against_the_team_creation_limit(self):
        from apps.hackathons import withdrawals

        for number in range(withdrawals.MAX_SELF_CREATED_TEAMS + 1):
            team = self.pending(name=f"Try {number}", reference=f"REF-{number}")
            self.assertEqual(api(self.leader).post(f"/api/hackathons/teams/{team.pk}/cancel-payment/").status_code, 200)
        self.assertEqual(self.create(name="Final", reference="REF-LAST").status_code, 201)

    def test_only_the_leader_can_cancel_and_only_while_pending(self):
        team = self.pending()
        member = make_user()
        TeamMember.objects.create(team=team, hackathon=self.hack, user=member)
        self.assertEqual(api(member).post(f"/api/hackathons/teams/{team.pk}/cancel-payment/").status_code, 403)
        self.assertEqual(api(make_user()).post(f"/api/hackathons/teams/{team.pk}/cancel-payment/").status_code, 404)
        self.assertEqual(self.act(team, "verify").status_code, 200)
        reply = api(self.leader).post(f"/api/hackathons/teams/{team.pk}/cancel-payment/")
        self.assertEqual((reply.status_code, reply.data["code"]), (400, "NOT_PENDING"))
        self.assertTrue(Team.objects.filter(pk=team.pk).exists())


class QueueTests(PaymentCase):
    def test_the_queue_lists_waiting_payments_first_with_counts_and_filters(self):
        first, second = self.pending(name="First", reference="TXN-AAA"), self.pending(make_user(first_name="Bo"), name="Second", reference="TXN-BBB")
        third = self.pending(make_user(), name="Third", reference="TXN-CCC")
        self.act(first, "verify")
        self.act(third, "reject", reason="blurry")
        data = api(self.admin).get(f"{self.base}/payments/").data
        self.assertEqual(data["counts"], {"PENDING": 1, "VERIFIED": 1, "REJECTED": 1})
        self.assertEqual([p["team_name"] for p in data["payments"]], ["Second", "First", "Third"])
        self.assertEqual((data["fee_amount"], data["stage"]), (200, "REGISTRATION"))
        row = data["payments"][0]
        self.assertEqual((row["leader_email"], row["amount"], row["transaction_id"], row["status"]), (second.leader.email, 200, "TXN-BBB", "PENDING"))
        self.assertEqual(row["screenshot"]["name"], "pay.png")
        self.assertEqual([p["team_name"] for p in api(self.admin).get(f"{self.base}/payments/", {"status": "REJECTED"}).data["payments"]], ["Third"])
        self.assertEqual([p["team_name"] for p in api(self.admin).get(f"{self.base}/payments/", {"search": "txn-aaa"}).data["payments"]], ["First"])
        self.assertEqual([p["team_name"] for p in api(self.admin).get(f"{self.base}/payments/", {"search": "Bo"}).data["payments"]], ["Second"])

    def test_only_organizers_with_the_teams_permission_can_use_it(self):
        team = self.pending()
        for who in (self.leader, make_user(), self.organizer(["ideas"])):
            self.assertEqual(api(who).get(f"{self.base}/payments/").status_code, 403)
            self.assertEqual(api(who).post(f"{self.base}/payments/{team.payment.pk}/verify/").status_code, 403)
        self.assertEqual(api(self.organizer(["teams"])).get(f"{self.base}/payments/").status_code, 200)
        self.assertEqual(api().get(f"{self.base}/payments/").status_code, 401)

    def test_the_screenshot_link_is_for_organizers_and_the_teams_leader_only(self):
        team = self.pending()
        url = f"{self.base}/payments/{team.payment.pk}/link/"
        self.assertEqual(api(self.admin).get(url).status_code, 200)
        self.assertEqual(api(self.leader).get(url).status_code, 200)
        self.assertEqual(api(make_user()).get(url).status_code, 404)
        self.assertTrue(AuditLog.objects.filter(action="Viewed Team Payment Proof").exists())


class VerifyTests(PaymentCase):
    def test_verifying_places_the_team_and_takes_a_seat(self):
        self.hack.max_teams = 2
        self.hack.save()
        team = self.pending()
        reply = self.act(team, "verify")
        self.assertEqual((reply.status_code, reply.data["payment"]["status"], reply.data["payment"]["reviewed_by"]), (200, "VERIFIED", f"{self.admin.first_name} {self.admin.last_name}"))
        team.refresh_from_db()
        self.assertEqual((team.status, team.is_waitlisted), (TeamStatus.REGISTERED, False))
        self.assertEqual(registration.placed_teams(self.hack).count(), 1)
        self.assertTrue(AuditLog.objects.filter(action="Verified Team Payment").exists())

    def test_a_team_below_the_minimum_size_becomes_forming(self):
        self.hack.min_team_size = 2
        self.hack.save()
        team = self.pending()
        self.act(team, "verify")
        team.refresh_from_db()
        self.assertEqual(team.status, TeamStatus.FORMING)

    def test_a_second_organizer_sees_who_already_handled_it(self):
        team = self.pending()
        self.act(team, "verify")
        for action in ("verify", "reject"):
            reply = self.act(team, action, user=self.organizer(["teams"]))
            self.assertEqual((reply.status_code, reply.data["code"], reply.data["payment_status"]), (409, "PAYMENT_ALREADY_HANDLED", "VERIFIED"))
            self.assertIn(f"{self.admin.first_name} {self.admin.last_name}", reply.data["detail"])

    def test_full_with_a_waitlist_places_the_paid_team_on_the_waitlist(self):
        self.hack.max_teams, self.hack.waitlist_enabled = 1, True
        self.hack.save()
        team = self.pending()
        make_team(self.hack, make_user(), name="Placed")
        self.assertEqual(self.act(team, "verify").status_code, 200)
        team.refresh_from_db()
        self.assertEqual((team.status, team.is_waitlisted), (TeamStatus.REGISTERED, True))

    def test_full_without_a_waitlist_blocks_the_verification_and_keeps_it_pending(self):
        self.hack.max_teams = 1
        self.hack.save()
        team = self.pending()
        make_team(self.hack, make_user(), name="Placed")
        reply = self.act(team, "verify")
        self.assertEqual((reply.status_code, reply.data["code"]), (400, "NO_SEATS"))
        team.refresh_from_db()
        self.assertEqual((team.status, team.payment.status), (TeamStatus.PAYMENT_PENDING, PaymentStatus.PENDING))

    def test_the_waitlist_follows_the_order_payments_were_verified_in(self):
        self.hack.max_teams, self.hack.waitlist_enabled = 1, True
        self.hack.save()
        placed = make_team(self.hack, make_user(), name="Placed")
        early, late = self.pending(name="Early", reference="E"), self.pending(make_user(), name="Late", reference="L")
        self.act(late, "verify")
        self.act(early, "verify")
        with self.captureOnCommitCallbacks(execute=True):
            from apps.hackathons.services import TeamService
            TeamService.admin_set_status(placed, self.admin, TeamStatus.WITHDRAWN, "gone")
        early.refresh_from_db(), late.refresh_from_db()
        self.assertEqual((late.is_waitlisted, early.is_waitlisted), (False, True))  # Late was verified first, so Late is placed first

    def test_the_leader_is_told_and_the_message_names_no_refund(self):
        team = self.pending()
        with mock.patch("apps.hackathons.services._notify_users") as notify, self.captureOnCommitCallbacks(execute=True):
            self.act(team, "verify")
        users, title, message = notify.call_args.args[:3]
        self.assertEqual(users, [self.leader])
        self.assertIn("verified", title)
        self.assertNotIn("refund", message.lower())


class RejectTests(PaymentCase):
    def test_rejecting_keeps_the_reason_for_organizers_and_tells_the_leader_to_contact_the_club(self):
        team = self.pending()
        with mock.patch("apps.hackathons.services._notify_users") as notify, self.captureOnCommitCallbacks(execute=True):
            reply = self.act(team, "reject", reason="  Amount   does not match ")
        self.assertEqual((reply.status_code, reply.data["payment"]["status"], reply.data["payment"]["reason"]), (200, "REJECTED", "Amount does not match"))
        team.refresh_from_db()
        self.assertEqual(team.status, TeamStatus.PAYMENT_REJECTED)
        users, title, message = notify.call_args.args[:3]
        self.assertEqual(users, [self.leader])
        self.assertIn("contact the club representatives", message)
        self.assertIn("Ravi (club lead)", message)
        self.assertNotIn("refund", f"{title} {message}".lower())
        self.assertNotIn("Amount does not match", message)
        self.assertEqual(registration.placed_teams(self.hack).count(), 0)

    def test_a_long_reason_is_refused_and_the_payment_stays_pending(self):
        team = self.pending()
        reply = self.act(team, "reject", reason="x" * 301)
        self.assertEqual((reply.status_code, reply.data["field"]), (400, "reason"))
        team.refresh_from_db()
        self.assertEqual(team.status, TeamStatus.PAYMENT_PENDING)

    def test_a_rejected_leader_cannot_start_a_second_team_or_invite_people(self):
        team = self.pending()
        self.act(team, "reject")
        self.assertEqual(self.create(name="Another", reference="NEW", file_id=None).data["code"], "ALREADY_IN_TEAM")
        reply = api(self.leader).post(f"/api/hackathons/teams/{team.pk}/invite/", {"email": make_user().email}, format="json")
        self.assertEqual(reply.status_code, 400)

    def test_a_verified_payment_cannot_be_rejected_here(self):
        team = self.pending()
        self.act(team, "verify")
        self.assertEqual(self.act(team, "reject").status_code, 409)


class ReopenAndRemoveTests(PaymentCase):
    def test_reopening_puts_a_rejected_payment_back_in_the_queue(self):
        team = self.pending()
        self.act(team, "reject", reason="blurry")
        reply = self.act(team, "reopen")
        self.assertEqual((reply.status_code, reply.data["payment"]["status"], reply.data["payment"]["reason"]), (200, "PENDING", ""))
        team.refresh_from_db()
        self.assertEqual(team.status, TeamStatus.PAYMENT_PENDING)
        self.assertEqual(self.act(team, "verify").status_code, 200)

    def test_only_a_rejected_payment_can_be_reopened(self):
        team = self.pending()
        reply = self.act(team, "reopen")
        self.assertEqual((reply.status_code, reply.data["code"]), (409, "NOT_REJECTED"))

    def test_removing_deletes_the_team_so_the_leader_can_start_again(self):
        team = self.pending()
        self.act(team, "reject")
        with mock.patch("apps.hackathons.services._notify_users") as notify, self.captureOnCommitCallbacks(execute=True):
            reply = self.act(team, "remove")
        self.assertEqual((reply.status_code, reply.data["removed"]), (200, True))
        self.assertFalse(Team.objects.exists() or TeamMember.objects.exists())
        record = TeamPayment.objects.get()
        self.assertEqual((record.status, record.team_id, record.reviewed_by), (PaymentStatus.REMOVED, None, self.admin))
        self.assertEqual(api(self.admin).get(f"{self.base}/payments/").data["payments"], [])
        self.assertIn("contact the club representatives", notify.call_args.args[2])
        self.assertEqual(self.create(name="Fresh", reference="UPI 123").status_code, 201)

    def test_a_pending_payment_can_be_removed_too(self):
        team = self.pending()
        self.assertEqual(self.act(team, "remove").status_code, 200)
        self.assertFalse(Team.objects.exists())

    def test_a_placed_team_cannot_be_removed_from_the_payments_page(self):
        team = self.pending()
        self.act(team, "verify")
        reply = self.act(team, "remove")
        self.assertEqual((reply.status_code, reply.data["code"]), (409, "ALREADY_PLACED"))
        self.assertTrue(Team.objects.filter(pk=team.pk).exists())

    def test_an_unknown_action_or_payment_is_not_found(self):
        team = self.pending()
        self.assertEqual(api(self.admin).post(f"{self.base}/payments/{team.payment.pk}/refund/").status_code, 404)
        self.assertEqual(api(self.admin).post(f"{self.base}/payments/9999/verify/").status_code, 404)
        other = make_hackathon(min_team_size=1, payment_stage="REGISTRATION", idea_fee_amount=100)
        self.assertEqual(api(self.admin).post(f"/api/hackathons/{other.slug}/payments/{team.payment.pk}/verify/").status_code, 404)


class StageChangeTests(PaymentCase):
    def test_a_verified_team_is_a_normal_team_for_everything_after(self):
        team = self.pending()
        self.act(team, "verify")
        reply = api(self.admin).post(f"/api/hackathons/teams/{team.pk}/admin-set-status/", {"status": "DISQUALIFIED", "reason": "rules"}, format="json")
        self.assertEqual(reply.status_code, 200, reply.data)
        team.refresh_from_db()
        self.assertEqual((team.status, team.payment.status), (TeamStatus.DISQUALIFIED, PaymentStatus.VERIFIED))

    def test_an_organizer_cannot_use_the_team_status_control_on_a_team_waiting_for_payment(self):
        team = self.pending()
        reply = api(self.admin).post(f"/api/hackathons/teams/{team.pk}/admin-set-status/", {"status": "REGISTERED"}, format="json")
        self.assertEqual((reply.status_code, reply.data["code"]), (400, "USE_PAYMENTS"))

    def test_the_summary_is_none_for_a_team_that_never_paid(self):
        team = make_team(self.hack, make_user(), name="Free")
        self.assertIsNone(payments.summary(team))


class IntegrationTests(PaymentCase):
    def rows(self, kind):
        import csv
        import io

        reply = api(self.admin).get(f"{self.base}/export/{kind}/", {"export_format": "csv"})
        return list(csv.reader(io.StringIO(reply.content.decode("utf-8-sig"))))

    def test_the_teams_export_gets_payment_columns_only_when_teams_pay_to_register(self):
        paid, waiting = self.pending(name="Paid", reference="P1"), self.pending(make_user(), name="Waiting", reference="P2")
        self.act(paid, "verify")
        make_team(self.hack, make_user(), name="Free rider")
        rows = self.rows("teams")
        header = rows[0]
        by_team = {r[header.index("Team")]: r for r in rows[1:]}
        self.assertEqual([by_team[n][header.index("Payment Status")] for n in ("Paid", "Waiting", "Free rider")], ["Verified", "Waiting for an organizer", "Not required"])
        self.assertEqual((by_team["Paid"][header.index("Payment Amount")], by_team["Paid"][header.index("Transaction ID")]), ("200", "P1"))
        self.assertEqual(by_team["Waiting"][header.index("Team Status")], "PAYMENT_PENDING")
        self.hack.payment_stage = "NONE"
        self.hack.save()
        self.assertNotIn("Payment Status", self.rows("teams")[0])

    def test_analytics_ignore_teams_that_have_not_been_paid_for(self):
        paid, waiting, turned_down = (self.pending(make_user(), name=n, reference=n) for n in ("Paid", "Waiting", "Turned down"))
        self.act(paid, "verify")
        self.act(turned_down, "reject")
        data = api(self.admin).get(f"{self.base}/analytics/").data
        funnel = {row["key"]: row["count"] for row in data["funnel"]}
        self.assertEqual((funnel["participants"], funnel["teams"], funnel["registered"]), (1, 1, 1))
        self.assertEqual(data["capacity"]["placed_teams"], 1)
        self.assertEqual(data["payments"], {"PENDING": 1, "VERIFIED": 1, "REJECTED": 1})

    def test_a_team_waiting_for_payment_cannot_submit_an_idea_or_a_project(self):
        team = self.pending()
        for path in ("my-idea", "my-submission"):
            reply = api(self.leader).put(f"{self.base}/{path}/", {"title": "x"}, format="json")
            self.assertIn(reply.status_code, (400, 403, 404, 405), (path, reply.data))
        self.assertFalse(team.idea_files.filter(kind="DECK").exists())


class FollowUpTests(PaymentCase):
    """Reminders for payments that wait too long, the payments export, and what happens to files nobody used."""

    def rows(self, **params):
        import csv
        import io

        reply = api(self.admin).get(f"{self.base}/export/payments/", {"export_format": "csv", **params})
        self.assertEqual(reply.status_code, 200)
        return list(csv.reader(io.StringIO(reply.content.decode("utf-8-sig"))))

    def age(self, team, hours):
        TeamPayment.objects.filter(team=team).update(created_at=timezone.now() - timedelta(hours=hours))

    # -- reminders
    def test_organizers_are_told_once_a_day_about_payments_waiting_over_a_day(self):
        staff = self.organizer(["teams"])
        old, fresh = self.pending(name="Old", reference="O1"), self.pending(make_user(), name="Fresh", reference="F1")
        self.age(old, 30)
        self.age(fresh, 2)
        with mock.patch("apps.hackathons.services._notify_users") as notify:
            self.assertEqual(payments.remind_organizers(), 1)
        recipients, title, message = notify.call_args.args[:3]
        self.assertEqual(recipients, [staff])
        self.assertIn("1 payment waiting for a check", title)
        self.assertIn("Payments tab", message)
        with mock.patch("apps.hackathons.services._notify_users") as notify:
            self.assertEqual(payments.remind_organizers(), 0)  # already told today
        notify.assert_not_called()
        TeamPayment.objects.filter(team=old).update(organizer_reminded_at=timezone.now() - timedelta(hours=25))
        with mock.patch("apps.hackathons.services._notify_users"):
            self.assertEqual(payments.remind_organizers(), 1)  # a day later it is mentioned again

    def test_site_admins_are_told_when_the_hackathon_has_no_staff_for_it(self):
        team = self.pending()
        self.age(team, 48)
        with mock.patch("apps.hackathons.services._notify_users") as notify:
            payments.remind_organizers()
        self.assertIn(self.admin, notify.call_args.args[0])

    def test_nothing_is_sent_for_handled_payments_or_other_stages(self):
        done, waiting = self.pending(name="Done", reference="D1"), self.pending(make_user(), name="Waiting", reference="W1")
        self.act(done, "verify")
        for team in (done, waiting):
            self.age(team, 50)
        with mock.patch("apps.hackathons.services._notify_users"):
            self.assertEqual(payments.remind_organizers(), 1)  # only the one still waiting
        self.hack.payment_stage = "IDEA"
        self.hack.save()
        TeamPayment.objects.update(organizer_reminded_at=None)
        with mock.patch("apps.hackathons.services._notify_users") as notify:
            self.assertEqual(payments.remind_organizers(), 0)
        notify.assert_not_called()

    def test_the_command_runs_and_reports(self):
        from io import StringIO

        from django.core.management import call_command

        self.age(self.pending(), 30)
        out = StringIO()
        with mock.patch("apps.hackathons.services._notify_users"):
            call_command("remind_pending_payments", stdout=out)
        self.assertIn("1 payment", out.getvalue())

    # -- export
    def test_the_payments_export_lists_every_payment_including_those_of_deleted_teams(self):
        paid, rejected, cancelled, removed = (self.pending(make_user(first_name=n), name=n, reference=f"R-{n}") for n in ("Paid", "Rejected", "Cancelled", "Removed"))
        self.pending_extra = self.pending(make_user(first_name="Waiting"), name="Waiting", reference="R-Waiting")
        self.act(paid, "verify")
        self.act(rejected, "reject", reason="Blurry")
        api(cancelled.leader).post(f"/api/hackathons/teams/{cancelled.pk}/cancel-payment/")
        self.act(removed, "remove")
        rows = self.rows()
        header = rows[0]
        by_team = {r[header.index("Team")]: r for r in rows[1:]}
        col = lambda team, name: by_team[team][header.index(name)]
        self.assertEqual(sorted(by_team), ["Cancelled", "Paid", "Rejected", "Removed", "Waiting"])
        self.assertEqual([col(t, "Payment Status") for t in ("Paid", "Rejected", "Cancelled", "Removed", "Waiting")],
                         ["Verified", "Rejected", "Cancelled by the leader", "Removed by an organizer", "Waiting for an organizer"])
        self.assertEqual([col(t, "Money To Settle") for t in ("Paid", "Rejected", "Cancelled", "Removed", "Waiting")], ["No", "Yes", "Yes", "Yes", "No"])
        self.assertEqual((col("Cancelled", "Team Status"), col("Paid", "Team Status")), ("DELETED", "REGISTERED"))
        self.assertEqual((col("Rejected", "Reason"), col("Rejected", "Transaction ID"), col("Rejected", "Amount")), ("Blurry", "R-Rejected", "200"))
        self.assertEqual(col("Cancelled", "Leader Email"), cancelled.leader.email)

    def test_a_team_that_dropped_out_after_paying_is_marked_for_settling(self):
        team = self.pending()
        self.act(team, "verify")
        api(self.admin).post(f"/api/hackathons/teams/{team.pk}/admin-set-status/", {"status": "WITHDRAWN", "reason": "left"}, format="json")
        rows = self.rows()
        self.assertEqual(rows[1][rows[0].index("Money To Settle")], "Yes")

    def test_the_export_can_be_filtered_and_needs_the_teams_permission(self):
        self.act(self.pending(), "reject")
        self.pending(make_user(), name="Beta", reference="B1")
        self.assertEqual(len(self.rows(status="REJECTED")) - 1, 1)
        self.assertEqual(len(self.rows(status="nonsense")) - 1, 2)
        holder = self.organizer(["teams"])
        self.assertEqual(api(holder).get(f"{self.base}/export/payments/").status_code, 200)
        for blocked in (self.organizer(["ideas"]), self.leader, make_user()):
            self.assertEqual(api(blocked).get(f"{self.base}/export/payments/").status_code, 403)

    # -- files nobody used
    def test_a_screenshot_uploaded_but_never_used_is_swept_like_any_unattached_upload(self):
        from apps.forms import upload_maintenance

        file_id = self.upload()
        IdeaFile.objects.filter(pk=file_id).update(expires_at=timezone.now() - timedelta(hours=1))
        with mock.patch("apps.forms.upload_maintenance._delete_objects") as delete:
            self.assertEqual(upload_maintenance.sweep_expired()["expired"], 1)
        self.assertEqual(IdeaFile.objects.get(pk=file_id).state, UploadState.DELETED)
        delete.assert_called_once()

    def test_a_screenshot_in_use_is_not_swept(self):
        from apps.forms import upload_maintenance

        team = self.pending()
        with mock.patch("apps.forms.upload_maintenance._delete_objects"):
            self.assertEqual(upload_maintenance.sweep_expired()["expired"], 0)
        self.assertEqual(team.payment.screenshot.state, UploadState.ATTACHED)

    # -- a notification for every new payment
    def test_organizers_are_notified_of_each_new_payment_but_not_of_a_retry(self):
        staff = self.organizer(["teams"])
        file_id = self.upload()
        with mock.patch("apps.hackathons.services._notify_users") as notify, self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.create(file_id=file_id).status_code, 201)
        to_organizers = [call for call in notify.call_args_list if call.args[0] == [staff]]
        self.assertEqual(len(to_organizers), 1)
        _, title, message = to_organizers[0].args[:3]
        self.assertEqual(title, "New payment to check: Alpha")
        self.assertIn("₹200", message)
        self.assertIn("Payments tab", message)
        with mock.patch("apps.hackathons.services._notify_users") as again, self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.create(file_id=file_id).status_code, 201)  # the same payment sent twice
        again.assert_not_called()

    def test_with_no_staff_the_site_admins_are_notified(self):
        with mock.patch("apps.hackathons.services._notify_users") as notify, self.captureOnCommitCallbacks(execute=True):
            self.pending()
        self.assertTrue(any(self.admin in call.args[0] for call in notify.call_args_list))

    def test_a_team_created_without_a_fee_notifies_nobody_about_payments(self):
        free = make_hackathon(min_team_size=1)
        with mock.patch("apps.hackathons.services._notify_users") as notify, self.captureOnCommitCallbacks(execute=True):
            api(make_user()).post(f"/api/hackathons/{free.slug}/teams/", {"name": "Free"}, format="json")
        self.assertFalse([call for call in notify.call_args_list if "payment" in call.args[1].lower()])
