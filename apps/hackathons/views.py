from rest_framework import viewsets, permissions
from .models import Hackathon, Team, Submission
from .serializers import HackathonSerializer, TeamSerializer, SubmissionSerializer
from apps.core.permissions import IsAdminOrClubLeadOrReadOnly, _is_admin_or_club_lead


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
    queryset = Hackathon.objects.all()
    serializer_class = HackathonSerializer
    permission_classes = [IsAdminOrClubLeadOrReadOnly]
    lookup_field = 'slug'

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
