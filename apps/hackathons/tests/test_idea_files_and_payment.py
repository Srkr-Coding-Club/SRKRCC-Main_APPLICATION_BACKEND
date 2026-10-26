"""The idea phase with slides, a LinkedIn post, a payment screenshot, and organizer-controlled editing."""

from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.audit.models import AuditLog
from apps.forms.models import ScanStatus, UploadState
from apps.forms.tests.test_upload_submission import CopyingStorage
from apps.hackathons.models import HackathonStaff, IdeaFile, IdeaStatus, IdeaSubmission
from .helpers import make_hackathon, make_team, make_user

HOUR = timedelta(hours=1)
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
PPTX = b"PK\x03\x04" + b"0" * 64
PPT = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"0" * 64
LINKEDIN = "https://www.linkedin.com/posts/team-activity-123"


def api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


@override_settings(FORM_UPLOADS_BACKEND="s3", FORM_UPLOAD_BUCKET="test-bucket")
class IdeaCase(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.storage = CopyingStorage()
        patcher = mock.patch("apps.forms.upload_storage.get_upload_storage", return_value=self.storage)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.hack = make_hackathon(min_team_size=1, features={"ideas": True, "judging": True})
        self.leader, self.member = make_user(), make_user()
        self.team = make_team(self.hack, self.leader, self.member)
        self.admin = make_user(role="ADMIN")
        self.url = f"/api/hackathons/{self.hack.slug}/my-idea/"
        self.files_url = f"{self.url}files/"
        self.body = {"title": "Smart bins", "problem": "Campus waste is not sorted, so recyclables reach landfill.", "solution": "Sensors at each bin and an app that tells the cleaners what is full.", "plan": "Week 1 build."}

    def staff(self, role, permissions=None):
        user = make_user()
        HackathonStaff.objects.create(hackathon=self.hack, user=user, role=role, permissions=permissions or [])
        return user

    def upload(self, kind, name, body, user=None, team_url=None):
        client = api(user or self.leader)
        asked = client.post(self.files_url, {"kind": kind, "name": name, "size": len(body), "type": "x"}, format="json")
        self.assertEqual(asked.status_code, 201, asked.data)
        file_id = asked.data["file"]["id"]
        self.storage.put(IdeaFile.objects.get(pk=file_id).key, body)
        done = client.post(f"{self.files_url}{file_id}/")
        self.assertEqual(done.status_code, 200, done.data)
        return file_id

    def save(self, user=None, **extra):
        return api(user or self.leader).put(self.url, {**self.body, **extra}, format="json")

    def full_idea(self, **extra):
        return self.save(deck_file_id=self.upload("DECK", "deck.pptx", PPTX), payment_file_id=self.upload("PAYMENT", "pay.png", PNG),
                         linkedin_url=LINKEDIN, transaction_id="UPI123", **extra)


class SettingsAndRequirements(IdeaCase):
    def test_the_idea_form_learns_the_organizers_choices(self):
        Hackathon_ = type(self.hack)
        Hackathon_.objects.filter(pk=self.hack.pk).update(idea_fee_amount=200, idea_payment_note="UPI: club@upi", idea_deck_required=True, idea_linkedin_required=True)
        config = api(self.leader).get(self.url).data["config"]
        self.assertEqual((config["fee_amount"], config["payment_note"], config["deck_required"], config["linkedin_required"], config["files_available"]),
                         (200, "UPI: club@upi", True, True, True))

    def test_nothing_extra_is_required_unless_the_organizers_ask(self):
        self.assertEqual(self.save().status_code, 200)

    def test_each_requirement_names_the_field_that_is_missing(self):
        type(self.hack).objects.filter(pk=self.hack.pk).update(idea_fee_amount=200, idea_deck_required=True, idea_linkedin_required=True)
        reply = self.save()
        self.assertEqual((reply.status_code, reply.data["field"]), (400, "linkedin_url"))
        reply = self.save(linkedin_url=LINKEDIN)
        self.assertEqual(reply.data["field"], "deck_file_id")
        reply = self.save(linkedin_url=LINKEDIN, deck_file_id=self.upload("DECK", "deck.pptx", PPTX))
        self.assertEqual(reply.data["field"], "payment_file_id")
        self.assertIn("200", reply.data["detail"])
        self.assertEqual(IdeaSubmission.objects.count(), 0)

    def test_a_complete_submission_is_saved_and_the_files_become_permanent(self):
        type(self.hack).objects.filter(pk=self.hack.pk).update(idea_fee_amount=200, idea_deck_required=True, idea_linkedin_required=True)
        with self.captureOnCommitCallbacks(execute=True):
            reply = self.full_idea()
        self.assertEqual(reply.status_code, 200, reply.data)
        idea = IdeaSubmission.objects.get()
        self.assertEqual((idea.linkedin_url, idea.transaction_id), (LINKEDIN, "UPI123"))
        for file in (idea.deck_file, idea.payment_proof):
            self.assertEqual(file.state, UploadState.ATTACHED)
            self.assertIn("/files/", file.key)
            self.assertIn(file.key, self.storage.objects)
        self.assertEqual(len(self.storage.objects), 2)  # the temporary copies are gone

    def test_linkedin_must_be_a_linkedin_address(self):
        for bad in ("https://example.org/post", "https://notlinkedin.com/x", "nope", "https://evil.com/?linkedin.com"):
            self.assertEqual(self.save(linkedin_url=bad).status_code, 400, bad)
        self.assertEqual(self.save(linkedin_url="https://lnkd.in/abc").status_code, 200)
        self.assertEqual(self.save(linkedin_url="").status_code, 200)

    def test_the_transaction_id_is_optional_and_limited(self):
        self.assertEqual(self.save(transaction_id="").status_code, 200)
        self.assertEqual(self.save(transaction_id="x" * 61).data["field"], "transaction_id")

    def test_the_hackathon_settings_are_checked(self):
        patch = lambda body, user=None: api(user or self.admin).patch(f"/api/hackathons/{self.hack.slug}/", body, format="json")  # noqa: E731
        self.assertEqual(patch({"idea_fee_amount": 200, "idea_payment_note": "pay here"}).status_code, 200)
        self.assertEqual(patch({"idea_fee_amount": 100001}).data["idea_fee_amount"][0][:18], "The fee can be at ")
        self.assertIn("idea_edits_until", patch({"idea_opens_at": (timezone.now() + HOUR).isoformat(), "idea_edits_until": timezone.now().isoformat()}).data)
        with override_settings(FORM_UPLOADS_BACKEND="inline"):
            self.assertIn("idea_fee_amount", patch({"idea_fee_amount": 300}).data)
            self.assertIn("idea_deck_required", patch({"idea_fee_amount": 0, "idea_deck_required": True}).data)
            self.assertEqual(patch({"idea_fee_amount": 0, "idea_deck_required": False}).status_code, 200)

    def test_the_settings_are_copied_to_a_cloned_hackathon(self):
        type(self.hack).objects.filter(pk=self.hack.pk).update(idea_fee_amount=200, idea_payment_note="pay", idea_deck_required=True, idea_edits_enabled=False)
        reply = api(self.admin).post(f"/api/hackathons/{self.hack.slug}/clone/", {"title": "Copy", "start_date": (timezone.now() + 30 * HOUR).isoformat()}, format="json")
        self.assertEqual(reply.status_code, 201, reply.data)
        copy = type(self.hack).objects.get(slug=reply.data["slug"])
        self.assertEqual((copy.idea_fee_amount, copy.idea_payment_note, copy.idea_deck_required, copy.idea_edits_enabled), (200, "pay", True, False))


class UploadingFiles(IdeaCase):
    def ask(self, kind="DECK", name="deck.pptx", size=100, user=None):
        return api(user or self.leader).post(self.files_url, {"kind": kind, "name": name, "size": size, "type": "x"}, format="json")

    def test_the_slides_must_be_ppt_pptx_or_pdf_and_the_screenshot_an_image(self):
        for name in ("deck.ppt", "deck.pptx", "deck.pdf", "DECK.PDF"):
            self.assertEqual(self.ask("DECK", name).status_code, 201, name)
        for name in ("deck.exe", "deck", "deck.png", "deck.docx", "deck.pdf.exe"):
            self.assertEqual(self.ask("DECK", name).data["code"], "FILE_TYPE_NOT_ALLOWED", name)
        self.assertIn(".ppt, .pptx or .pdf", self.ask("DECK", "deck.exe").data["detail"])
        for name in ("pay.png", "pay.jpg", "pay.jpeg", "pay.webp"):
            self.assertEqual(self.ask("PAYMENT", name).status_code, 201, name)
        for name in ("pay.pdf", "pay.svg", "pay.gif", "pay.pptx"):
            self.assertEqual(self.ask("PAYMENT", name).data["code"], "FILE_TYPE_NOT_ALLOWED", name)

    def test_size_limits_and_nonsense(self):
        self.assertEqual(self.ask("DECK", size=25 * 1024 * 1024).status_code, 201)
        self.assertEqual(self.ask("DECK", size=25 * 1024 * 1024 + 1).status_code, 413)
        self.assertEqual(self.ask("PAYMENT", "p.png", size=5 * 1024 * 1024 + 1).data["code"], "FILE_TOO_LARGE")
        self.assertEqual(self.ask("PAYMENT", "p.png", size=0).data["code"], "FILE_TOO_SMALL")
        self.assertEqual(self.ask("PAYMENT", "p.png", size="abc").status_code, 400)
        self.assertEqual(self.ask("OTHER", "p.png").data["field"], "kind")
        for name in ("", "../x.png", "a/b.png", "a\x00.png"):
            self.assertEqual(self.ask("PAYMENT", name).status_code, 400, repr(name))

    def test_the_signed_form_pins_the_size_and_type_and_the_key_is_not_the_file_name(self):
        reply = self.ask("PAYMENT", "my payment.png", size=1000)
        signed = self.storage.signed[-1]
        self.assertEqual((signed["content_type"], signed["max"], signed["min"]), ("image/png", 5 * 1024 * 1024, 1))
        self.assertNotIn("payment", signed["key"])
        self.assertTrue(signed["key"].startswith(f"hackathon-ideas/tmp/{self.hack.pk}/{self.team.pk}/"))
        self.assertEqual(reply.data["file"]["state"], "PENDING")

    def test_only_the_leader_of_a_registered_team_may_upload(self):
        self.assertEqual(self.ask(user=self.member).status_code, 403)
        self.assertEqual(self.ask(user=make_user()).data["code"], "NO_TEAM")
        self.assertEqual(api().post(self.files_url, {}, format="json").status_code, 401)
        type(self.team).objects.filter(pk=self.team.pk).update(is_waitlisted=True)
        self.assertEqual(self.ask().data["code"], "TEAM_NOT_REGISTERED")

    def test_uploads_follow_the_idea_window_and_the_edit_rules(self):
        type(self.hack).objects.filter(pk=self.hack.pk).update(idea_deadline=timezone.now() - HOUR)
        self.assertEqual(self.ask().data["code"], "IDEAS_CLOSED")
        type(self.hack).objects.filter(pk=self.hack.pk).update(idea_deadline=None, features={})
        self.assertEqual(self.ask().data["code"], "FEATURE_OFF")

    def test_nothing_is_offered_while_uploads_are_switched_off(self):
        with override_settings(FORM_UPLOADS_BACKEND="inline"):
            reply = self.ask()
        self.assertEqual((reply.status_code, reply.data["code"]), (503, "UPLOADS_UNAVAILABLE"))

    def test_a_new_pick_replaces_the_one_not_yet_saved(self):
        first = self.ask().data["file"]["id"]
        self.ask(name="other.pptx")
        self.assertEqual(IdeaFile.objects.get(pk=first).state, UploadState.DELETED)

    def test_completing_checks_what_really_arrived(self):
        asked = self.ask("PAYMENT", "pay.png")
        file = IdeaFile.objects.get()
        complete = lambda: api(self.leader).post(f"{self.files_url}{asked.data['file']['id']}/")  # noqa: E731
        self.assertEqual(complete().data["code"], "UPLOAD_MISSING")
        self.storage.put(file.key, b"MZ not a png at all............")
        refused = complete()
        self.assertEqual((refused.status_code, refused.data["code"]), (400, "FILE_CONTENT_MISMATCH"))
        file.refresh_from_db()
        self.assertEqual(file.state, UploadState.REJECTED)
        self.assertIn(file.key, self.storage.deleted)

    def test_real_powerpoint_files_pass_the_content_check(self):
        self.upload("DECK", "old.ppt", PPT)
        self.upload("DECK", "new.pptx", PPTX)

    def test_a_file_bigger_than_signed_is_removed(self):
        asked = self.ask("PAYMENT", "pay.png")
        self.storage.put(IdeaFile.objects.get().key, PNG + b"0" * (5 * 1024 * 1024))
        refused = api(self.leader).post(f"{self.files_url}{asked.data['file']['id']}/")
        self.assertEqual((refused.status_code, refused.data["code"]), (413, "FILE_TOO_LARGE"))

    def test_discarding_removes_the_object_and_junk_ids_are_not_found(self):
        file_id = self.upload("PAYMENT", "pay.png", PNG)
        self.assertEqual(api(self.leader).delete(f"{self.files_url}{file_id}/").status_code, 204)
        self.assertEqual(IdeaFile.objects.get().state, UploadState.DELETED)
        self.assertEqual(self.storage.objects, {})
        self.assertEqual(api(self.leader).delete(f"{self.files_url}{file_id}/").status_code, 404)
        self.assertEqual(api(self.leader).post(f"{self.files_url}{'0' * 8}-0000-4000-8000-{'0' * 12}/").status_code, 404)

    def test_a_storage_outage_while_discarding_does_not_fail_the_request(self):
        file_id = self.upload("PAYMENT", "pay.png", PNG)
        with mock.patch.object(self.storage, "delete", side_effect=RuntimeError("down")):
            self.assertEqual(api(self.leader).delete(f"{self.files_url}{file_id}/").status_code, 204)

    def test_another_teams_upload_cannot_be_used(self):
        rival_leader = make_user()
        rival = make_team(self.hack, rival_leader)
        theirs = self.upload("PAYMENT", "pay.png", PNG, user=rival_leader)
        reply = self.save(payment_file_id=theirs)
        self.assertEqual((reply.status_code, reply.data["code"], reply.data["field"]), (400, "FILE_NOT_FOUND", "payment_file_id"))
        self.assertEqual(api(self.leader).post(f"{self.files_url}{theirs}/").status_code, 404)
        self.assertEqual(api(self.leader).delete(f"{self.files_url}{theirs}/").status_code, 404)
        self.assertEqual(IdeaFile.objects.get(pk=theirs).team_id, rival.pk)

    def test_a_slides_upload_cannot_stand_in_for_a_payment_screenshot(self):
        deck = self.upload("DECK", "deck.pptx", PPTX)
        self.assertEqual(self.save(payment_file_id=deck).data["code"], "FILE_NOT_FOUND")

    def test_replacing_or_removing_a_saved_file_releases_the_old_one(self):
        first = self.upload("PAYMENT", "one.png", PNG)
        self.assertEqual(self.save(payment_file_id=first).status_code, 200)
        old_key = IdeaSubmission.objects.get().payment_proof.key
        second = self.upload("PAYMENT", "two.png", PNG)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.save(payment_file_id=second).status_code, 200)
        self.assertEqual(IdeaFile.objects.get(pk=first).state, UploadState.DELETED)
        self.assertNotIn(old_key, self.storage.objects)
        # leaving the field out keeps the file; null removes it
        self.assertEqual(IdeaSubmission.objects.get().payment_proof_id.hex, second.replace("-", ""))
        self.assertEqual(self.save().status_code, 200)
        self.assertIsNotNone(IdeaSubmission.objects.get().payment_proof)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.save(payment_file_id=None).status_code, 200)
        self.assertIsNone(IdeaSubmission.objects.get().payment_proof)

    def test_a_required_file_cannot_be_removed(self):
        type(self.hack).objects.filter(pk=self.hack.pk).update(idea_fee_amount=200)
        self.assertEqual(self.save(payment_file_id=self.upload("PAYMENT", "pay.png", PNG)).status_code, 200)
        self.assertEqual(self.save(payment_file_id=None).data["field"], "payment_file_id")

    def test_a_storage_failure_while_saving_changes_nothing(self):
        file_id = self.upload("PAYMENT", "pay.png", PNG)
        with mock.patch.object(self.storage, "copy", side_effect=RuntimeError("down")):
            reply = self.save(payment_file_id=file_id)
        self.assertEqual((reply.status_code, reply.data["code"]), (503, "UPLOAD_STORAGE_ERROR"))
        self.assertEqual(IdeaSubmission.objects.count(), 0)
        self.assertEqual(IdeaFile.objects.get(pk=file_id).state, UploadState.UPLOADED)

    def test_deleting_the_team_removes_its_files(self):
        self.save(payment_file_id=self.upload("PAYMENT", "pay.png", PNG), deck_file_id=self.upload("DECK", "d.pptx", PPTX))
        keys = list(IdeaFile.objects.values_list("key", flat=True))
        with self.captureOnCommitCallbacks(execute=True):
            self.team.delete()
        for key in keys:
            self.assertNotIn(key, self.storage.objects)


