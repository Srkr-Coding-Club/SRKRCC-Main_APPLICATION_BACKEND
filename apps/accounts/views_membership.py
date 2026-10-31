"""Club membership requests: a person's own (ask, see, withdraw) and the organizers' queue (read, approve, reject)."""
from django.db.models import Count, Q
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import ClubMembershipRequest, MembershipRequestStatus as Status
from apps.accounts.services import club_membership as service
from apps.accounts.services import email_verification as verification
from apps.accounts.services.club_membership import MembershipError
from apps.audit.utils import log_audit_event
from apps.core.pagination import StandardPagination
from apps.core.permissions import IsAdmin, IsAdminOrClubLead


class QueuePagination(StandardPagination):
    page_size = 25
    max_page_size = 100


def _error(exc: MembershipError) -> Response:
    return Response({'detail': exc.message, 'code': exc.code, **exc.extra}, status=exc.status)


def _own(entry: ClubMembershipRequest | None) -> dict | None:
    if entry is None:
        return None
    return {'id': entry.pk, 'status': entry.status, 'status_label': entry.get_status_display(), 'message': entry.message, 'note': entry.note, 'club_id': entry.club_id,
            'created_at': entry.created_at, 'decided_at': entry.decided_at}


def _queue_row(entry: ClubMembershipRequest) -> dict:
    user = entry.user
    return {
        **_own(entry),
        'person': {
            'id': user.pk, 'name': f'{user.first_name} {user.last_name}'.strip() or user.email.split('@')[0], 'email': user.email, 'email_verified': verification.is_verified(user),
            'phone': user.phone_number or '', 'roll_number': user.roll_number or '', 'branch': user.branch or '', 'year': user.year,
            'college': user.college.name if user.college_id else (user.other_college_name or ''), 'role': user.role, 'membership_status': user.membership_status,
            'club_id': user.club_id or None, 'joined': user.created_at,
        },
        'decided_by': getattr(entry.decided_by, 'email', ''),
    }


class MyMembershipView(APIView):
    """GET /api/auth/club-membership/me/ - am I a member, can I ask, what happened to my last request."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        info = service.status_for(request.user)
        return Response({**info, 'request': _own(info['request']), 'blockers': [{**b, **({'until': b['until']} if 'until' in b else {})} for b in info['blockers']]})


class RequestMembershipView(APIView):
    """POST /api/auth/club-membership/request/ {message?} - ask to become a member."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        try:
            entry, auto = service.request_membership(request.user, request.data.get('message', ''))
        except MembershipError as exc:
            return _error(exc)
        log_audit_event(actor=request.user, action='Club Membership Requested', target_model='User', target_id=str(request.user.pk), details={'auto_approved': auto})
        return Response({'request': _own(entry), 'auto_approved': auto}, status=status.HTTP_201_CREATED)


class CancelMembershipView(APIView):
    """POST /api/auth/club-membership/cancel/ - take my waiting request back."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        try:
            entry = service.cancel(request.user)
        except MembershipError as exc:
            return _error(exc)
        return Response({'request': _own(entry)})


class QueueView(generics.ListAPIView):
    """GET /api/auth/club-membership/requests/?status=&q= - the organizers' queue (waiting first), with counts."""
    permission_classes = [IsAdminOrClubLead]
    pagination_class = QueuePagination

    def get_queryset(self):
        rows = ClubMembershipRequest.objects.select_related('user', 'user__college', 'decided_by')
        params = self.request.query_params
        if wanted := params.get('status'):
            rows = rows.filter(status__in=wanted.split(','))
        if text := (params.get('q') or '').strip():
            for word in text.split()[:5]:
                rows = rows.filter(Q(user__email__icontains=word) | Q(user__first_name__icontains=word) | Q(user__last_name__icontains=word) | Q(user__roll_number__icontains=word))
        # Waiting ones first, oldest first (who has waited longest); decided ones newest first.
        return rows.order_by('status', 'created_at') if params.get('status') == Status.PENDING else rows.order_by('-created_at', '-id')

    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.get_queryset())
        counts = {row['status']: row['n'] for row in ClubMembershipRequest.objects.values('status').annotate(n=Count('pk'))}
        response = self.get_paginated_response([_queue_row(entry) for entry in page])
        response.data['counts'] = {key: counts.get(key, 0) for key in Status.values}
        return response


class ApproveView(APIView):
    """POST (Admin) /api/auth/club-membership/requests/{id}/approve/ - the next Club ID, the affiliate role, a notification and an email."""
    permission_classes = [IsAdmin]

    def post(self, request, pk):
        try:
            entry = service.approve(request.user, pk)
        except MembershipError as exc:
            return _error(exc)
        log_audit_event(actor=request.user, action='Club Membership Approved', target_model='User', target_id=str(entry.user_id), details={'club_id': entry.club_id})
        return Response(_queue_row(ClubMembershipRequest.objects.select_related('user', 'user__college', 'decided_by').get(pk=entry.pk)))


class RejectView(APIView):
    """POST (Admin) /api/auth/club-membership/requests/{id}/reject/ {reason} - the person is shown the reason."""
    permission_classes = [IsAdmin]

    def post(self, request, pk):
        try:
            entry = service.reject(request.user, pk, request.data.get('reason', ''))
        except MembershipError as exc:
            return _error(exc)
        log_audit_event(actor=request.user, action='Club Membership Rejected', target_model='User', target_id=str(entry.user_id), details={'reason': entry.note})
        return Response(_queue_row(ClubMembershipRequest.objects.select_related('user', 'user__college', 'decided_by').get(pk=entry.pk)))


class BulkApproveView(APIView):
    """POST (Admin) /api/auth/club-membership/requests/bulk-approve/ {ids: [..]} - each on its own; failures are listed."""
    permission_classes = [IsAdmin]

    def post(self, request):
        ids = request.data.get('ids')
        try:
            result = service.bulk_approve(request.user, [int(i) for i in ids] if isinstance(ids, list) else [])
        except (TypeError, ValueError):
            return Response({'detail': 'Choose the requests to approve.', 'code': 'INVALID'}, status=status.HTTP_400_BAD_REQUEST)
        except MembershipError as exc:
            return _error(exc)
        log_audit_event(actor=request.user, action='Club Membership Bulk Approved', target_model='User', target_id='bulk', details={'done': result['done'], 'failed': len(result['failed'])})
        return Response(result)
