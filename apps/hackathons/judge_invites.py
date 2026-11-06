"""
Inviting judges. An organizer gives a name and an email; nobody ever types, sees or emails a password.

* A person who already has an account is added as a judge of this hackathon and told by mail and in the app.
* Someone new gets an account with no usable password (``NEEDS_SETUP``) and a one-time link to choose their own, the same
  mechanism members restored from a backup use. The account carries no club role: its only power is the judge permission
  on this hackathon (the staff row), so it can score the teams it is given and nothing else.
* Opening the link and choosing a password also proves the address is theirs (see ``PasswordSetupService``).
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import PasswordStatus, UserRole
from apps.accounts.services.password_setup_service import PasswordSetupService
from apps.audit.utils import log_audit_event
from apps.core.services.email_service import EmailNotificationService

from .errors import HackathonError
from .judging import StaffService
from .models import Hackathon, JudgeInvite, JudgeInviteState, StaffRole

logger = logging.getLogger(__name__)

LINK_VALID_HOURS = 7 * 24
MAX_BATCH = 50
MAX_RESENDS = 5
RESEND_COOLDOWN = timedelta(seconds=60)
MAX_NAME = 100


def _clean_email(raw) -> str:
    email = str(raw or '').strip().lower()
    try:
        validate_email(email)
    except DjangoValidationError:
        raise HackathonError('Give a valid email address.', 'INVALID', field='email')
    if len(email) > 254:
        raise HackathonError('That email address is too long.', 'INVALID', field='email')
    return email


def _clean_name(raw) -> str:
    name = ' '.join(str(raw or '').split())
    if not name:
        raise HackathonError('Give the judge\'s name.', 'INVALID', field='name')
    if len(name) > MAX_NAME:
        raise HackathonError(f'Keep the name under {MAX_NAME} characters.', 'INVALID', field='name')
    return name


def _unique_username(email: str) -> str:
    User = get_user_model()
    base = re.sub(r'[^a-z0-9._-]', '', email.split('@')[0]) or 'judge'
    candidate, counter = base[:140], 1
    while User.objects.filter(username=candidate).exists():
        counter += 1
        candidate = f'{base[:130]}{counter}'
    return candidate


def _setup_url(raw_token: str) -> str:
    return f"{getattr(settings, 'FRONTEND_URL', 'http://localhost:3000').rstrip('/')}/account/setup-password?token={raw_token}"


def _judging_url(hackathon: Hackathon) -> str:
    return f"{getattr(settings, 'FRONTEND_URL', 'http://localhost:3000').rstrip('/')}/hackathons/{hackathon.slug}/judging"


def _send(hackathon: Hackathon, invite: JudgeInvite, *, raw_token: str | None) -> bool:
    """Email the invitation. A failed send never undoes the invite: the organizer sees it and can resend."""
    who = invite.user.first_name or 'there'
    lines = [f'Hello {who},', '', f'You have been invited to judge "{hackathon.title}" at SRKR Coding Club.', '']
    if raw_token:
        lines += [
            'Choose your password with this secure link. It works once and expires on '
            f'{timezone.localtime(invite.link_expires_at).strftime("%d %b %Y, %I:%M %p")} IST:',
            _setup_url(raw_token), '',
            'After that, sign in and open your judging page:',
        ]
    else:
        lines += ['Sign in with your existing account and open your judging page:']
    lines += [_judging_url(hackathon), '', 'If you were not expecting this, you can ignore this email.', '', 'SRKR Coding Club Team']
    try:
        delivered = EmailNotificationService.send_email(
            recipient_email=invite.email, subject=f'You are invited to judge {hackathon.title}', plain_content='\n'.join(lines),
        )
    except Exception:  # noqa: BLE001 - the provider is outside our control
        logger.error('JUDGE_INVITE_EMAIL_FAILED: invite_id=%s', invite.pk, exc_info=True)
        return False
    if not delivered:
        logger.error('JUDGE_INVITE_EMAIL_FAILED: invite_id=%s provider rejected the message', invite.pk)
    return bool(delivered)


def serialize(invite: JudgeInvite) -> dict:
    user = invite.user
    return {
        'id': invite.id, 'user_id': user.id, 'email': invite.email, 'name': f'{user.first_name} {user.last_name}'.strip() or invite.email,
        'state': invite.state, 'account_created': invite.account_created, 'last_sent_at': invite.last_sent_at,
        'resend_count': invite.resend_count, 'link_expires_at': invite.link_expires_at, 'accepted_at': invite.accepted_at,
        'invited_at': invite.created_at,
    }


class JudgeInviteService:

    @staticmethod
    def list(hackathon: Hackathon) -> list[dict]:
        return [serialize(invite) for invite in hackathon.judge_invites.select_related('user')]

    @staticmethod
    def invite(hackathon: Hackathon, actor, name, email, permissions=None) -> dict:
        """Invite one judge. Returns the invite and whether the email went out."""
        name, email = _clean_name(name), _clean_email(email)
        User = get_user_model()
        existing_invite = hackathon.judge_invites.select_related('user').filter(email__iexact=email).first()
        if existing_invite is not None and not existing_invite.revoked_at:
            raise HackathonError('That person is already invited. Use "Resend" to send the link again.', 'ALREADY_INVITED', field='email')

        raw_token = None
        with transaction.atomic():
            user = User.objects.filter(email__iexact=email).first()
            created = user is None
            if created:
                first, _, last = name.partition(' ')
                user = User(
                    username=_unique_username(email), email=email, first_name=first[:150], last_name=last[:150],
                    role=UserRole.NON_AFFILIATE, created_from='ADMIN', password_status=PasswordStatus.NEEDS_SETUP,
                )
                user.set_unusable_password()
                user.save()
            # Participants cannot judge, and the actor may only hand out what they hold: both checked here.
            staff = StaffService.add(hackathon, actor, email, StaffRole.JUDGE, permissions)
            needs_link = created or user.password_status == PasswordStatus.NEEDS_SETUP or not user.has_usable_password()
            invite = existing_invite or JudgeInvite(hackathon=hackathon, user=user, email=email)
            invite.user, invite.email, invite.invited_by = user, email, actor
            invite.account_created = invite.account_created or created
            invite.revoked_at = None
            invite.resend_count = 0
            invite.accepted_at = None if needs_link else timezone.now()
            if needs_link:
                token, raw_token = PasswordSetupService.issue_token(user, expiry_hours=LINK_VALID_HOURS)
                invite.link_expires_at = token.expires_at
            else:
                invite.link_expires_at = None
            invite.last_sent_at = timezone.now()
            invite.save()
            log_audit_event(
                actor=actor, action='Invited Hackathon Judge', target_model='Hackathon', target_id=hackathon.slug,
                details={'email': email, 'account_created': created, 'permissions': list(staff.permissions or [])},
            )
        sent = _send(hackathon, invite, raw_token=raw_token)
        return {**serialize(invite), 'email_sent': sent}

    @staticmethod
    def invite_many(hackathon: Hackathon, actor, rows) -> list[dict]:
        """Invite a pasted list. Each row succeeds or fails on its own, so one typo never blocks the rest."""
        if not isinstance(rows, list) or not rows:
            raise HackathonError('Send at least one judge.', 'INVALID')
        if len(rows) > MAX_BATCH:
            raise HackathonError(f'Invite at most {MAX_BATCH} judges at a time.', 'INVALID')
        results = []
        for row in rows:
            row = row if isinstance(row, dict) else {}
            try:
                results.append({'ok': True, **JudgeInviteService.invite(hackathon, actor, row.get('name'), row.get('email'))})
            except HackathonError as exc:
                results.append({'ok': False, 'email': str(row.get('email') or ''), 'code': exc.detail.get('code'), 'detail': exc.detail.get('detail')})
        return results

    @staticmethod
    def _get(hackathon: Hackathon, invite_id) -> JudgeInvite:
        invite = hackathon.judge_invites.select_related('user').filter(pk=invite_id).first()
        if invite is None:
            raise HackathonError('That invitation does not exist.', 'NOT_FOUND', status=404)
        return invite

    @staticmethod
    def resend(hackathon: Hackathon, actor, invite_id) -> dict:
        """A fresh link (the old one stops working) for someone who has not set a password yet."""
        invite = JudgeInviteService._get(hackathon, invite_id)
        if invite.revoked_at:
            raise HackathonError('That invitation was revoked. Invite the person again.', 'REVOKED')
        user = invite.user
        if invite.state == JudgeInviteState.ACTIVE:
            raise HackathonError('This judge has already set up their account.', 'ALREADY_ACTIVE')
        if invite.resend_count >= MAX_RESENDS:
            raise HackathonError(f'This invitation was already sent {MAX_RESENDS} times. Ask the person to check their spam folder.', 'LIMIT_REACHED')
        if invite.last_sent_at and timezone.now() - invite.last_sent_at < RESEND_COOLDOWN:
            raise HackathonError('Wait a minute before sending it again.', 'TOO_SOON')
        token, raw_token = PasswordSetupService.issue_token(user, expiry_hours=LINK_VALID_HOURS)
        invite.link_expires_at, invite.last_sent_at = token.expires_at, timezone.now()
        invite.resend_count += 1
        invite.save(update_fields=['link_expires_at', 'last_sent_at', 'resend_count', 'updated_at'])
        log_audit_event(
            actor=actor, action='Resent Judge Invite', target_model='Hackathon', target_id=hackathon.slug,
            details={'email': invite.email, 'resend_count': invite.resend_count},
        )
        sent = _send(hackathon, invite, raw_token=raw_token)
        return {**serialize(invite), 'email_sent': sent}

    @staticmethod
    @transaction.atomic
    def revoke(hackathon: Hackathon, actor, invite_id) -> dict:
        """Take the judge off this hackathon and kill any unused link. Their account stays; it holds no other power."""
        invite = JudgeInviteService._get(hackathon, invite_id)
        if invite.revoked_at:
            return serialize(invite)
        staff = hackathon.staff.filter(user=invite.user, role=StaffRole.JUDGE).first()
        if staff is not None:
            StaffService.remove(hackathon, actor, staff.id)
        if not invite.accepted_at:
            PasswordSetupService.revoke_tokens(invite.user)
        invite.revoked_at = timezone.now()
        invite.save(update_fields=['revoked_at', 'updated_at'])
        log_audit_event(
            actor=actor, action='Revoked Judge Invite', target_model='Hackathon', target_id=hackathon.slug, details={'email': invite.email},
        )
        return serialize(invite)
