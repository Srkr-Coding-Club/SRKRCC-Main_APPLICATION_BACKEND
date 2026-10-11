from django.db.models import Q
from rest_framework import viewsets
from rest_framework.exceptions import ValidationError

from .models import AuditLog
from .serializers import AuditLogSerializer
from apps.core.permissions import IsAdminOrClubLead

MAX_AUDIT_LIMIT = 200


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only audit trail for admins and club leads.

    Optional, additive query filters let a module scope the trail to its own
    events without a dedicated endpoint: ``action`` (exact), ``action_prefix``,
    ``target_model``, ``target_id``, ``actor`` and ``q`` (free text), plus
    ``limit`` (list action only). Omitting every parameter preserves the
    original unfiltered, newest-first behaviour.
    """
    serializer_class = AuditLogSerializer
    permission_classes = [IsAdminOrClubLead]

    def get_queryset(self):
        queryset = AuditLog.objects.select_related('actor').all().order_by('-created_at')
        params = self.request.query_params

        action = params.get('action')
        if action:
            queryset = queryset.filter(action=action)

        action_prefix = params.get('action_prefix')
        if action_prefix:
            queryset = queryset.filter(action__istartswith=action_prefix)

        target_model = params.get('target_model')
        if target_model:
            queryset = queryset.filter(target_model__iexact=target_model)

        target_id = params.get('target_id')
        if target_id:
            queryset = queryset.filter(target_id=target_id)

        actor = params.get('actor')
        if actor:
            queryset = queryset.filter(
                Q(actor__email__icontains=actor)
                | Q(actor__first_name__icontains=actor)
                | Q(actor__last_name__icontains=actor)
            )

        search = params.get('q')
        if search:
            queryset = queryset.filter(
                Q(action__icontains=search)
                | Q(target_model__icontains=search)
                | Q(target_id__icontains=search)
                | Q(actor__email__icontains=search)
            )

        if self.action == 'list':
            limit = self._parse_limit(params.get('limit'))
            if limit is not None:
                queryset = queryset[:limit]

        return queryset

    @staticmethod
    def _parse_limit(value):
        if value in (None, ''):
            return None
        try:
            limit = int(value)
        except (TypeError, ValueError):
            raise ValidationError({'limit': 'Provide a whole number.'}) from None
        if limit < 1:
            raise ValidationError({'limit': 'Limit must be a positive whole number.'})
        return min(limit, MAX_AUDIT_LIMIT)
