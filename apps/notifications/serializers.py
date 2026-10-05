from rest_framework import serializers
from .models import Notification, NotificationType, NotificationCategory


class NotificationSerializer(serializers.ModelSerializer):
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = [
            'id',
            'title',
            'message',
            'type',
            'category',
            'link_url',
            'is_read',
            'read_at',
            'created_at',
            'created_by_name',
        ]
        read_only_fields = ['id', 'created_at', 'created_by_name']

    def get_created_by_name(self, obj):
        if obj.created_by:
            return f"{obj.created_by.first_name} {obj.created_by.last_name}".strip() or obj.created_by.username
        return 'System'


class BroadcastNotificationSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=200)
    message = serializers.CharField()
    type = serializers.ChoiceField(choices=NotificationType.choices, default=NotificationType.INFO)
    category = serializers.ChoiceField(choices=NotificationCategory.choices, default=NotificationCategory.GENERAL)
    link_url = serializers.CharField(max_length=500, required=False, allow_blank=True, default='')
    
    channels = serializers.ListField(
        child=serializers.ChoiceField(choices=['IN_APP', 'EMAIL']),
        default=['IN_APP'],
    )
    audience = serializers.ChoiceField(
        choices=['ALL', 'ROLE', 'HACKATHON', 'USERS'],
        default='ALL',
    )
    target_role = serializers.CharField(required=False, allow_blank=True, default='')
    target_hackathon_slug = serializers.CharField(required=False, allow_blank=True, default='')
    target_user_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        default=list,
    )

    def validate(self, attrs):
        if not attrs.get('channels'):
            raise serializers.ValidationError({'channels': 'At least one delivery channel (IN_APP or EMAIL) is required.'})
        
        aud = attrs.get('audience')
        if aud == 'ROLE' and not attrs.get('target_role'):
            raise serializers.ValidationError({'target_role': 'Target role is required when audience is ROLE.'})
        if aud == 'HACKATHON' and not attrs.get('target_hackathon_slug'):
            raise serializers.ValidationError({'target_hackathon_slug': 'Target hackathon slug is required when audience is HACKATHON.'})
        if aud == 'USERS' and not attrs.get('target_user_ids'):
            raise serializers.ValidationError({'target_user_ids': 'At least one user ID is required when audience is USERS.'})
        
        return attrs
