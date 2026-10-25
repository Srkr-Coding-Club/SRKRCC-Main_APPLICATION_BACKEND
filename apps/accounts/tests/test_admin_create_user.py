"""An admin creating a member (POST /api/auth/register/ as an admin) and the Club ID formats it accepts."""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import ClubIDSequence
from apps.accounts.services.club_id_service import ClubIDService, InvalidClubIdError
from apps.colleges.models import College, DEFAULT_COLLEGE_CODE, DEFAULT_COLLEGE_NAME

User = get_user_model()
URL = "/api/auth/register/"


class ClubIdFormats(TestCase):
    def test_the_three_formats_the_club_uses_are_accepted(self):
        for given, canonical in (("25SCC123", "25SCC123"), ("26SCC123", "26SCC123"), ("21CCA041", "21CCA041"), ("1CCA041", "1CCA041")):
            parsed = ClubIDService.parse_club_id(given)
            self.assertEqual(parsed["canonical_id"], canonical, given)

    def test_case_and_spaces_are_forgiven_and_the_stored_form_is_upper_case(self):
        self.assertEqual(ClubIDService.parse_club_id("  25scc277 ")["canonical_id"], "25SCC277")
        self.assertEqual(ClubIDService.parse_club_id("21cca041")["canonical_id"], "21CCA041")

    def test_parts_are_read_correctly(self):
        parsed = ClubIDService.parse_club_id("25SCC277")
        self.assertEqual((parsed["year_2digit"], parsed["full_year"], parsed["prefix"], parsed["sequence"]), (25, 2025, "SCC", 277))
        self.assertEqual(ClubIDService.parse_club_id("21CCA041")["sequence"], 41)

    def test_everything_else_is_refused_with_a_message_that_shows_the_formats(self):
        for bad in ("", "   ", None, 25, "25XYZ123", "25SCC12", "25SCC", "SCC123", "5SCC123", "123SCC123", "25SCC12A", "25 SCC 123",
                    "25SCC-123", "25CCA", "xyz", "25SCCC123", "2SCC123", "0CCAX41"):
            with self.assertRaises(InvalidClubIdError, msg=repr(bad)) as raised:
                ClubIDService.parse_club_id(bad)
            if isinstance(bad, str) and bad.strip():
                self.assertIn("25SCC277", str(raised.exception), bad)

    def test_an_explicit_prefix_is_still_enforced(self):
        self.assertTrue(ClubIDService.validate_club_id_format("25SCC277", expected_prefix="SCC"))
        self.assertFalse(ClubIDService.validate_club_id_format("21CCA041", expected_prefix="SCC"))
        self.assertTrue(ClubIDService.validate_club_id_format("21CCA041", expected_prefix="CCA"))
        self.assertFalse(ClubIDService.validate_club_id_format("25XYZ277", expected_prefix="SCC"))

    def test_only_the_clubs_own_ids_move_the_counter(self):
        ClubIDService.sync_sequence_watermark(2025, 277, "SCC")
        self.assertEqual(ClubIDSequence.objects.get(prefix="SCC", year=2025).next_sequence, 278)
        ClubIDService.sync_sequence_watermark(2021, 41, "CCA")
        self.assertFalse(ClubIDSequence.objects.filter(prefix="CCA").exists())
        self.assertEqual(ClubIDService.allocate_next_club_id(year=2025), "25SCC278")   # new IDs are still SCC


