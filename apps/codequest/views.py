from rest_framework import viewsets, permissions
from .models import Problem, Submission, UserStreak
from .serializers import ProblemSerializer, SubmissionSerializer, UserStreakSerializer
from apps.core.permissions import IsAdminOrClubLeadOrReadOnly, IsOwnerOrAdminOrClubLead, _is_admin_or_club_lead

class ProblemViewSet(viewsets.ModelViewSet):
    queryset = Problem.objects.all()
    serializer_class = ProblemSerializer
    permission_classes = [IsAdminOrClubLeadOrReadOnly]
    lookup_field = 'slug'

class SubmissionViewSet(viewsets.ModelViewSet):
    serializer_class = SubmissionSerializer
    permission_classes = [permissions.IsAuthenticated, IsOwnerOrAdminOrClubLead]

    def get_queryset(self):
        qs = Submission.objects.all()
        if _is_admin_or_club_lead(self.request.user):
            return qs
        return qs.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

class UserStreakViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only: streaks have no user-facing write path (no automated updater exists yet)."""
    serializer_class = UserStreakSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = UserStreak.objects.all()
        if _is_admin_or_club_lead(self.request.user):
            return qs
        return qs.filter(user=self.request.user)
