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


# =============================================================================
# Universal Backup Engine & Schemaless Raw Vault Models
# =============================================================================

class BackupJobStatus(models.TextChoices):
    PENDING = 'PENDING', 'Pending Intake'
    PARSED = 'PARSED', 'Parsed & Preserved'
    AWAITING_DOMAIN_SELECTION = 'AWAITING_DOMAIN_SELECTION', 'Awaiting Domain Selection'
    READY = 'READY', 'Ready for Import'
    PARTIALLY_IMPORTED = 'PARTIALLY_IMPORTED', 'Partially Imported'
    ARCHIVED_RAW = 'ARCHIVED_RAW', 'Archived in Raw Vault'
    FAILED = 'FAILED', 'Failed'
    EXPIRED = 'EXPIRED', 'Expired'


class BackupJob(TimeStampedModel):
    """
    Immutable snapshot artifact of any uploaded backup file (.csv or .xlsx).
    Preserves original file, computes SHA-256 hash, and detects column headers.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    original_filename = models.CharField(max_length=255)
    file_sha256 = models.CharField(max_length=64, db_index=True)
    file_size_bytes = models.PositiveIntegerField()
    file_storage_path = models.CharField(max_length=500, blank=True)
    mime_type = models.CharField(max_length=100, default='application/octet-stream')
    file_format = models.CharField(max_length=10)  # 'CSV' or 'XLSX'

    headers = models.JSONField(default=list)
    total_rows = models.PositiveIntegerField(default=0)
    suggested_domain = models.CharField(max_length=50, blank=True)
    suggestion_confidence = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)

    status = models.CharField(
        max_length=30,
        choices=BackupJobStatus.choices,
        default=BackupJobStatus.PENDING,
        db_index=True
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"BackupJob({self.id} | {self.original_filename} | {self.status} | {self.total_rows} rows)"


class ImportAttemptStatus(models.TextChoices):
    PREVIEWED = 'PREVIEWED', 'Previewed'
    COMMITTED = 'COMMITTED', 'Committed'
    REJECTED = 'REJECTED', 'Rejected'
    FAILED = 'FAILED', 'Failed'


class ImportAttempt(TimeStampedModel):
    """
    Interpretation of a BackupJob into a specific target domain (USERS, EVENTS, FORMS, etc.).
    Supports multiple attempts per backup artifact.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    backup_job = models.ForeignKey(BackupJob, on_delete=models.CASCADE, related_name='import_attempts')
    idempotency_key = models.CharField(max_length=128, unique=True, null=True, blank=True, db_index=True)
    target_domain = models.CharField(max_length=50)  # USERS, FORMS, EVENTS, HACKATHONS, UNKNOWN_RAW
    target_form = models.ForeignKey('forms.Form', null=True, blank=True, on_delete=models.PROTECT)

    column_mapping = models.JSONField(default=dict)
    unmapped_columns = models.JSONField(default=list)

    required_fields_satisfied = models.BooleanField(default=False)
    schema_confidence_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)

    total_records = models.IntegerField(default=0)
    valid_records = models.IntegerField(default=0)
    conflict_records = models.IntegerField(default=0)
    inserted_records = models.IntegerField(default=0)
    updated_records = models.IntegerField(default=0)

    validation_summary = models.JSONField(default=dict)
    status = models.CharField(max_length=20, choices=ImportAttemptStatus.choices, default=ImportAttemptStatus.PREVIEWED)
    committed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    committed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"ImportAttempt({self.id} | {self.target_domain} | {self.status} | {self.valid_records}/{self.total_records})"


class ImportRowAction(models.TextChoices):
    CREATE = 'CREATE', 'Create'
    UPDATE = 'UPDATE', 'Update'
    SKIP = 'SKIP', 'Skip'
    CONFLICT = 'CONFLICT', 'Conflict'


class ImportRow(TimeStampedModel):
    """
    Row-level provenance preserving all raw source columns (including unmapped ones)
    alongside normalized domain payloads.
    """
    import_attempt = models.ForeignKey(ImportAttempt, on_delete=models.CASCADE, related_name='rows')
    source_row_number = models.PositiveIntegerField()
    raw_data = models.JSONField(default=dict, help_text="Includes all unmapped legacy columns")
    normalized_data = models.JSONField(default=dict)
    action = models.CharField(max_length=20, choices=ImportRowAction.choices)
    is_valid = models.BooleanField(default=True)
    error_code = models.CharField(max_length=50, blank=True, default='')
    error_message = models.TextField(blank=True, default='')
    target_object_id = models.CharField(max_length=100, blank=True, default='')

    class Meta:
        ordering = ['source_row_number']
        indexes = [models.Index(fields=['import_attempt', 'source_row_number'])]

    def __str__(self):
        return f"ImportRow({self.import_attempt_id} | Row #{self.source_row_number} | {self.action} | valid={self.is_valid})"


class RawBackupArchive(TimeStampedModel):
    """
    Schemaless Raw Vault archive preserving arbitrary tabular spreadsheets with zero schema restrictions.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    backup_job = models.OneToOneField(BackupJob, on_delete=models.CASCADE, related_name='raw_archive')
    title = models.CharField(max_length=255)
    category = models.CharField(max_length=50, default='UNKNOWN_RAW')
    headers = models.JSONField(default=list)
    total_rows = models.PositiveIntegerField(default=0)
    metadata = models.JSONField(default=dict)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"RawBackupArchive({self.id} | {self.title} | {self.total_rows} rows)"


class RawBackupRow(TimeStampedModel):
    """
    Individual row in a schemaless RawBackupArchive.
    """
    archive = models.ForeignKey(RawBackupArchive, on_delete=models.CASCADE, related_name='rows')
    row_number = models.PositiveIntegerField()
    row_data = models.JSONField(default=dict)

    class Meta:
        ordering = ['row_number']
        indexes = [models.Index(fields=['archive', 'row_number'])]

    def __str__(self):
        return f"RawBackupRow({self.archive_id} | Row #{self.row_number})"


# Import DMC models so they are discovered by Django's migration framework
from apps.core.dmc.models import ExportJob  # noqa: F401, E402
