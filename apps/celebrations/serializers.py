from rest_framework import serializers

from .models import Celebration


class CelebrationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Celebration
        fields = ['id', 'kind', 'size', 'title', 'message', 'link_url', 'payload', 'created_at']
        read_only_fields = fields
