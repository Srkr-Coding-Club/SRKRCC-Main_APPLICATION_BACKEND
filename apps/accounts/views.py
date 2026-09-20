from rest_framework import generics, permissions, status, filters
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView
from django.contrib.auth import get_user_model
from .serializers import (
    UserSerializer,
    UserRoleUpdateSerializer,
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

class UserDetailView(generics.RetrieveUpdateAPIView):
    """
    PATCH /auth/users/{id}/ — backs the admin Users tab's role dropdown and
    membership-status control. `role` and `membership_status` are writable
    (see UserRoleUpdateSerializer). Elevation to ADMIN/CLUB_LEAD via `role` is
    restricted to existing ADMINs, since IsAdminOrClubLead alone would let a
    CLUB_LEAD promote themselves or anyone else to ADMIN. `membership_status`
    carries no such privilege risk, so it has no extra restriction beyond
    IsAdminOrClubLead.
    """
    queryset = User.objects.all()
    serializer_class = UserRoleUpdateSerializer
    permission_classes = [IsAdminOrClubLead]

    ELEVATED_ROLES = {'ADMIN', 'CLUB_LEAD'}

    def perform_update(self, serializer):
        target = serializer.instance
        requester = self.request.user

        if target.id == requester.id:
            raise PermissionDenied("You cannot change your own role.")

        # Escalation check applies only when `role` is actually being changed —
        # scoped to serializer.validated_data (not target.role) so that a
        # membership_status-only PATCH on a user who already holds an elevated
        # role doesn't get wrongly blocked as a "role escalation".
        if 'role' in serializer.validated_data:
            new_role = serializer.validated_data['role']
            is_full_admin = requester.is_superuser or requester.is_staff or getattr(requester, 'role', None) == 'ADMIN'
            if new_role in self.ELEVATED_ROLES and not is_full_admin:
                raise PermissionDenied("Only an Admin can assign the Admin or Club Lead role.")
            # AFFILIATE always has a club_id. This endpoint doesn't accept
            # club_id in its own payload (UserRoleUpdateSerializer only writes
            # role/membership_status) — the admin must assign one first via
            # the existing Club ID tooling, then set the role.
            if new_role == 'AFFILIATE' and not target.club_id:
                raise ValidationError({
                    'club_id': ["Assign a Club ID to this member before setting their role to Affiliate."],
                })

        previous_role = target.role
        previous_membership_status = target.membership_status
        user = serializer.save()

        details = {}
        if 'role' in serializer.validated_data:
            details["previous_role"] = previous_role
            details["new_role"] = user.role
        if 'membership_status' in serializer.validated_data:
            details["previous_membership_status"] = previous_membership_status
            details["new_membership_status"] = user.membership_status

        if details:
            log_audit_event(
                actor=requester,
                action="User Role Changed" if 'role' in serializer.validated_data else "User Membership Status Changed",
                target_model="User",
                target_id=str(user.id),
                details=details,
            )


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
