from django.db.models import BooleanField, Count, Exists, OuterRef, Prefetch, Q, Value
from apps.accounts.services import email_verification
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
import os
import uuid
from PIL import Image as PILImage
from django.conf import settings
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response as DRFResponse
from apps.core.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.audit.utils import log_audit_event
from apps.core.permissions import IsAdmin, IsAdminOrClubLead, _is_admin_or_club_lead
from apps.core.cache import PublicReadCacheControlMixin

from . import team_emails
from .models import (
    EntryStatus, Hackathon, HackathonAnnouncement, HackathonStatus, InviteStatus,
    ProblemStatement, Round, RoundEntry, Submission, Team, TeamEmailInvite, TeamInvite, TeamMember, TeamStatus, WithdrawalStatus,
)
from .serializers import (
    AdminHackathonAnnouncementSerializer, AdminRoundEntrySerializer, AdminTeamSerializer,
    HackathonAnnouncementSerializer, HackathonSerializer, ProblemStatementSerializer,
    AdminStageSerializer, PublicStageSerializer, RoundSerializer, SubmissionSerializer, TeamInviteSerializer, TeamSerializer,
)
from . import access, payments, registration, withdrawals
from .access import scoped, scoped_or_read_only
from .services import (
    AnnouncementService, HackathonError, ProblemStatementService, RoundService, StageService, TeamService, WaitlistService,
    is_leader, is_member, effective_round_status,
    membership_of, missing_profile_fields, round_entry_for_details_form,
)

ACTIVE = (TeamStatus.FORMING, TeamStatus.REGISTERED)
LOOKUP_MIN_QUERY_LENGTH = 3
LOOKUP_MAX_RESULTS = 5


def _public_window_q() -> Q:
    """What an ordinary visitor may see: published, not hidden, inside the visibility window."""
    now = timezone.now()
    return (
        ~Q(status=HackathonStatus.DRAFT) & Q(hidden_at__isnull=True)
        & (Q(visible_from__isnull=True) | Q(visible_from__lte=now))
        & (Q(visible_until__isnull=True) | Q(visible_until__gt=now))
    )


def _visible_hackathons(user):
    """Organizers see everything; this hackathon's staff also see theirs, drafts and hidden included."""
    qs = Hackathon.objects.all()
    if _is_admin_or_club_lead(user):
        return qs
    return qs.filter(_public_window_q() | Q(pk__in=access.hackathon_ids_managed_by(user)))


def _can(hackathon, user, key: str) -> bool:
    return access.has_permission(hackathon, user, key)


def _get_hackathon(request, slug):
    return get_object_or_404(_visible_hackathons(request.user), slug=slug)


def _team_queryset():
    return Team.objects.select_related('hackathon', 'problem_statement', 'leader')


def _int(value, field):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise HackathonError(f'{field} must be a number.', 'INVALID', field=field)


def _parse_problem_choice(hackathon, data):
    """Read a team's problem choice from a request body.

    Returns (present, problem_statement, open_innovation): `present` says whether the
    body mentioned the problem at all (so a PATCH that only renames leaves it alone),
    the statement is looked up in this hackathon, and open_innovation is the raw
    {title, description, domain} object the service validates.
    """
    present = 'problem_statement' in data or 'open_innovation' in data
    statement = None
    statement_id = data.get('problem_statement')
    if statement_id not in (None, ''):
        statement = hackathon.problem_statements.filter(pk=_int(statement_id, 'problem_statement')).first()
        if statement is None:
            raise HackathonError('That problem statement is not available.', 'PROBLEM_STATEMENT_INVALID',
                                 field='problem_statement')
    open_innovation = data.get('open_innovation')
    if open_innovation is not None and not isinstance(open_innovation, dict):
        raise HackathonError('open_innovation must be an object with title, description and domain.', 'INVALID',
                             field='open_innovation')
    return present, statement, open_innovation or None


class IsSubmissionTeamMemberOrAdmin(permissions.BasePermission):
    """Writes require the submitting team's leader/member or admin."""
    def has_object_permission(self, request, view, obj):
        if _can(obj.team.hackathon, request.user, 'submissions'):
            return True
        team = obj.team
        if request.method in permissions.SAFE_METHODS:
            return is_member(team, request.user)
        return is_leader(team, request.user)


# ---------------------------------------------------------------------------
# Hackathon CRUD (admin) + public read
# ---------------------------------------------------------------------------

class HackathonObjectPermission(permissions.BasePermission):
    """Anyone reads; only site-wide organizers create; changing one needs its 'settings' permission."""

    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True
        if view.action == 'create':
            return _is_admin_or_club_lead(request.user)
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        if request.method in permissions.SAFE_METHODS:
            return True
        return _can(obj, request.user, 'settings')


