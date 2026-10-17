from django.contrib import admin

from .models import College


@admin.register(College)
class CollegeAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'district', 'state', 'college_type']
    list_filter = ['state', 'college_type']
    search_fields = ['name', 'code']
