from django.contrib import admin
from .models import JobListing

@admin.register(JobListing)
class JobListingAdmin(admin.ModelAdmin):
    list_display = ['title', 'company_name', 'job_type', 'deadline']
    list_filter = ['job_type']
