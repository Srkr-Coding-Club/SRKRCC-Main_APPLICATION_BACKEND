from django.db import models
from django.conf import settings
from apps.core.models import TimeStampedModel

class AuditLog(TimeStampedModel):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=100)
    target_model = models.CharField(max_length=100, blank=True)
    target_id = models.CharField(max_length=100, blank=True)
    details = models.JSONField(default=dict, blank=True)

    def __str__(self):
        actor_str = self.actor.email if self.actor else "System"
        return f"[{self.created_at}] {actor_str} - {self.action} on {self.target_model}:{self.target_id}"
