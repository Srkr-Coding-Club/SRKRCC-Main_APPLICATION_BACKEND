from django.db import models
from django.conf import settings
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
    scheduled_date = models.DateField(unique=True, help_text="Auto-publishes on this date as Today's Problem")

    def __str__(self):
        return f"[{self.scheduled_date}] {self.title} ({self.difficulty})"

class Submission(TimeStampedModel):
    problem = models.ForeignKey(Problem, on_delete=models.CASCADE, related_name='submissions')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='codequest_submissions')
    code = models.TextField()
    language = models.CharField(max_length=50, default='python')
    is_correct = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=['user', 'is_correct']),
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
