from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response as DRFResponse
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.audit.utils import log_audit_event
from apps.core.permissions import IsAdminOrClubLead, IsAdminOrClubLeadOrReadOnly, _is_admin_or_club_lead

from .models import (
    EntryStatus, Hackathon, HackathonAnnouncement, HackathonStatus, InviteStatus,
    ProblemStatement, Round, RoundEntry, Submission, Team, TeamInvite, TeamMember, TeamStatus,
)
from .serializers import (
    AdminHackathonAnnouncementSerializer, AdminRoundEntrySerializer, AdminTeamSerializer,
    HackathonAnnouncementSerializer, HackathonSerializer, ProblemStatementSerializer,
    RoundSerializer, SubmissionSerializer, TeamInviteSerializer, TeamSerializer,
)
from .services import (
    AnnouncementService, HackathonError, ProblemStatementService, RoundService, TeamService, is_leader, is_member,
    membership_of, missing_profile_fields, round_entry_for_details_form,
)

ACTIVE = (TeamStatus.FORMING, TeamStatus.REGISTERED)


def _visible_hackathons(user):
    qs = Hackathon.objects.all()
    if _is_admin_or_club_lead(user):
        return qs
    now = timezone.now()
    return qs.filter(Q(visible_from__isnull=True) | Q(visible_from__lte=now)).filter(
        Q(visible_until__isnull=True) | Q(visible_until__gt=now)
    )


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
        if _is_admin_or_club_lead(request.user):
            return True
        team = obj.team
        if request.method in permissions.SAFE_METHODS:
            return is_member(team, request.user)
        return is_leader(team, request.user)


# ---------------------------------------------------------------------------
# Hackathon CRUD (admin) + public read
# ---------------------------------------------------------------------------

