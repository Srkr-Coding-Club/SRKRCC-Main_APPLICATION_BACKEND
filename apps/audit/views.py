from rest_framework import filters, viewsets
from .models import AuditLog
from .serializers import AuditLogSerializer
from apps.core.pagination import StandardPagination
from apps.core.permissions import IsAdminOrClubLead

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
