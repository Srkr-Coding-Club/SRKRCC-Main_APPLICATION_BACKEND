from rest_framework import serializers

from .models import AttendanceBadge, AttendanceSession


class AttendanceSessionSerializer(serializers.ModelSerializer):
    session_label_display = serializers.CharField(source='get_session_label_display', read_only=True)

    class Meta:
        model = AttendanceSession
        fields = [
            'id', 'form', 'day_index', 'session_label', 'session_label_display',
            'date', 'opens_at', 'closes_at',
        ]
        read_only_fields = fields


class AttendanceBadgeSerializer(serializers.ModelSerializer):
    # `response` is a OneToOneField; expose the plain id rather than nesting
    # the full Response representation here (thin serializer, per this
    # codebase's convention — see apps/forms/serializers.py's ResponseSerializer).
    response_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = AttendanceBadge
        fields = ['token', 'response_id', 'revoked']
        read_only_fields = fields


class AttendanceScanRequestSerializer(serializers.Serializer):
    token = serializers.CharField()
    session_id = serializers.IntegerField()
