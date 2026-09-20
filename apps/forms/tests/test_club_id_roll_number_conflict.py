from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework.test import APITestCase

from apps.accounts.services.user_account_service import UserAccountService
from apps.forms.models import FieldType, FormStatus, Response
from .factories import make_form, add_field

User = get_user_model()


class ClubIdRollNumberConflictTests(APITestCase):
    """
    A public form with club_id_enabled + a roll_number field mapping runs
    UserAccountService.upsert_member() for the anonymous submitter (see
    apps/forms/views.py ResponseViewSet.create -> FormAutomationService.
    resolve_club_member). roll_number carries a DB-level unique constraint
    (migration 0005) — an ordinary submitter who typos someone else's roll
    number must get back a clean 400, not an unhandled 500. Regression
    coverage for RollNumberConflictError in
    apps/accounts/services/user_account_service.py.
    """

    def setUp(self):
        cache.clear()
        self.existing_user = User.objects.create_user(
            username="existing1", email="existing1@srkr.ac.in", password="pw12345!",
            role="NON_AFFILIATE", roll_number="21A91A0501",
        )

        self.form = make_form(status=FormStatus.PUBLISHED, club_id_enabled=True)
        self.email_field = add_field(self.form, FieldType.EMAIL, label="Email", required=True, order=1)
        self.roll_field = add_field(self.form, FieldType.TEXT, label="Roll Number", required=True, order=2)
        self.form.club_id_field_mapping = {
            "email": self.email_field.id,
            "roll_number": self.roll_field.id,
        }
        self.form.save(update_fields=["club_id_field_mapping"])

    def tearDown(self):
        cache.clear()

    def _submit(self, email, roll_number, idempotency_key):
        body = {
            "form": self.form.id,
            "answers": [
                {"field": self.email_field.id, "value": email},
                {"field": self.roll_field.id, "value": roll_number},
            ],
            "idempotency_key": idempotency_key,
        }
        return self.client.post("/api/forms/submissions/", body, format="json")

    def test_duplicate_roll_number_on_new_submitter_returns_400_not_500(self):
        """Create branch: a brand-new anonymous submitter accidentally supplies
        another member's roll_number (e.g. a typo'd digit)."""
        resp = self._submit(
            email="new-person@srkr.ac.in",
            roll_number=self.existing_user.roll_number,
            idempotency_key="roll-conflict-create-1",
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("error", resp.data)
        self.assertIn(self.existing_user.roll_number, resp.data["error"])

        # No duplicate user row was created for the colliding roll_number, and
        # the whole response was rolled back together with the conflict.
        self.assertEqual(
            User.objects.filter(roll_number=self.existing_user.roll_number).count(), 1
        )
        self.assertFalse(User.objects.filter(email="new-person@srkr.ac.in").exists())
        self.assertFalse(Response.objects.filter(form=self.form).exists())

    def test_duplicate_roll_number_on_update_of_partial_user_returns_400_not_500(self):
        """Update branch: an existing partial member (e.g. created by a prior
        form submission with no roll_number yet) submits again supplying a
        roll_number that collides with someone else's."""
        partial_user, _created, _changed = UserAccountService.upsert_member(
            {"email": "partial-person@srkr.ac.in"},
            source_origin="FORM_REGISTRATION",
        )
        self.assertIsNone(partial_user.roll_number)

        resp = self._submit(
            email=partial_user.email,
            roll_number=self.existing_user.roll_number,
            idempotency_key="roll-conflict-update-1",
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("error", resp.data)
        self.assertIn(self.existing_user.roll_number, resp.data["error"])

        partial_user.refresh_from_db()
        self.assertIsNone(partial_user.roll_number)
        self.assertEqual(
            User.objects.filter(roll_number=self.existing_user.roll_number).count(), 1
        )
