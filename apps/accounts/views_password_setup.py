from rest_framework import status, permissions
from rest_framework.views import APIView
from rest_framework.response import Response
from django.core.exceptions import ValidationError

from apps.accounts.services.password_setup_service import (
    PasswordSetupService,
    RateLimitExceededError,
    InvalidSetupTokenError,
)


def get_client_ip(request) -> str | None:
    # This deployment sits behind exactly one trusted reverse proxy
    # (SECURE_PROXY_SSL_HEADER in settings.py confirms it terminates TLS in
    # front of Django). That proxy appends the real client IP as the LAST hop
    # of X-Forwarded-For rather than replacing whatever the client already
    # sent — so the first entry is attacker-controlled and picking it let
    # anyone reset their own 20/hr/IP setup-link rate limit on every request
    # by sending a fresh forged X-Forwarded-For value each time.
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        return x_forwarded_for.split(',')[-1].strip()
    return request.META.get('REMOTE_ADDR')


class PasswordSetupRequestView(APIView):
    """
    POST /api/auth/setup-password/request/
    Initiates one-time password setup link creation.
    Strict rate-limiting (5/hr/email, 20/hr/IP) and anti-enumeration:
    always returns generic 200 OK response.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        email = request.data.get('email', '').strip()
        client_ip = get_client_ip(request)

        try:
            _, message = PasswordSetupService.request_setup_link(email=email, request_ip=client_ip)
            return Response({"detail": message}, status=status.HTTP_200_OK)
        except RateLimitExceededError as ex:
            return Response({"error": str(ex)}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        except Exception:
            # Shield against internal leaking, maintain generic response
            return Response(
                {"detail": "If an eligible account exists, a password setup link has been sent."},
                status=status.HTTP_200_OK,
            )


class PasswordSetupVerifyView(APIView):
    """
    GET /api/auth/setup-password/verify/?token=...
    POST /api/auth/setup-password/verify/
    Verifies that a token is valid, unexpired, and unused before rendering password inputs.
    """
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        token = request.query_params.get('token', '').strip()
        result = PasswordSetupService.verify_setup_token(token)
        if not result.get('valid'):
            return Response(result, status=status.HTTP_400_BAD_REQUEST)
        return Response(result, status=status.HTTP_200_OK)

    def post(self, request):
        token = request.data.get('token', '').strip()
        result = PasswordSetupService.verify_setup_token(token)
        if not result.get('valid'):
            return Response(result, status=status.HTTP_400_BAD_REQUEST)
        return Response(result, status=status.HTTP_200_OK)


class PasswordSetupConfirmView(APIView):
    """
    POST /api/auth/setup-password/confirm/
    Atomically verifies token using row locks (select_for_update), sets password,
    transitions status to ACTIVE, and marks token used.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        token = request.data.get('token', '').strip()
        password = request.data.get('password', '')

        if not token:
            return Response({"error": "Setup token is required."}, status=status.HTTP_400_BAD_REQUEST)
        if not password:
            return Response({"error": "New password is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = PasswordSetupService.confirm_password_setup(raw_token=token, new_password=password)
            return Response(
                {
                    "success": True,
                    "message": "Your password has been established successfully. You may now sign in.",
                    "club_id": user.club_id,
                },
                status=status.HTTP_200_OK,
            )
        except InvalidSetupTokenError as ex:
            return Response({"error": str(ex)}, status=status.HTTP_400_BAD_REQUEST)
        except ValidationError as ex:
            return Response({"error": ex.messages if hasattr(ex, 'messages') else str(ex)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as ex:
            return Response({"error": f"Password confirmation failed: {str(ex)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
