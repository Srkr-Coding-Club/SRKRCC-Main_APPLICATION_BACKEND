from django.db import models
from apps.core.models import TimeStampedModel


class AnnouncementType(models.TextChoices):
    INFO = 'INFO', 'Info'
    SUCCESS = 'SUCCESS', 'Success'
    WARNING = 'WARNING', 'Warning'
    URGENT = 'URGENT', 'Urgent'


class Announcement(TimeStampedModel):
    title = models.CharField(max_length=200)
    # Markdown — rendered client-side via the existing safe MarkdownRenderer
    # (src/components/ui/MarkdownRenderer.tsx), same as Event/Hackathon descriptions.
    message = models.TextField()
    type = models.CharField(max_length=10, choices=AnnouncementType.choices, default=AnnouncementType.INFO)
    # The admin's on/off switch. No scheduling window by design — this is a
    # manual toggle, not a visibility-window feature like Event/Hackathon have.
    is_active = models.BooleanField(default=True)

    class Meta:
        # -id as a tie-breaker: two announcements created in the same request
        # burst can land on the exact same created_at timestamp (DB timestamp
        # precision), which makes -created_at alone non-deterministic between
        # them — -id guarantees newest-first stays stable either way.
        ordering = ['-created_at', '-id']

    def __str__(self):
        return f"{self.title} ({'ACTIVE' if self.is_active else 'INACTIVE'})"
