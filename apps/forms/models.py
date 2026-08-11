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
    DATE = 'DATE', 'Date'

class Form(TimeStampedModel):
    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, unique=True)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=FormStatus.choices, default=FormStatus.DRAFT)
    open_at = models.DateTimeField(blank=True, null=True)
    close_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"{self.title} ({self.get_status_display()})"

class FormField(TimeStampedModel):
    form = models.ForeignKey(Form, on_delete=models.CASCADE, related_name='fields')
    label = models.CharField(max_length=255)
    type = models.CharField(max_length=20, choices=FieldType.choices, default=FieldType.TEXT)
    placeholder = models.CharField(max_length=255, blank=True)
    is_required = models.BooleanField(default=False)
    options = models.JSONField(default=list, blank=True, help_text="Choices for dropdown/radio/checkbox")
    validation_rules = models.JSONField(default=dict, blank=True)
    order = models.IntegerField(default=0)

    class Meta:
        ordering = ['order']

    def __str__(self):
        return f"{self.form.title} -> {self.label} ({self.type})"

class Response(TimeStampedModel):
    form = models.ForeignKey(Form, on_delete=models.CASCADE, related_name='responses')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        user_str = self.user.email if self.user else "Anonymous"
        return f"Response #{self.id} for {self.form.title} by {user_str}"

class Answer(TimeStampedModel):
    response = models.ForeignKey(Response, on_delete=models.CASCADE, related_name='answers')
    field = models.ForeignKey(FormField, on_delete=models.CASCADE)
    value = models.JSONField(blank=True, null=True)

    def __str__(self):
        return f"Answer to {self.field.label}: {self.value}"
