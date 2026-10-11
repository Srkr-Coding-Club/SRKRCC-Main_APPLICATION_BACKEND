from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import serializers
from .models import Problem, Submission, UserStreak

HTTPS_URL_VALIDATOR = URLValidator(
    schemes=['https'],
    message='Enter a valid HTTPS URL (https://...).',
)


def _reject_past_date(value):
    if value < timezone.localdate():
        raise serializers.ValidationError(f'Scheduled date {value.isoformat()} cannot be before today.')


class ProblemSerializer(serializers.ModelSerializer):
    # Declared explicitly instead of model-inferred so the auto-added UniqueValidator
    # is replaced by validate_scheduled_date, which names the conflicting date and
    # correctly ignores a problem's own date when it is being edited.
    scheduled_date = serializers.DateField()
    # External judge link is required for every scheduled problem.
    external_url = serializers.URLField(required=True, allow_blank=False)

    class Meta:
        model = Problem
        fields = '__all__'
        read_only_fields = ['slug', 'created_at', 'updated_at']

    def validate_tags(self, value):
        if not isinstance(value, list) or not all(isinstance(tag, str) and tag.strip() for tag in value):
            raise serializers.ValidationError('Tags must be a list of non-empty strings.')
        return [tag.strip() for tag in value]

    def validate_external_url(self, value):
        value = value.strip()
        if not value:
            return value
        try:
            HTTPS_URL_VALIDATOR(value)
        except DjangoValidationError:
            raise serializers.ValidationError('Enter a valid HTTPS URL (https://...).') from None
        return value

    def validate_scheduled_date(self, value):
        if self.instance is not None and value == self.instance.scheduled_date:
            return value
        _reject_past_date(value)
        conflicts = Problem.objects.filter(scheduled_date=value)
        if self.instance is not None:
            conflicts = conflicts.exclude(pk=self.instance.pk)
        if conflicts.exists():
            raise serializers.ValidationError(f'A problem is already scheduled on {value.isoformat()}.')
        return value

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


class BatchScheduleProblemSerializer(serializers.ModelSerializer):
    # Declared explicitly instead of model-inferred so the auto-added UniqueValidator
    # is replaced by validate_scheduled_date, which names the conflicting date.
    scheduled_date = serializers.DateField()
    # External judge link is required for every scheduled problem.
    external_url = serializers.URLField(required=True, allow_blank=False)

    class Meta:
        model = Problem
        fields = ['title', 'difficulty', 'statement', 'constraints', 'sample_input', 'sample_output', 'tags', 'external_url', 'external_platform', 'scheduled_date']

    def validate_external_url(self, value):
        value = value.strip()
        if not value:
            return value
        try:
            HTTPS_URL_VALIDATOR(value)
        except DjangoValidationError:
            raise serializers.ValidationError('Enter a valid HTTPS URL (https://...).') from None
        return value

    def validate_scheduled_date(self, value):
        _reject_past_date(value)
        if Problem.objects.filter(scheduled_date=value).exists():
            raise serializers.ValidationError(f'A problem is already scheduled on {value.isoformat()}.')
        return value

    def create(self, validated_data):
        title = validated_data['title']
        base_slug = slugify(title) or 'codequest-problem'
        slug = base_slug
        suffix = 2
        while Problem.objects.filter(slug=slug).exists():
            slug = f'{base_slug}-{suffix}'
            suffix += 1
        return Problem.objects.create(slug=slug, **validated_data)


class BatchScheduleSerializer(serializers.Serializer):
    problems = BatchScheduleProblemSerializer(many=True, min_length=1, max_length=5, required=False)
    titles = serializers.ListField(
        child=serializers.CharField(max_length=200),
        min_length=1,
        max_length=5,
        required=False,
        help_text='Problem titles (1-5 required)'
    )
    scheduled_dates = serializers.ListField(
        child=serializers.DateField(),
        min_length=1,
        max_length=5,
        required=False,
        help_text='Scheduled dates (1-5 required, must be unique)'
    )
    difficulties = serializers.ListField(
        child=serializers.ChoiceField(choices=[('EASY', 'Easy'), ('MEDIUM', 'Medium'), ('HARD', 'Hard')]),
        min_length=1,
        max_length=5,
        required=False,
        default=list,
        help_text='Difficulties for each problem (EASY/MEDIUM/HARD)'
    )
    tags = serializers.ListField(
        child=serializers.CharField(max_length=50),
        min_length=0,
        max_length=5,
        required=False,
        default=list,
        help_text='Tags for each problem (comma-separated or list)'
    )
    external_urls = serializers.ListField(
        child=serializers.CharField(max_length=500),
        min_length=1,
        max_length=5,
        required=False,
        help_text='External URLs for each title (required when titles is used)'
    )

    def validate(self, data):
        entries = data.get('problems')
        if entries is not None:
            dates = [entry['scheduled_date'] for entry in entries]
            duplicates = sorted({value for value in dates if dates.count(value) > 1})
            if duplicates:
                raise serializers.ValidationError({'problems': f'Scheduled dates must be unique: {", ".join(value.isoformat() for value in duplicates)}.'})
            existing_dates = set(Problem.objects.filter(scheduled_date__in=dates).values_list('scheduled_date', flat=True))
            if existing_dates:
                raise serializers.ValidationError({'problems': f'Already scheduled date(s): {", ".join(map(str, sorted(existing_dates)))}.'})
            return data

        if 'titles' not in data or 'scheduled_dates' not in data:
            raise serializers.ValidationError({'problems': 'Provide a problems array or matching titles and scheduled_dates arrays.'})

        titles = data.get('titles', [])
        dates = data.get('scheduled_dates', [])
        difficulties = data.get('difficulties', [])
        tags = data.get('tags', [])
        urls = data.get('external_urls', [])
        if len(urls) != len(titles):
            raise serializers.ValidationError({'external_urls': f'Number of external URLs ({len(urls)}) must match number of titles ({len(titles)}).'})

        if len(titles) != len(dates):
            raise serializers.ValidationError('Number of titles must match number of dates.')

        if len(titles) > 5:
            raise serializers.ValidationError('Maximum 5 problems can be scheduled at once.')

        if len(difficulties) > 0 and len(difficulties) != len(titles):
            raise serializers.ValidationError('Number of difficulties must match number of titles.')

        if len(tags) > 0 and len(tags) != len(titles):
            raise serializers.ValidationError('Number of tag lists must match number of titles.')

        for value in dates:
            _reject_past_date(value)

        duplicates = sorted({value for value in dates if dates.count(value) > 1})
        if duplicates:
            raise serializers.ValidationError(f'Scheduled dates must be unique: {", ".join(value.isoformat() for value in duplicates)}.')

        existing_dates = set(Problem.objects.filter(scheduled_date__in=dates).values_list('scheduled_date', flat=True))
        if existing_dates:
            raise serializers.ValidationError(f'Already scheduled date(s): {", ".join(map(str, sorted(existing_dates)))}.')

        return data