class WhoSeesWhat(IdeaCase):
    def setUp(self):
        super().setUp()
        self.save(deck_file_id=self.upload("DECK", "deck.pptx", PPTX), payment_file_id=self.upload("PAYMENT", "pay.png", PNG),
                  linkedin_url=LINKEDIN, transaction_id="UPI123")
        self.idea = IdeaSubmission.objects.get()
        self.link = lambda user, kind, query="": api(user).get(f"/api/hackathons/{self.hack.slug}/ideas/{self.idea.pk}/files/{kind}/link/{query}")  # noqa: E731

    def test_organizers_and_the_team_see_both_files(self):
        organizer = self.staff("ORGANIZER")
        for user in (self.admin, organizer, self.leader, self.member):
            self.assertEqual(self.link(user, "deck").status_code, 200)
            self.assertEqual(self.link(user, "payment").status_code, 200, user.username)

    def test_judges_see_the_slides_but_never_the_payment(self):
        judge = self.staff("JUDGE")
        self.assertEqual(self.link(judge, "deck").status_code, 200)
        self.assertEqual(self.link(judge, "payment").status_code, 404)

    def test_other_people_see_neither(self):
        outsider = make_user()
        volunteer = self.staff("VOLUNTEER")
        for user in (outsider, volunteer, make_user(role="NON_AFFILIATE")):
            self.assertEqual(self.link(user, "deck").status_code, 404)
            self.assertEqual(self.link(user, "payment").status_code, 404)
        self.assertEqual(api().get(f"/api/hackathons/{self.hack.slug}/ideas/{self.idea.pk}/files/deck/link/").status_code, 401)

    def test_an_organizer_narrowed_to_other_permissions_cannot_see_the_payment(self):
        narrowed = self.staff("ORGANIZER", ["content"])
        self.assertEqual(self.link(narrowed, "payment").status_code, 404)

    def test_links_are_short_lived_download_only_for_slides_and_inline_for_images(self):
        deck = self.link(self.admin, "deck", "?inline=1").data
        self.assertEqual((deck["expires_in"], deck["inline"]), (60, False))   # a .pptx is never opened in the page
        self.assertTrue(self.link(self.admin, "payment", "?inline=1").data["inline"])
        self.assertFalse(self.link(self.admin, "payment").data["inline"])
        self.assertEqual(self.storage.links[-1]["filename"], "pay.png")

    def test_viewing_a_payment_screenshot_is_audited_but_slides_are_not(self):
        self.link(self.admin, "deck")
        self.assertFalse(AuditLog.objects.filter(action="Viewed Idea Payment Proof").exists())
        self.link(self.admin, "payment")
        entry = AuditLog.objects.get(action="Viewed Idea Payment Proof")
        self.assertEqual((entry.actor, entry.target_id), (self.admin, str(self.idea.pk)))

    def test_unknown_kinds_ideas_and_removed_files_are_not_found(self):
        self.assertEqual(self.link(self.admin, "resume").status_code, 404)
        self.assertEqual(api(self.admin).get(f"/api/hackathons/{self.hack.slug}/ideas/999999/files/deck/link/").status_code, 404)
        other = make_hackathon(features={"ideas": True})
        self.assertEqual(api(self.admin).get(f"/api/hackathons/{other.slug}/ideas/{self.idea.pk}/files/deck/link/").status_code, 404)
        IdeaFile.objects.update(state=UploadState.DELETED)
        self.assertEqual(self.link(self.admin, "deck").status_code, 404)

    def test_the_judging_lists_never_carry_the_payment_details(self):
        judge = self.staff("JUDGE")
        row = api(judge).get(f"/api/hackathons/{self.hack.slug}/judging/ideas/").data["ideas"][0]
        self.assertEqual((row["linkedin_url"], row["deck_file"]["name"]), (LINKEDIN, "deck.pptx"))
        self.assertNotIn("payment_proof", row)
        self.assertNotIn("transaction_id", row)
        self.assertNotIn("UPI123", str(row))

    def test_organizers_and_the_team_get_the_payment_details_in_their_lists(self):
        row = api(self.admin).get(f"/api/hackathons/{self.hack.slug}/ideas/").data["rows"][0]
        self.assertEqual((row["transaction_id"], row["payment_proof"]["name"]), ("UPI123", "pay.png"))
        mine = api(self.leader).get(self.url).data["idea"]
        self.assertEqual((mine["transaction_id"], mine["payment_proof"]["name"]), ("UPI123", "pay.png"))

    def test_storage_keys_are_never_sent_to_anyone(self):
        for data in (api(self.admin).get(f"/api/hackathons/{self.hack.slug}/ideas/").data, api(self.leader).get(self.url).data):
            self.assertNotIn("hackathon-ideas/", str(data))

    @override_settings(FORM_UPLOAD_SCAN="guardduty")
    def test_a_file_is_only_served_once_it_is_clean(self):
        waiting = self.link(self.admin, "deck")
        self.assertEqual((waiting.status_code, waiting.data["code"]), (423, "SCAN_PENDING"))
        key = self.idea.deck_file.key
        self.storage.verdicts = {key: "NO_THREATS_FOUND"}
        self.assertEqual(self.link(self.admin, "deck").status_code, 200)

    @override_settings(FORM_UPLOAD_SCAN="guardduty")
    def test_malware_is_detached_from_the_idea_and_removed(self):
        from apps.forms import upload_maintenance
        file = self.idea.payment_proof
        self.storage.verdicts = {file.key: "THREATS_FOUND"}
        with self.captureOnCommitCallbacks(execute=True):
            upload_maintenance.scan_pending()
        file.refresh_from_db()
        self.assertEqual((file.scan_status, file.state), (ScanStatus.INFECTED, UploadState.DELETED))
        self.assertIsNone(IdeaSubmission.objects.get().payment_proof)
        self.assertNotIn(file.key, self.storage.objects)
        self.assertTrue(AuditLog.objects.filter(action="Malware Found In Idea File").exists())

    def test_the_maintenance_sweep_covers_unsaved_idea_files(self):
        from apps.forms import upload_maintenance
        stale = self.upload("DECK", "later.pptx", PPTX)
        IdeaFile.objects.filter(pk=stale).update(expires_at=timezone.now() - HOUR)
        self.assertEqual(upload_maintenance.count_expired(), 1)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(upload_maintenance.sweep_expired()["expired"], 1)
        self.assertEqual(IdeaFile.objects.get(pk=stale).state, UploadState.DELETED)
        self.assertEqual(IdeaFile.objects.exclude(pk=stale).filter(state=UploadState.ATTACHED).count(), 2)  # saved files untouched


