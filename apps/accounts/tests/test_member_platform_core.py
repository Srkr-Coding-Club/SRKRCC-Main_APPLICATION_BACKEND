import io
from datetime import datetime
from django.test import TestCase
from django.utils import timezone
from apps.accounts.models import User, ClubIDSequence, ImportJob, MembershipStatus
from apps.accounts.services.club_id_service import ClubIDService, InvalidClubIdError
from apps.accounts.services.user_account_service import UserAccountService, ClubIdImmutableError, ClubIdConflictError
from apps.accounts.services.referral_service import ReferralService
from apps.accounts.services.member_import_service import MemberImportService
from apps.core.models import EmailTemplate, EmailJob
from apps.core.services.email_service import EmailNotificationService, TemplateSecurityError, MemberEmailContext


class ClubPlatformCoreTests(TestCase):

    def setUp(self):
        User.objects.all().delete()
        ClubIDSequence.objects.all().delete()

    def test_club_id_allocation_and_rollover(self):
        """Tests sequential Club ID generation and prefix/year formatting for SRKR Coding Club."""
        id1 = ClubIDService.allocate_next_club_id(year=2025, prefix="SCC")
        self.assertEqual(id1, "25SCC001")

        id2 = ClubIDService.allocate_next_club_id(year=2025, prefix="SCC")
        self.assertEqual(id2, "25SCC002")

        # Rollover to 2026
        id_2026 = ClubIDService.allocate_next_club_id(year=2026, prefix="SCC")
        self.assertEqual(id_2026, "26SCC001")

        # Watermark sync
        ClubIDService.sync_sequence_watermark(year=2025, max_seen_sequence=277, prefix="SCC")
        id_next = ClubIDService.allocate_next_club_id(year=2025, prefix="SCC")
        self.assertEqual(id_next, "25SCC278")

    def test_club_id_parsing_and_validation(self):
        """Tests parsing and validation of Club IDs."""
        parsed = ClubIDService.parse_club_id("25SCC277", expected_prefix="SCC")
        self.assertEqual(parsed["year_2digit"], 25)
        self.assertEqual(parsed["full_year"], 2025)
        self.assertEqual(parsed["prefix"], "SCC")
        self.assertEqual(parsed["sequence"], 277)

        self.assertTrue(ClubIDService.validate_club_id_format("25SCC277", expected_prefix="SCC"))
        self.assertFalse(ClubIDService.validate_club_id_format("25XYZ277", expected_prefix="SCC"))
        self.assertFalse(ClubIDService.validate_club_id_format("invalid-id", expected_prefix="SCC"))

    def test_user_account_service_identity_and_immutability(self):
        """Tests email normalization, user creation, and Club ID immutability."""
        # 1. Create member
        user, created, _ = UserAccountService.upsert_member({
            "email": "Monisha@Gmail.com ",
            "full_name": "Talluri Naga Monisha",
            "branch": "Computer Science",
            "phone_number": "9848786959",
            "club_id": "25SCC277",
            "registered_at": "Nov 2, 2025",
            "membership_status": "Active",
            "referred_by": "Ankith",
        })
        self.assertTrue(created)
        self.assertEqual(user.email, "monisha@gmail.com")
        self.assertEqual(user.club_id, "25SCC277")
        self.assertEqual(user.branch, "CSE")
        self.assertEqual(user.membership_status, MembershipStatus.ACTIVE)
        self.assertEqual(user.referred_by_raw, "Ankith")

        # 2. Update member with same email and same Club ID (allowed)
        user2, created2, changed = UserAccountService.upsert_member({
            "email": "monisha@gmail.com",
            "phone_number": "9848786999",
            "club_id": "25SCC277",
        })
        self.assertFalse(created2)
        self.assertEqual(user2.phone_number, "9848786999")

        # 3. Attempting to change Club ID must raise ClubIdImmutableError
        with self.assertRaises(ClubIdImmutableError):
            UserAccountService.upsert_member({
                "email": "monisha@gmail.com",
                "club_id": "25SCC999",
            })

    def test_legacy_backup_csv_import_pipeline(self):
        """
        Tests 3-stage preview & commit pipeline with the user's provided exact sample dataset.
        """
        sample_csv = (
            "S.No,Full Name,Email,Phone Number,Branch,Club ID,Member,Registration Date,Status\n"
            "1,Talluri Naga Monisha,monishatalluri08@gmail.com,9848786959,CSE,25SCC277,Ankith,Nov 2, 2025,Active\n"
            "2,Sangu Kowsalya,kowsalyasangu@gmail.com,8309176663,CSE,25SCC276,Ankith,Nov 2, 2025,Active\n"
            "3,VARIKUTI SASI SRI CHANDANA,srichandanavarikuti@gmail.com,8179351248,AIDS,25SCC275,A.Asha,Nov 1, 2025,Active\n"
            "4,Pothamsetti Mohana Siri,mohanasiripothamsetti@gmail.com,8074027925,ECE,25SCC274,Ankith,Oct 28, 2025,Active\n"
            "5,R Tharani,reddytharani12@gmail.com,8885618973,IT,25SCC273,A.Vijay Babu,Oct 21, 2025,Active\n"
        )
        file_obj = io.BytesIO(sample_csv.encode("utf-8"))

        # Stage 2: Preview
        job, preview = MemberImportService.preview_import(
            file_obj=file_obj,
            filename="legacy_members_backup.csv",
        )
        self.assertEqual(preview["total_rows"], 5)
        self.assertEqual(preview["valid_rows"], 5)
        self.assertEqual(preview["conflict_rows"], 0)
        self.assertEqual(preview["new_users_count"], 5)

        # Stage 3: Commit
        commit_res = MemberImportService.commit_import(job_id=str(job.id))
        self.assertTrue(commit_res["success"])
        self.assertEqual(commit_res["imported_count"], 5)
        self.assertEqual(commit_res["new_users_count"], 5)

        # Verify DB records
        monisha = UserAccountService.find_by_email("monishatalluri08@gmail.com")
        self.assertIsNotNone(monisha)
        self.assertEqual(monisha.club_id, "25SCC277")
        self.assertEqual(monisha.branch, "CSE")
        self.assertEqual(monisha.phone_number, "9848786959")
        self.assertEqual(monisha.referred_by_raw, "Ankith")
        self.assertEqual(monisha.membership_status, MembershipStatus.ACTIVE)

        tharani = UserAccountService.find_by_club_id("25SCC273")
        self.assertIsNotNone(tharani)
        self.assertEqual(tharani.email, "reddytharani12@gmail.com")
        self.assertEqual(tharani.branch, "IT")

        # Verify sequence watermark bumped past 277
        seq_obj = ClubIDSequence.objects.get(year=2025, prefix="SCC")
        self.assertGreaterEqual(seq_obj.next_sequence, 278)

    def test_referral_service_resolution(self):
        """Tests referral tracking and link resolution."""
        ankith = User.objects.create(
            username="ankith",
            email="ankith@srkrcc.in",
            first_name="Ankith",
            last_name="Kumar",
            club_id="25SCC100",
        )

        resolved_user, raw_str, is_ambiguous = ReferralService.resolve_referrer("25SCC100")
        self.assertEqual(resolved_user, ankith)

        resolved_by_name, _, _ = ReferralService.resolve_referrer("Ankith")
        self.assertEqual(resolved_by_name, ankith)

        stats = ReferralService.get_referral_stats(ankith)
        self.assertEqual(stats["referred_count"], 0)

    def test_email_notification_service_security(self):
        """Tests parameter whitelisting security in email template rendering."""
        template = EmailTemplate.objects.create(
            name="welcome_test",
            subject_template="Welcome {{full_name}} to SRKRCC [{{club_id}}]",
            html_template="<p>Hello {{first_name}}, your branch is {{branch}}. Login at {{login_url}}</p>",
            allowed_parameters=["full_name", "first_name", "club_id", "branch", "login_url"],
        )

        user = User.objects.create(
            username="email_tester",
            email="tester@srkrcc.in",
            first_name="Jane",
            last_name="Doe",
            club_id="25SCC300",
            branch="CSE",
        )
        ctx = MemberEmailContext.build_for_user(user)
        subj, html, text = EmailNotificationService.render_template(template, ctx)
        self.assertIn("Jane Doe", subj)
        self.assertIn("25SCC300", subj)
        self.assertIn("Hello Jane", html)

        # Template attempting to use forbidden variable raises TemplateSecurityError
        bad_template = EmailTemplate.objects.create(
            name="forbidden_test",
            subject_template="Secret: {{password_hash}}",
            html_template="<p>{{is_superuser}}</p>",
            allowed_parameters=["password_hash"], # Not in global whitelist
        )
        with self.assertRaises(TemplateSecurityError):
            EmailNotificationService.render_template(bad_template, {"password_hash": "secret"})
