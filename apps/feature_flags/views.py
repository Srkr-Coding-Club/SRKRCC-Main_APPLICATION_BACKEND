from rest_framework import viewsets, permissions
from .models import FeatureFlag
from .serializers import FeatureFlagSerializer
from apps.audit.utils import log_audit_event

class FeatureFlagViewSet(viewsets.ModelViewSet):
    queryset = FeatureFlag.objects.all()
    serializer_class = FeatureFlagSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    lookup_field = 'key'

    def perform_update(self, serializer):
        flag = serializer.save()
        log_audit_event(
            actor=self.request.user,
            action=f"Toggled Feature Flag to {'ENABLED' if flag.is_enabled else 'DISABLED'}",
            target_model="FeatureFlag",
            target_id=flag.key,
            details={"key": flag.key, "is_enabled": flag.is_enabled}
        )
