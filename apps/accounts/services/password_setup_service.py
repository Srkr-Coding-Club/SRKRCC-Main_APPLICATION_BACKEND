import hashlib
import logging
import secrets
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from apps.accounts.models import PasswordSetupToken, PasswordStatus
from apps.core.services.email_service import EmailNotificationService, MemberEmailContext

User = get_user_model()
logger = logging.getLogger(__name__)

EMAIL_RATE_LIMIT_HOURLY = 5
IP_RATE_LIMIT_HOURLY = 20
TOKEN_EXPIRY_HOURS = 24


class PasswordSetupError(Exception):
    pass


class RateLimitExceededError(PasswordSetupError):
    pass


class InvalidSetupTokenError(PasswordSetupError):
    pass


class PasswordSetupService:
    """
    Manages the cryptographic one-time password setup lifecycle for members
    restored or imported from legacy backups (USERS domain).
    """

    @classmethod
    def _check_rate_limit(cls, email: str, request_ip: str | None) -> None:
        """
        Enforces hourly rate limiting per-email (5/hr) and per-IP (20/hr).
        """
        if email:
            email_key = f"rate_limit:setup_pwd:email:{hashlib.md5(email.lower().strip().encode()).hexdigest()}"
            email_count = cache.get(email_key, 0)
            if email_count >= EMAIL_RATE_LIMIT_HOURLY:
                raise RateLimitExceededError("Too many password setup requests for this email. Please try again later.")

        if request_ip:
            ip_key = f"rate_limit:setup_pwd:ip:{request_ip.strip()}"
            ip_count = cache.get(ip_key, 0)
            if ip_count >= IP_RATE_LIMIT_HOURLY:
                raise RateLimitExceededError("Too many password setup requests from this IP. Please try again later.")

    @classmethod
    def _increment_rate_limit(cls, email: str, request_ip: str | None) -> None:
        """
        Increments rate-limit counters with 1-hour expiration.
        """
        if email:
            email_key = f"rate_limit:setup_pwd:email:{hashlib.md5(email.lower().strip().encode()).hexdigest()}"
            cache.set(email_key, cache.get(email_key, 0) + 1, timeout=3600)

        if request_ip:
            ip_key = f"rate_limit:setup_pwd:ip:{request_ip.strip()}"
            cache.set(ip_key, cache.get(ip_key, 0) + 1, timeout=3600)

    @classmethod
    def request_setup_link(cls, email: str, request_ip: str | None = None) -> tuple[bool, str]:
        """
        Requests a one-time password setup link for an imported or eligible member.
        Strict anti-enumeration: returns identical message regardless of whether the email exists.
        """
        normalized_email = email.strip().lower() if email else ""
        if not normalized_email:
            return True, "If an eligible account exists, a password setup link has been sent."

        cls._check_rate_limit(normalized_email, request_ip)
        cls._increment_rate_limit(normalized_email, request_ip)

        user = User.objects.filter(email__iexact=normalized_email).first()

        # If user does not exist, or user is already ACTIVE with a usable password,
        # return generic non-enumerating success response without dispatching setup email.
        if not user or (user.password_status == PasswordStatus.ACTIVE and user.has_usable_password()):
            return True, "If an eligible account exists, a password setup link has been sent."

        # Generate cryptographic single-use token
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        expires_at = timezone.now() + timedelta(hours=TOKEN_EXPIRY_HOURS)

        # Commit DB token creation FIRST to ensure resilient transactional state
        with transaction.atomic():
            # Invalidate any prior unused setup tokens for this user
            PasswordSetupToken.objects.filter(user=user, is_used=False).update(
                is_used=True,
                used_at=timezone.now()
            )

            token_obj = PasswordSetupToken.objects.create(
                user=user,
                token_hash=token_hash,
                expires_at=expires_at,
                created_ip=request_ip,
            )

        # Audit log (ZERO raw token / credential logging invariant)
        logger.info(
            "PASSWORD_SETUP_REQUESTED: user_id=%s, token_id=%s, ip=%s",
            str(user.id),
            str(token_obj.id),
            request_ip or "unknown",
        )

        # Post-commit async email dispatch (Email failure does not rollback token)
        try:
            frontend_base = getattr(settings, 'FRONTEND_URL', 'http://localhost:3000').rstrip('/')
            setup_url = f"{frontend_base}/account/setup-password?token={raw_token}"

            context = MemberEmailContext.build_for_user(
                user=user,
                setup_password_url=setup_url,
            )
            full_name = context.get("full_name") or user.first_name or "Club Member"
            club_id = context.get("club_id") or user.club_id or "Assigned on Login"

            subject = "Set Up Your Password - SRKR Coding Club"
            body = (
                f"Hello {full_name},\n\n"
                f"Your account in the SRKR Coding Club portal has been prepared.\n"
                f"Your permanent Club ID is: {club_id}\n\n"
                f"Please click the secure link below within 24 hours to set up your password:\n"
                f"{setup_url}\n\n"
                f"If you did not request this link, please ignore this email.\n\n"
                f"— SRKR Coding Club Team"
            )

            EmailNotificationService.send_email(
                recipient_email=user.email,
                subject=subject,
                plain_content=body,
            )
            logger.info("PASSWORD_SETUP_EMAIL_SENT: user_id=%s", str(user.id))
        except Exception as ex:
            logger.error("PASSWORD_SETUP_EMAIL_FAILED: user_id=%s error=%s", str(user.id), str(ex))

        return True, "If an eligible account exists, a password setup link has been sent."

    @classmethod
    def verify_setup_token(cls, raw_token: str) -> dict[str, Any]:
        """
        Verifies whether a token is valid, unused, and unexpired.
        Returns user identity information for frontend display without exposing sensitive hashes.
        """
        if not raw_token or not isinstance(raw_token, str):
            return {"valid": False, "error": "Invalid token."}

        token_hash = hashlib.sha256(raw_token.strip().encode()).hexdigest()
        token = (
            PasswordSetupToken.objects
            .select_related('user')
            .filter(token_hash=token_hash)
            .first()
        )

        if not token:
            return {"valid": False, "error": "Setup link not found or invalid."}

        if token.is_used:
            logger.warning("PASSWORD_SETUP_TOKEN_REPLAYED: token_id=%s user_id=%s", str(token.id), str(token.user_id))
            return {"valid": False, "error": "This setup link has already been used."}

        if token.expires_at <= timezone.now():
            logger.info("PASSWORD_SETUP_TOKEN_EXPIRED: token_id=%s user_id=%s", str(token.id), str(token.user_id))
            return {"valid": False, "error": "This setup link has expired. Please request a new one."}

        return {
            "valid": True,
            "first_name": token.user.first_name or "Club Member",
            "club_id": token.user.club_id or "",
        }

    @classmethod
    def confirm_password_setup(cls, raw_token: str, new_password: str) -> Any:
        """
        Atomically validates token with database row lock (select_for_update),
        sets the new password, transitions status to ACTIVE, marks token used,
        and invalidates all remaining tokens for the user.
        """
        if not raw_token or not new_password:
            raise InvalidSetupTokenError("Token and new password are required.")

        if len(new_password) < 8:
            raise ValidationError("Password must be at least 8 characters in length.")

        token_hash = hashlib.sha256(raw_token.strip().encode()).hexdigest()

        with transaction.atomic():
            # Concurrency row-lock on token
            token = (
                PasswordSetupToken.objects
                .select_for_update()
                .select_related('user')
                .filter(token_hash=token_hash)
                .first()
            )

            if not token:
                raise InvalidSetupTokenError("Setup link not found or invalid.")

            if token.is_used:
                logger.warning("PASSWORD_SETUP_TOKEN_REPLAY_ATTEMPT: token_id=%s user_id=%s", str(token.id), str(token.user_id))
                raise InvalidSetupTokenError("This setup link has already been used.")

            if token.expires_at <= timezone.now():
                logger.info("PASSWORD_SETUP_TOKEN_EXPIRED_ON_CONFIRM: token_id=%s user_id=%s", str(token.id), str(token.user_id))
                raise InvalidSetupTokenError("This setup link has expired. Please request a new one.")

            user = token.user
            user.set_password(new_password)
            user.password_status = PasswordStatus.ACTIVE
            user.save(update_fields=['password', 'password_status', 'updated_at'])

            token.is_used = True
            token.used_at = timezone.now()
            token.save(update_fields=['is_used', 'used_at', 'updated_at'])

            # Invalidate all other setup tokens for this user
            PasswordSetupToken.objects.filter(user=user, is_used=False).update(
                is_used=True,
                used_at=timezone.now()
            )

        # Audit log (ZERO raw token / credential logging invariant)
        logger.info(
            "PASSWORD_SETUP_COMPLETED: user_id=%s token_id=%s",
            str(user.id),
            str(token.id),
        )
        return user

    @classmethod
    def cleanup_setup_tokens(cls, retention_days: int = 30) -> int:
        """
        Deletes used and expired tokens older than retention_days.
        """
        cutoff = timezone.now() - timedelta(days=retention_days)
        deleted_count, _ = (
            PasswordSetupToken.objects
            .filter(is_used=True, used_at__lt=cutoff)
            .delete()
        )
        expired_count, _ = (
            PasswordSetupToken.objects
            .filter(is_used=False, expires_at__lt=cutoff)
            .delete()
        )
        total_deleted = deleted_count + expired_count
        logger.info("CLEANUP_SETUP_TOKENS: deleted %d tokens", total_deleted)
        return total_deleted
