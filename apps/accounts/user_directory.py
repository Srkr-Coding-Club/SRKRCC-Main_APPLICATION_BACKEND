"""The admin user directory: filters, verification state and the numbers behind the Users and Members pages."""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db.models import Count, DateTimeField, ExpressionWrapper, F, Q
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework import serializers

from apps.accounts.models import MembershipStatus, PasswordStatus, User, UserRole
from apps.accounts.serializers import CollegeSerializer
from apps.accounts.services import email_verification as verification

EMAIL_STATES = ('verified', 'unverified', 'overdue', 'disputed')
SEARCH_FIELDS = ('email', 'first_name', 'last_name', 'club_id', 'roll_number', 'phone_number', 'branch', 'referred_by_raw')
MAX_SEARCH_TOKENS = 5
MAX_TOKEN_LENGTH = 100
ORDERINGS = {
    'newest': ('-created_at', '-id'), 'oldest': ('created_at', 'id'), 'name': ('first_name', 'last_name', 'id'),
    'email': ('email',), 'club_id': ('club_id', 'id'), 'last_login': ('-last_login', '-id'),
}
MEMBER = Q(club_id__isnull=False) & ~Q(club_id='')


def annotated(queryset):
    """Adds `verify_deadline`: the stored deadline, or the sign-up date plus the grace period (what the gate itself uses)."""
    grace = timedelta(days=getattr(settings, 'EMAIL_VERIFY_GRACE_DAYS', 7))
    return queryset.annotate(verify_deadline=Coalesce(
        'email_verify_deadline', ExpressionWrapper(F('created_at') + grace, output_field=DateTimeField()),
    ))


def _words(params, key: str) -> list[str]:
    return [part.strip() for part in (params.get(key) or '').split(',') if part.strip()]


def _search(queryset, text: str):
    for token in text.split()[:MAX_SEARCH_TOKENS]:
        token = token[:MAX_TOKEN_LENGTH]
        queryset = queryset.filter(_any_field(token))
    return queryset


def _any_field(token: str) -> Q:
    match = Q()
    for field in SEARCH_FIELDS:
        match |= Q(**{f'{field}__icontains': token})
    return match


def _email_state_filter(states: list[str], now) -> Q:
    wanted = Q()
    if 'verified' in states:
        wanted |= Q(email_verified_at__isnull=False)
    if 'unverified' in states:
        wanted |= Q(email_verified_at__isnull=True)
    if 'overdue' in states:
        wanted |= Q(email_verified_at__isnull=True, verify_deadline__lte=now)
    if 'disputed' in states:
        wanted |= Q(email_disputed_at__isnull=False)
    return wanted


def scoped(queryset, *, members_only: bool = False):
    """The base set of a page: everybody, or only the people holding a Club ID."""
    queryset = annotated(queryset.select_related('college', 'referred_by_user'))
    return queryset.filter(MEMBER) if members_only else queryset


def apply_filters(queryset, params):
    """Narrows the directory by the query string. Unknown values match nothing rather than being ignored."""
    now = timezone.now()
    text = (params.get('q') or '').strip()
    if text:
        queryset = _search(queryset, text)
    if roles := _words(params, 'role'):
        queryset = queryset.filter(role__in=roles)
    if statuses := _words(params, 'membership_status'):
        queryset = queryset.filter(membership_status__in=statuses)
    if states := _words(params, 'email'):
        queryset = queryset.filter(_email_state_filter(states, now))
    if (member := params.get('member')) in ('yes', 'no'):
        queryset = queryset.filter(MEMBER) if member == 'yes' else queryset.exclude(MEMBER)
    if password := params.get('password_status'):
        queryset = queryset.filter(password_status=password)
    if origin := _words(params, 'created_from'):
        queryset = queryset.filter(created_from__in=origin)
    if prefix := (params.get('club_id_prefix') or '').strip():
        queryset = queryset.filter(club_id__istartswith=prefix)
    if branch := (params.get('branch') or '').strip():
        queryset = queryset.filter(branch__iexact=branch)
    if (year := params.get('year', '')).isdigit():
        queryset = queryset.filter(year=int(year))
    if (college := params.get('college', '')).isdigit():
        queryset = queryset.filter(college_id=int(college))
    if joined_from := _date(params.get('joined_from')):
        queryset = queryset.filter(created_at__date__gte=joined_from)
    if joined_to := _date(params.get('joined_to')):
        queryset = queryset.filter(created_at__date__lte=joined_to)
    return queryset.order_by(*ORDERINGS.get(params.get('ordering'), ORDERINGS['newest']))


def _date(value):
    from datetime import date
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def summary(queryset) -> dict:
    """Counts over the whole base set (not the filtered page), for the tiles."""
    now = timezone.now()
    totals = queryset.aggregate(
        total=Count('pk'),
        verified=Count('pk', filter=Q(email_verified_at__isnull=False)),
        unverified=Count('pk', filter=Q(email_verified_at__isnull=True)),
        overdue=Count('pk', filter=Q(email_verified_at__isnull=True, verify_deadline__lte=now)),
        disputed=Count('pk', filter=Q(email_disputed_at__isnull=False)),
        members=Count('pk', filter=MEMBER),
        needs_password=Count('pk', filter=Q(password_status=PasswordStatus.NEEDS_SETUP)),
    )
    by_role = {row['role']: row['n'] for row in queryset.values('role').annotate(n=Count('pk'))}
    by_status = {row['membership_status']: row['n'] for row in queryset.values('membership_status').annotate(n=Count('pk'))}
    return {
        **totals,
        'by_role': {role: by_role.get(role, 0) for role in UserRole.values},
        'by_membership_status': {state: by_status.get(state, 0) for state in MembershipStatus.values},
    }


def email_state(user, now=None) -> str:
    """VERIFIED, OVERDUE (past its deadline), DISPUTED (the owner said it was not them) or UNVERIFIED."""
    if verification.is_verified(user):
        return 'VERIFIED'
    if user.email_disputed_at:
        return 'DISPUTED'
    return 'OVERDUE' if verification.is_past_deadline(user, now) else 'UNVERIFIED'


class DirectoryUserSerializer(serializers.ModelSerializer):
    """What an organizer sees about a person. Contact details and verification state: never exposed to the public."""
    college = CollegeSerializer(read_only=True)
    full_name = serializers.SerializerMethodField()
    email_state = serializers.SerializerMethodField()
    email_days_left = serializers.SerializerMethodField()
    email_deadline = serializers.SerializerMethodField()
    referred_by_display = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'email', 'first_name', 'last_name', 'full_name', 'role', 'club_id', 'membership_status', 'roll_number',
            'college', 'other_college_name', 'branch', 'year', 'phone_number', 'github_profile', 'linkedin_profile',
            'registered_at', 'created_from', 'created_at', 'last_login', 'is_active', 'password_status',
            'email_verified_at', 'email_disputed_at', 'email_state', 'email_days_left', 'email_deadline', 'referred_by_display',
        ]
        read_only_fields = fields

    def get_full_name(self, user):
        return f'{user.first_name} {user.last_name}'.strip() or user.email.split('@')[0]

    def get_email_state(self, user):
        return email_state(user)

    def get_email_days_left(self, user):
        return verification.days_left(user)

    def get_email_deadline(self, user):
        return None if verification.is_verified(user) else verification.deadline(user)

    def get_referred_by_display(self, user):
        if user.referred_by_user_id:
            return f'{user.referred_by_user.first_name} {user.referred_by_user.last_name}'.strip() or user.referred_by_user.email
        return user.referred_by_raw or ''
