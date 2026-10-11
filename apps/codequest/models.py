from django.db import models
from django.conf import settings
from django.utils import timezone
from apps.core.models import TimeStampedModel

class Difficulty(models.TextChoices):
    EASY = 'EASY', 'Easy'
    MEDIUM = 'MEDIUM', 'Medium'
    HARD = 'HARD', 'Hard'

class Problem(TimeStampedModel):
    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, unique=True)
    difficulty = models.CharField(max_length=10, choices=Difficulty.choices, default=Difficulty.EASY)
    statement = models.TextField()
    constraints = models.TextField(blank=True)
    sample_input = models.TextField(blank=True)
    sample_output = models.TextField(blank=True)
    tags = models.JSONField(default=list, blank=True)
    external_url = models.URLField(blank=True)
    external_platform = models.CharField(max_length=80, blank=True)
    scheduled_date = models.DateField(unique=True, help_text="Auto-publishes on this date as Today's Problem")

    def __str__(self):
        return f"[{self.scheduled_date}] {self.title} ({self.difficulty})"

class Submission(TimeStampedModel):
    problem = models.ForeignKey(Problem, on_delete=models.CASCADE, related_name='submissions')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='codequest_submissions')
    code = models.TextField()
    language = models.CharField(max_length=50, default='python')
    is_correct = models.BooleanField(default=False)
    # Review provenance: who recorded the verdict and when. Nullable because
    # historical submissions predate the review workflow; a null reviewed_at is
    # what distinguishes "still awaiting review" from a recorded rejection.
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='codequest_reviews',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['user', 'is_correct']),
            # Daily stats/heatmap scans filter by user and submission day.
            models.Index(fields=['user', 'created_at']),
            # Admin review queue filters on pending (reviewed_at IS NULL).
            models.Index(fields=['reviewed_at']),
        ]

    def __str__(self):
        return f"{self.user.email} - {self.problem.title} ({'PASS' if self.is_correct else 'FAIL'})"

class UserStreak(TimeStampedModel):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='streak')
    current_streak = models.PositiveIntegerField(default=0)
    max_streak = models.PositiveIntegerField(default=0)
    last_solved_date = models.DateField(null=True, blank=True)

    def __str__(self):
        return f"{self.user.email}: {self.current_streak} days streak"


class CodingStreak(TimeStampedModel):
    """General coding streak: consecutive days with at least one submission
    (any verdict). Distinct from UserStreak, which tracks POTD completions
    (accepted submissions against each day's scheduled problem)."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='coding_streak')
    current_streak = models.PositiveIntegerField(default=0)
    max_streak = models.PositiveIntegerField(default=0)
    last_active_date = models.DateField(null=True, blank=True)

    def __str__(self):
        return f"{self.user.email}: {self.current_streak} days coding streak"


class RewardType(models.TextChoices):
    SOLVE = 'SOLVE', 'Problem solve'
    POTD_BONUS = 'POTD_BONUS', 'POTD completion bonus'


class XPReward(TimeStampedModel):
    """Immutable, auditable XP ledger. Awards are written only by the
    server-side review flow (never trusted from the frontend) and are made
    idempotent through unique constraints — duplicate accepted reviews for
    the same user/problem/day cannot double-award.

    If a problem's difficulty changes after a solve, already-awarded ledger
    rows keep the amount and a difficulty snapshot taken at award time."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='codequest_rewards')
    reward_type = models.CharField(max_length=20, choices=RewardType.choices)
    problem = models.ForeignKey(Problem, on_delete=models.CASCADE, related_name='xp_rewards', null=True, blank=True)
    # POTD bonuses are credited to the scheduled day, not the submission day.
    scheduled_date = models.DateField(null=True, blank=True)
    amount = models.PositiveIntegerField()
    difficulty = models.CharField(max_length=10, choices=Difficulty.choices, blank=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'reward_type', 'problem'],
                condition=models.Q(reward_type='SOLVE', problem__isnull=False),
                name='codequest_uniq_solve_xp_per_user_problem',
            ),
            models.UniqueConstraint(
                fields=['user', 'reward_type', 'scheduled_date'],
                condition=models.Q(reward_type='POTD_BONUS', scheduled_date__isnull=False),
                name='codequest_uniq_potd_bonus_per_user_day',
            ),
        ]
        indexes = [
            models.Index(fields=['user', 'created_at']),
        ]

    def __str__(self):
        return f"{self.user.email}: +{self.amount} XP ({self.reward_type})"


class BadgeCategory(models.TextChoices):
    SOLVING = 'SOLVING', 'Problem Solving'
    CONSISTENCY = 'CONSISTENCY', 'Consistency'
    POTD = 'POTD', 'Problem of the Day'
    DIFFICULTY = 'DIFFICULTY', 'Difficulty Milestones'
    XP_LEVEL = 'XP_LEVEL', 'XP & Levels'


class BadgeMetric(models.TextChoices):
    """User aggregate each badge's threshold is measured against."""
    UNIQUE_SOLVED = 'UNIQUE_SOLVED', 'Unique problems solved'
    ACTIVE_DAYS = 'ACTIVE_DAYS', 'Active coding days'
    CODING_STREAK = 'CODING_STREAK', 'Longest coding streak'
    POTD_COMPLETED = 'POTD_COMPLETED', 'POTDs completed'
    POTD_STREAK = 'POTD_STREAK', 'Longest POTD streak'
    MEDIUM_SOLVED = 'MEDIUM_SOLVED', 'Medium problems solved'
    HARD_SOLVED = 'HARD_SOLVED', 'Hard problems solved'
    LIFETIME_XP = 'LIFETIME_XP', 'Lifetime XP'
    LEVEL = 'LEVEL', 'Account level'


class BadgeDefinition(TimeStampedModel):
    """Badge catalog, separate from user-earned rows. Thresholds live here so
    requirements stay configurable (editable via admin) without code changes."""
    code = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=255)
    category = models.CharField(max_length=20, choices=BadgeCategory.choices)
    metric = models.CharField(max_length=20, choices=BadgeMetric.choices)
    threshold = models.PositiveIntegerField()
    icon = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ['category', 'threshold']

    def __str__(self):
        return f"{self.name} ({self.metric} >= {self.threshold})"


class UserBadge(TimeStampedModel):
    """Earned badge instance. Unique per (user, badge) so an achievement can
    never be awarded twice; earned_at records the unlock moment."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='earned_badges')
    badge = models.ForeignKey(BadgeDefinition, on_delete=models.CASCADE, related_name='earned_by')
    earned_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-earned_at']
        constraints = [
            models.UniqueConstraint(fields=['user', 'badge'], name='codequest_uniq_user_badge'),
        ]

    def __str__(self):
        return f"{self.user.email} earned {self.badge.code}"


class CodingGoal(TimeStampedModel):
    """Per-user daily/weekly targets measured in distinct problems solved.

    Defaults come from the gamification config; members may tune their own
    targets, which only affects goal progress — never XP, streaks, or badges.
    A missing row means "use the configured defaults"."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='coding_goal')
    daily_problems = models.PositiveIntegerField(default=1)
    weekly_problems = models.PositiveIntegerField(default=5)

    def __str__(self):
        return f"{self.user.email}: {self.daily_problems}/day, {self.weekly_problems}/week"
