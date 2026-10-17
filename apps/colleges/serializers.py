from rest_framework import serializers

from .models import College


class CollegeSerializer(serializers.ModelSerializer):
    class Meta:
        model = College
        fields = ['id', 'code', 'name', 'district', 'state']
        read_only_fields = fields
