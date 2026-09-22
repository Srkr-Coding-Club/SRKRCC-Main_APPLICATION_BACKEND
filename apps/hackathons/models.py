from django.db import models
from django.conf import settings
from apps.core.models import TimeStampedModel
from apps.forms.models import Form

class HackathonStatus(models.TextChoices):
    LIVE = 'LIVE', 'Live'
    CLOSED = 'CLOSED', 'Closed'

class Hackathon(TimeStampedModel):
    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, unique=True)
    is_flagship = models.BooleanField(default=False, help_text="Flag as IconCoders flagship edition")
    theme = models.CharField(max_length=255)
    description = models.TextField()
    prize_pool = models.CharField(max_length=100, default='₹50,000')
    banner_image = models.URLField(blank=True, null=True)
    status = models.CharField(max_length=20, choices=HackathonStatus.choices, default=HackathonStatus.LIVE)

    start_date = models.DateTimeField()
    end_date = models.DateTimeField()
    visible_from = models.DateTimeField(blank=True, null=True)
    visible_until = models.DateTimeField(blank=True, null=True)

    registration_form = models.ForeignKey(Form, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        flag_str = " (IconCoders Flagship)" if self.is_flagship else ""
        return f"{self.title}{flag_str}"

class Team(TimeStampedModel):
    hackathon = models.ForeignKey(Hackathon, on_delete=models.CASCADE, related_name='teams')
    name = models.CharField(max_length=150)
    # SET_NULL, not CASCADE: deleting one user must not wipe the whole team
    # (other members + the team's Submission) along with them.
    leader = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='led_teams')
    members = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name='hackathon_teams', blank=True)

    def __str__(self):
        return f"Team '{self.name}' - {self.hackathon.title}"

class Submission(TimeStampedModel):
    team = models.OneToOneField(Team, on_delete=models.CASCADE, related_name='submission')
    project_title = models.CharField(max_length=200)
    description = models.TextField()
    repo_url = models.URLField()
    demo_url = models.URLField(blank=True, null=True)
    video_url = models.URLField(blank=True, null=True)
    score = models.FloatField(default=0.0)

    def __str__(self):
        return f"Submission for {self.team.name}: {self.project_title}"
