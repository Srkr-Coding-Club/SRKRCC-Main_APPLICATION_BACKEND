"""
Server-side field rules for PATCH /api/auth/me/ (ProfileView / self-service
profile update).

UserProfileDetailSerializer only marks id/role/created_at/club_id read-only,
so first_name/last_name/roll_number/branch/year are all writable here. They
must be held to the exact same rules RegisterSerializer enforces at signup
(apps/accounts/validators.py) — otherwise a user could PATCH their own
profile to a value the signup form would have rejected outright.
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

    def test_malformed_roll_number_is_rejected(self):
        resp = self._patch(roll_number="bad!")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("roll_number", resp.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.roll_number, "21B91A0501")

    def test_roll_number_already_taken_by_another_user_is_rejected(self):
        User.objects.create_user(
            username="otherperson",
            email="otherperson@srkr.ac.in",
            password="Str0ng!Pass",
            roll_number="22C91A0777",
            role="NON_AFFILIATE",
        )
        resp = self._patch(roll_number="22C91A0777")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("roll_number", resp.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.roll_number, "21B91A0501")

    def test_patching_own_unchanged_roll_number_succeeds(self):
        """Regression guard: the uniqueness check must exclude the user's own
        row, or re-saving an unchanged valid roll number would incorrectly
        get rejected as 'already taken by themselves'."""
        resp = self._patch(roll_number="21B91A0501", first_name="Ravi")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.roll_number, "21B91A0501")
        self.assertEqual(self.user.first_name, "Ravi")

    def test_other_currently_valid_field_still_succeeds(self):
        resp = self._patch(branch="IT")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.branch, "IT")
