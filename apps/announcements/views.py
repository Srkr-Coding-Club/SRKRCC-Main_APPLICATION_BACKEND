from rest_framework import viewsets
from .models import Announcement
from .serializers import AnnouncementSerializer
from apps.audit.utils import log_audit_event
from apps.core.permissions import IsAdminOrClubLeadOrReadOnly


def _is_admin_or_club_lead(user) -> bool:
    if not (user and user.is_authenticated):
        return False
    if user.is_staff or user.is_superuser:
        return True
    return getattr(user, 'role', None) in ('ADMIN', 'CLUB_LEAD')


class AnnouncementViewSet(viewsets.ModelViewSet):
    """
    Public GET is filtered to is_active=True (what the landing page shows) —
    an admin/club-lead viewer sees everything, including inactive ones they're
    managing, the same asymmetric-queryset pattern Event/Hackathon use for
    their visibility windows.
    """
    serializer_class = AnnouncementSerializer
    permission_classes = [IsAdminOrClubLeadOrReadOnly]

    def get_queryset(self):
        qs = Announcement.objects.all()
        if _is_admin_or_club_lead(self.request.user):
            return qs
        return qs.filter(is_active=True)

    def perform_create(self, serializer):
        announcement = serializer.save()
        log_audit_event(
            actor=self.request.user,
            action="Created Announcement",
            target_model="Announcement",
            target_id=announcement.id,
            details={"title": announcement.title, "type": announcement.type, "is_active": announcement.is_active},
        )

    def perform_update(self, serializer):
        announcement = serializer.save()
        log_audit_event(
            actor=self.request.user,
            action="Updated Announcement",
            target_model="Announcement",
            target_id=announcement.id,
            details={"title": announcement.title, "type": announcement.type, "is_active": announcement.is_active},
        )

    def perform_destroy(self, instance):
        log_audit_event(
            actor=self.request.user,
            action="Deleted Announcement",
            target_model="Announcement",
            target_id=instance.id,
            details={"title": instance.title},
        )
        instance.delete()
