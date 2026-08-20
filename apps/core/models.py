from django.db import models
from django.conf import settings

class TimeStampedModel(models.Model):
    """Abstract base model providing self-updating created_at and updated_at fields."""
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class IdempotencyRecord(TimeStampedModel):
    """Stores response payloads for idempotent mutating HTTP requests."""
    key = models.CharField(max_length=128, unique=True, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='idempotency_records',
    )
    path = models.CharField(max_length=255)
    request_hash = models.CharField(max_length=64)
    response_status = models.PositiveIntegerField(default=200)
    response_body = models.JSONField(default=dict)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['key']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        return f"IdempotencyRecord({self.key} -> {self.path} [{self.response_status}])"