class AdminCreatesAMember(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        College.objects.get_or_create(code=DEFAULT_COLLEGE_CODE, defaults={"name": DEFAULT_COLLEGE_NAME})
        self.other_college = College.objects.create(code="C-1", name="Godavari Institute of Engineering", state="Andhra Pradesh", district="East Godavari")
        self.admin = User.objects.create_user(username="ada", email="ada@srkrec.ac.in", password="x", role="ADMIN")
        self.lead = User.objects.create_user(username="lee", email="lee@srkrec.ac.in", password="x", role="CLUB_LEAD")
        self.student = User.objects.create_user(username="stu", email="stu@srkrec.ac.in", password="x", role="NON_AFFILIATE")
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.n = 0

    def body(self, **extra):
        self.n += 1
        return {"email": f"new{self.n}@example.org", "password": "StrongPass123!", "first_name": "Ravi", "last_name": "Kumar",
                "branch": "CSE", "year": 2, "role": "NON_AFFILIATE", **extra}

    def make(self, user=None, **extra):
        client = self.client
        if user is not None:
            client = APIClient()
            client.force_authenticate(user)
        return client.post(URL, self.body(**extra), format="json")

    # --- year and college ---------------------------------------------------------------------------------

    def test_the_year_is_saved(self):
        reply = self.make(year=3)
        self.assertEqual(reply.status_code, 201, reply.data)
        self.assertEqual(User.objects.get(email=reply.data["email"]).year, 3)

    def test_the_year_is_required_and_must_be_one_to_four(self):
        for year in (0, 5, -1, 99, "x", "", None, 2.5):
            reply = self.make(year=year)
            self.assertEqual(reply.status_code, 400, repr(year))
            self.assertIn("year", reply.data, repr(year))
        body = self.body()
        del body["year"]
        self.assertIn("year", self.client.post(URL, body, format="json").data)
        for year in (1, 2, 3, 4, "3"):
            self.assertEqual(self.make(year=year).status_code, 201, year)

    def test_a_catalogue_college_is_saved_and_clears_any_typed_name(self):
        reply = self.make(college_id=self.other_college.pk, other_college_name="Ignored")
        self.assertEqual(reply.status_code, 201, reply.data)
        user = User.objects.get(email=reply.data["email"])
        self.assertEqual((user.college_id, user.other_college_name), (self.other_college.pk, ""))

    def test_a_typed_college_name_is_saved_without_a_catalogue_row(self):
        reply = self.make(other_college_name="  Village Polytechnic  ")
        self.assertEqual(reply.status_code, 201, reply.data)
        user = User.objects.get(email=reply.data["email"])
        self.assertEqual((user.college_id, user.other_college_name), (None, "Village Polytechnic"))

    def test_leaving_the_college_out_means_the_clubs_home_college(self):
        user = User.objects.get(email=self.make().data["email"])
        self.assertEqual(user.college.code, DEFAULT_COLLEGE_CODE)

    def test_an_unknown_college_is_refused_and_a_silly_typed_name_too(self):
        self.assertIn("college_id", self.make(college_id=999999).data)
        self.assertIn("college_id", self.make(college_id="abc").data)
        for name in ("x", "<script>alert(1)</script>", "A" * 300):
            reply = self.make(other_college_name=name)
            self.assertEqual(reply.status_code, 400, name)
            self.assertIn("other_college_name", reply.data, name)

    # --- the other fields ------------------------------------------------------------------------------------

    def test_branch_phone_and_roll_number_rules(self):
        self.assertIn("branch", self.make(branch="XYZ").data)
        self.assertIn("branch", self.make(branch="").data)
        self.assertEqual(self.make(branch="aiml").status_code, 201)
        for phone in ("12345", "98765432101", "98765abcde"):
            self.assertIn("phone_number", self.make(phone_number=phone).data, phone)
        self.assertEqual(self.make(phone_number="").status_code, 201)
        self.assertEqual(self.make(phone_number="9876543210").status_code, 201)
        for roll in ("123", "21B91A05011", "21B91A05!1", "B191A05011"):
            self.assertIn("roll_number", self.make(roll_number=roll).data, roll)
        self.assertEqual(self.make(roll_number="21b91a0501").status_code, 201)
        self.assertIn("roll_number", self.make(roll_number="21B91A0501").data)   # now taken

    def test_the_email_must_be_new_whatever_its_case_and_a_single_word_name_is_fine(self):
        self.assertEqual(self.make(email="Same@Example.org").status_code, 201)
        self.assertIn("email", self.make(email="same@example.ORG").data)
        self.assertIn("email", self.make(email="not-an-email").data)
        self.assertEqual(self.make(last_name="").status_code, 201)
        self.assertIn("first_name", self.make(first_name="R2D2").data)
        self.assertIn("first_name", self.make(first_name="").data)

    def test_passwords_are_checked(self):
        for password in ("short", "alllowercaseletters", "12345678", ""):
            self.assertIn("password", self.make(password=password).data, repr(password))

    def test_every_error_is_reported_under_its_own_field_so_a_form_can_show_it_there(self):
        reply = self.make(year=9, branch="XYZ", phone_number="1", roll_number="1", email="nope")
        self.assertEqual(sorted(reply.data), ["branch", "email", "phone_number", "roll_number", "year"])

    # --- club id and role --------------------------------------------------------------------------------------

    def test_an_affiliate_needs_a_valid_unused_club_id_in_any_of_the_formats(self):
        for club_id in ("25SCC123", "26SCC123", "21CCA041", "1CCA041"):
            reply = self.make(role="AFFILIATE", club_id=club_id)
            self.assertEqual(reply.status_code, 201, (club_id, reply.data))
            self.assertEqual(User.objects.get(email=reply.data["email"]).club_id, club_id)
        self.assertIn("club_id", self.make(role="AFFILIATE", club_id="25SCC123").data)            # taken
        self.assertIn("club_id", self.make(role="AFFILIATE", club_id="25scc123").data)            # taken, case-insensitive
        self.assertIn("club_id", self.make(role="AFFILIATE").data)                                 # missing
        self.assertIn("club_id", self.make(role="AFFILIATE", club_id="25XYZ123").data)
        self.assertIn("club_id", self.make(role="AFFILIATE", club_id="5SCC123").data)

    def test_a_club_id_given_to_a_non_affiliate_is_still_checked(self):
        self.assertEqual(self.make(club_id="22SCC500").status_code, 201)
        self.assertIn("club_id", self.make(club_id="bad").data)

    def test_lowercase_club_ids_are_stored_in_capitals(self):
        reply = self.make(role="AFFILIATE", club_id="21cca077")
        self.assertEqual(User.objects.get(email=reply.data["email"]).club_id, "21CCA077")

    def test_only_admins_and_club_leads_may_create_volunteers_and_nobody_may_create_elevated_roles_here(self):
        self.assertEqual(self.make(role="VOLUNTEER").data["role"], "VOLUNTEER")
        self.assertEqual(self.make(user=self.lead, role="VOLUNTEER").data["role"], "VOLUNTEER")
        for role in ("ADMIN", "CLUB_LEAD", "JUDGE"):
            self.assertEqual(self.make(role=role).data["role"], "NON_AFFILIATE", role)
        self.assertEqual(self.make(user=self.student, role="VOLUNTEER").data["role"], "NON_AFFILIATE")
        self.assertEqual(APIClient().post(URL, self.body(role="VOLUNTEER"), format="json").data["role"], "NON_AFFILIATE")

    def test_the_account_can_sign_in_with_the_password_the_admin_set(self):
        email = self.make(password="AdminSet#2026").data["email"]
        reply = APIClient().post("/api/auth/login/", {"email": email, "password": "AdminSet#2026"}, format="json")
        self.assertEqual(reply.status_code, 200, reply.data)

    def test_the_response_never_contains_the_password(self):
        self.assertNotIn("password", self.make().data)


class AdminEditsAMembersCollege(TestCase):
    """PATCH /api/auth/users/{id}/ - the admin can correct a member's college as well as the other details."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.home, _ = College.objects.get_or_create(code=DEFAULT_COLLEGE_CODE, defaults={"name": DEFAULT_COLLEGE_NAME})
        self.other = College.objects.create(code="C-2", name="Kakinada Institute", state="Andhra Pradesh", district="Kakinada")
        self.admin = User.objects.create_user(username="ada", email="ada@srkrec.ac.in", password="x", role="ADMIN")
        self.member = User.objects.create_user(username="mem", email="mem@example.org", password="x", role="NON_AFFILIATE", college=self.home, branch="CSE", year=2)
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.url = f"/api/auth/users/{self.member.pk}/"

    def patch(self, **body):
        return self.client.patch(self.url, body, format="json")

    def test_a_catalogue_college_can_be_set_and_is_returned_with_its_name(self):
        reply = self.patch(college_id=self.other.pk)
        self.assertEqual(reply.status_code, 200, reply.data)
        self.assertEqual(reply.data["college"]["name"], "Kakinada Institute")
        self.member.refresh_from_db()
        self.assertEqual((self.member.college_id, self.member.other_college_name), (self.other.pk, ""))

    def test_a_typed_college_replaces_the_catalogue_one(self):
        reply = self.patch(other_college_name="  Village Polytechnic ")
        self.assertEqual(reply.status_code, 200, reply.data)
        self.member.refresh_from_db()
        self.assertEqual((self.member.college_id, self.member.other_college_name), (None, "Village Polytechnic"))
        self.assertIsNone(reply.data["college"])

    def test_going_back_to_the_catalogue_clears_the_typed_name(self):
        self.patch(other_college_name="Village Polytechnic")
        self.patch(college_id=self.home.pk)
        self.member.refresh_from_db()
        self.assertEqual((self.member.college_id, self.member.other_college_name), (self.home.pk, ""))

    def test_clearing_the_college_without_a_name_is_refused(self):
        reply = self.patch(college_id=None)
        self.assertEqual(reply.status_code, 400)
        self.assertIn("college_id", reply.data)
        self.member.refresh_from_db()
        self.assertEqual(self.member.college_id, self.home.pk)

    def test_unknown_and_silly_values_are_refused(self):
        self.assertIn("college_id", self.patch(college_id=999999).data)
        self.assertIn("other_college_name", self.patch(other_college_name="<b>x</b>").data)
        self.assertIn("other_college_name", self.patch(other_college_name="ab").data)

    def test_other_edits_do_not_touch_the_college(self):
        self.assertEqual(self.patch(branch="IT", year=3).status_code, 200)
        self.member.refresh_from_db()
        self.assertEqual((self.member.college_id, self.member.branch, self.member.year), (self.home.pk, "IT", 3))

    def test_the_change_is_audited_with_readable_values(self):
        from apps.audit.models import AuditLog
        self.patch(college_id=self.other.pk)
        entry = AuditLog.objects.filter(target_model="User", target_id=str(self.member.pk)).latest("id")
        self.assertEqual(entry.details["new_college"], "Kakinada Institute")
        self.assertEqual(entry.details["previous_college"], DEFAULT_COLLEGE_NAME)

    def test_a_non_admin_cannot_change_it(self):
        student = User.objects.create_user(username="stu", email="stu@srkrec.ac.in", password="x", role="NON_AFFILIATE")
        client = APIClient()
        client.force_authenticate(student)
        self.assertEqual(client.patch(self.url, {"college_id": self.other.pk}, format="json").status_code, 403)
