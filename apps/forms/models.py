from django.db import models
from django.conf import settings
from apps.core.models import TimeStampedModel

class FormStatus(models.TextChoices):
    DRAFT = 'DRAFT', 'Draft'
    PUBLISHED = 'PUBLISHED', 'Published'
    CLOSED = 'CLOSED', 'Closed'

class FieldType(models.TextChoices):
    TEXT = 'TEXT', 'Text'
    PARAGRAPH = 'PARAGRAPH', 'Paragraph'
    EMAIL = 'EMAIL', 'Email'
    NUMBER = 'NUMBER', 'Number'
    DROPDOWN = 'DROPDOWN', 'Dropdown'
    RADIO = 'RADIO', 'Radio Button'
    CHECKBOX = 'CHECKBOX', 'Checkbox'
    FILE = 'FILE', 'File Upload'
    MULTI_FILE = 'MULTI_FILE', 'Multi File Upload'
    DATE = 'DATE', 'Date'
    SECTION = 'SECTION', 'Section Header'

class Form(TimeStampedModel):
    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, unique=True)
    description = models.TextField(blank=True)
    image_url = models.URLField(blank=True, null=True)
    category = models.CharField(max_length=100, default='General')
    status = models.CharField(max_length=20, choices=FormStatus.choices, default=FormStatus.DRAFT)
    version = models.PositiveIntegerField(default=1, help_text="Schema version incremented on form updates")
    allow_multiple_responses = models.BooleanField(default=False, help_text="Allow a user to submit multiple times")
    allow_edits_until = models.DateTimeField(blank=True, null=True, help_text="Deadline after which responses are locked")
    open_at = models.DateTimeField(blank=True, null=True)
    close_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"{self.title} (v{self.version} - {self.get_status_display()})"

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
    conditional_logic = models.JSONField(default=dict, blank=True, help_text="Visibility rules e.g. {'if': 'field_id', 'equals': 'val'}")
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

    def __str__(self):
        user_str = self.user.email if self.user else ("Admin Entry" if self.is_manual_entry else "Anonymous")
        return f"Response #{self.id} for {self.form.title} (v{self.form_version}) by {user_str}"

class Answer(TimeStampedModel):
    response = models.ForeignKey(Response, on_delete=models.CASCADE, related_name='answers')
    field = models.ForeignKey(FormField, on_delete=models.CASCADE)
    value = models.JSONField(blank=True, null=True)

    def __str__(self):
        return f"Answer to {self.field.label}: {self.value}"
