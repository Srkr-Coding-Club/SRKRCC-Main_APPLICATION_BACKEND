from django.db.models import Count, Q, F
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response as DRFResponse
from .models import Event, EventStatus
from .serializers import EventSerializer
from apps.core.permissions import IsAdminOrClubLeadOrReadOnly, _is_admin_or_club_lead
from apps.audit.utils import log_audit_event

class EventViewSet(viewsets.ModelViewSet):
    serializer_class = EventSerializer
    permission_classes = [IsAdminOrClubLeadOrReadOnly]
    lookup_field = 'slug'

    def get_queryset(self):
        """Annotate events with real registration count (form responses, excluding test submissions)."""
        queryset = Event.objects.select_related('registration_form').annotate(
            registration_count=Count(
                'registration_form__responses',
                filter=Q(registration_form__responses__is_test_submission=False),
            )
        # nulls_last: an undated (schedule TBD) event sorts to the bottom
        # instead of Postgres's DESC-default of putting NULLs first.
        ).order_by(F('start_time').desc(nulls_last=True))

        if _is_admin_or_club_lead(self.request.user):
            return queryset

        # Public/anonymous viewers only ever see events inside their visibility
        # window — admins still see everything (including hidden ones) so they
        # can find and re-show them. visible_from/visible_until are nullable:
        # a null bound means "no restriction on that side".
        now = timezone.now()
        return queryset.filter(
            Q(visible_from__isnull=True) | Q(visible_from__lte=now)
        ).filter(
            Q(visible_until__isnull=True) | Q(visible_until__gt=now)
        )

    def perform_destroy(self, instance):
        details = {"title": instance.title, "registration_form": instance.registration_form_id}
        slug = instance.slug
        instance.delete()
        log_audit_event(
            actor=self.request.user, action="Deleted Event",
            target_model="Event", target_id=slug, details=details,
        )

    @action(detail=True, methods=['post'], url_path='close')
    def close(self, request, slug=None):
        """POST /api/events/{slug}/close/ — marks the event CLOSED (hides the Register CTA)."""
        event = self.get_object()
        event.status = EventStatus.CLOSED
        event.save(update_fields=['status', 'updated_at'])
        log_audit_event(
            actor=request.user, action="Closed Event",
            target_model="Event", target_id=event.slug,
            details={"title": event.title, "status": "CLOSED"},
        )
        return DRFResponse(self.get_serializer(event).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='reopen')
    def reopen(self, request, slug=None):
        """POST /api/events/{slug}/reopen/ — reverts a CLOSED event back to LIVE."""
        event = self.get_object()
        event.status = EventStatus.LIVE
        event.save(update_fields=['status', 'updated_at'])
        log_audit_event(
            actor=request.user, action="Reopened Event",
            target_model="Event", target_id=event.slug,
            details={"title": event.title, "status": "LIVE"},
        )
        return DRFResponse(self.get_serializer(event).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='hide')
    def hide(self, request, slug=None):
        """
        POST /api/events/{slug}/hide/ — removes the event from the public
        list/detail entirely (get_queryset above excludes it for non-admins).
        Distinct from `close`: closing only stops registration but keeps the
        event listed; hiding is for an event that shouldn't be publicly
        findable at all (e.g. still being drafted, or fully retired).
        Implemented via visible_until rather than a new field — it already
        existed on the model for exactly this and was previously unused.
        """
        event = self.get_object()
        event.visible_until = timezone.now()
        event.save(update_fields=['visible_until', 'updated_at'])
        log_audit_event(
            actor=request.user, action="Hid Event From Public",
            target_model="Event", target_id=event.slug, details={"title": event.title},
        )
        return DRFResponse(self.get_serializer(event).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='show')
    def show(self, request, slug=None):
        """POST /api/events/{slug}/show/ — undoes `hide`, clearing visible_until."""
        event = self.get_object()
        event.visible_until = None
        event.save(update_fields=['visible_until', 'updated_at'])
        log_audit_event(
            actor=request.user, action="Made Event Publicly Visible Again",
            target_model="Event", target_id=event.slug, details={"title": event.title},
        )
        return DRFResponse(self.get_serializer(event).data, status=status.HTTP_200_OK)
