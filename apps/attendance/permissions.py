from rest_framework import permissions


def _is_volunteer_or_above(user) -> bool:
    if not (user and user.is_authenticated):
        return False
    if user.is_staff or user.is_superuser:
        return True
    return getattr(user, 'role', None) in ['ADMIN', 'CLUB_LEAD', 'VOLUNTEER']


class IsVolunteerOrAbove(permissions.BasePermission):
    """
    Allows access only to authenticated users who are either:
    - Superusers / Django staff
    - Assigned the 'ADMIN', 'CLUB_LEAD', or 'VOLUNTEER' role

    Intentionally broader than apps.core.permissions.IsAdminOrClubLead — used
    only for the attendance scan endpoint, since event volunteers need to
    scan registrant QR codes without any other admin capability.
    """
    def has_permission(self, request, view):
        return _is_volunteer_or_above(request.user)
