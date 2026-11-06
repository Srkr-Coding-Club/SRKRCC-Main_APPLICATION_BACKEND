from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase
from apps.forms.models import FieldType, FormStatus, Response
from apps.forms.services import evaluate_form_restrictions
from apps.forms.serializers import FormSerializer
from .factories import make_form, add_field

User = get_user_model()


class FormRestrictionsServiceTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="test_student",
            email="student@srkr.ac.in",
            password="password123",
            role="NON_AFFILIATE",
            year=2,
            branch="CSE",
            roll_number="21B91A0501",
            other_college_name="SRKR Engineering College",
        )

    def test_disabled_restrictions_always_pass(self):
        restrictions = {
            "enabled": False,
            "club_members_only": True,
            "allowed_roles": ["CLUB_LEAD"],
        }
        reasons = evaluate_form_restrictions(self.user, restrictions)
        self.assertEqual(reasons, [])

    def test_club_member_only_check(self):
        restrictions = {"enabled": True, "club_members_only": True}
        # Non-affiliate without club_id
        reasons = evaluate_form_restrictions(self.user, restrictions)
        self.assertTrue(len(reasons) > 0)
        self.assertIn("registered Club Members", reasons[0])

        # With club_id
        self.user.club_id = "SRKR-001"
        reasons = evaluate_form_restrictions(self.user, restrictions)
        self.assertEqual(reasons, [])

        # Or with affiliate role
        self.user.club_id = None
        self.user.role = "AFFILIATE"
        reasons = evaluate_form_restrictions(self.user, restrictions)
        self.assertEqual(reasons, [])

    def test_allowed_roles_check(self):
        restrictions = {"enabled": True, "allowed_roles": ["CLUB_LEAD", "ADMIN"]}
        self.user.role = "NON_AFFILIATE"
        reasons = evaluate_form_restrictions(self.user, restrictions)
        self.assertEqual(len(reasons), 1)
        self.assertIn("Restricted to specific roles", reasons[0])

        self.user.role = "CLUB_LEAD"
        reasons = evaluate_form_restrictions(self.user, restrictions)
        self.assertEqual(reasons, [])

    def test_allowed_years_check(self):
        restrictions = {"enabled": True, "allowed_years": [3, 4]}
        self.user.year = 2
        reasons = evaluate_form_restrictions(self.user, restrictions)
        self.assertEqual(len(reasons), 1)
        self.assertIn("Restricted to academic years", reasons[0])

        self.user.year = 3
        reasons = evaluate_form_restrictions(self.user, restrictions)
        self.assertEqual(reasons, [])

    def test_allowed_branches_check(self):
        restrictions = {"enabled": True, "allowed_branches": ["ECE", "EEE"]}
        self.user.branch = "CSE"
        reasons = evaluate_form_restrictions(self.user, restrictions)
        self.assertEqual(len(reasons), 1)
        self.assertIn("Restricted to branches", reasons[0])

        self.user.branch = "ECE"
        reasons = evaluate_form_restrictions(self.user, restrictions)
        self.assertEqual(reasons, [])

    def test_home_college_and_allowed_colleges(self):
        restrictions = {"enabled": True, "home_college_only": True}
        self.user.other_college_name = "SRKR Engineering College"
        reasons = evaluate_form_restrictions(self.user, restrictions)
        self.assertEqual(reasons, [])

        self.user.other_college_name = "Other Tech University"
        reasons = evaluate_form_restrictions(self.user, restrictions)
        self.assertEqual(len(reasons), 1)
        self.assertIn("Restricted to students of SRKR Engineering College", reasons[0])

    def test_specific_member_whitelist(self):
        restrictions = {"enabled": True, "allowed_members": ["student@srkr.ac.in", "21B91A0502"]}
        reasons = evaluate_form_restrictions(self.user, restrictions)
        self.assertEqual(reasons, [])

        restrictions["allowed_members"] = ["other@srkr.ac.in"]
        reasons = evaluate_form_restrictions(self.user, restrictions)
        self.assertEqual(len(reasons), 1)
        self.assertIn("pre-approved invited members", reasons[0])


class SubmissionRestrictionsEndpointTests(APITestCase):
    def setUp(self):
        self.eligible_user = User.objects.create_user(
            username="eligible_user",
            email="eligible@srkr.ac.in",
            password="password123",
            role="NON_AFFILIATE",
            year=3,
            branch="CSE",
            other_college_name="SRKR Engineering College",
        )
        self.ineligible_user = User.objects.create_user(
            username="ineligible_user",
            email="ineligible@srkr.ac.in",
            password="password123",
            role="NON_AFFILIATE",
            year=1,
            branch="MECH",
            other_college_name="Other College",
        )

        self.form = make_form(
            status=FormStatus.PUBLISHED,
            allow_multiple_responses=True,
            restrictions_enabled=True,
            restrictions={
                "enabled": True,
                "allowed_years": [3, 4],
                "allowed_branches": ["CSE", "IT"],
                "custom_restriction_message": "Exclusively for 3rd and 4th year CSE/IT students.",
            },
            submission_note_enabled=True,
            submission_note_title="Mandatory Guidelines",
            submission_note_content="Please adhere to the event rules.",
            submission_note_confirm_label="I Accept",
            submission_note_cancel_label="Go Back",
        )
        self.field = add_field(self.form, FieldType.TEXT, label="Full Name", required=True)

    def test_anonymous_submission_blocked_when_restrictions_enabled(self):
        payload = {
            "form": self.form.id,
            "answers": [{"field": self.field.id, "value": "Anonymous"}],
        }
        resp = self.client.post("/api/forms/submissions/", payload, format="json")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.data.get("code"), "AUTH_REQUIRED")

    def test_ineligible_user_submission_blocked(self):
        self.client.force_authenticate(self.ineligible_user)
        payload = {
            "form": self.form.id,
            "answers": [{"field": self.field.id, "value": "Ineligible Student"}],
        }
        resp = self.client.post("/api/forms/submissions/", payload, format="json")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.data.get("code"), "ACCESS_RESTRICTED")
        self.assertEqual(resp.data.get("error"), "Exclusively for 3rd and 4th year CSE/IT students.")
        self.assertTrue(len(resp.data.get("reasons", [])) >= 2)

    def test_eligible_user_submission_allowed(self):
        self.client.force_authenticate(self.eligible_user)
        payload = {
            "form": self.form.id,
            "answers": [{"field": self.field.id, "value": "Eligible Student"}],
        }
        resp = self.client.post("/api/forms/submissions/", payload, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(Response.objects.filter(form=self.form, user=self.eligible_user).exists())

    def test_form_serializer_includes_restrictions_and_notes(self):
        serializer = FormSerializer(self.form)
        data = serializer.data
        self.assertTrue(data["restrictions_enabled"])
        self.assertEqual(data["restrictions"]["allowed_years"], [3, 4])
        self.assertTrue(data["submission_note_enabled"])
        self.assertEqual(data["submission_note_title"], "Mandatory Guidelines")
        self.assertEqual(data["submission_note_content"], "Please adhere to the event rules.")
        self.assertEqual(data["submission_note_confirm_label"], "I Accept")
        self.assertEqual(data["submission_note_cancel_label"], "Go Back")

