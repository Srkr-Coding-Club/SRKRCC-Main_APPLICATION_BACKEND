import secrets

from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel
from apps.forms.models import Form, Response


class SessionLabel(models.TextChoices):
    MORNING = 'MORNING', 'Morning'
    AFTERNOON = 'AFTERNOON', 'Afternoon'
    EVENING = 'EVENING', 'Evening'


def generate_badge_token() -> str:
    return secrets.token_urlsafe(24)


class AttendanceSession(TimeStampedModel):
    """
    One scannable attendance window for a Form's workshop/hackathon registrants
    — e.g. "Day 2, Morning". Generated and kept in sync with the owning Form's
    attendance_* config by apps.attendance.services.generate_sessions, which
    runs whenever the Form is saved (see apps/forms/serializers.py).
    """
    form = models.ForeignKey(Form, on_delete=models.CASCADE, related_name='attendance_sessions')
    day_index = models.PositiveIntegerField(help_text="0-based day offset from the form's attendance_start_date")
    session_label = models.CharField(max_length=20, choices=SessionLabel.choices)
    date = models.DateField()
    opens_at = models.DateTimeField(null=True, blank=True, help_text="Scheduled start of this session")
    closes_at = models.DateTimeField(null=True, blank=True, help_text="Scheduled end of this session")

    class Meta:
        unique_together = ('form', 'day_index', 'session_label')
        ordering = ['form', 'day_index', 'session_label']

    def __str__(self):
        return f"{self.form.title} - Day {self.day_index + 1} {self.get_session_label_display()}"


class AttendanceBadge(TimeStampedModel):
    """
    Permanent QR-code identity for one Response, issued once via
    apps.attendance.services.issue_badge as soon as that response completes on
    an attendance-enabled form. `token` is the value actually encoded into the
    registrant's QR code and looked up by the scan endpoint.
    """
    response = models.OneToOneField(Response, on_delete=models.CASCADE, related_name='attendance_badge')
    token = models.CharField(max_length=64, unique=True, db_index=True, default=generate_badge_token)
    revoked = models.BooleanField(default=False, help_text="Revoked badges are rejected by the scan endpoint.")

    def __str__(self):
        return f"AttendanceBadge({self.token[:8]}... for Response #{self.response_id})"


class AttendanceRecord(TimeStampedModel):
    """A single successful scan of a badge against a session."""
    badge = models.ForeignKey(AttendanceBadge, on_delete=models.CASCADE, related_name='records')
    session = models.ForeignKey(AttendanceSession, on_delete=models.CASCADE, related_name='records')
    scanned_at = models.DateTimeField(auto_now_add=True)
    scanned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='attendance_scans_performed',
    )

    class Meta:
        unique_together = ('badge', 'session')
        ordering = ['-scanned_at']

    def __str__(self):
        return f"AttendanceRecord(badge={self.badge_id}, session={self.session_id})"
