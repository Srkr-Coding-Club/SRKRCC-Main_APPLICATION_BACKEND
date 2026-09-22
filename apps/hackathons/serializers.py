from django.utils import timezone
from rest_framework import serializers
from .models import Hackathon, Team, Submission

class HackathonSerializer(serializers.ModelSerializer):
    form_slug = serializers.CharField(source='registration_form.slug', read_only=True)
    form_title = serializers.CharField(source='registration_form.title', read_only=True)
    registration_count = serializers.IntegerField(read_only=True, default=0)
    team_count = serializers.IntegerField(read_only=True, default=0)
    is_hidden = serializers.SerializerMethodField()

    class Meta:
        model = Hackathon
        fields = [
            'id', 'title', 'slug', 'is_flagship', 'theme', 'description',
            'prize_pool', 'banner_image', 'status', 'start_date', 'end_date',
            'visible_from', 'visible_until', 'is_hidden', 'registration_form',
            'form_slug', 'form_title', 'registration_count', 'team_count',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['status']

    def get_is_hidden(self, obj) -> bool:
        """See EventSerializer.get_is_hidden — identical window logic."""
        now = timezone.now()
        if obj.visible_from and obj.visible_from > now:
            return True
        if obj.visible_until and obj.visible_until <= now:
            return True
        return False

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
