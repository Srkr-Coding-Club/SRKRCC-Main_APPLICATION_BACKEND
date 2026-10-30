"""Admin user directory endpoints: the paged, filterable list, its numbers and the verification actions."""
from datetime import timedelta

from django.utils import timezone
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts import user_directory as directory
from apps.accounts.models import User
from apps.accounts.services import email_verification as verification
from apps.accounts.services.email_verification import VerificationError
from apps.audit.utils import log_audit_event
from apps.core.client_ip import get_client_ip
from apps.core.pagination import StandardPagination
from apps.core.permissions import IsAdmin, IsAdminOrClubLead

MAX_EXTENSION_DAYS = 60


class DirectoryPagination(StandardPagination):
    page_size = 25
    max_page_size = 100


def _members_only(request) -> bool:
    return request.query_params.get('scope') == 'members'


class UserDirectoryView(generics.ListAPIView):
    """GET /api/auth/users/directory/ - everybody (or `scope=members`: only Club ID holders), filtered, ordered and paged."""
    serializer_class = directory.DirectoryUserSerializer
    permission_classes = [IsAdminOrClubLead]
    pagination_class = DirectoryPagination

    def get_queryset(self):
        base = directory.scoped(User.objects.all(), members_only=_members_only(self.request))
        return directory.apply_filters(base, self.request.query_params)


class UserDirectorySummaryView(APIView):
    """GET /api/auth/users/directory/summary/ - the counts behind the tiles (whole set, not the current filter)."""
    permission_classes = [IsAdminOrClubLead]

    def get(self, request):
        return Response(directory.summary(directory.scoped(User.objects.all(), members_only=_members_only(request))))


MAX_RECIPIENTS = 5000


class UserDirectoryRecipientsView(APIView):
    """GET /api/auth/users/directory/recipients/ - id and email of everybody the current filters match (for exports and broadcasts)."""
    permission_classes = [IsAdminOrClubLead]

    def get(self, request):
        base = directory.scoped(User.objects.all(), members_only=_members_only(request))
        matches = directory.apply_filters(base, request.query_params)
        rows = list(matches.values('id', 'email')[:MAX_RECIPIENTS + 1])
        return Response({'recipients': rows[:MAX_RECIPIENTS], 'truncated': len(rows) > MAX_RECIPIENTS, 'limit': MAX_RECIPIENTS})


def _target(pk):
    return User.objects.filter(pk=pk).first()


def _not_found():
    return Response({'detail': 'No such user.', 'code': 'NOT_FOUND'}, status=status.HTTP_404_NOT_FOUND)


class ExtendVerificationDeadlineView(APIView):
    """POST /api/auth/users/{id}/verification/extend/ {days} - give an unverified person more time (audited)."""
    permission_classes = [IsAdmin]

    def post(self, request, pk):
        target = _target(pk)
        if target is None:
            return _not_found()
        try:
            days = int(request.data.get('days'))
        except (TypeError, ValueError):
            days = 0
        if not 1 <= days <= MAX_EXTENSION_DAYS:
            return Response({'detail': f'Choose between 1 and {MAX_EXTENSION_DAYS} days.', 'code': 'INVALID'}, status=status.HTTP_400_BAD_REQUEST)
        if verification.is_verified(target):
            return Response({'detail': 'This email address is already verified.', 'code': 'ALREADY_VERIFIED'}, status=status.HTTP_409_CONFLICT)
        start = max(timezone.now(), verification.deadline(target))
        target.email_verify_deadline = start + timedelta(days=days)
        target.save(update_fields=['email_verify_deadline', 'updated_at'])
        log_audit_event(actor=request.user, action='Email Verification Deadline Extended', target_model='User', target_id=target.pk, details={'days': days})
        return Response({'deadline': target.email_verify_deadline})


class ResendVerificationView(APIView):
    """POST /api/auth/users/{id}/verification/resend/ - send the person the verification link again (audited, rate-limited)."""
    permission_classes = [IsAdmin]

    def post(self, request, pk):
        target = _target(pk)
        if target is None:
            return _not_found()
        try:
            result = verification.request_link(target, ip=get_client_ip(request))
        except VerificationError as ex:
            return Response({'detail': ex.message, 'code': ex.code, **ex.extra}, status=ex.status)
        if result.get('already_verified'):
            return Response({'detail': 'This email address is already verified.', 'code': 'ALREADY_VERIFIED'}, status=status.HTTP_409_CONFLICT)
        if not result.get('sent'):
            return Response({'detail': 'The email could not be sent just now. Try again in a moment.', 'code': 'SEND_FAILED'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        log_audit_event(actor=request.user, action='Verification Email Resent By Admin', target_model='User', target_id=target.pk, details={})
        return Response({'sent': True, 'email_masked': result.get('email_masked')})
