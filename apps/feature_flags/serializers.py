from rest_framework import serializers
from .models import FeatureFlag

class FeatureFlagSerializer(serializers.ModelSerializer):
    class Meta:
        model = FeatureFlag
        fields = ['id', 'key', 'name', 'description', 'is_enabled', 'updated_at']
