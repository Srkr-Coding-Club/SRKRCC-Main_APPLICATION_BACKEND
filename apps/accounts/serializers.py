from django.db import models
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from django.contrib.auth import get_user_model

User = get_user_model()

class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    """
    Custom JWT Serializer embedding user profile data and roles directly into tokens.
    """
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token['email'] = user.email
        token['username'] = user.username
        token['role'] = user.role
        token['first_name'] = user.first_name
        token['last_name'] = user.last_name
        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        data['user'] = {
            'id': self.user.id,
            'email': self.user.email,
            'username': self.user.username,
            'first_name': self.user.first_name,
            'last_name': self.user.last_name,
            'role': self.user.role,
            'roll_number': self.user.roll_number,
            'branch': self.user.branch,
            'year': self.user.year,
        }
        return data

class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = [
            'id', 'username', 'email', 'first_name', 'last_name', 
            'role', 'roll_number', 'branch', 'year', 'phone_number',
            'github_profile', 'linkedin_profile', 'created_at'
        ]
        read_only_fields = ['id', 'role', 'created_at']

class UserProfileDetailSerializer(serializers.ModelSerializer):
    """
    Detailed profile serializer computing real database-driven statistics,
    registered events/forms, streak count, and earned badges.
    """
    streak = serializers.SerializerMethodField()
    points = serializers.SerializerMethodField()
    events_count = serializers.SerializerMethodField()
    projects_count = serializers.SerializerMethodField()
    registered_events = serializers.SerializerMethodField()
    badges = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'username', 'email', 'first_name', 'last_name', 
            'role', 'roll_number', 'branch', 'year', 'phone_number',
            'github_profile', 'linkedin_profile', 'created_at',
            'streak', 'points', 'events_count', 'projects_count',
            'registered_events', 'badges'
        ]
        read_only_fields = ['id', 'role', 'created_at']

    def get_streak(self, obj):
        try:
            return obj.streak.current_streak
        except Exception:
            return 0

    def get_points(self, obj):
        points = 0
        try:
            # 50 points per correct problem solved
            points += obj.codequest_submissions.filter(is_correct=True).count() * 50
        except Exception:
            pass
        try:
            from apps.forms.models import Response
            # 25 points per form/event registration
            points += Response.objects.filter(user=obj, is_test_submission=False).count() * 25
        except Exception:
            pass
        return max(points, 50)  # Baseline onboarding points

    def get_events_count(self, obj):
        try:
            from apps.forms.models import Response
            return Response.objects.filter(user=obj, is_test_submission=False).count()
        except Exception:
            return 0

    def get_projects_count(self, obj):
        try:
            from apps.hackathons.models import Team
            return Team.objects.filter(models.Q(leader=obj) | models.Q(members=obj)).distinct().count()
        except Exception:
            return 0

    def get_registered_events(self, obj):
        events_list = []
        try:
            from apps.forms.models import Response
            user_responses = Response.objects.filter(user=obj, is_test_submission=False).select_related('form').order_by('-submitted_at')
            for resp in user_responses:
                form = resp.form
                events_list.append({
                    'id': resp.id,
                    'form_slug': form.slug,
                    'title': form.title,
                    'track': form.category or 'General Track',
                    'date': str(form.open_at or resp.submitted_at.date() if resp.submitted_at else 'Active'),
                    'status': 'Seat Confirmed' if form.status == 'PUBLISHED' else 'Registration Received',
                    'badgeBg': 'bg-emerald-50 dark:bg-emerald-950/40 text-emerald-600 dark:text-emerald-400' if form.status == 'PUBLISHED' else 'bg-orange-50 dark:bg-orange-950/40 text-[#FF7A00]',
                })
        except Exception:
            pass
        return events_list

    def get_badges(self, obj):
        badges = [
            {
                'id': 'verified_member',
                'title': 'SRKRCC Member',
                'description': f"Active {obj.get_role_display() if hasattr(obj, 'get_role_display') else obj.role} of SRKR Coding Club",
                'icon': 'shield',
                'tone': 'emerald'
            }
        ]
        try:
            if hasattr(obj, 'streak') and obj.streak.current_streak >= 5:
                badges.append({
                    'id': 'streak_master',
                    'title': 'Streak Master',
                    'description': f"Maintained {obj.streak.current_streak}+ consecutive days of problem solving",
                    'icon': 'flame',
                    'tone': 'orange'
                })
        except Exception:
            pass

        if obj.role in ['ADMIN', 'CLUB_LEAD']:
            badges.append({
                'id': 'lead_badge',
                'title': 'Executive Lead',
                'description': 'Authorized administrative and club management privileges',
                'icon': 'award',
                'tone': 'purple'
            })
        return badges

class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=6)

    class Meta:
        model = User
        fields = ['username', 'email', 'password', 'first_name', 'last_name', 'role', 'roll_number', 'branch', 'year']

    def create(self, validated_data):
        user = User.objects.create_user(
            username=validated_data['username'],
            email=validated_data['email'],
            password=validated_data['password'],
            first_name=validated_data.get('first_name', ''),
            last_name=validated_data.get('last_name', ''),
            roll_number=validated_data.get('roll_number', ''),
            branch=validated_data.get('branch', ''),
            year=validated_data.get('year', None),
        )
        if 'role' in validated_data and validated_data['role']:
            user.role = validated_data['role']
            user.save()
        return user
