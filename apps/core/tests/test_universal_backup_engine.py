import io
from decimal import Decimal
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from apps.core.models import BackupJob, ImportAttempt, ImportAttemptStatus, RawBackupArchive, RawBackupRow
from apps.core.services.backup.backup_service import UniversalBackupService
from apps.core.services.backup.registry import BackupImporterRegistry
from apps.accounts.models import ClubIDSequence

User = get_user_model()


class UniversalBackupEngineTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="admin_backup",
            email="admin_backup@srkr.ac.in",
            first_name="Admin",
            last_name="Ingestion",
            password="StrongAdminPassword123!",
            role="ADMIN",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def test_backup_preservation_and_no_implicit_mutation(self):
        """
        Uploading a backup creates a BackupJob, computes SHA-256, but NEVER mutates User counts.
        """
        initial_user_count = User.objects.count()
        csv_content = b"Full Name,Email,Phone Number,Branch,Club ID\nAlice Sharma,alice@test.ac.in,9876543210,CSE,25SCC101\n"
        file_obj = io.BytesIO(csv_content)

        backup_job, meta = UniversalBackupService.intake_backup_file(
            file_obj=file_obj,
            filename="test_members.csv",
            user=self.admin,
        )

        self.assertIsNotNone(backup_job.id)
        self.assertEqual(backup_job.total_rows, 1)
        self.assertEqual(backup_job.headers, ["Full Name", "Email", "Phone Number", "Branch", "Club ID"])
        self.assertEqual(backup_job.suggested_domain, "USERS")
        self.assertGreaterEqual(backup_job.suggestion_confidence, Decimal("50.00"))

        # No Implicit Mutation Guarantee:
        self.assertEqual(User.objects.count(), initial_user_count)

    def test_exact_50_percent_formula_and_alias_deduplication(self):
        """
        Verifies exact formula:
        (unique canonical fields matched / total expected canonical fields) * 100
        Duplicate aliases (e.g. Email, E-mail) must NOT be counted multiple times.
        """
        importer = BackupImporterRegistry.get_importer('USERS')

        # 4 canonical fields: full_name, email, phone_number, branch out of 8 = 50.00%
        headers_50 = ["Name", "Email Address", "Phone", "Department"]
        req, conf, mapping, _ = importer.calculate_confidence(headers_50)
        self.assertTrue(req)
        self.assertEqual(conf, Decimal("50.00"))

        # Duplicate aliases: 'Email' and 'E-mail' should both map to 'email', counting as 1 match!
        headers_dupes = ["Email", "E-mail", "Phone Number"]  # 2 canonical fields out of 8 = 25.00%
        req, conf, _, _ = importer.calculate_confidence(headers_dupes)
        self.assertTrue(req)
        self.assertEqual(conf, Decimal("25.00"))
        self.assertLess(conf, Decimal("50.00"))  # Below 50% threshold

    def test_structured_member_import_and_watermark_sync(self):
        """
        Previews and commits a valid member backup, creates User account,
        and fast-forwards the ClubIDSequence watermark.
        """
        csv_content = (
            b"Full Name,Email,Phone Number,Branch,Club ID,Legacy Unknown Column\n"
            b"Monisha Talluri,monisha@test.ac.in,9848786959,CSE,25SCC277,OldNotes123\n"
        )
        backup_job, _ = UniversalBackupService.intake_backup_file(
            file_obj=io.BytesIO(csv_content),
            filename="legacy_members.csv",
            user=self.admin,
        )

        # Preview
        attempt, preview = UniversalBackupService.generate_preview(
            backup_job=backup_job,
            target_domain='USERS',
            idempotency_key="test-key-001",
        )
        self.assertEqual(attempt.valid_records, 1)
        self.assertIn("Legacy Unknown Column", attempt.unmapped_columns)

        # Verify unmapped column is preserved in ImportRow.raw_data
        row_record = attempt.rows.first()
        self.assertEqual(row_record.raw_data.get("Legacy Unknown Column"), "OldNotes123")

        # Commit
        result = UniversalBackupService.commit_import(
            attempt_id=str(attempt.id),
            user=self.admin,
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["new_users_count"], 1)

        # Verify user in database
        user = User.objects.get(email="monisha@test.ac.in")
        self.assertEqual(user.full_name, "Monisha Talluri")
        self.assertEqual(user.club_id, "25SCC277")

        # Verify ClubIDSequence watermark synchronized to 278
        seq = ClubIDSequence.objects.get(year=2025, prefix="SCC")
        self.assertEqual(seq.next_sequence, 278)

    def test_raw_vault_schemaless_archiving(self):
        """
        Unknown / arbitrary spreadsheets with zero schema matching are archived safely into Raw Vault.
        """
        csv_content = (
            b"ArbitraryA,ArbitraryB,ArbitraryC\n"
            b"Val1,Val2,Val3\n"
            b"Val4,Val5,Val6\n"
        )
        backup_job, _ = UniversalBackupService.intake_backup_file(
            file_obj=io.BytesIO(csv_content),
            filename="unknown_data.csv",
            user=self.admin,
        )

        result = UniversalBackupService.archive_directly_to_raw_vault(
            backup_job=backup_job,
            user=self.admin,
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["imported_count"], 2)

        archive = RawBackupArchive.objects.get(backup_job=backup_job)
        self.assertEqual(archive.total_rows, 2)
        self.assertEqual(archive.headers, ["ArbitraryA", "ArbitraryB", "ArbitraryC"])
        self.assertEqual(RawBackupRow.objects.filter(archive=archive).count(), 2)

    def test_security_access_control(self):
        """
        Non-admin users cannot upload or access backup endpoints.
        """
        member = User.objects.create_user(
            username="regular_member",
            email="regular_member@srkr.ac.in",
            first_name="Regular",
            last_name="Member",
            password="MemberPassword123!",
            role="MEMBER",
        )
        member_client = APIClient()
        member_client.force_authenticate(user=member)

        response = member_client.get("/api/admin/backups/")
        self.assertEqual(response.status_code, 403)