class HackathonViewSet(PublicReadCacheControlMixin, viewsets.ModelViewSet):
    serializer_class = HackathonSerializer
    permission_classes = [HackathonObjectPermission]
    lookup_field = 'slug'
    ADMIN_ONLY_ACTIONS = ('destroy', 'delete_impact')

    def get_permissions(self):
        if self.action in self.ADMIN_ONLY_ACTIONS:
            return [IsAdmin()]
        return super().get_permissions()

    def get_queryset(self):
        """Annotate hackathons with real registration count and team count."""
        queryset = Hackathon.objects.select_related('registration_form').annotate(
            registration_count=Count(
                'registration_form__responses',
                filter=Q(registration_form__responses__is_test_submission=False),
                distinct=True,
            ),
            team_count=Count('teams', filter=Q(teams__status__in=ACTIVE), distinct=True),
            registered_team_count=Count('teams', filter=Q(teams__status=TeamStatus.REGISTERED), distinct=True),
            waitlisted_team_count=Count('teams', filter=Q(teams__status__in=ACTIVE, teams__is_waitlisted=True), distinct=True),
        ).order_by('-start_date')

        if _is_admin_or_club_lead(self.request.user):
            return queryset

        # Public/anonymous viewers only see hackathons inside their visibility window (see
        # apps.events.views.EventViewSet); a hackathon's own staff also see it while it is a draft or hidden.
        return queryset.filter(_public_window_q() | Q(pk__in=access.hackathon_ids_managed_by(self.request.user)))

    def perform_create(self, serializer):
        # New hackathons start private: nothing is public or open for teams until an admin publishes it.
        serializer.save(status=HackathonStatus.DRAFT)

    def perform_update(self, serializer):
        before = {f: getattr(serializer.instance, f) for f in serializer.validated_data}
        instance = serializer.save()
        changed = {
            f: [str(before[f]), str(getattr(instance, f))]
            for f in serializer.validated_data if before[f] != getattr(instance, f)
        }
        if 'min_team_size' in changed:
            changed['teams_status_rechecked'] = TeamService.recompute_statuses(instance)
        if {'max_teams', 'max_participants', 'waitlist_enabled'} & set(changed):
            WaitlistService.promote(instance)
        if 'certificate_config' in serializer.validated_data:
            was = bool((before.get('certificate_config') or {}).get('released'))
            now = bool((instance.certificate_config or {}).get('released'))
            if now and not was and instance.feature('certificates'):
                from . import celebrate
                celebrate.certificate_ready(instance)
                from .services import _notify_users
                _notify_users(
                    [m.user for m in instance.memberships.select_related('user')],
                    f'{instance.title}: certificates are ready', 'Open the hackathon dashboard to download yours.',
                    instance, self.request.user,
                )
        if changed:
            log_audit_event(
                actor=self.request.user, action="Updated Hackathon",
                target_model="Hackathon", target_id=instance.slug, details={"changes": changed},
            )

    @staticmethod
    def _delete_impact(hackathon):
        """What a hard delete removes. The linked registration form and its responses are kept."""
        return {
            'teams': hackathon.teams.count(),
            'members': hackathon.memberships.count(),
            'problem_statements': hackathon.problem_statements.count(),
            'rounds': hackathon.rounds.count(),
            'round_entries': RoundEntry.objects.filter(round__hackathon=hackathon).count(),
            'announcements': hackathon.announcements.count(),
            'stages': hackathon.stages.count(),
            'submissions': Submission.objects.filter(team__hackathon=hackathon).count(),
        }

    @action(detail=True, methods=['get'], url_path='delete-impact')
    def delete_impact(self, request, slug=None):
        """GET /api/hackathons/{slug}/delete-impact/ - counts of what deleting this hackathon would remove (admin only)."""
        return DRFResponse(self._delete_impact(self.get_object()))

    def perform_destroy(self, instance):
        if self.request.query_params.get('confirm_slug') != instance.slug:
            raise HackathonError(
                'Type the hackathon slug to confirm deletion.', 'CONFIRM_SLUG_MISMATCH',
            )
        impact = self._delete_impact(instance)
        details = {
            "title": instance.title,
            "registration_form": instance.registration_form_id,
            "teams_deleted": impact['teams'],
            "deleted": impact,
        }
        slug = instance.slug
        instance.delete()
        log_audit_event(
            actor=self.request.user, action="Deleted Hackathon",
            target_model="Hackathon", target_id=slug, details=details,
        )

    def _set(self, request, action_label, **fields):
        hackathon = self.get_object()
        for k, v in fields.items():
            setattr(hackathon, k, v)
        hackathon.save(update_fields=[*fields, 'updated_at'])
        log_audit_event(
            actor=request.user, action=action_label,
            target_model="Hackathon", target_id=hackathon.slug,
            details={"title": hackathon.title, **{k: str(v) for k, v in fields.items()}},
        )
        return DRFResponse(self.get_serializer(self.get_object()).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='publish')
    def publish(self, request, slug=None):
        """POST /api/hackathons/{slug}/publish/ - takes a DRAFT live once its checklist passes."""
        hackathon = self.get_object()
        if hackathon.status != HackathonStatus.DRAFT:
            raise HackathonError('Only a draft hackathon can be published.', 'NOT_A_DRAFT', status=409)
        missing = []
        if not hackathon.description.strip():
            missing.append('Write the event description and rules.')
        if not hackathon.allow_open_innovation and not hackathon.problem_statements.filter(is_active=True).exists():
            missing.append('Add at least one problem statement, or allow open innovation.')
        if hackathon.end_date <= timezone.now():
            missing.append('The end date is already in the past.')
        if missing:
            raise HackathonError(
                'Finish these before publishing: ' + ' '.join(missing), 'PUBLISH_CHECKLIST', items=missing,
            )
        return self._set(request, "Published Hackathon", status=HackathonStatus.LIVE)

    @action(detail=True, methods=['post'], url_path='close')
    def close(self, request, slug=None):
        """POST /api/hackathons/{slug}/close/ - marks a LIVE hackathon CLOSED (stops registration)."""
        if self.get_object().status != HackathonStatus.LIVE:
            raise HackathonError('Only a live hackathon can be closed.', 'NOT_LIVE', status=409)
        return self._set(request, "Closed Hackathon", status=HackathonStatus.CLOSED)

    @action(detail=True, methods=['post'], url_path='reopen')
    def reopen(self, request, slug=None):
        """POST /api/hackathons/{slug}/reopen/ - reverts a CLOSED hackathon back to LIVE."""
        if self.get_object().status == HackathonStatus.DRAFT:
            raise HackathonError('This hackathon is still a draft. Publish it instead.', 'IS_DRAFT', status=409)
        return self._set(request, "Reopened Hackathon", status=HackathonStatus.LIVE)

    @action(detail=True, methods=['post'], url_path='hide')
    def hide(self, request, slug=None):
        """POST /api/hackathons/{slug}/hide/ - removes the hackathon from the public list entirely."""
        return self._set(request, "Hid Hackathon From Public", hidden_at=timezone.now())

    @action(detail=True, methods=['post'], url_path='show')
    def show(self, request, slug=None):
        """POST /api/hackathons/{slug}/show/ - undoes `hide`. The visible_from/visible_until window is left as configured."""
        return self._set(request, "Made Hackathon Publicly Visible Again", hidden_at=None)


# ---------------------------------------------------------------------------
# Problem statements
# ---------------------------------------------------------------------------

def _ps_queryset(hackathon):
    return hackathon.problem_statements.annotate(
        active_team_count=Count('teams', filter=Q(teams__status__in=ACTIVE), distinct=True),
        total_team_count=Count('teams', distinct=True),
        # Explicit: Meta.ordering is dropped on aggregated querysets, which left the list unordered.
    ).order_by('order', 'id')


