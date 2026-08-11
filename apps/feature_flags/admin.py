from django.contrib import admin
from .models import FeatureFlag

@admin.register(FeatureFlag)
class FeatureFlagAdmin(admin.ModelAdmin):
    list_display = ['name', 'key', 'is_enabled', 'updated_at']
    list_editable = ['is_enabled']
    search_fields = ['name', 'key']
