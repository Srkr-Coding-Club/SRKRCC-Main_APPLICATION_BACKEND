from django.conf import settings
from django.db import models
from apps.core.models import TimeStampedModel


class NotificationType(models.TextChoices):
    INFO = 'INFO', 'Info'
    SUCCESS = 'SUCCESS', 'Success'
    WARNING = 'WARNING', 'Warning'
    URGENT = 'URGENT', 'Urgent'


class NotificationCategory(models.TextChoices):
    GENERAL = 'GENERAL', 'General'
    HACKATHON = 'HACKATHON', 'Hackathon'
    EVENT = 'EVENT', 'Event'
    FORM = 'FORM', 'Form'
    TEAM = 'TEAM', 'Team'
    SYSTEM = 'SYSTEM', 'System'


class Notification(TimeStampedModel):
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notifications',
        db_index=True,
    )
    title = models.CharField(max_length=200)
    message = models.TextField()
    type = models.CharField(
        max_length=15,
        choices=NotificationType.choices,
        default=NotificationType.INFO,
    )
    category = models.CharField(
        max_length=20,
        choices=NotificationCategory.choices,
        default=NotificationCategory.GENERAL,
    )
    link_url = models.CharField(
        max_length=500,
        blank=True,
        default='',
        help_text='Optional in-app URL or external link for user action',
    )
    is_read = models.BooleanField(default=False, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='notifications_created',
    )

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['recipient', 'is_read', '-created_at']),
        ]

    def __str__(self):
        return f"[{self.type}] {self.title} -> {self.recipient.email} ({'READ' if self.is_read else 'UNREAD'})"
