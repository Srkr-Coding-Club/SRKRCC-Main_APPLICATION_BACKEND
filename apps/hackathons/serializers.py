from rest_framework import serializers, viewsets, permissions
from .models import Hackathon, Team, Submission

class HackathonSerializer(serializers.ModelSerializer):
    class Meta:
        model = Hackathon
        fields = '__all__'

class TeamSerializer(serializers.ModelSerializer):
    class Meta:
        model = Team
        fields = '__all__'
        # leader is set server-side from the authenticated caller (see TeamViewSet.perform_create)
        # so a member can't register a team with someone else as leader.
        read_only_fields = ['leader']

class SubmissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Submission
        fields = '__all__'
        # score is judge/admin-only (assigned via Django admin); no grading UI writes it yet,
        # so leaving it writable would let a team self-assign its own hackathon score.
        read_only_fields = ['score']
