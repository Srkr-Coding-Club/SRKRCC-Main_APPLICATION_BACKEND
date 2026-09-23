from rest_framework import serializers
from .models import Announcement


class AnnouncementSerializer(serializers.ModelSerializer):
    class Meta:
        model = Announcement
        fields = ['id', 'title', 'message', 'type', 'is_active', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']

    def validate_title(self, value):
        title = (value or '').strip()
        if not title:
            raise serializers.ValidationError("Title is required.")
        return title

    def validate_message(self, value):
        message = (value or '').strip()
        if not message:
            raise serializers.ValidationError("Message is required.")
        return message
