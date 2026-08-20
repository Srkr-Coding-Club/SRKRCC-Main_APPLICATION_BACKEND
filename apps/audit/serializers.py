from rest_framework import serializers
from .models import AuditLog

class AuditLogSerializer(serializers.ModelSerializer):
    actor_email = serializers.ReadOnlyField(source='actor.email')
    actor_name = serializers.SerializerMethodField()
    timestamp = serializers.DateTimeField(source='created_at', format='%Y-%m-%d %H:%M:%S', read_only=True)
    target = serializers.SerializerMethodField()

    class Meta:
        model = AuditLog
        fields = [
            'id',
            'actor',
            'actor_email',
            'actor_name',
            'action',
            'target_model',
            'target_id',
            'target',
            'details',
            'timestamp',
            'created_at',
        ]

    def get_actor_name(self, obj):
        if obj.actor:
            full_name = f"{obj.actor.first_name or ''} {obj.actor.last_name or ''}".strip()
            return full_name if full_name else (obj.actor.username or obj.actor.email)
        return "System"

    def get_target(self, obj):
        if obj.target_model:
            return f"{obj.target_model} #{obj.target_id}" if obj.target_id else obj.target_model
        return "System"
