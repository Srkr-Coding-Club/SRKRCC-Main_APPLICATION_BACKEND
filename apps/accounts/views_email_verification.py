import logging

from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.accounts.services import email_verification as verification
from apps.accounts.services.email_verification import VerificationError
from apps.audit.utils import log_audit_event
from apps.core.client_ip import get_client_ip
from apps.core.permissions import IsAdmin
from apps.core.throttling import AuthAwareScopedThrottle

logger = logging.getLogger(__name__)


def _error(ex: VerificationError) -> Response:
    body = {'detail': ex.message, 'code': ex.code, **ex.extra}
    return Response(body, status=ex.status)


class _SignedInVerificationView(APIView):
    """Needed to verify or leave, so a person who is blocked can still reach these."""
    permission_classes = [permissions.IsAuthenticated]
    verification_exempt = True
    throttle_classes = [AuthAwareScopedThrottle]
    throttle_scope = 'email_verify'


class EmailVerificationRequestView(_SignedInVerificationView):
    """POST /api/auth/email/verification/request/ - the "Verify email" button: send the link to the account's address."""

    def post(self, request):
        try:
            result = verification.request_link(request.user, ip=get_client_ip(request))
        except VerificationError as ex:
            return _error(ex)
        if result.get('already_verified'):
            return Response({'detail': 'Your email address is already verified.', 'already_verified': True, **verification.state(request.user)})
        if not result['sent']:
            return Response({
                'detail': 'We could not send the email just now. Please try again in a moment.', 'code': 'SEND_FAILED', 'sent': False,
                'retry_after': verification.FAILED_SEND_COOLDOWN_SECONDS,
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({'detail': f"We sent a link to {result['email_masked']}. Check your inbox and spam folder.", **result})


class EmailChangeRequestView(_SignedInVerificationView):
    """POST /api/auth/email/change/ {new_email, password} - the new address gets a link; it applies only once confirmed."""

    def post(self, request):
        try:
            result = verification.request_email_change(
                request.user, request.data.get('new_email'), request.data.get('password'), ip=get_client_ip(request),
            )
        except VerificationError as ex:
            return _error(ex)
        if not result['sent']:
            return Response({'detail': 'We could not send the email just now. Please try again in a moment.', 'code': 'SEND_FAILED', 'sent': False}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({'detail': f"We sent a link to {result['email_masked']}. Your address changes once you confirm it.", **result})


class _PublicLinkView(APIView):
    """Opened from an email, often on another device or while signed out."""
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def _token(self, request) -> str:
        data = request.data if request.method == 'POST' else request.query_params
        value = data.get('token', '') if hasattr(data, 'get') else ''
        return value.strip() if isinstance(value, str) else ''


class EmailVerificationInspectView(_PublicLinkView):
    """GET ?token= - read-only: shows the address on the confirmation page. Never consumes the link (mail scanners open links)."""

    def get(self, request):
        try:
            return Response(verification.inspect_token(self._token(request)))
        except VerificationError as ex:
            return _error(ex)


class EmailVerificationConfirmView(_PublicLinkView):
    """POST {token} - the "Confirm my email" button."""

    def post(self, request):
        try:
            return Response(verification.confirm(self._token(request)))
        except VerificationError as ex:
            return _error(ex)


class EmailVerificationDisputeView(_PublicLinkView):
    """POST {token} - the "This wasn't me" button."""

    def post(self, request):
        try:
            return Response(verification.dispute(self._token(request)))
        except VerificationError as ex:
            return _error(ex)


class AdminMarkEmailVerifiedView(APIView):
    """POST /api/auth/users/<id>/verify-email/ - an admin vouches for a member who cannot receive the link (audited)."""
    permission_classes = [IsAdmin]

    def post(self, request, pk):
        target = User.objects.filter(pk=pk).first()
        if target is None:
            return Response({'detail': 'No such user.', 'code': 'NOT_FOUND'}, status=status.HTTP_404_NOT_FOUND)
        changed = verification.mark_verified(target, via=f'admin:{request.user.pk}')
        log_audit_event(actor=request.user, action='Email Marked Verified By Admin', target_model='User', target_id=target.pk, details={'changed': changed})
        return Response({'verified': True, 'changed': changed})
