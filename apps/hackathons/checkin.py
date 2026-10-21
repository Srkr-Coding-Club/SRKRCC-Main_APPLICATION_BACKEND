"""Day-of check-in: each participant's signed QR code, scanning or manual check-in, and the live roster."""
from __future__ import annotations

from django.core import signing
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit.utils import log_audit_event

from .access import can_check_in
from .errors import HackathonError
from .models import CheckIn, Hackathon, TeamMember, TeamStatus
from rest_framework.exceptions import PermissionDenied

TOKEN_SALT = 'hackathon-check-in'


def make_token(hackathon: Hackathon, user) -> str:
    return signing.dumps({'h': hackathon.id, 'u': user.id}, salt=TOKEN_SALT, compress=True)


def _user_from_token(hackathon: Hackathon, token: str):
    try:
        payload = signing.loads(str(token or '').strip(), salt=TOKEN_SALT)
    except signing.BadSignature:
        raise HackathonError('That QR code is not valid.', 'INVALID_QR')
    if payload.get('h') != hackathon.id:
        raise HackathonError('That QR code is for a different hackathon.', 'WRONG_HACKATHON')
    return payload.get('u')


def _placed_membership(hackathon: Hackathon, user_id) -> TeamMember:
    membership = (
        TeamMember.objects.select_related('team', 'user')
        .filter(hackathon=hackathon, user_id=user_id).first()
    )
    if membership is None:
        raise HackathonError('That person is not on a team for this hackathon.', 'NOT_A_PARTICIPANT', status=404)
    if membership.team.status != TeamStatus.REGISTERED or membership.team.is_waitlisted:
        raise HackathonError(
            f"{membership.user.first_name or membership.user.email}'s team is not registered (still forming, waitlisted or removed).",
            'TEAM_NOT_REGISTERED',
        )
    return membership


class CheckInService:

    @staticmethod
    def _require_feature(hackathon: Hackathon, actor):
        if not hackathon.feature('check_in'):
            raise HackathonError('Check-in is not enabled for this hackathon.', 'FEATURE_OFF')
        if not can_check_in(hackathon, actor):
            raise PermissionDenied('You are not allowed to check participants in.')

    @staticmethod
    @transaction.atomic
    def check_in(hackathon: Hackathon, actor, *, user_id=None, token=None) -> tuple[CheckIn, bool]:
        """Idempotent: scanning twice reports ``created=False`` instead of failing."""
        CheckInService._require_feature(hackathon, actor)
        if token:
            user_id = _user_from_token(hackathon, token)
        membership = _placed_membership(hackathon, user_id)
        try:
            with transaction.atomic():
                record, created = CheckIn.objects.get_or_create(
                    hackathon=hackathon, user=membership.user,
                    defaults={'team': membership.team, 'checked_in_by': actor, 'checked_in_at': timezone.now()},
                )
        except IntegrityError:
            record, created = CheckIn.objects.get(hackathon=hackathon, user=membership.user), False
        if created:
            log_audit_event(
                actor=actor, action='Checked In Participant', target_model='Hackathon', target_id=hackathon.slug,
                details={'user': membership.user.email, 'team': membership.team.name},
            )
        return record, created

    @staticmethod
    @transaction.atomic
    def undo(hackathon: Hackathon, actor, user_id: int) -> None:
        CheckInService._require_feature(hackathon, actor)
        deleted, _ = CheckIn.objects.filter(hackathon=hackathon, user_id=user_id).delete()
        if deleted:
            log_audit_event(
                actor=actor, action='Undid Participant Check-in', target_model='Hackathon', target_id=hackathon.slug,
                details={'user_id': user_id},
            )

    @staticmethod
    def roster(hackathon: Hackathon) -> dict:
        """Everyone on a placed team, with who has arrived."""
        arrived = {c.user_id: c for c in hackathon.check_ins.all()}
        memberships = (
            hackathon.memberships.select_related('user', 'team')
            .filter(team__status=TeamStatus.REGISTERED, team__is_waitlisted=False)
            .order_by('team__name', 'role', 'user__first_name')
        )
        rows = []
        for membership in memberships:
            record = arrived.get(membership.user_id)
            name = f'{membership.user.first_name} {membership.user.last_name}'.strip() or membership.user.email
            rows.append({
                'user_id': membership.user_id, 'name': name, 'email': membership.user.email,
                'team_id': membership.team_id, 'team_name': membership.team.name, 'role': membership.role,
                'checked_in': record is not None, 'checked_in_at': record.checked_in_at if record else None,
            })
        return {'participants': rows, 'total': len(rows), 'checked_in': sum(1 for r in rows if r['checked_in'])}
