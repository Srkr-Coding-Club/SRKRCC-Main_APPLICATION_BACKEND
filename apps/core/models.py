import uuid
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


class EmailTemplate(TimeStampedModel):
    """
    Stores reusable email subject and body templates with strict allowed parameter whitelists.
    """
    name = models.CharField(max_length=100, unique=True, db_index=True) # e.g. member_welcome, event_confirmation
    display_title = models.CharField(max_length=200, default='')
    subject_template = models.CharField(max_length=255)
    html_template = models.TextField()
    text_template = models.TextField(blank=True, default='')
    allowed_parameters = models.JSONField(
        default=list,
        help_text="Whitelisted parameter keys: ['full_name', 'email', 'club_id', 'branch', 'setup_password_url']"
    )
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_email_templates'
    )

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"EmailTemplate({self.name} | {self.display_title or self.name})"


class EmailJobStatus(models.TextChoices):
    PENDING = 'PENDING', 'Pending'
    PROCESSING = 'PROCESSING', 'Processing'
    COMPLETED = 'COMPLETED', 'Completed'
    FAILED = 'FAILED', 'Failed'
    PARTIALLY_FAILED = 'PARTIALLY_FAILED', 'Partially Failed'


class EmailJob(TimeStampedModel):
    """
    Asynchronous batch email campaign dispatcher tracking aggregate delivery progress.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    template = models.ForeignKey(EmailTemplate, on_delete=models.PROTECT, related_name='jobs')
    campaign_name = models.CharField(max_length=200, blank=True, default='')
    total_recipients = models.IntegerField(default=0)
    sent_count = models.IntegerField(default=0)
    failed_count = models.IntegerField(default=0)
    status = models.CharField(
        max_length=20,
        choices=EmailJobStatus.choices,
        default=EmailJobStatus.PENDING,
        db_index=True
    )
    error_summary = models.TextField(blank=True, default='')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='triggered_email_jobs'
    )

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"EmailJob({self.id} | {self.template.name} | {self.status} | {self.sent_count}/{self.total_recipients})"


class DeliveryStatus(models.TextChoices):
    PENDING = 'PENDING', 'Pending'
    SENT = 'SENT', 'Sent'
    FAILED = 'FAILED', 'Failed'
    RETRYING = 'RETRYING', 'Retrying'


class EmailDelivery(TimeStampedModel):
    """
    Recipient-level delivery record providing audit fidelity on who received what email and when.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(EmailJob, on_delete=models.CASCADE, related_name='deliveries')
    recipient_email = models.EmailField(db_index=True)
    recipient_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='email_deliveries'
    )
    status = models.CharField(
        max_length=20,
        choices=DeliveryStatus.choices,
        default=DeliveryStatus.PENDING,
        db_index=True
    )
    rendered_subject = models.CharField(max_length=255, blank=True, default='')
    error_message = models.TextField(blank=True, default='')
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['job', 'status']),
            models.Index(fields=['recipient_email']),
        ]

    def __str__(self):
        return f"EmailDelivery({self.recipient_email} | {self.status} | Job={self.job_id})"


# Import DMC models so they are discovered by Django's migration framework
from apps.core.dmc.models import ExportJob  # noqa: F401, E402
