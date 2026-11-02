from django.db.models import Count, Q, F
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.views import APIView
from rest_framework.response import Response as DRFResponse
from . import certificates as certificate_service
from . import registration as registration_service
from . import feedback as feedback_service
from .models import Event, EventFeedback, EventStatus
from .serializers import EventCertificateSettingsSerializer, EventFeedbackCommentSerializer, EventFeedbackSerializer, EventSerializer
from apps.core.dmc.export_service import export_filename, serialize_records
from apps.core.permissions import IsAdminOrClubLead, IsAdminOrClubLeadOrReadOnly, _is_admin_or_club_lead
from apps.core.cache import PublicReadCacheControlMixin
from apps.audit.utils import log_audit_event

FEEDBACK_EXPORT_FORMATS = ('csv', 'xlsx', 'json')
FEEDBACK_BLOCKED_MESSAGE = {
    feedback_service.EVENT_NOT_ENDED: 'Feedback opens once the event has ended.',
    feedback_service.NOT_ATTENDED: 'Only people who were checked in at the event can leave feedback.',
}

class EventViewSet(PublicReadCacheControlMixin, viewsets.ModelViewSet):
    serializer_class = EventSerializer
    permission_classes = [IsAdminOrClubLeadOrReadOnly]
    lookup_field = 'slug'

    def get_queryset(self):
        """Annotate events with real registration count (form responses, excluding test submissions)."""
        queryset = Event.objects.select_related('registration_form').prefetch_related(
            'registration_form__fields',
        ).annotate(
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
        # window - admins still see everything (including hidden ones) so they
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
        """POST /api/events/{slug}/close/ - marks the event CLOSED (hides the Register CTA)."""
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
        """POST /api/events/{slug}/reopen/ - reverts a CLOSED event back to LIVE."""
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
        POST /api/events/{slug}/hide/ - removes the event from the public
        list/detail entirely (get_queryset above excludes it for non-admins).
        Distinct from `close`: closing only stops registration but keeps the
        event listed; hiding is for an event that shouldn't be publicly
        findable at all (e.g. still being drafted, or fully retired).
        Implemented via visible_until rather than a new field - it already
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
        """POST /api/events/{slug}/show/ - undoes `hide`, clearing visible_until."""
        event = self.get_object()
        event.visible_until = None
        event.save(update_fields=['visible_until', 'updated_at'])
        log_audit_event(
            actor=request.user, action="Made Event Publicly Visible Again",
            target_model="Event", target_id=event.slug, details={"title": event.title},
        )
        return DRFResponse(self.get_serializer(event).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['get', 'put'], url_path='feedback', permission_classes=[IsAuthenticated])
    def feedback(self, request, slug=None):
        """
        GET /api/events/{slug}/feedback/ - the caller's own feedback and whether they may leave it now.
        PUT the same path - create or change it. Looked up outside the visibility window on purpose: an
        event is often hidden from the public list after it ends, and attendees must still be able to rate it.
        """
        event = get_object_or_404(Event, slug=slug)
        mine = EventFeedback.objects.filter(event=event, user=request.user).first()
        reason = feedback_service.blocked_reason(event, request.user)
        if request.method == 'GET':
            return DRFResponse({
                'eligible': reason is None,
                'reason': reason,
                'feedback': EventFeedbackSerializer(mine).data if mine else None,
            })
        if reason:
            return DRFResponse({'code': reason, 'error': FEEDBACK_BLOCKED_MESSAGE[reason]}, status=status.HTTP_403_FORBIDDEN)
        serializer = EventFeedbackSerializer(mine, data=request.data)
        serializer.is_valid(raise_exception=True)
        saved = serializer.save(event=event, user=request.user)
        return DRFResponse(EventFeedbackSerializer(saved).data, status=status.HTTP_200_OK if mine else status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'], url_path='feedback/summary', permission_classes=[IsAdminOrClubLead])
    def feedback_summary(self, request, slug=None):
        """GET /api/events/{slug}/feedback/summary/ - ratings, recommend rate, response rate and written answers."""
        event = self.get_object()
        return DRFResponse({
            **feedback_service.summary(event),
            'ended': feedback_service.has_ended(event),
            'comments': EventFeedbackCommentSerializer(feedback_service.comments(event), many=True).data,
        })

    @action(detail=True, methods=['get'], url_path='feedback/export', permission_classes=[IsAdminOrClubLead])
    def feedback_export(self, request, slug=None):
        """GET /api/events/{slug}/feedback/export/?export_format=csv|xlsx|json - every answer, one row each."""
        event = self.get_object()
        fmt = request.query_params.get('export_format', 'csv').lower()
        if fmt not in FEEDBACK_EXPORT_FORMATS:
            return DRFResponse({'error': f"Unsupported format. Choose: {', '.join(FEEDBACK_EXPORT_FORMATS)}."}, status=status.HTTP_400_BAD_REQUEST)
        data, content_type = serialize_records(feedback_service.export_columns(), feedback_service.export_rows(event), fmt)
        log_audit_event(
            actor=request.user, action=f'Event Feedback Export ({fmt.upper()})',
            target_model='Event', target_id=event.slug, details={'title': event.title},
        )
        response = HttpResponse(data, content_type=content_type)
        response['Content-Disposition'] = f'attachment; filename="{export_filename(event.slug, "feedback", fmt)}"'
        return response

    @action(detail=True, methods=['delete'], url_path='my-registration', permission_classes=[IsAuthenticated])
    def my_registration(self, request, slug=None):
        """DELETE /api/events/{slug}/my-registration/ - the caller cancels their own registration before the event starts."""
        event = get_object_or_404(Event, slug=slug)
        try:
            registration_service.cancel(event, request.user)
        except registration_service.CancellationError as error:
            return DRFResponse({'code': error.code, 'error': error.message}, status=error.status)
        return DRFResponse(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['get'], url_path='my-certificate', permission_classes=[IsAuthenticated])
    def my_certificate(self, request, slug=None):
        """GET /api/events/{slug}/my-certificate/ - the caller's attendance certificate, once organizers release them."""
        event = get_object_or_404(Event, slug=slug)
        try:
            return DRFResponse(certificate_service.certificate_for(event, request.user))
        except certificate_service.CertificateError as error:
            return DRFResponse({'code': error.code, 'error': error.message}, status=error.status)

    @action(detail=True, methods=['get', 'put'], url_path='certificates', permission_classes=[IsAdminOrClubLead])
    def certificates(self, request, slug=None):
        """GET/PUT /api/events/{slug}/certificates/ - release switch, wording, signatory, who is eligible and a preview."""
        event = self.get_object()
        if request.method == 'PUT':
            serializer = EventCertificateSettingsSerializer(event, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            log_audit_event(
                actor=request.user, action="Updated Event Certificates",
                target_model="Event", target_id=event.slug, details={"released": event.certificates_released},
            )
        return DRFResponse({
            **EventCertificateSettingsSerializer(event).data,
            'eligible_count': certificate_service.eligible_count(event),
            'preview': certificate_service.preview(event, request.user),
        })


class EventCertificateVerifyView(APIView):
    """GET /api/events/certificates/verify/{code}/ - public: is this attendance certificate genuine?"""
    permission_classes = [AllowAny]

    def get(self, request, code):
        try:
            return DRFResponse(certificate_service.verify(code))
        except certificate_service.CertificateError as error:
            return DRFResponse({'code': error.code, 'error': error.message}, status=error.status)
