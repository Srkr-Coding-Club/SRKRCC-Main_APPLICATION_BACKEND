"""
Email verification.

Anyone can sign up with any address. Nothing is sent at sign-up: a person clicks "Verify email" when they need to, and
receives a one-time link (the same pattern as a password reset). What a verified address unlocks and what an unverified
one loses is decided in ONE place (``gate_decision`` below), driven by ``settings.EMAIL_VERIFICATION_MODE``:

    off       nothing happens
    banner    people see their status and can verify; nothing is restricted
    features  private features need a verified address; staff-level accounts need one for everything
    block     as above, and an account still unverified after its deadline is locked out of the private app
              (public pages and the verify screen stay reachable)

No account is exempt: admins and club leads verify like everyone else.
"""
from __future__ import annotations

import hashlib
import logging
import math
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import (
    EmailVerificationSuppression,
    EmailVerificationToken,
    SendStatus,
    User,
    VerificationPurpose,
    VerificationStatus,
)
from apps.accounts.validators import EMAIL_MAX_LENGTH, EMAIL_REGEX, normalize_email
from apps.audit.utils import log_audit_event
from apps.core.services.email_service import EmailNotificationService

logger = logging.getLogger(__name__)

MODES = ('off', 'banner', 'features', 'block')
REQUIRED_CODE = 'EMAIL_VERIFICATION_REQUIRED'

COOLDOWN_SECONDS = 60
FAILED_SEND_COOLDOWN_SECONDS = 15
PER_ACCOUNT_PER_HOUR = 5
PER_ADDRESS_PER_DAY = 6


