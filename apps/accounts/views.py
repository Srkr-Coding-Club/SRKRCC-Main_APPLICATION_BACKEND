from rest_framework import generics, permissions, status, filters
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView
from django.contrib.auth import get_user_model
from .serializers import (
    UserSerializer,
    UserProfileDetailSerializer,
    RegisterSerializer,
    CustomTokenObtainPairSerializer,
)
from apps.core.permissions import IsAdminOrClubLead
from apps.audit.utils import log_audit_event

User = get_user_model()

class CustomTokenObtainPairView(TokenObtainPairView):
    """
    Custom login view returning access token, refresh token, and user profile data.
    """
    serializer_class = CustomTokenObtainPairSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
        except Exception as ex:
            detail = getattr(ex, 'detail', None)
            if isinstance(detail, dict) and detail.get('code'):
                code_val = detail['code']
                if isinstance(code_val, list):
                    code_val = code_val[0]
                detail_val = detail.get('detail', 'Password setup is required.')
                if isinstance(detail_val, list):
                    detail_val = detail_val[0]
                # 403 (not 400): matches docs/architecture/password-setup-lifecycle.md,
                # which documents this as a Forbidden response, not a validation error.
                return Response(
                    {"code": str(code_val), "detail": str(detail_val)},
                    status=status.HTTP_403_FORBIDDEN,
                )
            raise
        log_audit_event(
            actor=serializer.user,
            action="User Logged In",
            target_model="User",
            target_id=str(serializer.user.id),
            details={"email": serializer.user.email},
        )
        return Response(serializer.validated_data, status=status.HTTP_200_OK)

class UserListView(generics.ListCreateAPIView):
    """
    Admin/club-lead member directory. Not for public consumption — returns
    every member's email, phone, roll number, branch, and social links.
    """
    queryset = User.objects.all().order_by('-created_at')
    serializer_class = UserSerializer
    permission_classes = [IsAdminOrClubLead]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = [
        'club_id',
        'email',
        'first_name',
        'last_name',
        'branch',
        'phone_number',
        'roll_number',
        'referred_by_raw',
    ]
    ordering_fields = ['created_at', 'registered_at', 'club_id', 'first_name', 'email']

class RegisterView(generics.CreateAPIView):
    queryset = User.objects.all()
    permission_classes = [permissions.AllowAny]
    serializer_class = RegisterSerializer

    def perform_create(self, serializer):
        user = serializer.save()
        log_audit_event(
            actor=user,
            action="User Self-Registered",
            target_model="User",
            target_id=str(user.id),
            details={"email": user.email},
        )

class ProfileView(generics.RetrieveUpdateAPIView):
    serializer_class = UserProfileDetailSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user


class LogoutView(generics.GenericAPIView):
    """
    POST /api/auth/logout/  body: {"refresh": "<refresh_token>"}
    Blacklists the refresh token so it can no longer be used to mint new
    access tokens after logout (previously a leaked refresh token stayed
    valid for its full 7-day lifetime with no way to revoke it).
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, *args, **kwargs):
        from rest_framework_simplejwt.tokens import RefreshToken
        from rest_framework_simplejwt.exceptions import TokenError

        refresh_token = request.data.get('refresh')
        if not refresh_token:
            return Response({"error": "refresh token is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            token = RefreshToken(refresh_token)
            token.blacklist()
        except TokenError:
            return Response({"error": "Invalid or already-blacklisted token."}, status=status.HTTP_400_BAD_REQUEST)

        log_audit_event(
            actor=request.user,
            action="User Logged Out",
            target_model="User",
            target_id=str(request.user.id),
        )
        return Response({"success": True}, status=status.HTTP_200_OK)
