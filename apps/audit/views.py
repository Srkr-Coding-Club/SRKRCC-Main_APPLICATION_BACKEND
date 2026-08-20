from rest_framework import viewsets
from .models import AuditLog
from .serializers import AuditLogSerializer
from apps.core.permissions import IsAdminOrClubLead

class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = AuditLog.objects.select_related('actor').all().order_by('-created_at')
    serializer_class = AuditLogSerializer
    permission_classes = [IsAdminOrClubLead]
