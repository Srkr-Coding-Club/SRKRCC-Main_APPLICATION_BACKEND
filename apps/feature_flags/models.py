from django.db import models
from apps.core.models import TimeStampedModel

class FeatureFlag(TimeStampedModel):
    key = models.SlugField(max_length=50, unique=True)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    is_enabled = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.name} ({'ENABLED' if self.is_enabled else 'DISABLED'})"
