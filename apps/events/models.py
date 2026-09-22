from django.db import models
from apps.core.models import TimeStampedModel
from apps.forms.models import Form

class EventStatus(models.TextChoices):
    LIVE = 'LIVE', 'Live'
    CLOSED = 'CLOSED', 'Closed'

class Event(TimeStampedModel):
    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, unique=True)
    description = models.TextField()
    category = models.CharField(max_length=100, default='Workshop')
    venue = models.CharField(max_length=200, default='Campus Auditorium')
    capacity = models.PositiveIntegerField(default=100)
    poster_image = models.URLField(blank=True, null=True)
    status = models.CharField(max_length=20, choices=EventStatus.choices, default=EventStatus.LIVE)

    # Nullable: an event can be created before its schedule is finalized
    # (e.g. announced with a venue TBD) — the public card/detail page shows
    # "Date to be announced" instead of forcing a placeholder date.
    start_time = models.DateTimeField(null=True, blank=True)
    end_time = models.DateTimeField(null=True, blank=True)
    visible_from = models.DateTimeField(blank=True, null=True)
    visible_until = models.DateTimeField(blank=True, null=True)
    
    registration_form = models.ForeignKey(Form, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['start_time', 'visible_from']),
        ]

    def __str__(self):
        return self.title
