from django.shortcuts import get_object_or_404
from rest_framework import permissions, status
from rest_framework.response import Response as DRFResponse
from rest_framework.views import APIView

from apps.core.permissions import IsAdminOrClubLead
from apps.forms.models import Form, Response as FormResponse

from .models import AttendanceBadge, AttendanceRecord, AttendanceSession
from .permissions import IsVolunteerOrAbove
from .serializers import (
    AttendanceBadgeSerializer,
    AttendanceScanRequestSerializer,
    AttendanceSessionSerializer,
)
from .services import issue_badge, resolve_display_name, scan_time_allowed


class AttendanceSessionListView(APIView):
    """
    GET /api/forms/<form_id>/attendance/sessions/
    Lists the form's AttendanceSession rows (generated from its attendance_*
    config — see apps.attendance.services.generate_sessions).

    IsVolunteerOrAbove, not IsAdminOrClubLead: the scanner UI calls this to
    populate its "Session" picker before a volunteer can scan anything, so it
    has to carry the same permission as the scan endpoint itself — a
    volunteer who can POST /api/attendance/scan/ but can't GET the session
    list to pick a session_id for it is blocked from scanning either way.
    """
    permission_classes = [IsVolunteerOrAbove]

    def get(self, request, form_id):
        form = get_object_or_404(Form, id=form_id)
        sessions = form.attendance_sessions.all()
        serializer = AttendanceSessionSerializer(sessions, many=True)
        return DRFResponse(serializer.data, status=status.HTTP_200_OK)


