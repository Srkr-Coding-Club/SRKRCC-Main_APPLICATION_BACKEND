from django.db import models
from django.db.models import Q
from django.conf import settings
from django.utils import timezone
from apps.core.models import TimeStampedModel

class FormStatus(models.TextChoices):
    DRAFT = 'DRAFT', 'Draft'
    PUBLISHED = 'PUBLISHED', 'Published'
    CLOSED = 'CLOSED', 'Closed'
    SCHEDULED = 'SCHEDULED', 'Scheduled'
    ARCHIVED = 'ARCHIVED', 'Archived'

class FieldType(models.TextChoices):
    TEXT = 'TEXT', 'Text'
    PARAGRAPH = 'PARAGRAPH', 'Paragraph'
    EMAIL = 'EMAIL', 'Email'
    NUMBER = 'NUMBER', 'Number'
    PHONE = 'PHONE', 'Phone'
    URL = 'URL', 'URL'
    DROPDOWN = 'DROPDOWN', 'Dropdown'
    RADIO = 'RADIO', 'Radio Button'
    CHECKBOX = 'CHECKBOX', 'Checkbox'
    FILE = 'FILE', 'File Upload'
    MULTI_FILE = 'MULTI_FILE', 'Multi File Upload'
    DATE = 'DATE', 'Date'
    TIME = 'TIME', 'Time'
    SECTION = 'SECTION', 'Section Header'
    RATING = 'RATING', 'Star Rating'
    LINEAR_SCALE = 'LINEAR_SCALE', 'Linear Scale'
    MATRIX_RADIO = 'MATRIX_RADIO', 'Matrix Radio Grid'
    MATRIX_CHECKBOX = 'MATRIX_CHECKBOX', 'Matrix Checkbox Grid'
    SIGNATURE = 'SIGNATURE', 'Signature Capture'