class EditingControl(IdeaCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.save().status_code, 200)
        self.set = lambda **fields: type(self.hack).objects.filter(pk=self.hack.pk).update(**fields)  # noqa: E731

    def state(self):
        return api(self.leader).get(self.url).data

    def test_editing_is_on_by_default_so_existing_hackathons_behave_as_before(self):
        self.assertEqual((self.state()["can_edit"], self.state()["config"]["edits_enabled"]), (True, True))
        self.assertEqual(self.save(title="Smarter bins").status_code, 200)

    def test_switching_editing_off_locks_a_submitted_idea(self):
        self.set(idea_edits_enabled=False)
        state = self.state()
        self.assertFalse(state["can_edit"])
        self.assertIn("switched off editing", state["edit_blocked_reason"])
        reply = self.save(title="Sneaky change")
        self.assertEqual((reply.status_code, reply.data["code"]), (400, "IDEA_EDITS_OFF"))
        self.assertEqual(IdeaSubmission.objects.get().title, "Smart bins")

    def test_files_cannot_be_changed_while_editing_is_off_either(self):
        self.set(idea_edits_enabled=False)
        reply = api(self.leader).post(self.files_url, {"kind": "PAYMENT", "name": "p.png", "size": 10, "type": "x"}, format="json")
        self.assertEqual(reply.data["code"], "IDEA_EDITS_OFF")

    def test_a_first_submission_is_never_blocked_by_the_toggle(self):
        IdeaSubmission.objects.all().delete()
        self.set(idea_edits_enabled=False)
        self.assertEqual(self.save().status_code, 200)

    def test_changes_the_organizers_asked_for_are_allowed_even_with_editing_off(self):
        idea = IdeaSubmission.objects.get()
        api(self.admin).post(f"/api/hackathons/{self.hack.slug}/ideas/{idea.pk}/review/", {"status": "CHANGES_REQUESTED", "feedback": "Add a plan"}, format="json")
        self.set(idea_edits_enabled=False)
        self.assertTrue(self.state()["can_edit"])
        self.assertEqual(self.save(plan="Added").status_code, 200)

    def test_decided_ideas_stay_final(self):
        idea = IdeaSubmission.objects.get()
        api(self.admin).post(f"/api/hackathons/{self.hack.slug}/ideas/{idea.pk}/review/", {"status": "APPROVED"}, format="json")
        self.assertEqual(self.save().data["code"], "IDEA_LOCKED")
        self.set(idea_edits_until=timezone.now() + HOUR)
        self.assertEqual(self.save().data["code"], "IDEA_LOCKED")

    def test_without_an_end_time_editing_stops_at_the_deadline(self):
        self.set(idea_deadline=timezone.now() - HOUR)
        self.assertEqual(self.save().data["code"], "IDEAS_CLOSED")
        self.assertFalse(self.state()["can_edit"])

    def test_an_edit_window_can_reopen_editing_after_the_deadline(self):
        self.set(idea_deadline=timezone.now() - HOUR, idea_edits_until=timezone.now() + HOUR)
        self.assertTrue(self.state()["can_edit"])
        self.assertEqual(self.save(title="Late fix").status_code, 200)
        self.set(idea_edits_until=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.save().data["code"], "IDEAS_CLOSED")

    def test_an_edit_window_can_also_close_editing_before_the_deadline(self):
        self.set(idea_deadline=timezone.now() + 5 * HOUR, idea_edits_until=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.save().data["code"], "IDEAS_CLOSED")

    def test_the_edit_window_does_not_let_a_new_team_submit_after_the_deadline(self):
        IdeaSubmission.objects.all().delete()
        self.set(idea_deadline=timezone.now() - HOUR, idea_edits_until=timezone.now() + HOUR)
        self.assertEqual(self.save().data["code"], "IDEAS_CLOSED")

    def test_the_edit_window_is_ignored_while_editing_is_off(self):
        self.set(idea_edits_enabled=False, idea_edits_until=timezone.now() + HOUR)
        self.assertEqual(self.save().data["code"], "IDEA_EDITS_OFF")


