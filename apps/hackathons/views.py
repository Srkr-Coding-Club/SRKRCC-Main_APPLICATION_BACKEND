from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response as DRFResponse
from .models import Hackathon, HackathonStatus, Team, Submission
from .serializers import HackathonSerializer, TeamSerializer, SubmissionSerializer
from apps.core.permissions import IsAdminOrClubLeadOrReadOnly, _is_admin_or_club_lead
from apps.audit.utils import log_audit_event


class IsTeamMemberOrAdmin(permissions.BasePermission):
    """Safe methods open to any authenticated user; writes require team leader/member or admin."""
    def has_object_permission(self, request, view, obj):
        if request.method in permissions.SAFE_METHODS:
            return True
        if _is_admin_or_club_lead(request.user):
            return True
        return obj.leader_id == request.user.id or obj.members.filter(id=request.user.id).exists()


class IsSubmissionTeamMemberOrAdmin(permissions.BasePermission):
    """Safe methods open to any authenticated user; writes require the submitting team's leader/member or admin."""
    def has_object_permission(self, request, view, obj):
        if request.method in permissions.SAFE_METHODS:
            return True
        if _is_admin_or_club_lead(request.user):
            return True
        team = obj.team
        return team.leader_id == request.user.id or team.members.filter(id=request.user.id).exists()


class HackathonViewSet(viewsets.ModelViewSet):
    serializer_class = HackathonSerializer
    permission_classes = [IsAdminOrClubLeadOrReadOnly]
    lookup_field = 'slug'

    def get_queryset(self):
        """Annotate hackathons with real registration count (form responses) and team count."""
        queryset = Hackathon.objects.select_related('registration_form').annotate(
            registration_count=Count(
                'registration_form__responses',
                filter=Q(registration_form__responses__is_test_submission=False),
                distinct=True,
            ),
            team_count=Count('teams', distinct=True),
        ).order_by('-start_date')

        if _is_admin_or_club_lead(self.request.user):
            return queryset

        # Public/anonymous viewers only see hackathons inside their visibility
        # window — see the matching comment in apps.events.views.EventViewSet.
        now = timezone.now()
        return queryset.filter(
            Q(visible_from__isnull=True) | Q(visible_from__lte=now)
        ).filter(
            Q(visible_until__isnull=True) | Q(visible_until__gt=now)
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

    @action(detail=True, methods=['post'], url_path='close')
    def close(self, request, slug=None):
        """POST /api/hackathons/{slug}/close/ — marks the hackathon CLOSED (hides the Register CTA)."""
        hackathon = self.get_object()
        hackathon.status = HackathonStatus.CLOSED
        hackathon.save(update_fields=['status', 'updated_at'])
        log_audit_event(
            actor=request.user, action="Closed Hackathon",
            target_model="Hackathon", target_id=hackathon.slug,
            details={"title": hackathon.title, "status": "CLOSED"},
        )
        return DRFResponse(self.get_serializer(hackathon).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='reopen')
    def reopen(self, request, slug=None):
        """POST /api/hackathons/{slug}/reopen/ — reverts a CLOSED hackathon back to LIVE."""
        hackathon = self.get_object()
        hackathon.status = HackathonStatus.LIVE
        hackathon.save(update_fields=['status', 'updated_at'])
        log_audit_event(
            actor=request.user, action="Reopened Hackathon",
            target_model="Hackathon", target_id=hackathon.slug,
            details={"title": hackathon.title, "status": "LIVE"},
        )
        return DRFResponse(self.get_serializer(hackathon).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='hide')
    def hide(self, request, slug=None):
        """POST /api/hackathons/{slug}/hide/ — removes the hackathon from the
        public list/detail entirely. See EventViewSet.hide for the full
        close-vs-hide rationale (identical here)."""
        hackathon = self.get_object()
        hackathon.visible_until = timezone.now()
        hackathon.save(update_fields=['visible_until', 'updated_at'])
        log_audit_event(
            actor=request.user, action="Hid Hackathon From Public",
            target_model="Hackathon", target_id=hackathon.slug, details={"title": hackathon.title},
        )
        return DRFResponse(self.get_serializer(hackathon).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='show')
    def show(self, request, slug=None):
        """POST /api/hackathons/{slug}/show/ — undoes `hide`, clearing visible_until."""
        hackathon = self.get_object()
        hackathon.visible_until = None
        hackathon.save(update_fields=['visible_until', 'updated_at'])
        log_audit_event(
            actor=request.user, action="Made Hackathon Publicly Visible Again",
            target_model="Hackathon", target_id=hackathon.slug, details={"title": hackathon.title},
        )
        return DRFResponse(self.get_serializer(hackathon).data, status=status.HTTP_200_OK)

class TeamViewSet(viewsets.ModelViewSet):
    queryset = Team.objects.all()
    serializer_class = TeamSerializer
    permission_classes = [permissions.IsAuthenticated, IsTeamMemberOrAdmin]

    def perform_create(self, serializer):
        serializer.save(leader=self.request.user)

class SubmissionViewSet(viewsets.ModelViewSet):
    queryset = Submission.objects.all()
    serializer_class = SubmissionSerializer
    permission_classes = [permissions.IsAuthenticated, IsSubmissionTeamMemberOrAdmin]

    def perform_create(self, serializer):
        team = serializer.validated_data.get('team')
        user = self.request.user
        if team and not _is_admin_or_club_lead(user):
            if team.leader_id != user.id and not team.members.filter(id=user.id).exists():
                from rest_framework.exceptions import PermissionDenied
                raise PermissionDenied("You are not a member of this team.")
        serializer.save()