class VerificationError(Exception):
    """A refusal the API turns into a clear message. ``status`` is the HTTP status, ``extra`` goes into the body."""

    def __init__(self, code: str, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.code, self.message, self.status, self.extra = code, message, status, extra


# ---------------------------------------------------------------------------
# Settings and state
# ---------------------------------------------------------------------------

def mode() -> str:
    value = str(getattr(settings, 'EMAIL_VERIFICATION_MODE', 'off')).strip().lower()
    return value if value in MODES else 'off'


def mode_at_least(level: str) -> bool:
    return MODES.index(mode()) >= MODES.index(level)


def is_verified(user) -> bool:
    return bool(getattr(user, 'email_verified_at', None))


def deadline(user):
    """When this account is blocked unless verified. Falls back to the sign-up date plus the grace period."""
    if getattr(user, 'email_verify_deadline', None):
        return user.email_verify_deadline
    created = getattr(user, 'created_at', None) or getattr(user, 'date_joined', None) or timezone.now()
    return created + timedelta(days=getattr(settings, 'EMAIL_VERIFY_GRACE_DAYS', 7))


def is_past_deadline(user, now=None) -> bool:
    return not is_verified(user) and (now or timezone.now()) >= deadline(user)


def days_left(user, now=None):
    if is_verified(user):
        return None
    seconds = (deadline(user) - (now or timezone.now())).total_seconds()
    return max(0, math.ceil(seconds / 86400))


def mask_email(address: str) -> str:
    local, _, domain = (address or '').partition('@')
    if not domain:
        return '***'
    return f"{local[:1]}{'*' * max(2, min(len(local) - 1, 6))}@{domain}"


def pending_change(user):
    return (
        EmailVerificationToken.objects
        .filter(user=user, purpose=VerificationPurpose.CHANGE, status=VerificationStatus.PENDING, expires_at__gt=timezone.now())
        .order_by('-created_at').first()
    )


def state(user) -> dict:
    """What the website needs to draw the banner, the dialog and the blocked screen."""
    verified = is_verified(user)
    blocked = mode_at_least('block') and is_past_deadline(user)
    change = pending_change(user)
    return {
        'mode': mode(),
        'verified': verified,
        'verified_at': user.email_verified_at.isoformat() if verified else None,
        'days_left': days_left(user),
        'deadline': None if verified else deadline(user).isoformat(),
        'blocked': blocked,
        'email_masked': mask_email(user.email),
        'pending_email_masked': mask_email(change.email) if change else None,
    }


# ---------------------------------------------------------------------------
# The gate: one decision for every request (called from apps/core/verification_gate.py)
# ---------------------------------------------------------------------------

@dataclass
class GateDecision:
    allowed: bool
    reason: str = ''   # 'feature' (this feature needs a verified address), 'staff', or 'deadline' (the account is blocked)
    message: str = ''


_STAFF_MESSAGE = 'Verify your email address to use the admin tools.'
_FEATURE_MESSAGE = 'Verify your email address to use this feature.'
_BLOCKED_MESSAGE = 'Your verification period has ended. Verify your email address to continue.'


def _is_staff_like(user) -> bool:
    from apps.core.permissions import _is_admin_or_club_lead
    return bool(_is_admin_or_club_lead(user))


def gate_decision(user, *, method: str, is_public: bool, exempt: bool, reads_need_verified: bool, writes_ok_in_grace: bool = False) -> GateDecision:
    """
    May this signed-in person use this endpoint right now?

    ``is_public``           the endpoint is open to everyone (always allowed)
    ``exempt``              needed to verify or leave: profile read, sign out, the verification endpoints
    ``reads_need_verified`` a read-only endpoint that still counts as a private feature (a certificate, an attendance badge)
    ``writes_ok_in_grace``  writing here is allowed while unverified, until the deadline (editing your own profile)
    """
    if mode() == 'off' or not getattr(user, 'is_authenticated', False) or is_verified(user) or exempt or is_public:
        return GateDecision(True)
    if mode_at_least('block') and is_past_deadline(user):
        return GateDecision(False, 'deadline', _BLOCKED_MESSAGE)
    if mode_at_least('features'):
        if _is_staff_like(user):
            return GateDecision(False, 'staff', _STAFF_MESSAGE)
        if (method.upper() not in ('GET', 'HEAD', 'OPTIONS') and not writes_ok_in_grace) or reads_need_verified:
            return GateDecision(False, 'feature', _FEATURE_MESSAGE)
    return GateDecision(True)


def needs_verified_account(user) -> bool:
    """For code that matches people by email: only verified accounts may be matched once verification is in force."""
    return mode_at_least('features') and not is_verified(user)


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _address_key(address: str) -> str:
    return hashlib.sha256(address.strip().lower().encode()).hexdigest()


def _suppressed(address: str) -> bool:
    return EmailVerificationSuppression.objects.filter(email=address.strip().lower(), until__gt=timezone.now()).exists()


def _check_limits(user, address: str) -> None:
    if _suppressed(address):
        raise VerificationError('ADDRESS_SUPPRESSED', 'This address asked not to receive verification emails, so none can be sent to it right now.', 400)
    if cache.get(f'email_verify:hour:{user.pk}', 0) >= PER_ACCOUNT_PER_HOUR:
        raise VerificationError('TOO_MANY_REQUESTS', 'Too many verification emails were requested. Please try again in an hour.', 429, retry_after=3600)
    if cache.get(f'email_verify:addr:{_address_key(address)}', 0) >= PER_ADDRESS_PER_DAY:
        raise VerificationError('ADDRESS_LIMIT', 'This address has received the most verification emails allowed for today. Please try again tomorrow.', 429, retry_after=86400)
    # Claimed last and atomically (add fails if the key exists): two taps at the same instant send one email, not two.
    if not cache.add(f'email_verify:cooldown:{user.pk}', 1, COOLDOWN_SECONDS):
        raise VerificationError('COOLDOWN', f'A link was just sent. You can ask for another in about {COOLDOWN_SECONDS} seconds.', 429, retry_after=COOLDOWN_SECONDS)


def _count_send(user, address: str, delivered: bool) -> None:
    cache.set(f'email_verify:cooldown:{user.pk}', 1, COOLDOWN_SECONDS if delivered else FAILED_SEND_COOLDOWN_SECONDS)
    if delivered:
        hour_key = f'email_verify:hour:{user.pk}'
        cache.set(hour_key, cache.get(hour_key, 0) + 1, 3600)
        addr_key = f'email_verify:addr:{_address_key(address)}'
        cache.set(addr_key, cache.get(addr_key, 0) + 1, 86400)


def _frontend() -> str:
    return getattr(settings, 'FRONTEND_URL', 'http://localhost:3000').rstrip('/')


def _compose(user, token_row: EmailVerificationToken, raw: str, raw_dispute: str) -> tuple[str, str, str]:
    base = _frontend()
    name = user.first_name or 'there'
    hours = getattr(settings, 'EMAIL_VERIFY_LINK_HOURS', 24)
    verify_url = f"{base}/account/verify-email?token={raw}"
    dispute_url = f"{base}/account/verify-email/not-me?token={raw_dispute}"
    changing = token_row.purpose == VerificationPurpose.CHANGE
    subject = 'Confirm your new email address - SRKR Coding Club' if changing else 'Verify your email address - SRKR Coding Club'
    intro = (
        'Someone asked to use this address for an SRKR Coding Club account.' if changing
        else 'Please confirm that this is your email address for your SRKR Coding Club account.'
    )
    text = (
        f"Hello {name},\n\n{intro}\n\n"
        f"Open this link within {hours} hours to confirm:\n{verify_url}\n\n"
        f"The link works once. You will be asked to press a button on that page to finish.\n\n"
        f"Did not ask for this? You can ignore this email. If somebody used your address without you knowing, "
        f"open the link below and press \"This wasn't me\":\n{dispute_url}\n\n"
        f"SRKR Coding Club Team"
    )
    html = (
        f"<p>Hello {name},</p><p>{intro}</p>"
        f"<p><a href=\"{verify_url}\">Confirm my email address</a><br>"
        f"The link works once and expires in {hours} hours. You will press a button on that page to finish.</p>"
        f"<p style=\"color:#555\">Did not ask for this? You can ignore this email. If somebody used your address without "
        f"you knowing, <a href=\"{dispute_url}\">tell us it wasn't you</a>.</p><p>SRKR Coding Club Team</p>"
    )
    return subject, text, html


def _issue(user, address: str, purpose: str, ip: str | None) -> dict:
    """Create the link, send it, and record whether the send worked. Never raises for a failed send."""
    raw, raw_dispute = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    now = timezone.now()
    with transaction.atomic():
        EmailVerificationToken.objects.filter(user=user, purpose=purpose, status=VerificationStatus.PENDING).update(status=VerificationStatus.SUPERSEDED)
        row = EmailVerificationToken.objects.create(
            user=user, purpose=purpose, email=address.lower(), token_hash=_hash(raw), dispute_hash=_hash(raw_dispute),
            expires_at=now + timedelta(hours=getattr(settings, 'EMAIL_VERIFY_LINK_HOURS', 24)),
            dispute_expires_at=now + timedelta(days=getattr(settings, 'EMAIL_VERIFY_DISPUTE_DAYS', 14)),
            created_ip=ip or None,
        )
    subject, text, html = _compose(user, row, raw, raw_dispute)
    try:
        delivered = bool(EmailNotificationService.send_email(recipient_email=address, subject=subject, plain_content=text, html_content=html))
    except Exception:  # noqa: BLE001 - a provider problem must not break the request
        logger.exception('EMAIL_VERIFY_SEND_ERROR user_id=%s', user.pk)
        delivered = False
    if not delivered:
        EmailVerificationToken.objects.filter(pk=row.pk).update(send_status=SendStatus.FAILED, send_error='The mail provider did not accept the message.')
        logger.error('EMAIL_VERIFY_SEND_FAILED user_id=%s purpose=%s', user.pk, purpose)
    _count_send(user, address, delivered)
    return {'sent': delivered, 'email_masked': mask_email(address), 'expires_in_hours': getattr(settings, 'EMAIL_VERIFY_LINK_HOURS', 24)}


def request_link(user, ip: str | None = None) -> dict:
    """The "Verify email" button."""
    if is_verified(user):
        return {'already_verified': True, 'sent': False}
    _check_limits(user, user.email)
    return _issue(user, user.email, VerificationPurpose.VERIFY, ip)


def request_email_change(user, new_email: str, password: str, ip: str | None = None) -> dict:
    """Move the account to another address. The new address only takes effect once its link is confirmed."""
    if not user.check_password(password or ''):
        raise VerificationError('WRONG_PASSWORD', 'That password is not correct.', 400)
    address = normalize_email(new_email or '')
    if not address or len(address) > EMAIL_MAX_LENGTH or not EMAIL_REGEX.match(address):
        raise VerificationError('INVALID_EMAIL', 'Enter a valid email address, for example you@example.com.', 400)
    if address.lower() == (user.email or '').lower():
        raise VerificationError('SAME_EMAIL', 'That is already your email address.', 400)
    if User.objects.filter(email__iexact=address).exclude(pk=user.pk).exists():
        raise VerificationError('EMAIL_TAKEN', 'An account with this email already exists.', 400)
    _check_limits(user, address)
    return _issue(user, address, VerificationPurpose.CHANGE, ip)


# ---------------------------------------------------------------------------
# Using the links
# ---------------------------------------------------------------------------

_USED_MESSAGE = 'This link has already been used.'
_EXPIRED_MESSAGE = 'This link has expired or was replaced by a newer one. Request a new one from your account.'
_INVALID_MESSAGE = 'This link is not valid.'


def _lookup(raw: str, field: str) -> EmailVerificationToken | None:
    if not raw or not isinstance(raw, str) or len(raw) > 200:
        return None
    return EmailVerificationToken.objects.select_related('user').filter(**{field: _hash(raw.strip())}).first()


def inspect_token(raw: str) -> dict:
    """Read-only look for the confirmation page (shows the address before the person presses the button)."""
    row = _lookup(raw, 'token_hash')
    if row is None or not row.user.is_active:
        raise VerificationError('INVALID_LINK', _INVALID_MESSAGE, 400)
    if row.status == VerificationStatus.USED:
        return {'valid': False, 'code': 'ALREADY_USED', 'already_verified': is_verified(row.user), 'email_masked': mask_email(row.email), 'purpose': row.purpose}
    if row.status != VerificationStatus.PENDING or row.expires_at <= timezone.now():
        raise VerificationError('LINK_EXPIRED', _EXPIRED_MESSAGE, 400)
    return {'valid': True, 'email_masked': mask_email(row.email), 'purpose': row.purpose, 'first_name': row.user.first_name or ''}


def mark_verified(user, via: str = 'link') -> bool:
    """Record that the owner of ``user.email`` has proved it. Also used by a completed password reset or setup."""
    if is_verified(user):
        return False
    user.email_verified_at = timezone.now()
    user.save(update_fields=['email_verified_at', 'updated_at'])
    log_audit_event(actor=None, action='Email Verified', target_model='User', target_id=user.pk, details={'via': via})
    return True


def confirm(raw: str) -> dict:
    """The button on the confirmation page. Safe to call twice and from two tabs at once."""
    token_hash = _hash((raw or '').strip()) if isinstance(raw, str) and raw and len(raw) <= 200 else None
    if token_hash is None:
        raise VerificationError('INVALID_LINK', _INVALID_MESSAGE, 400)
    with transaction.atomic():
        row = EmailVerificationToken.objects.select_for_update().select_related('user').filter(token_hash=token_hash).first()
        if row is None or not row.user.is_active:
            raise VerificationError('INVALID_LINK', _INVALID_MESSAGE, 400)
        user = row.user
        if row.status == VerificationStatus.USED:
            # Pressing the button twice, or opening the link on a second device: it already worked.
            return {'verified': True, 'already': True, 'email_masked': mask_email(user.email), 'purpose': row.purpose}
        if row.status != VerificationStatus.PENDING or row.expires_at <= timezone.now():
            raise VerificationError('LINK_EXPIRED', _EXPIRED_MESSAGE, 400)

        now = timezone.now()
        if row.purpose == VerificationPurpose.VERIFY:
            if (user.email or '').lower() != row.email.lower():
                raise VerificationError('EMAIL_CHANGED', 'The address on this account has changed since this link was sent. Request a new link.', 400)
            user.email_verified_at = user.email_verified_at or now
            user.save(update_fields=['email_verified_at', 'updated_at'])
        else:
            if User.objects.filter(email__iexact=row.email).exclude(pk=user.pk).exists():
                raise VerificationError('EMAIL_TAKEN', 'Someone else now uses this address, so it cannot be added to this account.', 400)
            user.email = row.email
            user.email_verified_at = now
            user.save(update_fields=['email', 'email_verified_at', 'updated_at'])
            EmailVerificationToken.objects.filter(user=user, status=VerificationStatus.PENDING).exclude(pk=row.pk).update(status=VerificationStatus.SUPERSEDED)
        row.status, row.used_at = VerificationStatus.USED, now
        row.save(update_fields=['status', 'used_at', 'updated_at'])
    log_audit_event(actor=None, action='Email Verified' if row.purpose == VerificationPurpose.VERIFY else 'Email Changed', target_model='User', target_id=user.pk, details={'via': 'link'})
    return {'verified': True, 'already': False, 'email_masked': mask_email(user.email), 'purpose': row.purpose}


# ---------------------------------------------------------------------------
# "This wasn't me"
# ---------------------------------------------------------------------------

# Things that hang off an account but say nothing about whether a person has been using it.
_NOT_ACTIVITY = {
    'emailverificationtoken', 'passwordsetuptoken', 'outstandingtoken', 'blacklistedtoken', 'auditlog', 'emaildelivery',
    'emailjob', 'notification', 'usernotification', 'importjob', 'logentry',
}


def has_activity(user) -> bool:
    """True if the account has ever been used for anything: signed in, answered a form, joined a team, and so on."""
    if user.last_login is not None:
        return True
    for relation in User._meta.related_objects:
        if relation.related_model._meta.model_name in _NOT_ACTIVITY:
            continue
        try:
            accessor = relation.get_accessor_name()
            related = getattr(user, accessor)
            exists = related.exists() if hasattr(related, 'exists') else related is not None
        except ObjectDoesNotExist:
            continue  # a one-to-one that was never created
        except Exception:  # noqa: BLE001 - an unreadable relation counts as "in use" (the safe side)
            return True
        if exists:
            return True
    return False


def _revoke_sessions(user) -> None:
    from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
    for outstanding in OutstandingToken.objects.filter(user=user):
        BlacklistedToken.objects.get_or_create(token=outstanding)


def _suppress(address: str, reason: str) -> None:
    until = timezone.now() + timedelta(days=getattr(settings, 'EMAIL_VERIFY_SUPPRESS_DAYS', 30))
    EmailVerificationSuppression.objects.update_or_create(email=address.strip().lower(), defaults={'until': until, 'reason': reason[:100]})


def dispute(raw: str) -> dict:
    """
    The address owner says an account was made with their address (or a change to it was asked for) without them.
    * A sign-up with no history is removed, so the real owner can register; one with history is switched off for an
      admin to look at.
    * A change request only cancels that request.
    Either way, verification mail to the address stops for a while so it cannot be used to flood their inbox.
    """
    row = _lookup(raw, 'dispute_hash')
    if row is None:
        raise VerificationError('INVALID_LINK', _INVALID_MESSAGE, 400)
    if row.status == VerificationStatus.DISPUTED:
        return {'done': True, 'already': True, 'removed': False}
    if row.dispute_expires_at <= timezone.now():
        raise VerificationError('LINK_EXPIRED', 'This link has expired. Contact the club if someone is using your address.', 400)

    address, user = row.email.lower(), row.user
    with transaction.atomic():
        row.status = VerificationStatus.DISPUTED
        row.save(update_fields=['status', 'updated_at'])
        _suppress(address, f'disputed ({row.purpose.lower()})')
        if row.purpose == VerificationPurpose.CHANGE:
            log_audit_event(actor=None, action='Email Change Disputed', target_model='User', target_id=user.pk, details={'address_hash': _address_key(address)[:12]})
            return {'done': True, 'already': False, 'removed': False}

        removed = False
        user.is_active = False
        user.email_disputed_at = timezone.now()
        user.save(update_fields=['is_active', 'email_disputed_at', 'updated_at'])
        EmailVerificationToken.objects.filter(user=user, status=VerificationStatus.PENDING).update(status=VerificationStatus.SUPERSEDED)
        _revoke_sessions(user)
        log_audit_event(actor=None, action='Account Disabled: Email Owner Disputed', target_model='User', target_id=user.pk, details={'address_hash': _address_key(address)[:12]})
        if not has_activity(user):
            user.delete()
            removed = True
    return {'done': True, 'already': False, 'removed': removed}


# ---------------------------------------------------------------------------
# Deadlines (used by commands and by the admin screens)
# ---------------------------------------------------------------------------

def schedule_deadlines(*, start_days: int | None = None, batch: int | None = None, only_unscheduled: bool = False) -> int:
    """Give every unverified account a deadline, in daily waves (most recently active first). Returns how many were set."""
    from django.db.models import F
    start = settings.EMAIL_VERIFY_EXISTING_START_DAYS if start_days is None else start_days
    size = max(1, settings.EMAIL_VERIFY_EXISTING_BATCH if batch is None else batch)
    qs = User.objects.filter(email_verified_at__isnull=True, is_active=True)
    if only_unscheduled:
        qs = qs.filter(email_verify_deadline__isnull=True)
    ids = list(qs.order_by(F('last_login').desc(nulls_last=True), 'id').values_list('id', flat=True))
    now = timezone.now()
    for index, user_id in enumerate(ids):
        User.objects.filter(pk=user_id).update(email_verify_deadline=now + timedelta(days=start + index // size))
    return len(ids)
