from django.utils.text import slugify
from rest_framework import serializers
from .models import Problem, Submission, UserStreak

class ProblemSerializer(serializers.ModelSerializer):
    class Meta:
        model = Problem
        fields = '__all__'
        read_only_fields = ['slug', 'created_at', 'updated_at']

    def validate_tags(self, value):
        if not isinstance(value, list) or not all(isinstance(tag, str) and tag.strip() for tag in value):
            raise serializers.ValidationError('Tags must be a list of non-empty strings.')
        return [tag.strip() for tag in value]

    def create(self, validated_data):
        title = validated_data['title']
        base_slug = slugify(title) or 'codequest-problem'
        slug = base_slug
        suffix = 2
        while Problem.objects.filter(slug=slug).exists():
            slug = f'{base_slug}-{suffix}'
            suffix += 1
        return Problem.objects.create(slug=slug, **validated_data)

class SubmissionSerializer(serializers.ModelSerializer):
    user_name = serializers.CharField(source='user.full_name', read_only=True)
    user_email = serializers.EmailField(source='user.email', read_only=True)
    problem_title = serializers.CharField(source='problem.title', read_only=True)
    scheduled_date = serializers.DateField(source='problem.scheduled_date', read_only=True)

    class Meta:
        model = Submission
        fields = ['id', 'problem', 'problem_title', 'scheduled_date', 'user', 'user_name', 'user_email', 'code', 'language', 'is_correct', 'created_at', 'updated_at']
        # is_correct has no automated grader wired up yet; if it becomes user-writable,
        # any member can mark their own submission correct and inflate their leaderboard
        # points (see UserProfileDetailSerializer.get_points). user is set server-side
        # from the authenticated caller so nobody can submit on another user's behalf.
        read_only_fields = ['is_correct', 'user']

class UserStreakSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserStreak
        fields = '__all__'
