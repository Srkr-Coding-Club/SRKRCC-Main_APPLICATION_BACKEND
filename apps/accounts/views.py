from rest_framework import generics, permissions, status, filters
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView
from django.db import models
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
    Admin/club-lead member directory. Not for public consumption - returns
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

    def get_queryset(self):
        qs = super().get_queryset()
        role = self.request.query_params.get('role')
        if role and role != 'ALL':
            if role == 'AFFILIATE':
                qs = qs.filter(models.Q(role='AFFILIATE') | (models.Q(club_id__isnull=False) & ~models.Q(club_id='')))
            elif role == 'NON_AFFILIATE':
                qs = qs.filter(role='NON_AFFILIATE').filter(models.Q(club_id__isnull=True) | models.Q(club_id=''))
            else:
                qs = qs.filter(role=role)
        affiliates_only = self.request.query_params.get('affiliates_only')
        if affiliates_only in ('true', '1', 'True'):
            qs = qs.filter(models.Q(role='AFFILIATE') | (models.Q(club_id__isnull=False) & ~models.Q(club_id='')))
        return qs

class UserDetailView(generics.RetrieveUpdateAPIView):
    """
    PATCH /auth/users/{id}/ - backs the admin Users tab's role dropdown and
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

        if target.id == requester.id and 'role' in serializer.validated_data and serializer.validated_data['role'] != target.role:
            raise PermissionDenied("You cannot change your own role.")

        # Escalation check applies only when `role` is actually being changed -
        # scoped to serializer.validated_data (not target.role) so that a
        # membership_status-only PATCH on a user who already holds an elevated
        # role doesn't get wrongly blocked as a "role escalation".
        new_role = serializer.validated_data.get('role', target.role)
        is_full_admin = requester.is_superuser or requester.is_staff or getattr(requester, 'role', None) == 'ADMIN'

        if 'role' in serializer.validated_data:
            if target.role in self.ELEVATED_ROLES and not is_full_admin:
                raise PermissionDenied("Only an Admin can modify an Admin or Club Lead role.")
            if new_role in self.ELEVATED_ROLES and not is_full_admin:
                raise PermissionDenied("Only an Admin can assign the Admin or Club Lead role.")

        # AFFILIATE always has a club_id. Check the effective club_id
        # (either provided in the payload or already assigned to the user).
        if 'club_id' in serializer.validated_data:
            effective_club_id = serializer.validated_data['club_id']
        else:
            effective_club_id = target.club_id

        if new_role == 'AFFILIATE' and not effective_club_id:
            raise ValidationError({
                'club_id': ["Assign a Club ID to this member before setting their role to Affiliate."],
            })

        # Snapshot all previous values BEFORE serializer.save() mutates target in-place
        previous_snapshots = {
            field: getattr(target, field, None)
            for field in [
                'role', 'membership_status', 'roll_number', 'club_id',
                'first_name', 'last_name', 'branch', 'year', 'phone_number',
                'github_profile', 'linkedin_profile'
            ]
        }

        user = serializer.save()

        details = {}
        for field, prev_val in previous_snapshots.items():
            if field in serializer.validated_data:
                details[f"previous_{field}"] = prev_val
                details[f"new_{field}"] = getattr(user, field, None)

        if details:
            if 'role' in serializer.validated_data:
                action = "User Role Changed"
            elif 'club_id' in serializer.validated_data:
                action = "User Club ID Changed"
            elif 'membership_status' in serializer.validated_data:
                action = "User Membership Status Changed"
            elif 'roll_number' in serializer.validated_data:
                action = "User Roll Number Changed"
            else:
                action = "User Profile Updated by Admin"
            log_audit_event(
                actor=requester,
                action=action,
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
