"""
Server-side field rules for PATCH /api/auth/me/ (ProfileView / self-service
profile update).

UserProfileDetailSerializer only marks id/role/created_at/club_id/email
read-only, so first_name/last_name/roll_number/phone_number/branch/year are
all writable here. They must be held to the exact same rules
RegisterSerializer enforces at signup (apps/accounts/validators.py) —
otherwise a user could PATCH their own profile to a value the signup form
would have rejected outright.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

User = get_user_model()


class ProfileUpdateValidationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="selfupdater",
            email="selfupdater@srkr.ac.in",
            password="Str0ng!Pass",
            first_name="Ravi",
            last_name="Kumar",
            roll_number="21B91A0501",
            role="NON_AFFILIATE",
        )
        self.client.force_authenticate(self.user)

    def _patch(self, **fields):
        return self.client.patch("/api/auth/me/", fields, format="json")

    def test_name_with_digits_is_rejected(self):
        resp = self._patch(first_name="Ravi123")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("first_name", resp.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Ravi")

    # --- Roll number: locked after the member's own first self-set ---------
    #
    # setUp()'s self.user already has roll_number="21B91A0501", so any PATCH
    # here that tries to change or clear it is exercising the "already set"
    # lock (see UserProfileDetailSerializer.validate_roll_number). Tests for
    # the first-time-set path use a separate user created with no roll
    # number, since that's the only state where this endpoint still accepts
    # a new value.

    def test_already_set_roll_number_cannot_be_changed_by_member(self):
        resp = self._patch(roll_number="22C91A0777")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("roll_number", resp.data)
        self.assertIn("admin", str(resp.data["roll_number"]).lower())
        self.user.refresh_from_db()
        self.assertEqual(self.user.roll_number, "21B91A0501")

    def test_already_set_roll_number_cannot_be_cleared_by_member(self):
        resp = self._patch(roll_number="")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("roll_number", resp.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.roll_number, "21B91A0501")

    def test_patching_own_unchanged_roll_number_succeeds(self):
        """Regression guard: resubmitting the same (unchanged) value must be
        treated as a no-op, not as an attempted change, or the profile form
        round-tripping this field on every save would always be rejected."""
        resp = self._patch(roll_number="21B91A0501", first_name="Ravi")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.roll_number, "21B91A0501")
        self.assertEqual(self.user.first_name, "Ravi")

    def test_member_with_no_roll_number_can_set_one(self):
        blank_user = User.objects.create_user(
            username="norollyet", email="norollyet@srkr.ac.in", password="Str0ng!Pass", role="NON_AFFILIATE",
        )
        client = APIClient()
        client.force_authenticate(blank_user)
        resp = client.patch("/api/auth/me/", {"roll_number": "23C91A0888"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        blank_user.refresh_from_db()
        self.assertEqual(blank_user.roll_number, "23C91A0888")

    def test_malformed_roll_number_is_rejected_on_first_set(self):
        blank_user = User.objects.create_user(
            username="norollyet2", email="norollyet2@srkr.ac.in", password="Str0ng!Pass", role="NON_AFFILIATE",
        )
        client = APIClient()
        client.force_authenticate(blank_user)
        resp = client.patch("/api/auth/me/", {"roll_number": "bad!"}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("roll_number", resp.data)

    def test_roll_number_already_taken_by_another_user_is_rejected_on_first_set(self):
        blank_user = User.objects.create_user(
            username="norollyet3", email="norollyet3@srkr.ac.in", password="Str0ng!Pass", role="NON_AFFILIATE",
        )
        client = APIClient()
        client.force_authenticate(blank_user)
        resp = client.patch("/api/auth/me/", {"roll_number": "21B91A0501"}, format="json")  # self.user's roll number
        self.assertEqual(resp.status_code, 400)
        self.assertIn("roll_number", resp.data)
        blank_user.refresh_from_db()
        self.assertIsNone(blank_user.roll_number)

    def test_once_set_via_admin_it_locks_against_the_member_too(self):
        """A roll number set by an admin (not the member) still locks the
        self-service path the same way a self-set one does — the lock is
        keyed on "does the user row already have a value", not on who put it
        there."""
        admin_set_user = User.objects.create_user(
            username="adminsetroll", email="adminsetroll@srkr.ac.in", password="Str0ng!Pass",
            role="NON_AFFILIATE", roll_number="24D91A0999",
        )
        client = APIClient()
        client.force_authenticate(admin_set_user)
        resp = client.patch("/api/auth/me/", {"roll_number": "25E91A0111"}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("roll_number", resp.data)

    def test_other_currently_valid_field_still_succeeds(self):
        resp = self._patch(branch="IT")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.branch, "IT")

    def test_email_is_immutable_via_profile_patch(self):
        """email is the account identity (login + USERNAME_FIELD) — it must
        never change through this endpoint, silently or otherwise."""
        resp = self._patch(email="attacker@evil.com", first_name="Ravi")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "selfupdater@srkr.ac.in")

    def test_phone_number_with_letters_is_rejected(self):
        resp = self._patch(phone_number="98765abcde")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("phone_number", resp.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.phone_number, None)

    def test_phone_number_wrong_length_is_rejected(self):
        resp = self._patch(phone_number="98765")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("phone_number", resp.data)

    def test_valid_phone_number_succeeds(self):
        resp = self._patch(phone_number="9876543210")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.phone_number, "9876543210")

    def test_phone_number_with_spaces_is_normalized(self):
        resp = self._patch(phone_number="98765 43210")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.phone_number, "9876543210")

    def test_phone_number_can_be_cleared(self):
        self.user.phone_number = "9876543210"
        self.user.save(update_fields=["phone_number"])
        resp = self._patch(phone_number="")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.phone_number, "")
