from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel


class CelebrationSize(models.TextChoices):
    SMALL = 'SMALL', 'Small'
    MEDIUM = 'MEDIUM', 'Medium'
    BIG = 'BIG', 'Big'


class Celebration(TimeStampedModel):
    """
    One happy moment for one person ("your team is registered", "welcome"). It is created when the moment happens and
    shown by the site the next time that person is on it, on any device, until it is marked seen or expires.
    """
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='celebrations')
    kind = models.CharField(max_length=40, help_text='What happened, e.g. hackathon.team_registered (see kinds.py).')
    key = models.CharField(max_length=120, help_text='Names this exact moment (e.g. team-registered:12) so it is celebrated once per person.')
    size = models.CharField(max_length=6, choices=CelebrationSize.choices, default=CelebrationSize.MEDIUM)
    title = models.CharField(max_length=120)
    message = models.CharField(max_length=300, blank=True)
    link_url = models.CharField(max_length=300, blank=True, help_text='Where the card sends the person, if anywhere.')
    payload = models.JSONField(default=dict, blank=True, help_text='Plain facts for the card (team name, round...). Nothing private.')
    expires_at = models.DateTimeField(help_text='A moment nobody saw in time is dropped rather than celebrated late.')
    seen_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(fields=['user', 'key'], name='uniq_celebration_per_user_and_moment'),
        ]
        indexes = [models.Index(fields=['user', 'seen_at', 'expires_at'])]

    def __str__(self):
        return f'{self.kind} for {self.user_id} ({self.key})'


class CelebrationSetting(TimeStampedModel):
    """A person's choice about celebrations. No row means they are on."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='celebration_setting')
    enabled = models.BooleanField(default=True)
