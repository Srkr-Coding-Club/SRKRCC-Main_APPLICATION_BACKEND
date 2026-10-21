"""
Who may do what in one hackathon.

Site-wide ADMIN / CLUB_LEAD users can do everything everywhere. Everyone else gets powers only through a
``HackathonStaff`` row for that hackathon: a role (ORGANIZER, JUDGE, VOLUNTEER) that starts from a preset and
can be tuned permission by permission. The server checks these on every request; the UI only mirrors them.
"""
from __future__ import annotations

from rest_framework import permissions

from apps.core.permissions import _is_admin_or_club_lead

from .models import Hackathon, HackathonStaff, StaffRole

# key -> (label, what it allows). Order is the order shown in the UI.
PERMISSIONS: dict[str, tuple[str, str]] = {
    'settings': ('Settings and registration controls', 'Edit the hackathon, publish / close / reopen it, hide or show it, and set the public page, capacity, rules and modules.'),
    'content': ('Content', 'Problem statements, public timeline, announcements and team emails.'),
    'teams': ('Teams and participants', 'See every team and participant, edit teams, change team status, and export participants.'),
    'rounds': ('Rounds and shortlisting', 'Create rounds and decide who is shortlisted.'),
    'ideas': ('Review ideas', 'See submitted ideas and approve them, ask for changes or reject them.'),
    'submissions': ('Project submissions', 'See every project submission, set the submission window and remind teams.'),
    'judging': ('Judging setup', 'Edit the scoring rubric, see the leaderboard and every judge\'s scores.'),
    'judge': ('Score projects', 'Open the judging page and score submissions.'),
    'results': ('Results and certificates', 'Choose winners, publish results and release certificates.'),
    'check_in': ('Check-in', 'Scan QR codes and check participants in at the venue.'),
    'insights': ('Insights', 'See the overview, participation funnel and breakdowns.'),
    'staff': ('Team and roles', 'Add people to this hackathon and change their permissions (only up to their own).'),
}
ALL_PERMISSIONS = tuple(PERMISSIONS)

ROLE_PRESETS: dict[str, tuple[str, ...]] = {
    StaffRole.ORGANIZER: (
        'settings', 'content', 'teams', 'rounds', 'ideas', 'submissions', 'judging', 'results', 'check_in', 'insights',
    ),
    StaffRole.JUDGE: ('judge',),
    StaffRole.VOLUNTEER: ('check_in',),
}


def preset_permissions(role: str) -> list[str]:
    return list(ROLE_PRESETS.get(role, ()))


def is_organizer(user) -> bool:
    """Site-wide organizer (ADMIN, CLUB_LEAD or staff): all permissions in every hackathon."""
    return _is_admin_or_club_lead(user)


def _staff_rows(hackathon: Hackathon, user):
    if not (user and user.is_authenticated):
        return []
    return list(HackathonStaff.objects.filter(hackathon=hackathon, user=user))


def staff_roles(hackathon: Hackathon, user) -> set[str]:
    return {row.role for row in _staff_rows(hackathon, user)}


def my_permissions(hackathon: Hackathon, user) -> list[str]:
    """Everything this person may do here, in catalog order."""
    if is_organizer(user):
        return list(ALL_PERMISSIONS)
    granted: set[str] = set()
    for row in _staff_rows(hackathon, user):
        granted.update(row.permissions or preset_permissions(row.role))
    return [key for key in ALL_PERMISSIONS if key in granted]


def has_permission(hackathon: Hackathon, user, key: str) -> bool:
    return is_organizer(user) or key in my_permissions(hackathon, user)


def has_any_permission(hackathon: Hackathon, user, keys) -> bool:
    return is_organizer(user) or bool(set(keys) & set(my_permissions(hackathon, user)))


def is_hackathon_staff(hackathon: Hackathon, user) -> bool:
    return bool(_staff_rows(hackathon, user))


def can_judge(hackathon: Hackathon, user) -> bool:
    return has_permission(hackathon, user, 'judge')


def can_check_in(hackathon: Hackathon, user) -> bool:
    return has_permission(hackathon, user, 'check_in')


def hackathon_ids_managed_by(user) -> list[int]:
    """Hackathons where this (non site-wide) user holds any staff role."""
    if not (user and user.is_authenticated):
        return []
    return list(HackathonStaff.objects.filter(user=user).values_list('hackathon_id', flat=True).distinct())


class HackathonPermission(permissions.BasePermission):
    """
    View-level check for ``/{slug}/...`` endpoints: site-wide organizers pass; anyone else needs ``required``
    in this hackathon. Build one with ``scoped('content')``.
    """
    required: str = ''
    message = 'You do not have permission to do this in this hackathon.'

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if is_organizer(user):
            return True
        slug = view.kwargs.get('slug')
        hackathon = Hackathon.objects.filter(slug=slug).first() if slug else None
        return bool(hackathon and has_permission(hackathon, user, self.required))


def scoped(key: str) -> type[HackathonPermission]:
    if key not in PERMISSIONS:
        raise ValueError(f'Unknown hackathon permission: {key}')
    return type(f'Requires_{key}', (HackathonPermission,), {'required': key})


def scoped_or_read_only(key: str) -> type[HackathonPermission]:
    """Anyone may read; writes need ``key`` in this hackathon (or a site-wide organizer)."""
    scoped_class = scoped(key)

    class _ReadOrScoped(scoped_class):
        def has_permission(self, request, view):
            return request.method in permissions.SAFE_METHODS or super().has_permission(request, view)

    _ReadOrScoped.__name__ = f'ReadOrRequires_{key}'
    return _ReadOrScoped