class HackathonViewSet(viewsets.ModelViewSet):
    serializer_class = HackathonSerializer
    permission_classes = [IsAdminOrClubLeadOrReadOnly]
    lookup_field = 'slug'

    def get_queryset(self):
        """Annotate hackathons with real registration count and team count."""
        queryset = Hackathon.objects.select_related('registration_form').annotate(
            registration_count=Count(
                'registration_form__responses',
                filter=Q(registration_form__responses__is_test_submission=False),
                distinct=True,
            ),
            team_count=Count('teams', filter=Q(teams__status__in=ACTIVE), distinct=True),
        ).order_by('-start_date')

        if _is_admin_or_club_lead(self.request.user):
            return queryset

        # Public/anonymous viewers only see hackathons inside their visibility
        # window - see the matching comment in apps.events.views.EventViewSet.
        now = timezone.now()
        return queryset.filter(
            Q(visible_from__isnull=True) | Q(visible_from__lte=now)
        ).filter(
            Q(visible_until__isnull=True) | Q(visible_until__gt=now)
        )

    def perform_update(self, serializer):
        before = {f: getattr(serializer.instance, f) for f in serializer.validated_data}
        instance = serializer.save()
        changed = {
            f: [str(before[f]), str(getattr(instance, f))]
            for f in serializer.validated_data if before[f] != getattr(instance, f)
        }
        if changed:
            log_audit_event(
                actor=self.request.user, action="Updated Hackathon",
                target_model="Hackathon", target_id=instance.slug, details={"changes": changed},
            )

    def perform_destroy(self, instance):
        details = {
            "title": instance.title,
            "registration_form": instance.registration_form_id,
            "teams_deleted": instance.teams.count(),
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

    @action(detail=True, methods=['post'], url_path='close')
    def close(self, request, slug=None):
        """POST /api/hackathons/{slug}/close/ - marks the hackathon CLOSED (stops registration)."""
        return self._set(request, "Closed Hackathon", status=HackathonStatus.CLOSED)

    @action(detail=True, methods=['post'], url_path='reopen')
    def reopen(self, request, slug=None):
        """POST /api/hackathons/{slug}/reopen/ - reverts a CLOSED hackathon back to LIVE."""
        return self._set(request, "Reopened Hackathon", status=HackathonStatus.LIVE)

    @action(detail=True, methods=['post'], url_path='hide')
    def hide(self, request, slug=None):
        """POST /api/hackathons/{slug}/hide/ - removes the hackathon from the public list entirely."""
        return self._set(request, "Hid Hackathon From Public", visible_until=timezone.now())

    @action(detail=True, methods=['post'], url_path='show')
    def show(self, request, slug=None):
        """POST /api/hackathons/{slug}/show/ - undoes `hide`, clearing visible_until."""
        return self._set(request, "Made Hackathon Publicly Visible Again", visible_until=None)


# ---------------------------------------------------------------------------
# Problem statements
# ---------------------------------------------------------------------------

def _ps_queryset(hackathon):
    return hackathon.problem_statements.annotate(
        active_team_count=Count('teams', filter=Q(teams__status__in=ACTIVE), distinct=True),
    )


class ProblemStatementListView(APIView):
    """GET (public: active only; admin: all) / POST (admin) /api/hackathons/{slug}/problem-statements/"""
    permission_classes = [IsAdminOrClubLeadOrReadOnly]

    def get(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        qs = _ps_queryset(hackathon)
        if not _is_admin_or_club_lead(request.user):
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
    permission_classes = [IsAdminOrClubLead]
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


class ProblemStatementDetailView(APIView):
    """PATCH / DELETE (admin) /api/hackathons/{slug}/problem-statements/{id}/"""
    permission_classes = [IsAdminOrClubLead]

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
        'ends_at': round_obj.ends_at, 'status': round_obj.status,
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
        else:
            for r in hackathon.rounds.all():
                rounds.append(_round_payload(r, None, viewer_is_leader=False, user=user))

        return DRFResponse({
            'hackathon': HackathonSerializer(hackathon).data,
            'profile_missing': missing_profile_fields(hackathon, user),
            'team': TeamSerializer(team).data if team else None,
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
        if not _is_admin_or_club_lead(request.user):
            raise PermissionDenied('Only admins can list all teams.')
        hackathon = _get_hackathon(request, slug)
        qs = _team_queryset().filter(hackathon=hackathon).prefetch_related(
            'memberships__user', 'round_entries__round',
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
        if _is_admin_or_club_lead(request.user) and request.data.get('leader_email'):
            leader_email = request.data['leader_email'].strip()
            from django.contrib.auth import get_user_model
            UserModel = get_user_model()
            leader_user = UserModel.objects.filter(email__iexact=leader_email).first()
            if not leader_user:
                raise HackathonError(f'No account found with email {leader_email}.', 'USER_NOT_FOUND', field='leader_email')
            leader = leader_user
        team = TeamService.create_team(hackathon, leader, request.data.get('name', ''), ps, open_innovation, actor=request.user)
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

        def _evaluate(u):
            if target_team is not None:
                if target_team.leader_id != request.user.id and not _is_admin_or_club_lead(request.user):
                    can_invite, reason = False, 'Only the team leader can invite.'
                else:
                    can_invite, reason = TeamService.invite_eligibility(target_team, u)
            else:
                if not _is_admin_or_club_lead(request.user):
                    can_invite, reason = False, 'Create a team first.'
                elif TeamMember.objects.filter(hackathon=hackathon, user=u).exists():
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
        if q:
            users = User.objects.filter(
                Q(email__icontains=q) |
                Q(first_name__icontains=q) |
                Q(last_name__icontains=q) |
                Q(club_id__icontains=q)
            ).distinct()[:8]

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
            team = TeamService.accept_invite(invite, request.user)
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
        if not (is_member(team, request.user) or _is_admin_or_club_lead(request.user)):
            # 404, not 403: don't confirm another team's existence to outsiders.
            from django.http import Http404
            raise Http404
        return team

    def _serialize(self, request, team):
        cls = AdminTeamSerializer if _is_admin_or_club_lead(request.user) else TeamSerializer
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
        TeamService.update_team(team, request.user, **kwargs)
        return DRFResponse(self._serialize(request, team))

    def delete(self, request, pk):
        if not _is_admin_or_club_lead(request.user):
            raise PermissionDenied('Only admins can delete teams.')
        team = get_object_or_404(_team_queryset(), pk=pk)
        team_id = team.pk
        team_name = team.name
        slug = team.hackathon.slug
        team.delete()
        log_audit_event(actor=request.user, action="Deleted Hackathon Team", target_model="HackathonTeam",
                        target_id=team_id, details={"hackathon": slug, "name": team_name})
        return DRFResponse({'deleted': True, 'id': team_id})


class TeamActionView(APIView):
    """
    POST /api/hackathons/teams/{id}/{action}/ where action is one of:
      invite              {email}          leader
      cancel-invite       {invite_id}      leader
      remove-member       {user_id}        leader / admin
      transfer-leadership {user_id}        leader / admin
      leave               -                member
      admin-add-member    {email}          admin
      admin-set-status    {status}         admin
    """
    permission_classes = [permissions.IsAuthenticated]
    ADMIN_ONLY = {'admin-add-member', 'admin-set-status'}

    def post(self, request, pk, team_action):
        team = get_object_or_404(_team_queryset(), pk=pk)
        user = request.user
        admin = _is_admin_or_club_lead(user)
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
        elif team_action == 'remove-member':
            TeamService.remove_member(team, user, _int(data.get('user_id'), 'user_id'))
        elif team_action == 'transfer-leadership':
            TeamService.transfer_leadership(team, user, _int(data.get('user_id'), 'user_id'))
        elif team_action == 'leave':
            TeamService.leave_team(team, user)
            return DRFResponse({'left': True, 'hackathon_slug': team.hackathon.slug})
        elif team_action == 'admin-add-member':
            TeamService.admin_add_member(team, user, data.get('email', ''))
        elif team_action == 'admin-set-status':
            TeamService.admin_set_status(team, user, data.get('status', ''))
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
    permission_classes = [IsAdminOrClubLead]

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


class RoundDetailView(APIView):
    """PATCH / DELETE (admin) /api/hackathons/{slug}/rounds/{id}/"""
    permission_classes = [IsAdminOrClubLead]

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
        name = round_obj.name
        round_obj.delete()
        log_audit_event(actor=request.user, action="Deleted Hackathon Round", target_model="HackathonRound",
                        target_id=pk, details={"hackathon": slug, "round": name})
        return DRFResponse({'deleted': True, 'id': pk})


class RoundEntriesView(APIView):
    """GET (admin) /api/hackathons/{slug}/rounds/{id}/entries/?status=&search="""
    permission_classes = [IsAdminOrClubLead]

    def get(self, request, slug, pk):
        hackathon = _get_hackathon(request, slug)
        round_obj = get_object_or_404(hackathon.rounds, pk=pk)
        qs = round_obj.entries.select_related('team__leader', 'team__problem_statement', 'decided_by')
        if request.query_params.get('status'):
            qs = qs.filter(status=request.query_params['status'])
        if request.query_params.get('search'):
            q = request.query_params['search'].strip()
            qs = qs.filter(Q(team__name__icontains=q) | Q(team__memberships__user__email__icontains=q)).distinct()
        return DRFResponse(AdminRoundEntrySerializer(qs, many=True).data)


class RoundActionView(APIView):
    """
    POST (admin) /api/hackathons/{slug}/rounds/{id}/{action}/:
      decide     {team_ids: [], status, feedback?, admin_notes?}
      publish    {announce?: bool, message?: str}
      unpublish
      populate
    """
    permission_classes = [IsAdminOrClubLead]

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
            )
        elif round_action == 'publish':
            RoundService.publish_results(
                round_obj, request.user, announce=bool(data.get('announce')), message=data.get('message', ''),
                email=bool(data.get('email')),
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
    permission_classes = [IsAdminOrClubLeadOrReadOnly]

    def get(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        if request.query_params.get('all') == 'true' and _is_admin_or_club_lead(request.user):
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
    permission_classes = [IsAdminOrClubLead]

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
    permission_classes = [IsAdminOrClubLead]

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

class HackathonStatsView(APIView):
    """GET (admin) /api/hackathons/{slug}/stats/"""
    permission_classes = [IsAdminOrClubLead]

    def get(self, request, slug):
        hackathon = _get_hackathon(request, slug)
        teams = hackathon.teams.all()
        by_status = {s: 0 for s in TeamStatus.values}
        for s in teams.values_list('status', flat=True):
            by_status[s] += 1
        participants = TeamMember.objects.filter(hackathon=hackathon, team__status__in=ACTIVE).count()
        per_ps = [
            {'id': ps.id, 'code': ps.code, 'title': ps.title, 'teams': ps.active_team_count, 'max_teams': ps.max_teams}
            for ps in _ps_queryset(hackathon)
        ]
        rounds = [
            {'id': r.id, 'order': r.order, 'name': r.name, 'results_published': r.results_published,
             **RoundSerializer().get_entry_counts(r)}
            for r in hackathon.rounds.all()
        ]
        return DRFResponse({
            'teams_by_status': by_status,
            'teams_total': sum(by_status.values()),
            'participants': participants,
            'pending_invites': hackathon.team_invites.filter(status=InviteStatus.PENDING).count(),
            'problem_statements': per_ps,
            'open_innovation_teams': hackathon.teams.filter(is_open_innovation=True, status__in=ACTIVE).count(),
            'rounds': rounds,
            'is_registration_open': hackathon.is_registration_open,
        })


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