class Form(TimeStampedModel):
    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, unique=True)
    description = models.TextField(blank=True)
    image_url = models.TextField(blank=True, null=True)
    category = models.CharField(max_length=100, default='General')
    status = models.CharField(max_length=20, choices=FormStatus.choices, default=FormStatus.DRAFT)
    version = models.PositiveIntegerField(default=1, help_text="Schema version incremented on form updates")
    allow_multiple_responses = models.BooleanField(default=False, help_text="Allow a user to submit multiple times")
    allow_response_editing = models.BooleanField(default=True, help_text="Allow users to view and update their previously submitted response")
    enable_prefill = models.BooleanField(default=True, help_text="Automatically pre-fill student profile details when limit is 1")
    max_responses_per_user = models.PositiveIntegerField(default=1, help_text="Maximum allowed submissions per user (1 for single submission)")
    allow_edits_until = models.DateTimeField(blank=True, null=True, help_text="Deadline after which responses are locked")
    open_at = models.DateTimeField(blank=True, null=True)
    close_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        indexes = [
            models.Index(fields=['status', 'open_at']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        return f"{self.title} (v{self.version} - {self.get_status_display()})"

    def evaluate_schedule_status(self, now=None):
        """
        Evaluates and transitions schedule status automatically based on current time:
        - If SCHEDULED:
            - If close_at and now >= close_at: transition to CLOSED
            - Else if open_at and now >= open_at: transition to PUBLISHED
        - If PUBLISHED:
            - If close_at and now >= close_at: transition to CLOSED (deadline passed)
        Returns True if status was changed and saved.
        """
        if not now:
            now = timezone.now()
        changed = False

        if self.status == FormStatus.SCHEDULED:
            if self.close_at and now >= self.close_at:
                self.status = FormStatus.CLOSED
                changed = True
            elif self.open_at and now >= self.open_at:
                self.status = FormStatus.PUBLISHED
                changed = True
        elif self.status == FormStatus.PUBLISHED:
            if self.close_at and now >= self.close_at:
                self.status = FormStatus.CLOSED
                changed = True

        if changed:
            self.save(update_fields=['status', 'updated_at'])
        return changed


def sync_all_scheduled_form_statuses(now=None):
    """
    Bulk updates all scheduled and published forms whose start or end timestamps have passed:
    1. SCHEDULED forms with now >= close_at -> CLOSED
    2. SCHEDULED forms with now >= open_at and (close_at is None or now < close_at) -> PUBLISHED
    3. PUBLISHED forms with close_at is not None and now >= close_at -> CLOSED
    """
    if not now:
        now = timezone.now()

    # 1. SCHEDULED forms past close_at -> CLOSED
    Form.objects.filter(
        status=FormStatus.SCHEDULED,
        close_at__isnull=False,
        close_at__lte=now,
    ).update(status=FormStatus.CLOSED, updated_at=now)

    # 2. SCHEDULED forms past open_at (and not past close_at) -> PUBLISHED
    Form.objects.filter(
        status=FormStatus.SCHEDULED,
        open_at__isnull=False,
        open_at__lte=now,
    ).filter(
        Q(close_at__isnull=True) | Q(close_at__gt=now)
    ).update(status=FormStatus.PUBLISHED, updated_at=now)

    # 3. PUBLISHED forms past close_at -> CLOSED
    Form.objects.filter(
        status=FormStatus.PUBLISHED,
        close_at__isnull=False,
        close_at__lte=now,
    ).update(status=FormStatus.CLOSED, updated_at=now)

class ActiveFieldManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)

class FormField(TimeStampedModel):
    form = models.ForeignKey(Form, on_delete=models.CASCADE, related_name='fields')
    label = models.CharField(max_length=255)
    type = models.CharField(max_length=20, choices=FieldType.choices, default=FieldType.TEXT)
    placeholder = models.CharField(max_length=255, blank=True)
    is_required = models.BooleanField(default=False)
    options = models.JSONField(default=list, blank=True, help_text="Choices for dropdown/radio/checkbox")
    rows = models.JSONField(default=list, blank=True, help_text="Row labels for MATRIX field types")
    min_value = models.IntegerField(blank=True, null=True, help_text="Minimum value for RATING / LINEAR_SCALE")
    max_value = models.IntegerField(blank=True, null=True, help_text="Maximum value for RATING / LINEAR_SCALE")
    conditional_logic = models.JSONField(
        default=dict, blank=True,
        help_text=(
            "Multi-rule visibility: {'logic': 'AND', 'rules': [{'if': field_id, 'operator': 'equals', 'value': 'x'}]}. "
            "Legacy single-rule format {'if': field_id, 'equals': 'x'} is also supported for backward compatibility."
        )
    )
    validation_rules = models.JSONField(default=dict, blank=True, help_text="Validation rules e.g. {'max_size_mb': 10, 'allowed_extensions': ['pdf']}")
    order = models.IntegerField(default=0)
    is_deleted = models.BooleanField(default=False, help_text="Soft-deleted fields preserved for historical response export")

    objects = ActiveFieldManager()
    all_objects = models.Manager()

    class Meta:
        ordering = ['order']

    def __str__(self):
        return f"{self.form.title} -> {self.label} ({self.type})"

class Response(TimeStampedModel):
    form = models.ForeignKey(Form, on_delete=models.CASCADE, related_name='responses')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    form_version = models.PositiveIntegerField(default=1, help_text="Form schema version snapshot at submission time")
    is_test_submission = models.BooleanField(default=False, help_text="Isolated preview test submission flag")
    submitted_at = models.DateTimeField(auto_now_add=True)
    is_manual_entry = models.BooleanField(default=False, help_text="Flagged if added via Admin Manual Override on closed form")
    created_by_admin = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='admin_manual_responses')

    class Meta:
        indexes = [
            models.Index(fields=['form', 'submitted_at']),
            models.Index(fields=['user', 'submitted_at']),
        ]

    def __str__(self):
        user_str = self.user.email if self.user else ("Admin Entry" if self.is_manual_entry else "Anonymous")
        return f"Response #{self.id} for {self.form.title} (v{self.form_version}) by {user_str}"

class Answer(TimeStampedModel):
    response = models.ForeignKey(Response, on_delete=models.CASCADE, related_name='answers')
    field = models.ForeignKey(FormField, on_delete=models.CASCADE)
    value = models.JSONField(blank=True, null=True)

    class Meta:
        indexes = [
            models.Index(fields=['response', 'field']),
        ]

    def __str__(self):
        return f"Answer to {self.field.label}: {self.value}"


class BulkIngestSession(models.Model):
    STATUS_COMPLETED = 'COMPLETED'
    STATUS_PARTIAL = 'PARTIAL'
    STATUS_FAILED = 'FAILED'
    STATUS_CHOICES = [
        (STATUS_COMPLETED, 'Completed'),
        (STATUS_PARTIAL, 'Partial'),
        (STATUS_FAILED, 'Failed'),
    ]

    form = models.ForeignKey(Form, on_delete=models.CASCADE, related_name='ingest_sessions')
    idempotency_key = models.CharField(max_length=64, unique=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ingest_sessions',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    imported_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    duplicate_count = models.PositiveIntegerField(default=0)
    error_log = models.JSONField(default=list)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_COMPLETED)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"BulkIngestSession #{self.id} for {self.form.title} ({self.status})"


class MemberNote(models.Model):
    """Admin notes attached to a user/member profile."""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='admin_notes',
    )
    note = models.TextField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='authored_notes',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Note for {self.user.email} by {self.created_by}"
