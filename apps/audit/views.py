import re
from datetime import datetime

from django.db.models import Count
from django.db.models.functions import TruncMonth
from django.utils import timezone
from rest_framework import filters, status, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import AuditLog
from .serializers import AuditLogSerializer
from .utils import log_audit_event
from apps.core.pagination import StandardPagination
from apps.core.permissions import IsAdmin, IsAdminOrClubLead, _is_admin

MONTH_PATTERN = re.compile(r'^(\d{4})-(0[1-9]|1[0-2])$')


def month_bounds(month: str):
    """[start, end) of a 'YYYY-MM' month in the project timezone."""
    match = MONTH_PATTERN.match(month or '')
    if not match:
        raise ValidationError({'month': 'Use the format YYYY-MM.'})
    year, number = int(match.group(1)), int(match.group(2))
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime(year, number, 1), tz)
    end = timezone.make_aware(datetime(year + (number == 12), number % 12 + 1, 1), tz)
    return start, end


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = AuditLog.objects.select_related('actor').all().order_by('-created_at')
    serializer_class = AuditLogSerializer
    permission_classes = [IsAdminOrClubLead]
    pagination_class = StandardPagination
    filter_backends = [filters.SearchFilter]
    search_fields = [
        'action',
        'target_model',
        'target_id',
        'actor__email',
        'actor__first_name',
        'actor__last_name',
    ]

    def get_queryset(self):
        queryset = super().get_queryset()
        month = self.request.query_params.get('month')
        if month:
            start, end = month_bounds(month)
            queryset = queryset.filter(created_at__gte=start, created_at__lt=end)
        return queryset


class AuditMonthListView(APIView):
    """GET /api/audit/months/ - how many audit entries each month holds, newest month first."""
    permission_classes = [IsAdminOrClubLead]

    def get(self, request):
        rows = (
            AuditLog.objects.annotate(month=TruncMonth('created_at'))
            .values('month').annotate(count=Count('id')).order_by('-month')
        )
        return Response({
            'months': [{'month': row['month'].strftime('%Y-%m'), 'count': row['count']} for row in rows],
            'can_delete': _is_admin(request.user),
        })


class AuditMonthDeleteView(APIView):
    """
    DELETE /api/audit/months/<YYYY-MM>/ - permanently remove one month of audit entries (admins only).

    The deletion is itself recorded as a new audit entry, which is created after the
    delete so it is never removed by the very action it describes.
    """
    permission_classes = [IsAdmin]

    def delete(self, request, month):
        start, end = month_bounds(month)
        deleted, _ = AuditLog.objects.filter(created_at__gte=start, created_at__lt=end).delete()
        log_audit_event(
            actor=request.user, action='Deleted Audit Logs', target_model='AuditLog', target_id=month,
            details={'month': month, 'deleted_entries': deleted},
        )
        return Response({'month': month, 'deleted': deleted}, status=status.HTTP_200_OK)
