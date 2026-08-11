from django.db import models
from apps.core.models import TimeStampedModel
from apps.forms.models import Form

class JobType(models.TextChoices):
    INTERNSHIP = 'INTERNSHIP', 'Internship'
    FULL_TIME = 'FULL_TIME', 'Full Time'
    PART_TIME = 'PART_TIME', 'Part Time'

class JobListing(TimeStampedModel):
    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, unique=True)
    company_name = models.CharField(max_length=150)
    job_type = models.CharField(max_length=20, choices=JobType.choices, default=JobType.INTERNSHIP)
    location = models.CharField(max_length=150, default='Remote / On-site')
    salary_range = models.CharField(max_length=100, blank=True)
    description = models.TextField()
    deadline = models.DateTimeField()
    
    application_form = models.ForeignKey(Form, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return f"{self.title} at {self.company_name}"
