from django.db import transaction
from django.utils import timezone
from rest_framework import decorators, permissions, response, status, viewsets
from rest_framework.exceptions import ValidationError
from .models import Problem, Submission, UserStreak
from .serializers import ProblemSerializer, SubmissionSerializer, UserStreakSerializer
from apps.core.permissions import IsAdminOrClubLead, IsAdminOrClubLeadOrReadOnly, IsOwnerOrAdminOrClubLead, _is_admin_or_club_lead
from .services import rebuild_user_streak

class ProblemViewSet(viewsets.ModelViewSet):
    serializer_class = ProblemSerializer
    permission_classes = [IsAdminOrClubLeadOrReadOnly]
    lookup_field = 'slug'

    def get_queryset(self):
        queryset = Problem.objects.all().order_by('-scheduled_date')
        if _is_admin_or_club_lead(self.request.user):
            return queryset
        # The public route exposes today's challenge and the completed archive,
        # never the scheduling calendar. Future problems must not be discoverable
        # through either list or detail endpoints before their assigned club date.
        return queryset.filter(scheduled_date__lte=timezone.localdate())

class SubmissionViewSet(viewsets.ModelViewSet):
    serializer_class = SubmissionSerializer
    permission_classes = [permissions.IsAuthenticated, IsOwnerOrAdminOrClubLead]

    def get_queryset(self):
        qs = Submission.objects.select_related('user', 'problem').all().order_by('-created_at')
        if _is_admin_or_club_lead(self.request.user):
            return qs
        return qs.filter(user=self.request.user)

    def perform_create(self, serializer):
        if serializer.validated_data['problem'].scheduled_date != timezone.localdate():
            raise ValidationError({
                'problem': 'This challenge is not available for submission today.'
            })
        serializer.save(user=self.request.user)

    @decorators.action(detail=True, methods=['post'], permission_classes=[IsAdminOrClubLead])
    def review(self, request, pk=None):
        is_correct = request.data.get('is_correct')
        if not isinstance(is_correct, bool):
            return response.Response({'is_correct': ['Provide a boolean verdict.']}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            submission = self.get_queryset().select_for_update().get(pk=pk)
            submission.is_correct = is_correct
            submission.save(update_fields=['is_correct', 'updated_at'])
            rebuild_user_streak(submission.user)
        return response.Response(self.get_serializer(submission).data)

class UserStreakViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only: streaks have no user-facing write path (no automated updater exists yet)."""
    serializer_class = UserStreakSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = UserStreak.objects.all()
        if _is_admin_or_club_lead(self.request.user):
            return qs
        return qs.filter(user=self.request.user)
