import hashlib
import secrets
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import PasswordSetupToken, PasswordStatus
from apps.accounts.services.password_setup_service import (
    PasswordSetupService,
    InvalidSetupTokenError,
    RateLimitExceededError,
)
from apps.accounts.services.user_account_service import UserAccountService
from apps.core.models import BackupJob, ImportAttempt, ImportRow
from apps.core.services.backup.user_importer import UserBackupImporter

User = get_user_model()


class PasswordSetupLifecycleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def tearDown(self):
        cache.clear()

    def test_backup_imported_new_user_has_unusable_password_and_needs_setup(self):
        """
        Credential Safety: New members created from backup must have unusable password
        and password_status = NEEDS_SETUP. Any incoming password columns are ignored.
        """
        payload = {
            "email": "fresh_member@srkr.ac.in",
            "full_name": "Fresh Student",
            "branch": "CSE",
            "password": "HackerSuppliedPassword123!",
            "password_hash": "pbkdf2_sha256$somehash",
            "pwd": "plain_password",
        }

        user, created, changed = UserAccountService.upsert_member(
            payload=payload,
            is_backup_import=True,
            source_origin="LEGACY_IMPORT",
        )

        self.assertTrue(created)
        self.assertEqual(user.password_status, PasswordStatus.NEEDS_SETUP)
        self.assertFalse(user.has_usable_password())
        # Verify incoming password values were never set
        self.assertFalse(user.check_password("HackerSuppliedPassword123!"))
        self.assertFalse(user.check_password("plain_password"))

    def test_login_interception_for_needs_setup_member(self):
        """
        When a NEEDS_SETUP member attempts to sign in, the login serializer intercepts
        and returns PASSWORD_SETUP_REQUIRED without leaking the email.
        """
        user = User.objects.create(
            username="needs_setup_user",
            email="needs_setup@srkr.ac.in",
            password_status=PasswordStatus.NEEDS_SETUP,
        )
        user.set_unusable_password()
        user.save()

        response = self.client.post(
            "/api/auth/login/",
            {"email": "needs_setup@srkr.ac.in", "password": "AnyRandomPassword123!"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertEqual(data.get("code"), "PASSWORD_SETUP_REQUIRED")
        # Anti-enumeration invariant: email must NOT be returned in payload
        self.assertNotIn("email", data)

    def test_setup_request_anti_enumeration(self):
        """
        POST /api/auth/setup-password/request/ returns identical 200 OK whether
        the email exists or not.
        """
        # 1. Non-existent email
        res_nonexistent = self.client.post(
            "/api/auth/setup-password/request/",
            {"email": "nobody_exists_here_xyz@srkr.ac.in"},
            format="json",
        )
        self.assertEqual(res_nonexistent.status_code, 200)

        # 2. Existing NEEDS_SETUP email
        User.objects.create(
            username="eligible_user",
            email="eligible@srkr.ac.in",
            password_status=PasswordStatus.NEEDS_SETUP,
        )
        res_existent = self.client.post(
            "/api/auth/setup-password/request/",
            {"email": "eligible@srkr.ac.in"},
            format="json",
        )
        self.assertEqual(res_existent.status_code, 200)

        # Responses must be identical
        self.assertEqual(res_nonexistent.json(), res_existent.json())

    def test_setup_request_rate_limiting(self):
        """
        Enforces rate limiting on setup requests (5/hr/email).
        """
        test_email = "ratelimit_target@srkr.ac.in"
        User.objects.create(
            username="ratelimit_user",
            email=test_email,
            password_status=PasswordStatus.NEEDS_SETUP,
        )

        # 5 successful requests
        for _ in range(5):
            res = self.client.post(
                "/api/auth/setup-password/request/",
                {"email": test_email},
                format="json",
            )
            self.assertEqual(res.status_code, 200)

        # 6th request triggers 429
        res_blocked = self.client.post(
            "/api/auth/setup-password/request/",
            {"email": test_email},
            format="json",
        )
        self.assertEqual(res_blocked.status_code, 429)

    def test_token_creation_and_hash_storage(self):
        """
        Verifies that only SHA-256 hashes are stored in the database, never raw tokens.
        """
        user = User.objects.create(
            username="token_user",
            email="token_user@srkr.ac.in",
            password_status=PasswordStatus.NEEDS_SETUP,
        )
        user.set_unusable_password()
        user.save()

        with patch("apps.core.services.email_service.EmailNotificationService.send_email") as mock_send:
            PasswordSetupService.request_setup_link("token_user@srkr.ac.in")
            self.assertTrue(mock_send.called)

        tokens = PasswordSetupToken.objects.filter(user=user)
        self.assertEqual(tokens.count(), 1)
        token_record = tokens.first()

        # Token hash is 64 hex characters (SHA-256)
        self.assertEqual(len(token_record.token_hash), 64)
        self.assertFalse(token_record.is_used)

    def test_token_verification_and_confirmation_lifecycle(self):
        """
        End-to-end token validation, password confirmation, status transition to ACTIVE,
        and invalidation of prior tokens.
        """
        user = User.objects.create(
            username="confirm_user",
            email="confirm@srkr.ac.in",
            password_status=PasswordStatus.NEEDS_SETUP,
        )
        user.set_unusable_password()
        user.save()

        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        token_obj = PasswordSetupToken.objects.create(
            user=user,
            token_hash=token_hash,
            expires_at=timezone.now() + timedelta(hours=24),
        )

        # 1. Verify token endpoint
        verify_res = self.client.get(f"/api/auth/setup-password/verify/?token={raw_token}")
        self.assertEqual(verify_res.status_code, 200)
        self.assertTrue(verify_res.json().get("valid"))

        # 2. Confirm password setup
        new_password = "SecureClubPassword2026!#"
        confirm_res = self.client.post(
            "/api/auth/setup-password/confirm/",
            {"token": raw_token, "password": new_password},
            format="json",
        )
        self.assertEqual(confirm_res.status_code, 200)

        # 3. Verify user is now ACTIVE and password works
        user.refresh_from_db()
        self.assertEqual(user.password_status, PasswordStatus.ACTIVE)
        self.assertTrue(user.has_usable_password())
        self.assertTrue(user.check_password(new_password))

        # 4. Token is marked used
        token_obj.refresh_from_db()
        self.assertTrue(token_obj.is_used)
        self.assertIsNotNone(token_obj.used_at)

        # 5. Replay attack fails
        replay_res = self.client.post(
            "/api/auth/setup-password/confirm/",
            {"token": raw_token, "password": "AnotherPassword123!"},
            format="json",
        )
        self.assertEqual(replay_res.status_code, 400)

    def test_expired_token_rejected(self):
        """
        Tokens past 24-hour expiration are rejected.
        """
        user = User.objects.create(
            username="expired_user",
            email="expired@srkr.ac.in",
            password_status=PasswordStatus.NEEDS_SETUP,
        )
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        PasswordSetupToken.objects.create(
            user=user,
            token_hash=token_hash,
            expires_at=timezone.now() - timedelta(hours=1),  # already expired
        )

        verify_res = self.client.get(f"/api/auth/setup-password/verify/?token={raw_token}")
        self.assertEqual(verify_res.status_code, 400)

        confirm_res = self.client.post(
            "/api/auth/setup-password/confirm/",
            {"token": raw_token, "password": "ValidPassword123!"},
            format="json",
        )
        self.assertEqual(confirm_res.status_code, 400)

    def test_critical_post_setup_reimport_regression(self):
        """
        CRITICAL REGRESSION TEST:
        1. User imported from backup -> created with NEEDS_SETUP.
        2. User establishes password via setup token -> status transitions to ACTIVE.
        3. Same backup re-imported -> password remains ACTIVE and password hash remains byte-for-byte identical!
        """
        email = "reimport_champion@srkr.ac.in"
        payload = {
            "email": email,
            "full_name": "Champion Coder",
            "branch": "CSE",
            "club_id": "25SCC999",
        }

        # Step 1: Initial import
        user, created, _ = UserAccountService.upsert_member(
            payload=payload,
            is_backup_import=True,
            source_origin="LEGACY_IMPORT",
        )
        self.assertTrue(created)
        self.assertEqual(user.password_status, PasswordStatus.NEEDS_SETUP)
        self.assertFalse(user.has_usable_password())

        # Step 2: Establish password
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        PasswordSetupToken.objects.create(
            user=user,
            token_hash=token_hash,
            expires_at=timezone.now() + timedelta(hours=24),
        )
        PasswordSetupService.confirm_password_setup(raw_token, "SuperStrongPass2026!#")

        user.refresh_from_db()
        self.assertEqual(user.password_status, PasswordStatus.ACTIVE)
        self.assertTrue(user.has_usable_password())
        established_hash = user.password

        # Step 3: Re-import same spreadsheet data
        updated_user, was_created, changed = UserAccountService.upsert_member(
            payload=payload,
            is_backup_import=True,
            source_origin="LEGACY_IMPORT",
        )
        self.assertFalse(was_created)
        self.assertEqual(updated_user.id, user.id)

        # Invariant check: password is byte-for-byte untouched and status is still ACTIVE
        self.assertEqual(updated_user.password, established_hash)
        self.assertEqual(updated_user.password_status, PasswordStatus.ACTIVE)
        self.assertTrue(updated_user.check_password("SuperStrongPass2026!#"))

    def test_cleanup_setup_tokens_command(self):
        """
        Management command cleanup_setup_tokens removes used and expired tokens older than threshold.
        """
        user = User.objects.create(
            username="cleanup_user",
            email="cleanup@srkr.ac.in",
        )
        # Old used token (>30 days ago)
        old_used = PasswordSetupToken.objects.create(
            user=user,
            token_hash=hashlib.sha256(b"old_used").hexdigest(),
            expires_at=timezone.now() - timedelta(days=35),
            is_used=True,
            used_at=timezone.now() - timedelta(days=32),
        )
        # Old expired token (>30 days ago)
        old_expired = PasswordSetupToken.objects.create(
            user=user,
            token_hash=hashlib.sha256(b"old_expired").hexdigest(),
            expires_at=timezone.now() - timedelta(days=35),
            is_used=False,
        )
        # Recent active token (<24h)
        recent_active = PasswordSetupToken.objects.create(
            user=user,
            token_hash=hashlib.sha256(b"recent_active").hexdigest(),
            expires_at=timezone.now() + timedelta(hours=20),
            is_used=False,
        )

        call_command("cleanup_setup_tokens", days=30)

        self.assertFalse(PasswordSetupToken.objects.filter(id=old_used.id).exists())
        self.assertFalse(PasswordSetupToken.objects.filter(id=old_expired.id).exists())
        self.assertTrue(PasswordSetupToken.objects.filter(id=recent_active.id).exists())
