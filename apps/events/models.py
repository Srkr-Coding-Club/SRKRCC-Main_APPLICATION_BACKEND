from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
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
    # (e.g. announced with a venue TBD) - the public card/detail page shows
    # "Date to be announced" instead of forcing a placeholder date.
    start_time = models.DateTimeField(null=True, blank=True)
    end_time = models.DateTimeField(null=True, blank=True)
    visible_from = models.DateTimeField(blank=True, null=True)
    visible_until = models.DateTimeField(blank=True, null=True)
    
    registration_form = models.ForeignKey(Form, on_delete=models.SET_NULL, null=True, blank=True)

    # Whether registered people may cancel on their own, and how long before the start that stops.
    cancellation_locked = models.BooleanField(default=False, help_text='When on, registered people cannot cancel on their own; organizers still can.')
    cancellation_cutoff_hours = models.PositiveSmallIntegerField(
        default=0, validators=[MaxValueValidator(24 * 30)],
        help_text='Cancelling stops this many hours before the start. 0 = until the event starts.',
    )

    # Attendance certificates: nobody can open one until an organizer releases them.
    certificates_released = models.BooleanField(default=False)
    certificate_body = models.CharField(max_length=600, blank=True, help_text='Wording with {name} and {event}; blank uses the default.')
    certificate_signatory_name = models.CharField(max_length=100, blank=True)
    certificate_signatory_title = models.CharField(max_length=100, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['start_time', 'visible_from']),
        ]

    def __str__(self):
        return self.title


class EventFeedback(TimeStampedModel):
    """One attendee's rating of an event they attended; see apps.events.feedback for who may leave it."""
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='feedback')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='event_feedback')
    rating = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    would_recommend = models.BooleanField()
    went_well = models.CharField(max_length=500, blank=True)
    to_improve = models.CharField(max_length=500, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['event', 'user'], name='one_feedback_per_user_per_event')]
        indexes = [models.Index(fields=['event', 'created_at'])]

    def __str__(self):
        return f"Feedback({self.rating}/5 on {self.event_id} by {self.user_id})"


class EventReminder(TimeStampedModel):
    """Records that a person was reminded about an event at a given lead time, so each reminder goes out once."""
    class Lead(models.TextChoices):
        DAY = 'DAY', 'A day before'
        HOUR = 'HOUR', 'An hour before'

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='reminders')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='event_reminders')
    lead = models.CharField(max_length=10, choices=Lead.choices)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['event', 'user', 'lead'], name='one_reminder_per_lead')]

    def __str__(self):
        return f'Reminder({self.lead} for event {self.event_id}, user {self.user_id})'