class MyBadgeView(APIView):
    """
    GET /api/forms/<form_id>/attendance/my-badge/
    Returns the authenticated caller's own attendance badge for this form,
    resolved from their own Response. 404 if they haven't submitted a
    response, or attendance isn't enabled on this form.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, form_id):
        form = get_object_or_404(Form, id=form_id)
        if not form.attendance_enabled:
            return DRFResponse(
                {"error": "Attendance tracking is not enabled for this form."},
                status=status.HTTP_404_NOT_FOUND,
            )

        response_obj = FormResponse.objects.filter(
            form=form, user=request.user, is_test_submission=False,
        ).order_by('-submitted_at').first()
        if not response_obj:
            return DRFResponse(
                {"error": "You have not submitted a response to this form."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Defensive get_or_create rather than a strict lookup: covers the case
        # where attendance was turned on for the form *after* this response
        # was originally submitted (the auto-issue hook in
        # ResponseViewSet.create only fires at submission time).
        badge = issue_badge(response_obj)
        serializer = AttendanceBadgeSerializer(badge)
        return DRFResponse(serializer.data, status=status.HTTP_200_OK)


class MyAttendanceRecordView(APIView):
    """
    GET /api/forms/<form_id>/attendance/my-record/
    Self-service counterpart to AttendanceReportView (which is admin-only and
    covers every registrant): returns just the authenticated caller's own
    session-by-session attendance so the profile page can show "did I actually
    check in", not just the QR pass itself. 404 under the same conditions as
    MyBadgeView (no response, or attendance isn't enabled on this form).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, form_id):
        form = get_object_or_404(Form, id=form_id)
        if not form.attendance_enabled:
            return DRFResponse(
                {"error": "Attendance tracking is not enabled for this form."},
                status=status.HTTP_404_NOT_FOUND,
            )

        response_obj = FormResponse.objects.filter(
            form=form, user=request.user, is_test_submission=False,
        ).order_by('-submitted_at').first()
        if not response_obj:
            return DRFResponse(
                {"error": "You have not submitted a response to this form."},
                status=status.HTTP_404_NOT_FOUND,
            )

        badge = issue_badge(response_obj)
        attended_session_ids = set(badge.records.values_list('session_id', flat=True))

        sessions = form.attendance_sessions.all().order_by('day_index', 'session_label')
        session_rows = [
            {
                "id": s.id,
                "day_index": s.day_index,
                "session_label": s.session_label,
                "session_label_display": s.get_session_label_display(),
                "date": s.date,
                "opens_at": s.opens_at,
                "attended": s.id in attended_session_ids,
            }
            for s in sessions
        ]

        total = len(session_rows)
        attended_count = len(attended_session_ids)
        return DRFResponse({
            "sessions": session_rows,
            "attended_count": attended_count,
            "total_sessions": total,
            "percentage": round((attended_count / total) * 100, 1) if total else 0.0,
        }, status=status.HTTP_200_OK)


class AttendanceScanView(APIView):
    """
    POST /api/attendance/scan/
    Body: {"token": "<badge token>", "session_id": <int>}

    Records a scan of a registrant's badge against a session. Idempotent: a
    repeat scan of the same (badge, session) pair returns the existing record
    with already_recorded=True instead of erroring.
    """
    permission_classes = [IsVolunteerOrAbove]

    def post(self, request):
        serializer = AttendanceScanRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token = serializer.validated_data['token']
        session_id = serializer.validated_data['session_id']

        try:
            badge = AttendanceBadge.objects.select_related('response__form').get(token=token)
        except AttendanceBadge.DoesNotExist:
            return DRFResponse(
                {"error": "Invalid attendance badge token.", "code": "BADGE_NOT_FOUND"},
                status=status.HTTP_404_NOT_FOUND,
            )

        if badge.revoked:
            return DRFResponse(
                {"error": "This attendance badge has been revoked.", "code": "BADGE_REVOKED"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            session = AttendanceSession.objects.select_related('form').get(id=session_id)
        except AttendanceSession.DoesNotExist:
            return DRFResponse(
                {"error": "Attendance session not found.", "code": "SESSION_NOT_FOUND"},
                status=status.HTTP_404_NOT_FOUND,
            )

        if session.form_id != badge.response.form_id:
            return DRFResponse(
                {"error": "This session belongs to a different form than this badge.",
                 "code": "FORM_MISMATCH"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not scan_time_allowed(session.form, session):
            return DRFResponse(
                {"error": "Scanning for this session is only allowed near its scheduled start time.",
                 "code": "OUTSIDE_SCAN_WINDOW"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # get_or_create (not a manual exists()-then-create) so a race between
        # two near-simultaneous scans of the same badge/session still resolves
        # to a single record rather than an IntegrityError bubbling up.
        record, created = AttendanceRecord.objects.get_or_create(
            badge=badge, session=session, defaults={'scanned_by': request.user},
        )

        return DRFResponse({
            "success": True,
            "new_scan": created,
            "already_recorded": not created,
            "display_name": resolve_display_name(badge.response),
            "response_id": badge.response_id,
            "session_id": session.id,
            "scanned_at": record.scanned_at,
        }, status=status.HTTP_200_OK)


class AttendanceReportView(APIView):
    """
    GET /api/forms/<form_id>/attendance/report/
    Per-session attendance counts/percentages plus a per-registrant grid of
    which sessions each Response attended.
    """
    permission_classes = [IsAdminOrClubLead]

    def get(self, request, form_id):
        form = get_object_or_404(Form, id=form_id)
        sessions = list(form.attendance_sessions.all().order_by('day_index', 'session_label'))

        responses = list(
            FormResponse.objects.filter(form=form, is_test_submission=False)
            .select_related('user', 'created_by_admin')
            .prefetch_related('answers__field', 'attendance_badge__records')
        )

        total_registrants = len(responses)
        session_counts = {s.id: 0 for s in sessions}
        registrant_rows = []

        for resp in responses:
            badge = getattr(resp, 'attendance_badge', None)
            attended_session_ids = set()
            if badge is not None:
                attended_session_ids = {r.session_id for r in badge.records.all()}

            for sid in attended_session_ids:
                if sid in session_counts:
                    session_counts[sid] += 1

            registrant_rows.append({
                "response_id": resp.id,
                "display_name": resolve_display_name(resp),
                "sessions": {s.id: (s.id in attended_session_ids) for s in sessions},
            })

        session_summaries = []
        for s in sessions:
            attended = session_counts.get(s.id, 0)
            percentage = round((attended / total_registrants) * 100, 1) if total_registrants else 0.0
            session_summaries.append({
                "id": s.id,
                "day_index": s.day_index,
                "session_label": s.session_label,
                "date": s.date,
                "attended_count": attended,
                "total_registrants": total_registrants,
                "percentage": percentage,
            })

        return DRFResponse({
            "sessions": session_summaries,
            "registrants": registrant_rows,
        }, status=status.HTTP_200_OK)
