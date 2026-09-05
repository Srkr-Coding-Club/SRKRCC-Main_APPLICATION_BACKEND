from rest_framework import permissions


def _is_admin_or_club_lead(user) -> bool:
    if not (user and user.is_authenticated):
        return False
    if user.is_staff or user.is_superuser:
        return True
    return getattr(user, 'role', None) in ['ADMIN', 'CLUB_LEAD']


class IsAdminOrClubLead(permissions.BasePermission):
    """
    Allows access only to authenticated users who are either:
    - Superusers / Django staff
    - Assigned the 'ADMIN' role
    - Assigned the 'CLUB_LEAD' role
    """
    def has_permission(self, request, view):
        return _is_admin_or_club_lead(request.user)


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


class IsAdminOrClubLeadOrReadOnly(permissions.BasePermission):
    """
    Anyone (including anonymous) may read. Only ADMIN/CLUB_LEAD/staff may write.
    Use for admin-managed content: Events, Hackathons, CodeQuest Problems,
    BlogPosts, JobListings, FeatureFlags, Forms.
    """
    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True
        return _is_admin_or_club_lead(request.user)


class IsOwnerOrAdminOrClubLead(permissions.BasePermission):
    """
    Object-level permission: safe methods allowed to any authenticated user;
    unsafe methods require the caller to own the object (via `owner_field`,
    default 'user') or be ADMIN/CLUB_LEAD/staff. Pair with IsAuthenticated
    for the view-level has_permission check.
    """
    owner_field = 'user'

    def has_object_permission(self, request, view, obj):
        if request.method in permissions.SAFE_METHODS:
            return bool(request.user and request.user.is_authenticated)
        if _is_admin_or_club_lead(request.user):
            return True
        owner = getattr(obj, getattr(view, 'owner_field', self.owner_field), None)
        return owner is not None and owner == request.user