class ProblemStatementListView(APIView):
    """GET (public: active only; content managers: all) / POST (content managers) /api/hackathons/{slug}/problem-statements/"""
    permission_classes = [scoped_or_read_only('content')]

    def get(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        qs = _ps_queryset(hackathon)
        if not _can(hackathon, request.user, 'content'):
            if hackathon.problem_statements_reveal_at and hackathon.problem_statements_reveal_at > timezone.now():
                return DRFResponse([])
            qs = qs.filter(is_active=True)
        return DRFResponse(ProblemStatementSerializer(qs, many=True).data)

    def post(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        serializer = ProblemStatementSerializer(data=request.data, context={'hackathon': hackathon})
        serializer.is_valid(raise_exception=True)
        ps = ProblemStatementService.create(hackathon, request.user, **serializer.validated_data)
        return DRFResponse(ProblemStatementSerializer(_ps_queryset(hackathon).get(pk=ps.pk)).data,
                           status=status.HTTP_201_CREATED)


class ProblemStatementUploadView(APIView):
    """POST (admin) /api/hackathons/{slug}/problem-statements/upload/ - multipart `file`.

    CSV with title, description and domain columns; the application assigns each
    statement's ID. Responds with how many were created, skipped and which rows failed.
    """
    permission_classes = [scoped('content')]
    parser_classes = [MultiPartParser]
    MAX_BYTES = 1024 * 1024

    def post(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        upload = request.FILES.get('file')
        if upload is None:
            raise HackathonError('Attach a CSV file.', 'CSV_REQUIRED', field='file')
        if not upload.name.lower().endswith('.csv'):
            raise HackathonError('Only .csv files are accepted.', 'CSV_TYPE', field='file')
        if upload.size > self.MAX_BYTES:
            raise HackathonError('The file is too large (max 1 MB).', 'CSV_TOO_LARGE', field='file')
        result = ProblemStatementService.import_csv(hackathon, request.user, upload.read())
        return DRFResponse(result, status=status.HTTP_201_CREATED if result['created'] else status.HTTP_200_OK)


class ProblemStatementReorderView(APIView):
    """POST (admin) /api/hackathons/{slug}/problem-statements/reorder/ - body {ids: [every statement id, in the new order]}."""
    permission_classes = [scoped('content')]

    def post(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        ids = request.data.get('ids')
        if not isinstance(ids, list):
            raise HackathonError('ids must be a list.', 'INVALID', field='ids')
        ProblemStatementService.reorder(hackathon, request.user, [_int(i, 'ids') for i in ids])
        return DRFResponse(ProblemStatementSerializer(_ps_queryset(hackathon), many=True).data)


class ProblemStatementDetailView(APIView):
    """PATCH / DELETE (admin) /api/hackathons/{slug}/problem-statements/{id}/"""
    permission_classes = [scoped('content')]

    def _get(self, request, slug, pk):
        hackathon = _get_hackathon(request, slug)
        return hackathon, get_object_or_404(_ps_queryset(hackathon), pk=pk)

    def patch(self, request, slug, pk):
        hackathon, ps = self._get(request, slug, pk)
        serializer = ProblemStatementSerializer(ps, data=request.data, partial=True, context={'hackathon': hackathon})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        log_audit_event(actor=request.user, action="Updated Problem Statement", target_model="ProblemStatement",
                        target_id=ps.pk, details={"hackathon": slug, "fields": list(serializer.validated_data)})
        return DRFResponse(ProblemStatementSerializer(_ps_queryset(hackathon).get(pk=ps.pk)).data)

    def delete(self, request, slug, pk):
        hackathon, ps = self._get(request, slug, pk)
        code = ps.code
        force = str(request.query_params.get('force', 'false')).lower() in ('true', '1') or str(request.data.get('force', 'false')).lower() in ('true', '1')
        total_teams = ps.teams.count()
        if total_teams > 0 and not force:
            raise HackathonError(
                f'{total_teams} team(s) have picked this problem statement. Deactivate it or confirm force deletion to unassign them.',
                'PROBLEM_STATEMENT_IN_USE',
            )
        if total_teams > 0:
            ps.teams.update(problem_statement=None)
        ps.delete()
        data = {'deleted': True, 'id': pk}
        if total_teams > 0:
            data['teams_unassigned'] = total_teams
        return DRFResponse(data)


# ---------------------------------------------------------------------------
# Participant: my team, team creation, lookup, invites
# ---------------------------------------------------------------------------

def _round_payload(round_obj, entry, *, viewer_is_leader, user):
    published = round_obj.results_published
    data = {
        'id': round_obj.id, 'order': round_obj.order, 'name': round_obj.name,
        'description': round_obj.description, 'starts_at': round_obj.starts_at,
        'ends_at': round_obj.ends_at, 'status': round_obj.status, 'kind': round_obj.kind,
        'effective_status': effective_round_status(round_obj),
        'results_published': published, 'entry': None, 'details_form': None,
    }
    if entry is not None:
        data['entry'] = {
            'status': entry.status if published else EntryStatus.PENDING,
            'feedback': entry.feedback if published else '',
            'details_submitted': entry.details_response_id is not None,
        }
        form = round_obj.details_form
        if form is not None and published and entry.status == EntryStatus.SHORTLISTED:
            data['details_form'] = {
                'slug': form.slug, 'title': form.title, 'status': form.status,
                'open_at': form.open_at, 'close_at': form.close_at,
                'can_submit': viewer_is_leader and round_entry_for_details_form(form, user) is not None,
            }
    return data


class HackathonStagesView(APIView):
    """GET /api/hackathons/{slug}/stages/ - the visible entries of the public timeline (no auth)."""
    permission_classes = [permissions.AllowAny]

    def get(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        return DRFResponse(PublicStageSerializer(hackathon.stages.filter(is_visible=True), many=True).data)


class StageListView(APIView):
    """GET / POST (admin) /api/hackathons/{slug}/timeline/ - every timeline entry, hidden ones included."""
    permission_classes = [scoped('content')]

    def get(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        return DRFResponse(AdminStageSerializer(hackathon.stages.select_related('source_round'), many=True).data)

    def post(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        serializer = AdminStageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        stage = StageService.create(hackathon, request.user, **serializer.validated_data)
        return DRFResponse(AdminStageSerializer(stage).data, status=status.HTTP_201_CREATED)


class StageDetailView(APIView):
    """PATCH / DELETE (admin) /api/hackathons/{slug}/timeline/{id}/"""
    permission_classes = [scoped('content')]

    def _get(self, request, slug, pk):
        hackathon = _get_hackathon(request, slug)
        return hackathon, get_object_or_404(hackathon.stages.select_related('source_round'), pk=pk)

    def patch(self, request, slug, pk):
        _, stage = self._get(request, slug, pk)
        serializer = AdminStageSerializer(stage, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        log_audit_event(actor=request.user, action='Updated Hackathon Stage', target_model='HackathonStage',
                        target_id=stage.pk, details={'fields': list(serializer.validated_data)})
        return DRFResponse(AdminStageSerializer(stage).data)

    def delete(self, request, slug, pk):
        _, stage = self._get(request, slug, pk)
        title = stage.title
        stage.delete()
        log_audit_event(actor=request.user, action='Deleted Hackathon Stage', target_model='HackathonStage',
                        target_id=pk, details={'title': title})
        return DRFResponse({'deleted': True, 'id': pk})


class StageReorderView(APIView):
    """POST (admin) /api/hackathons/{slug}/timeline/reorder/ - {ids: [every stage id, in the new order]}."""
    permission_classes = [scoped('content')]

    def post(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        ids = request.data.get('ids')
        if not isinstance(ids, list):
            raise HackathonError('ids must be a list.', 'INVALID', field='ids')
        StageService.reorder(hackathon, request.user, [_int(i, 'ids') for i in ids])
        return DRFResponse(AdminStageSerializer(hackathon.stages.select_related('source_round'), many=True).data)


class StageImportRoundsView(APIView):
    """POST (admin) /api/hackathons/{slug}/timeline/import-rounds/ - add a stage for every round not yet in the timeline."""
    permission_classes = [scoped('content')]

    def post(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        created = StageService.import_rounds(hackathon, request.user)
        return DRFResponse({
            'created': created,
            'stages': AdminStageSerializer(hackathon.stages.select_related('source_round'), many=True).data,
        })


def _withdrawal_payload(team):
    """The latest withdrawal request on a team, for the leader's dashboard (None when there never was one)."""
    if team is None:
        return None
    request = withdrawals.WithdrawalService.latest_for(team)
    if request is None or request.status == WithdrawalStatus.CANCELLED:
        return None
    return {
        'id': request.id, 'status': request.status, 'reason': request.reason, 'created_at': request.created_at,
        'decision_note': request.decision_note, 'decided_at': request.decided_at,
    }


class MyTeamView(APIView):
    """GET /api/hackathons/{slug}/my-team/ - everything the participant dashboard needs."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        user = request.user
        membership = membership_of(hackathon, user)
        team = membership.team if membership else None

        invites = TeamInvite.objects.filter(
            hackathon=hackathon, invited_user=user, status=InviteStatus.PENDING,
        ).select_related('team__problem_statement', 'invited_by', 'invited_user', 'hackathon')

        rounds = []
        if team is not None:
            entries = {e.round_id: e for e in team.round_entries.all()}
            leader = team.leader_id == user.id
            for r in hackathon.rounds.select_related('details_form').all():
                rounds.append(_round_payload(r, entries.get(r.id), viewer_is_leader=leader, user=user))
        # Someone with no team has no business seeing round names and descriptions.

        return DRFResponse({
            'hackathon': HackathonSerializer(hackathon).data,
            'profile_missing': missing_profile_fields(hackathon, user),
            'registration': {
                'rules_accepted': membership.rules_accepted_at is not None,
                'answers': membership.answers,
                'missing': registration.missing_registration_details(hackathon, membership),
            } if membership else None,
            'team': TeamSerializer(team).data if team else None,
            'payment': payments.summary(team) if team else None,
            'withdrawal_request': _withdrawal_payload(team),
            'team_limits': {
                'max_teams': withdrawals.MAX_SELF_CREATED_TEAMS,
                'cooldown_minutes': int(withdrawals.WITHDRAW_COOLDOWN.total_seconds() // 60),
            },
            'is_leader': bool(team and team.leader_id == user.id),
            'invites': TeamInviteSerializer(invites, many=True).data,
            'rounds': rounds,
        })


class HackathonTeamsView(APIView):
    """
    POST /api/hackathons/{slug}/teams/ - create a team (any authenticated user).
    GET  /api/hackathons/{slug}/teams/ - admin: every team with contact details.
         Filters: ?status=, ?problem_statement=, ?round=&entry_status=, ?search=
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        if not access.has_any_permission(hackathon, request.user, ('teams', 'rounds', 'results')):
            raise PermissionDenied('You are not allowed to list every team.')
        qs = _team_queryset().filter(hackathon=hackathon).prefetch_related(
            Prefetch('memberships', queryset=TeamMember.objects.select_related('user', 'user__college')),
            Prefetch('email_invites', queryset=TeamEmailInvite.objects.all()),
            Prefetch('invites', queryset=TeamInvite.objects.select_related('invited_user', 'invited_by', 'hackathon', 'team')),
            Prefetch('round_entries', queryset=RoundEntry.objects.select_related('round')),
        )
        params = request.query_params
        if params.get('status'):
            qs = qs.filter(status=params['status'])
        if params.get('problem_statement') == 'open_innovation':
            qs = qs.filter(is_open_innovation=True)
        elif params.get('problem_statement'):
            qs = qs.filter(problem_statement_id=params['problem_statement'])
        if params.get('round'):
            qs = qs.filter(round_entries__round_id=params['round'])
            if params.get('entry_status'):
                qs = qs.filter(round_entries__status=params['entry_status'])
        if params.get('search'):
            q = params['search'].strip()
            qs = qs.filter(
                Q(name__icontains=q) | Q(memberships__user__email__icontains=q)
                | Q(memberships__user__first_name__icontains=q) | Q(memberships__user__last_name__icontains=q)
            )
        qs = qs.distinct().order_by('-created_at')
        return DRFResponse(AdminTeamSerializer(qs, many=True).data)

    def post(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        _, ps, open_innovation = _parse_problem_choice(hackathon, request.data)
        leader = request.user
        if _can(hackathon, request.user, 'teams') and request.data.get('leader_email'):
            leader_email = request.data['leader_email'].strip()
            from django.contrib.auth import get_user_model
            UserModel = get_user_model()
            leader_user = UserModel.objects.filter(email__iexact=leader_email).first()
            if not leader_user:
                raise HackathonError(f'No account found with email {leader_email}.', 'USER_NOT_FOUND', field='leader_email')
            leader = leader_user
        team = TeamService.create_team(
            hackathon, leader, request.data.get('name', ''), ps, open_innovation, actor=request.user,
            registration_data=request.data,
        )
        return DRFResponse(TeamSerializer(team).data, status=status.HTTP_201_CREATED)


class UserLookupView(APIView):
    """
    GET /api/hackathons/{slug}/user-lookup/
    Supports:
    - ?email=... : exact-email teammate lookup (legacy/strict mode). Requires full email with '@'.
    - ?q=...     : prefix or partial search (email, name, club_id) for instant autocomplete as user types.
    - ?team_id=...: optional target team ID to check invite eligibility against.
    Returns only name/email/club ID plus whether the team can invite them;
    never phone, roll number or other private profile data. Throttled.

    Only admins and the leader of the target team may search: this is a member
    directory, so it is not offered to every signed-in account.
    """
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'hackathon_lookup'

    def get(self, request, slug):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        hackathon = _get_hackathon(request, slug)
        email = (request.query_params.get('email') or '').strip()
        q = (request.query_params.get('q') or '').strip()
        team_id = request.query_params.get('team_id')

        # Determine target team for eligibility checking
        target_team = None
        if team_id:
            target_team = Team.objects.filter(hackathon=hackathon, id=team_id).first()
        if target_team is None:
            membership = membership_of(hackathon, request.user)
            if membership is not None:
                target_team = membership.team

        is_admin = _can(hackathon, request.user, 'teams')
        if not is_admin and not (target_team is not None and target_team.leader_id == request.user.id):
            raise PermissionDenied('Only a team leader can search for teammates.')

        def _evaluate(u):
            if target_team is not None:
                can_invite, reason = TeamService.invite_eligibility(target_team, u)
            else:
                if TeamMember.objects.filter(hackathon=hackathon, user=u).exists():
                    can_invite, reason = False, 'Already in another team for this hackathon.'
                else:
                    can_invite, reason = True, ''

            name = f"{u.first_name} {u.last_name}".strip() or u.username
            return {
                'found': True,
                'id': u.id,
                'name': name,
                'email': u.email,
                'club_id': u.club_id,
                'can_invite': can_invite,
                'reason': reason,
            }

        # If email param is explicitly provided, enforce strict email contract (for existing tests & direct lookup)
        if email:
            if '@' not in email:
                raise HackathonError('Enter a full email address.', 'EMAIL_REQUIRED', field='email')
            user = User.objects.filter(email__iexact=email).first()
            if user is not None and email_verification.needs_verified_account(user):
                user = None  # an unproven address is not matched: anyone could have signed up with someone else's
            if user is None:
                return DRFResponse({
                    'found': False,
                    'can_invite': False,
                    'reason': 'No SRKRCC account uses that email. Ask them to sign up first.',
                    'results': [],
                })
            res = _evaluate(user)
            item = dict(res)
            res['results'] = [item]
            return DRFResponse(res)

        # Autocomplete search via ?q=
        if q and len(q) < LOOKUP_MIN_QUERY_LENGTH:
            return DRFResponse({
                'found': False,
                'can_invite': False,
                'reason': f'Type at least {LOOKUP_MIN_QUERY_LENGTH} characters.',
                'results': [],
            })
        if q:
            users = User.objects.filter(
                Q(email__icontains=q) |
                Q(first_name__icontains=q) |
                Q(last_name__icontains=q) |
                Q(club_id__icontains=q)
            ).distinct()
            if email_verification.mode_at_least('features'):
                users = users.filter(email_verified_at__isnull=False)
            users = users[:LOOKUP_MAX_RESULTS]

            results = [_evaluate(u) for u in users]
            top = dict(results[0]) if results else {
                'found': False,
                'can_invite': False,
                'reason': f'No members found matching "{q}".',
            }
            top['results'] = results
            return DRFResponse(top)

        return DRFResponse({
            'found': False,
            'can_invite': False,
            'reason': 'Enter a search term.',
            'results': [],
        })


class MyInvitesView(APIView):
    """GET /api/hackathons/my-invites/ - the caller's pending invites across all hackathons."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        invites = TeamInvite.objects.filter(
            invited_user=request.user, status=InviteStatus.PENDING,
        ).select_related('team__problem_statement', 'invited_by', 'invited_user', 'hackathon')
        return DRFResponse(TeamInviteSerializer(invites, many=True).data)


class MyTeamsView(APIView):
    """GET /api/hackathons/my-teams/ - every hackathon team the caller belongs to (profile page)."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        teams = _team_queryset().filter(memberships__user=request.user).order_by('-created_at')
        data = []
        for team in teams:
            item = TeamSerializer(team).data
            item['hackathon_title'] = team.hackathon.title
            item['is_leader'] = team.leader_id == request.user.id
            data.append(item)
        return DRFResponse(data)


class InviteRespondView(APIView):
    """POST /api/hackathons/invites/{id}/accept|decline/"""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk, decision):
        if decision not in ('accept', 'decline'):
            from django.http import Http404
            raise Http404
        invite = get_object_or_404(TeamInvite.objects.select_related('team__hackathon'), pk=pk)
        if invite.invited_user_id != request.user.id:
            raise PermissionDenied('This invite is not for you.')
        if decision == 'accept':
            team = TeamService.accept_invite(invite, request.user, registration_data=request.data)
            return DRFResponse({'accepted': True, 'team': TeamSerializer(team).data,
                                'hackathon_slug': team.hackathon.slug})
        invite = TeamService.decline_invite(invite, request.user)
        return DRFResponse({'declined': True, 'invite': TeamInviteSerializer(invite).data})


# ---------------------------------------------------------------------------
# Team actions (leader / member / admin)
# ---------------------------------------------------------------------------

class TeamDetailView(APIView):
    """GET (members/admin) / PATCH (leader/admin) /api/hackathons/teams/{id}/"""
    permission_classes = [permissions.IsAuthenticated]

    def _get(self, request, pk):
        team = get_object_or_404(_team_queryset(), pk=pk)
        if not (is_member(team, request.user) or _can(team.hackathon, request.user, 'teams')):
            # 404, not 403: don't confirm another team's existence to outsiders.
            from django.http import Http404
            raise Http404
        return team

    def _serialize(self, request, team):
        cls = AdminTeamSerializer if _can(team.hackathon, request.user, 'teams') else TeamSerializer
        return cls(_team_queryset().get(pk=team.pk)).data

    def get(self, request, pk):
        return DRFResponse(self._serialize(request, self._get(request, pk)))

    def patch(self, request, pk):
        team = self._get(request, pk)
        kwargs = {}
        if 'name' in request.data:
            kwargs['name'] = request.data.get('name') or ''
        present, ps, open_innovation = _parse_problem_choice(team.hackathon, request.data)
        if present:
            kwargs.update(change_problem=True, problem_statement=ps, open_innovation=open_innovation)
        if 'looking_for_members' in request.data or 'looking_for' in request.data:
            kwargs['recruiting'] = {
                'looking_for_members': request.data.get('looking_for_members', team.looking_for_members),
                'looking_for': request.data.get('looking_for', team.looking_for),
            }
        TeamService.update_team(team, request.user, **kwargs)
        return DRFResponse(self._serialize(request, team))

    def delete(self, request, pk):
        team = get_object_or_404(_team_queryset(), pk=pk)
        if not _can(team.hackathon, request.user, 'teams'):
            raise PermissionDenied('You are not allowed to delete teams in this hackathon.')
        team_id = team.pk
        team_name = team.name
        slug = team.hackathon.slug
        hackathon = team.hackathon
        team.delete()
        WaitlistService.promote(hackathon)
        log_audit_event(actor=request.user, action="Deleted Hackathon Team", target_model="HackathonTeam",
                        target_id=team_id, details={"hackathon": slug, "name": team_name})
        return DRFResponse({'deleted': True, 'id': team_id})


class TeamEmailInviteView(APIView):
    """POST /api/hackathons/teams/{id}/invite-by-email/ {email} - leader. Throttled: it sends mail to arbitrary addresses."""
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'hackathon_email_invite'

    def post(self, request, pk):
        team = get_object_or_404(_team_queryset(), pk=pk)
        if not (is_member(team, request.user) or _can(team.hackathon, request.user, 'teams')):
            from django.http import Http404
            raise Http404
        invite = TeamService.invite_by_email(team, request.user, request.data.get('email', ''))
        return DRFResponse(
            {'invite': {'id': invite.id, 'email': invite.email, 'created_at': invite.created_at},
             'team': TeamSerializer(_team_queryset().get(pk=team.pk)).data},
            status=status.HTTP_201_CREATED,
        )


class TeamActionView(APIView):
    """
    POST /api/hackathons/teams/{id}/{action}/ where action is one of:
      invite              {email}          leader
      cancel-invite       {invite_id}      leader
      remove-member       {user_id}        leader / admin
      transfer-leadership {user_id}        leader / admin
      leave               {reason}         member (the sole leader withdraws the team: a reason is required and a team that holds a place needs approval)
      cancel-withdrawal   -                leader
      cancel-payment      -                leader (a team still waiting for its payment check is deleted)
      admin-add-member    {email}          admin
      admin-set-status    {status}         admin
    """
    permission_classes = [permissions.IsAuthenticated]
    ADMIN_ONLY = {'admin-add-member', 'admin-set-status'}

    def post(self, request, pk, team_action):
        team = get_object_or_404(_team_queryset(), pk=pk)
        user = request.user
        admin = _can(team.hackathon, user, 'teams')
        if team_action in self.ADMIN_ONLY and not admin:
            raise PermissionDenied('Admins only.')
        if not admin and not is_member(team, user):
            from django.http import Http404
            raise Http404

        data = request.data
        if team_action == 'invite':
            invite = TeamService.invite(team, user, data.get('email', ''))
            return DRFResponse({'invite': TeamInviteSerializer(invite).data,
                                'team': TeamSerializer(_team_queryset().get(pk=team.pk)).data},
                               status=status.HTTP_201_CREATED)
        if team_action == 'cancel-invite':
            invite = get_object_or_404(team.invites, pk=_int(data.get('invite_id'), 'invite_id'))
            TeamService.cancel_invite(invite, user)
        elif team_action == 'cancel-email-invite':
            invite = get_object_or_404(team.email_invites, pk=_int(data.get('invite_id'), 'invite_id'))
            TeamService.cancel_email_invite(invite, user)
        elif team_action == 'remove-member':
            TeamService.remove_member(team, user, _int(data.get('user_id'), 'user_id'))
        elif team_action == 'transfer-leadership':
            TeamService.transfer_leadership(team, user, _int(data.get('user_id'), 'user_id'))
        elif team_action == 'leave':
            _, request = TeamService.leave_team(team, user, data.get('reason', ''))
            if request is not None:
                return DRFResponse({'left': False, 'withdrawal_requested': True, 'hackathon_slug': team.hackathon.slug})
            return DRFResponse({'left': True, 'withdrawal_requested': False, 'hackathon_slug': team.hackathon.slug})
        elif team_action == 'cancel-payment':
            payments.cancel_pending(team, user)
            return DRFResponse({'cancelled': True, 'hackathon_slug': team.hackathon.slug})
        elif team_action == 'cancel-withdrawal':
            if not is_leader(team, user):
                raise PermissionDenied('Only the team leader can do this.')
            withdrawals.WithdrawalService.cancel(team, user)
        elif team_action == 'admin-add-member':
            TeamService.admin_add_member(team, user, data.get('email', ''))
        elif team_action == 'admin-set-status':
            TeamService.admin_set_status(team, user, data.get('status', ''), data.get('reason', ''))
        else:
            from django.http import Http404
            raise Http404

        cls = AdminTeamSerializer if admin else TeamSerializer
        return DRFResponse(cls(_team_queryset().get(pk=team.pk)).data)


# ---------------------------------------------------------------------------
# Rounds (admin)
# ---------------------------------------------------------------------------

class RoundListView(APIView):
    """GET / POST (admin) /api/hackathons/{slug}/rounds/"""
    permission_classes = [scoped('rounds')]

    def get(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        return DRFResponse(RoundSerializer(hackathon.rounds.select_related('details_form'), many=True).data)

    def post(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        serializer = RoundSerializer(data=request.data, context={'hackathon': hackathon})
        serializer.is_valid(raise_exception=True)
        populate = str(request.data.get('populate', 'true')).lower() != 'false'
        round_obj = RoundService.create_round(hackathon, request.user, populate=populate, **serializer.validated_data)
        return DRFResponse(RoundSerializer(round_obj).data, status=status.HTTP_201_CREATED)


class RoundReorderView(APIView):
    """POST (rounds) /api/hackathons/{slug}/rounds/reorder/ {ids: [...]} - every round once, in the new order."""
    permission_classes = [scoped('rounds')]

    def post(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        ids = request.data.get('ids')
        if not isinstance(ids, list):
            raise HackathonError('Send the round ids in order.', 'INVALID', field='ids')
        RoundService.reorder(hackathon, request.user, [_int(i, 'ids') for i in ids])
        return DRFResponse(RoundSerializer(hackathon.rounds.select_related('details_form'), many=True).data)


class RoundDetailView(APIView):
    """PATCH / DELETE (admin) /api/hackathons/{slug}/rounds/{id}/"""
    permission_classes = [scoped('rounds')]

    def _get(self, request, slug, pk):
        hackathon = _get_hackathon(request, slug)
        return hackathon, get_object_or_404(hackathon.rounds, pk=pk)

    def patch(self, request, slug, pk):
        hackathon, round_obj = self._get(request, slug, pk)
        serializer = RoundSerializer(round_obj, data=request.data, partial=True, context={'hackathon': hackathon})
        serializer.is_valid(raise_exception=True)
        round_obj = serializer.save()
        if 'details_form' in serializer.validated_data and round_obj.details_form_id:
            from .services import _prepare_details_form
            _prepare_details_form(round_obj.details_form)
        log_audit_event(actor=request.user, action="Updated Hackathon Round", target_model="HackathonRound",
                        target_id=round_obj.pk, details={"fields": list(serializer.validated_data)})
        return DRFResponse(RoundSerializer(round_obj).data)

    def delete(self, request, slug, pk):
        hackathon, round_obj = self._get(request, slug, pk)
        if hackathon.rounds.filter(order__gt=round_obj.order).exists():
            raise HackathonError('Delete later rounds first, since they depend on this round\'s shortlist.', 'ROUND_HAS_LATER')
        RoundService.delete_round(round_obj, request.user)
        return DRFResponse({'deleted': True, 'id': pk})


def round_entries_queryset(round_obj, params):
    """A round's entries with everything the organizer table shows; ``params`` may carry ?status= and ?search=."""
    hackathon = round_obj.hackathon
    next_round = hackathon.rounds.filter(order__gt=round_obj.order).order_by('order').first()
    promoted = (
        Exists(RoundEntry.objects.filter(round_id=next_round.id, team_id=OuterRef('team_id')))
        if next_round else Value(False, output_field=BooleanField())
    )
    qs = round_obj.entries.select_related(
        'team__leader', 'team__problem_statement', 'decided_by', 'team__idea', 'team__submission',
    ).annotate(
        member_total=Count('team__memberships', distinct=True), promoted_total=promoted,
        checked_in_total=Count('team__check_ins', filter=Q(team__check_ins__session__is_main=True), distinct=True),
    )
    if params.get('status'):
        qs = qs.filter(status=params['status'])
    if params.get('search'):
        q = params['search'].strip()
        qs = qs.filter(Q(team__name__icontains=q) | Q(team__memberships__user__email__icontains=q)).distinct()
    return qs


class RoundEntriesView(APIView):
    """GET (admin) /api/hackathons/{slug}/rounds/{id}/entries/?status=&search="""
    permission_classes = [scoped('rounds')]

    def get(self, request, slug, pk):
        hackathon = _get_hackathon(request, slug)
        round_obj = get_object_or_404(hackathon.rounds, pk=pk)
        return DRFResponse(AdminRoundEntrySerializer(round_entries_queryset(round_obj, request.query_params), many=True).data)


class RoundActionView(APIView):
    """
    POST (admin) /api/hackathons/{slug}/rounds/{id}/{action}/:
      decide     {team_ids: [], status, feedback?, admin_notes?}
      publish    {announce?: bool, message?: str}
      unpublish
      populate
    """
    permission_classes = [scoped('rounds')]

    def post(self, request, slug, pk, round_action):
        hackathon = _get_hackathon(request, slug)
        round_obj = get_object_or_404(hackathon.rounds, pk=pk)
        data = request.data
        result = {}
        if round_action == 'decide':
            team_ids = data.get('team_ids') or []
            if not isinstance(team_ids, list) or not team_ids:
                raise HackathonError('Select at least one team.', 'TEAMS_REQUIRED', field='team_ids')
            team_ids = [_int(t, 'team_ids') for t in team_ids]
            result['updated'] = RoundService.decide(
                round_obj, request.user, team_ids, data.get('status', ''),
                feedback=data.get('feedback'), admin_notes=data.get('admin_notes'),
                confirm_published=bool(data.get('confirm_published')),
                confirm_over_limit=bool(data.get('confirm_over_limit')),
            )
        elif round_action == 'auto-shortlist':
            result.update(RoundService.auto_shortlist(
                round_obj, request.user, str(data.get('rule', '')), data.get('value'),
                reject_rest=bool(data.get('reject_rest')), apply=bool(data.get('apply')),
                confirm_published=bool(data.get('confirm_published')), confirm_over_limit=bool(data.get('confirm_over_limit')),
            ))
        elif round_action == 'publish':
            RoundService.publish_results(
                round_obj, request.user, announce=bool(data.get('announce')), message=data.get('message', ''),
                email=bool(data.get('email')),
                announce_rejected=bool(data.get('announce_rejected')),
                rejected_message=data.get('rejected_message', ''),
            )
        elif round_action == 'unpublish':
            RoundService.unpublish_results(round_obj, request.user)
        elif round_action == 'populate':
            result['added'] = RoundService.populate_entries(round_obj, request.user)
        elif round_action == 'advance':
            next_round, promoted, already_in_round = RoundService.advance_shortlisted(round_obj, request.user)
            result['promoted'] = promoted
            result['already_in_round'] = already_in_round
            result['target_round'] = RoundSerializer(next_round).data
        elif round_action == 'add-team':
            team_id = _int(data.get('team_id'), 'team_id')
            team = get_object_or_404(hackathon.teams, pk=team_id)
            entry, added = RoundService.add_team_entry(round_obj, team, request.user)
            result['added'] = added
            result['entry_id'] = entry.pk
        elif round_action == 'remove-team':
            team_id = _int(data.get('team_id'), 'team_id')
            team = get_object_or_404(hackathon.teams, pk=team_id)
            removed = RoundService.remove_team_entry(round_obj, team, request.user)
            result['removed'] = removed
        else:
            from django.http import Http404
            raise Http404
        round_obj.refresh_from_db()
        return DRFResponse({**result, 'round': RoundSerializer(round_obj).data})


# ---------------------------------------------------------------------------
# Announcements
# ---------------------------------------------------------------------------

class AnnouncementListView(APIView):
    """
    GET  /api/hackathons/{slug}/announcements/ - what the caller may see
         (anonymous: public only). Admins add ?all=true for every announcement.
    POST (admin) - create; send_email=true emails the audience.
    """
    permission_classes = [scoped_or_read_only('content')]

    def get(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        # No scheduler runs here, so scheduled emails go out the first time the
        # hackathon's announcements are read after their publish time.
        AnnouncementService.send_due_emails(hackathon)
        if request.query_params.get('all') == 'true' and _can(hackathon, request.user, 'content'):
            qs = hackathon.announcements.select_related('round', 'created_by').prefetch_related('target_teams')
            return DRFResponse(AdminHackathonAnnouncementSerializer(qs, many=True).data)
        qs = AnnouncementService.visible_for(hackathon, request.user).select_related('round')
        return DRFResponse(HackathonAnnouncementSerializer(qs, many=True).data)

    def post(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        serializer = AdminHackathonAnnouncementSerializer(data=request.data, context={'hackathon': hackathon})
        serializer.is_valid(raise_exception=True)
        target_ids = [_int(t, 'target_teams') for t in (request.data.get('target_teams') or [])]
        announcement = AnnouncementService.create(hackathon, request.user, target_team_ids=target_ids,
                                                  **serializer.validated_data)
        return DRFResponse(AdminHackathonAnnouncementSerializer(announcement).data, status=status.HTTP_201_CREATED)


class AnnouncementDetailView(APIView):
    """PATCH / DELETE (admin) /api/hackathons/{slug}/announcements/{id}/"""
    permission_classes = [scoped('content')]

    def _get(self, request, slug, pk):
        hackathon = _get_hackathon(request, slug)
        return hackathon, get_object_or_404(hackathon.announcements, pk=pk)

    def patch(self, request, slug, pk):
        hackathon, announcement = self._get(request, slug, pk)
        serializer = AdminHackathonAnnouncementSerializer(
            announcement, data=request.data, partial=True, context={'hackathon': hackathon},
        )
        serializer.is_valid(raise_exception=True)
        target_ids = None
        if 'target_teams' in request.data:
            target_ids = [_int(t, 'target_teams') for t in (request.data.get('target_teams') or [])]
        audience = serializer.validated_data.get('audience', announcement.audience)
        round_obj = serializer.validated_data['round'] if 'round' in serializer.validated_data else announcement.round
        AnnouncementService.validate(
            audience, round_obj,
            target_ids if target_ids is not None else list(announcement.target_teams.values_list('id', flat=True)),
            hackathon,
        )
        announcement = serializer.save()
        if target_ids is not None:
            announcement.target_teams.set(target_ids)
        log_audit_event(actor=request.user, action="Updated Hackathon Announcement",
                        target_model="HackathonAnnouncement", target_id=announcement.pk,
                        details={"fields": list(serializer.validated_data)})
        return DRFResponse(AdminHackathonAnnouncementSerializer(announcement).data)

    def delete(self, request, slug, pk):
        hackathon, announcement = self._get(request, slug, pk)
        title = announcement.title
        announcement.delete()
        log_audit_event(actor=request.user, action="Deleted Hackathon Announcement",
                        target_model="HackathonAnnouncement", target_id=pk, details={"title": title})
        return DRFResponse({'deleted': True, 'id': pk})


class AnnouncementNotifyView(APIView):
    """POST (admin) /api/hackathons/{slug}/announcements/{id}/notify/ - (re)send the email."""
    permission_classes = [scoped('content')]

    def post(self, request, slug, pk):
        hackathon = _get_hackathon(request, slug)
        announcement = get_object_or_404(hackathon.announcements, pk=pk)
        count = AnnouncementService.notify(announcement)
        log_audit_event(actor=request.user, action="Emailed Hackathon Announcement",
                        target_model="HackathonAnnouncement", target_id=pk, details={"recipients": count})
        return DRFResponse({'recipients': count})


# ---------------------------------------------------------------------------
# Stats (admin)
# ---------------------------------------------------------------------------

class TeamEmailSettingsView(APIView):
    """
    GET/PUT (admin) /api/hackathons/{slug}/team-emails/ - the team lifecycle emails and their wording.

    PUT {emails: {<key>: {enabled, subject, body}}} replaces the overrides; a blank
    subject/body means "use the built-in wording".
    """
    permission_classes = [scoped('content')]

    @staticmethod
    def _payload(hackathon):
        return {
            'emails': team_emails.describe_settings(hackathon),
            'placeholders': [{'key': k, 'description': d} for k, d in team_emails.PLACEHOLDERS.items()],
        }

    def get(self, request, slug):
        return DRFResponse(self._payload(_get_hackathon(request, slug)))

    def put(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        try:
            hackathon.team_email_config = team_emails.normalize_config(request.data.get('emails'))
        except team_emails.TeamEmailConfigError as exc:
            raise HackathonError(str(exc), 'INVALID_EMAIL_SETTINGS', field='emails')
        hackathon.save(update_fields=['team_email_config', 'updated_at'])
        log_audit_event(actor=request.user, action="Updated Hackathon Team Emails", target_model="Hackathon",
                        target_id=str(hackathon.pk), details={"hackathon": hackathon.slug})
        return DRFResponse(self._payload(hackathon))


class TeamEmailTestView(APIView):
    """POST (admin) /api/hackathons/{slug}/team-emails/test/ {email, subject?, body?} - send a sample to yourself."""
    permission_classes = [scoped('content')]

    def post(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        key = request.data.get('email')
        if key not in team_emails.EVENTS:
            raise HackathonError('Choose which email to test.', 'INVALID_EMAIL_TYPE', field='email')
        try:
            clean = team_emails.normalize_config({key: {
                'subject': request.data.get('subject'), 'body': request.data.get('body'),
            }})[key]
        except team_emails.TeamEmailConfigError as exc:
            raise HackathonError(str(exc), 'INVALID_EMAIL_SETTINGS', field='body')
        _, default_subject, default_body = team_emails.event_settings(hackathon, key)
        sent = team_emails.send_test(
            hackathon, key, request.user,
            clean['subject'] or default_subject, clean['body'] or default_body,
        )
        if not sent:
            raise HackathonError('The test email could not be sent. Check the email configuration.', 'EMAIL_SEND_FAILED')
        return DRFResponse({'sent_to': request.user.email})


class FormingTeamsReminderView(APIView):
    """POST (admin) /api/hackathons/{slug}/remind-forming/ - nudge leaders of teams below the minimum size."""
    permission_classes = [scoped('teams')]

    def post(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        return DRFResponse({'reminded': TeamService.remind_forming_teams(hackathon, request.user)})


def stats_payload(hackathon):
    """The Overview's team, statement and round numbers; shared by /stats/ and /overview/."""
    teams = hackathon.teams.all()
    by_status = {s: 0 for s in TeamStatus.values}
    for s in teams.values_list('status', flat=True):
        by_status[s] += 1
    participants = TeamMember.objects.filter(hackathon=hackathon, team__status__in=ACTIVE).count()
    per_ps = [
        {'id': ps.id, 'code': ps.code, 'title': ps.title, 'domain': ps.domain, 'teams': ps.active_team_count, 'max_teams': ps.max_teams}
        for ps in _ps_queryset(hackathon)
    ]
    rounds = [
        {'id': r.id, 'order': r.order, 'name': r.name, 'results_published': r.results_published,
         **RoundSerializer().get_entry_counts(r)}
        for r in hackathon.rounds.all()
    ]
    return {
        'teams_by_status': by_status,
        'teams_total': sum(by_status.values()),
        'participants': participants,
        'pending_invites': hackathon.team_invites.filter(status=InviteStatus.PENDING).count(),
        'problem_statements': per_ps,
        'open_innovation_teams': hackathon.teams.filter(is_open_innovation=True, status__in=ACTIVE).count(),
        'rounds': rounds,
        'forming_teams': [
            {
                'id': team.id, 'name': team.name, 'leader_email': team.leader.email if team.leader else None,
                'members': team.member_total, 'needed': max(hackathon.min_team_size - team.member_total, 1),
            }
            for team in TeamService.forming_teams(hackathon)[:50]
        ],
        'min_team_size': hackathon.min_team_size,
        'registration_closes_at': hackathon.registration_closes_at,
        'is_registration_open': hackathon.is_registration_open,
    }


class HackathonStatsView(APIView):
    """GET (admin) /api/hackathons/{slug}/stats/"""
    permission_classes = [scoped('insights')]

    def get(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        return DRFResponse(stats_payload(hackathon))


# ---------------------------------------------------------------------------
# Legacy project submissions
# ---------------------------------------------------------------------------

class SubmissionViewSet(viewsets.ModelViewSet):
    serializer_class = SubmissionSerializer
    permission_classes = [permissions.IsAuthenticated, IsSubmissionTeamMemberOrAdmin]

    def get_queryset(self):
        qs = Submission.objects.select_related('team')
        if _is_admin_or_club_lead(self.request.user):
            return qs
        return qs.filter(team__memberships__user=self.request.user)

    def perform_create(self, serializer):
        team = serializer.validated_data.get('team')
        user = self.request.user
        if team and not _is_admin_or_club_lead(user) and not is_leader(team, user):
            raise PermissionDenied("Only the team leader can submit for this team.")
        serializer.save()


# ---------------------------------------------------------------------------
# Direct Image Uploads for Hackathon Assets (Banner & Logo)
# ---------------------------------------------------------------------------

class HackathonAssetUploadView(APIView):
    """
    Direct file upload for hackathon banners and logos.
    Validates image binary integrity with Pillow, extracts natural width and height,
    saves the file to MEDIA_ROOT/hackathons/assets/, and returns the URL and image dimensions.
    """
    permission_classes = [permissions.IsAuthenticated, IsAdminOrClubLead]
    parser_classes = [MultiPartParser, FormParser]

    ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.svg'}
    MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

    def post(self, request, slug=None):
        file_obj = request.FILES.get('file')
        if not file_obj:
            return DRFResponse({'error': 'No file uploaded.'}, status=status.HTTP_400_BAD_REQUEST)

        if file_obj.size > self.MAX_SIZE_BYTES:
            return DRFResponse({'error': 'Image file size exceeds the 10 MB limit.'}, status=status.HTTP_400_BAD_REQUEST)

        name = file_obj.name
        ext = os.path.splitext(name)[1].lower()
        if ext not in self.ALLOWED_EXTENSIONS:
            return DRFResponse(
                {'error': f'Unsupported file extension "{ext}". Allowed extensions: .png, .jpg, .jpeg, .webp, .svg.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        width, height = None, None
        img_format = None

        if ext == '.svg':
            content = file_obj.read()
            file_obj.seek(0)
            if b'<script' in content.lower():
                return DRFResponse({'error': 'SVGs containing script tags are rejected for security.'}, status=status.HTTP_400_BAD_REQUEST)
            img_format = 'SVG'
        else:
            try:
                with PILImage.open(file_obj) as img:
                    img.verify()
                file_obj.seek(0)
                with PILImage.open(file_obj) as img:
                    width, height = img.size
                    img_format = img.format
                file_obj.seek(0)
            except Exception as e:
                return DRFResponse({'error': f'Corrupt or invalid image data: {str(e)}'}, status=status.HTTP_400_BAD_REQUEST)

        dest_dir = os.path.join(settings.MEDIA_ROOT, 'hackathons', 'assets')
        os.makedirs(dest_dir, exist_ok=True)

        clean_slug = slug or 'common'
        unique_name = f"{clean_slug}_{uuid.uuid4().hex[:10]}{ext}"
        dest_path = os.path.join(dest_dir, unique_name)

        with open(dest_path, 'wb+') as destination:
            for chunk in file_obj.chunks():
                destination.write(chunk)

        asset_url = f"/media/hackathons/assets/{unique_name}"

        log_audit_event(
            actor=request.user,
            action="Uploaded Hackathon Asset",
            target_model="Hackathon",
            target_id=slug or "general",
            details={
                "original_name": name,
                "file_name": unique_name,
                "size_bytes": file_obj.size,
                "width": width,
                "height": height,
                "format": img_format,
            },
        )

        return DRFResponse({
            'url': asset_url,
            'name': name,
            'size': file_obj.size,
            'width': width,
            'height': height,
            'format': img_format,
        }, status=status.HTTP_201_CREATED)


class HackathonAssetPresignedUrlView(APIView):
    """
    Generates a presigned S3 POST URL so the browser can upload banner and logo
    images directly to an S3 bucket without passing through Django (avoiding serverless payload limits).
    If S3 is not configured in the environment, gracefully returns mode: 'local'
    so the client can fall back to direct multipart upload.
    """
    permission_classes = [permissions.IsAuthenticated, IsAdminOrClubLead]

    ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.svg'}
    MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

    def post(self, request, slug=None):
        filename = (request.data.get('filename') or '').strip()
        content_type = (request.data.get('content_type') or 'application/octet-stream').strip().lower()
        size = request.data.get('size')

        if not filename:
            return DRFResponse({'error': 'Filename is required.'}, status=status.HTTP_400_BAD_REQUEST)

        ext = os.path.splitext(filename)[1].lower()
        if ext not in self.ALLOWED_EXTENSIONS:
            return DRFResponse(
                {'error': f'Unsupported file extension "{ext}". Allowed extensions: .png, .jpg, .jpeg, .webp, .svg.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if size is not None:
            try:
                size_int = int(size)
                if size_int > self.MAX_SIZE_BYTES:
                    return DRFResponse({'error': 'Image file size exceeds the 10 MB limit.'}, status=status.HTTP_400_BAD_REQUEST)
            except (ValueError, TypeError):
                pass

        bucket = getattr(settings, 'AWS_STORAGE_BUCKET_NAME', '') or getattr(settings, 'FORM_UPLOAD_BUCKET', '')
        use_s3 = (getattr(settings, 'USE_S3', False) or getattr(settings, 'FORM_UPLOADS_BACKEND', '') == 's3' or bool(bucket)) and bool(bucket)

        if not use_s3:
            return DRFResponse({
                'mode': 'local',
                'upload_url': f'/api/hackathons/{slug}/upload-asset/' if slug else '/api/hackathons/upload-asset/',
            }, status=status.HTTP_200_OK)

        import boto3
        from botocore.config import Config

        region = getattr(settings, 'AWS_S3_REGION_NAME', '') or getattr(settings, 'FORM_UPLOAD_REGION', 'ap-south-1') or 'ap-south-1'
        endpoint_url = getattr(settings, 'FORM_UPLOAD_ENDPOINT_URL', '') or None

        clean_slug = slug or 'common'
        unique_name = f"{clean_slug}_{uuid.uuid4().hex[:10]}{ext}"
        s3_key = f"hackathons/assets/{unique_name}"

        s3_client = boto3.client(
            's3',
            region_name=region,
            endpoint_url=endpoint_url,
            config=Config(
                signature_version='s3v4',
                s3={'addressing_style': 'virtual'},
            ),
        )

        presigned_post = s3_client.generate_presigned_post(
            Bucket=bucket,
            Key=s3_key,
            Fields={'Content-Type': content_type},
            Conditions=[
                {'Content-Type': content_type},
                ['content-length-range', 1, self.MAX_SIZE_BYTES],
            ],
            ExpiresIn=900,
        )

        if endpoint_url:
            public_url = f"{endpoint_url.rstrip('/')}/{bucket}/{s3_key}"
        else:
            public_url = f"https://{bucket}.s3.{region}.amazonaws.com/{s3_key}"

        return DRFResponse({
            'mode': 's3',
            'post': presigned_post,
            'public_url': public_url,
            'key': s3_key,
            'max_bytes': self.MAX_SIZE_BYTES,
        }, status=status.HTTP_200_OK)