class PaymentQr(IdeaCase):
    """The QR code the organizers upload for teams to scan when they pay."""

    def setUp(self):
        super().setUp()
        self.qr_url = f"/api/hackathons/{self.hack.slug}/idea-qr/"
        type(self.hack).objects.filter(pk=self.hack.pk).update(idea_fee_amount=200)

    def upload_qr(self, user=None, name="pay-qr.png", body=PNG):
        client = api(user or self.admin)
        asked = client.post(self.qr_url, {"name": name, "size": len(body), "type": "x"}, format="json")
        self.assertEqual(asked.status_code, 201, asked.data)
        file_id = asked.data["file"]["id"]
        self.storage.put(IdeaFile.objects.get(pk=file_id).key, body)
        return file_id, client.post(f"{self.qr_url}{file_id}/")

    def link(self, user):
        return api(user).get(f"{self.qr_url}link/")

    def test_an_admin_uploads_the_qr_and_it_becomes_the_hackathon_payment_code(self):
        with self.captureOnCommitCallbacks(execute=True):
            file_id, done = self.upload_qr()
        self.assertEqual(done.status_code, 200, done.data)
        self.assertEqual(done.data["payment_qr"], {"id": file_id, "name": "pay-qr.png"})
        file = IdeaFile.objects.get(pk=file_id)
        self.assertEqual((file.kind, file.state, file.team_id), ("QR", UploadState.ATTACHED, None))
        self.assertIn(f"hackathon-ideas/files/{self.hack.pk}/hackathon/", file.key)
        self.assertEqual(len(self.storage.objects), 1)
        self.hack.refresh_from_db()
        self.assertEqual(self.hack.idea_payment_qr_id, file.pk)
        self.assertTrue(AuditLog.objects.filter(action="Set Hackathon Payment QR", actor=self.admin).exists())

    def test_the_team_and_the_admin_are_told_a_qr_exists_but_never_its_storage_key(self):
        self.upload_qr()
        for data in (api(self.leader).get(self.url).data["config"], api(self.admin).get(f"/api/hackathons/{self.hack.slug}/ideas/").data["config"]):
            self.assertEqual(data["payment_qr"]["name"], "pay-qr.png")
            self.assertNotIn("hackathon-ideas/", str(data))

    def test_without_a_qr_the_config_says_none(self):
        self.assertIsNone(api(self.leader).get(self.url).data["config"]["payment_qr"])

    def test_only_organizers_with_the_settings_permission_may_change_it(self):
        organizer = self.staff("ORGANIZER")
        self.assertEqual(self.upload_qr(organizer)[1].status_code, 200)
        reviewer = self.staff("ORGANIZER", ["ideas"])
        for user in (reviewer, self.staff("JUDGE"), self.staff("VOLUNTEER"), self.leader, self.member, make_user()):
            reply = api(user).post(self.qr_url, {"name": "q.png", "size": 10, "type": "x"}, format="json")
            self.assertEqual(reply.status_code, 403, user.username)
        self.assertEqual(api().post(self.qr_url, {}, format="json").status_code, 401)
        self.assertEqual(api(self.leader).delete(self.qr_url).status_code, 403)

    def test_registered_team_members_and_organizers_can_see_it_nobody_else(self):
        self.upload_qr()
        for user in (self.leader, self.member, self.admin, self.staff("ORGANIZER"), self.staff("ORGANIZER", ["ideas"])):
            reply = self.link(user)
            self.assertEqual(reply.status_code, 200, user.username)
            self.assertEqual((reply.data["expires_in"], reply.data["inline"]), (60, True))
        forming_leader = make_user()
        make_team(self.hack, forming_leader, status="FORMING")
        for user in (make_user(), self.staff("JUDGE"), self.staff("VOLUNTEER"), forming_leader):
            self.assertEqual(self.link(user).status_code, 404, user.username)
        self.assertEqual(api().get(f"{self.qr_url}link/").status_code, 401)

    def test_a_waitlisted_team_does_not_see_it_yet(self):
        self.upload_qr()
        type(self.team).objects.filter(pk=self.team.pk).update(is_waitlisted=True)
        self.assertEqual(self.link(self.leader).status_code, 404)

    def test_members_of_another_hackathon_cannot_see_it(self):
        self.upload_qr()
        other = make_hackathon(min_team_size=1, features={"ideas": True})
        outsider = make_user()
        make_team(other, outsider)
        self.assertEqual(self.link(outsider).status_code, 404)

    def test_there_is_a_404_until_one_is_uploaded(self):
        self.assertEqual(self.link(self.admin).status_code, 404)

    def test_replacing_releases_the_old_image_and_removing_clears_it(self):
        with self.captureOnCommitCallbacks(execute=True):
            first, _ = self.upload_qr()
        old_key = IdeaFile.objects.get(pk=first).key
        with self.captureOnCommitCallbacks(execute=True):
            second, done = self.upload_qr(name="new-qr.png")
        self.assertEqual(done.data["payment_qr"]["name"], "new-qr.png")
        self.assertEqual(IdeaFile.objects.get(pk=first).state, UploadState.DELETED)
        self.assertNotIn(old_key, self.storage.objects)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(api(self.admin).delete(self.qr_url).status_code, 204)
        self.hack.refresh_from_db()
        self.assertIsNone(self.hack.idea_payment_qr)
        self.assertEqual(IdeaFile.objects.get(pk=second).state, UploadState.DELETED)
        self.assertEqual(self.storage.objects, {})
        self.assertEqual(api(self.admin).delete(self.qr_url).status_code, 204)   # removing nothing is fine
        self.assertEqual(self.link(self.leader).status_code, 404)
        self.assertTrue(AuditLog.objects.filter(action="Removed Hackathon Payment QR").exists())

    def test_it_must_be_an_image_of_a_sensible_size(self):
        post = lambda **body: api(self.admin).post(self.qr_url, {"name": "q.png", "size": 100, "type": "x", **body}, format="json")  # noqa: E731
        for name in ("q.pdf", "q.svg", "q.gif", "q.pptx", "q", "../q.png"):
            self.assertEqual(post(name=name).status_code, 400, name)
        self.assertEqual(post(size=5 * 1024 * 1024 + 1).status_code, 413)
        self.assertEqual(post(size=0).data["code"], "FILE_TOO_SMALL")

    def test_a_file_that_is_not_really_an_image_is_refused_and_removed(self):
        file_id, done = self.upload_qr(body=b"MZ not a png at all............")
        self.assertEqual((done.status_code, done.data["code"]), (400, "FILE_CONTENT_MISMATCH"))
        self.hack.refresh_from_db()
        self.assertIsNone(self.hack.idea_payment_qr)
        self.assertEqual(self.storage.objects, {})

    def test_completing_twice_or_before_the_file_arrives(self):
        asked = api(self.admin).post(self.qr_url, {"name": "q.png", "size": 10, "type": "x"}, format="json")
        file_id = asked.data["file"]["id"]
        self.assertEqual(api(self.admin).post(f"{self.qr_url}{file_id}/").data["code"], "UPLOAD_MISSING")
        self.storage.put(IdeaFile.objects.get(pk=file_id).key, PNG)
        self.assertEqual(api(self.admin).post(f"{self.qr_url}{file_id}/").status_code, 200)
        self.assertEqual(api(self.admin).post(f"{self.qr_url}{file_id}/").status_code, 200)

    def test_a_storage_failure_while_saving_leaves_the_old_qr_in_place(self):
        self.upload_qr()
        before = type(self.hack).objects.get(pk=self.hack.pk).idea_payment_qr_id
        with mock.patch.object(self.storage, "copy", side_effect=RuntimeError("down")):
            _, done = self.upload_qr(name="new.png")
        self.assertEqual(done.status_code, 503)
        self.assertEqual(type(self.hack).objects.get(pk=self.hack.pk).idea_payment_qr_id, before)

    def test_teams_cannot_use_the_qr_through_the_idea_form(self):
        file_id, _ = self.upload_qr()
        reply = api(self.leader).post(self.files_url, {"kind": "QR", "name": "q.png", "size": 10, "type": "x"}, format="json")
        self.assertEqual((reply.status_code, reply.data["field"]), (400, "kind"))
        self.assertEqual(api(self.leader).post(f"{self.files_url}{file_id}/").status_code, 404)
        self.assertEqual(api(self.leader).delete(f"{self.files_url}{file_id}/").status_code, 404)
        self.assertEqual(self.save(payment_file_id=file_id).data["code"], "FILE_NOT_FOUND")

    def test_uploads_being_switched_off_is_reported(self):
        with override_settings(FORM_UPLOADS_BACKEND="inline"):
            reply = api(self.admin).post(self.qr_url, {"name": "q.png", "size": 10, "type": "x"}, format="json")
        self.assertEqual((reply.status_code, reply.data["code"]), (503, "UPLOADS_UNAVAILABLE"))

    @override_settings(FORM_UPLOAD_SCAN="guardduty")
    def test_the_qr_is_only_shown_once_clean_and_malware_removes_it(self):
        from apps.forms import upload_maintenance
        file_id, _ = self.upload_qr()
        key = IdeaFile.objects.get(pk=file_id).key
        self.assertEqual(self.link(self.leader).data["code"], "SCAN_PENDING")
        self.storage.verdicts = {key: "NO_THREATS_FOUND"}
        self.assertEqual(self.link(self.leader).status_code, 200)
        IdeaFile.objects.filter(pk=file_id).update(scan_status=ScanStatus.NOT_SCANNED)
        self.storage.verdicts = {key: "THREATS_FOUND"}
        with self.captureOnCommitCallbacks(execute=True):
            upload_maintenance.scan_pending()
        self.hack.refresh_from_db()
        self.assertIsNone(self.hack.idea_payment_qr)
        self.assertNotIn(key, self.storage.objects)

    def test_deleting_the_hackathon_removes_the_qr_object(self):
        file_id, _ = self.upload_qr()
        key = IdeaFile.objects.get(pk=file_id).key
        with self.captureOnCommitCallbacks(execute=True):
            self.hack.delete()
        self.assertNotIn(key, self.storage.objects)

    def test_an_unsaved_qr_upload_is_swept_like_any_other_pending_file(self):
        from apps.forms import upload_maintenance
        asked = api(self.admin).post(self.qr_url, {"name": "q.png", "size": 10, "type": "x"}, format="json")
        IdeaFile.objects.filter(pk=asked.data["file"]["id"]).update(expires_at=timezone.now() - HOUR)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(upload_maintenance.sweep_expired()["expired"], 1)

    def test_a_cloned_hackathon_does_not_inherit_the_qr(self):
        self.upload_qr()
        reply = api(self.admin).post(f"/api/hackathons/{self.hack.slug}/clone/", {"title": "Copy", "start_date": (timezone.now() + 30 * HOUR).isoformat()}, format="json")
        self.assertEqual(reply.status_code, 201, reply.data)
        self.assertIsNone(type(self.hack).objects.get(slug=reply.data["slug"]).idea_payment_qr)
