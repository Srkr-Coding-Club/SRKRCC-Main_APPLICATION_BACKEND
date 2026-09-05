from rest_framework import serializers, viewsets, permissions
from .models import Problem, Submission, UserStreak

class ProblemSerializer(serializers.ModelSerializer):
    class Meta:
        model = Problem
        fields = '__all__'

class SubmissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Submission
        fields = '__all__'
        # is_correct has no automated grader wired up yet; if it becomes user-writable,
        # any member can mark their own submission correct and inflate their leaderboard
        # points (see UserProfileDetailSerializer.get_points). user is set server-side
        # from the authenticated caller so nobody can submit on another user's behalf.
        read_only_fields = ['is_correct', 'user']

class UserStreakSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserStreak
        fields = '__all__'
