from rest_framework import permissions

class IsAdminOrClubLead(permissions.BasePermission):
    """
    Allows access only to authenticated users who are either:
    - Superusers / Django staff
    - Assigned the 'ADMIN' role
    - Assigned the 'CLUB_LEAD' role
    """
    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        if request.user.is_staff or request.user.is_superuser:
            return True
        user_role = getattr(request.user, 'role', None)
        return user_role in ['ADMIN', 'CLUB_LEAD']


class IsClubMember(permissions.BasePermission):
    """
    Allows access to all authenticated club members.
    """
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)


class IsJudgeOrAdmin(permissions.BasePermission):
    """
    Allows access to Judges, Club Leads, and Admins.
    """
    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        if request.user.is_staff or request.user.is_superuser:
            return True
        user_role = getattr(request.user, 'role', None)
        return user_role in ['ADMIN', 'CLUB_LEAD', 'JUDGE']
