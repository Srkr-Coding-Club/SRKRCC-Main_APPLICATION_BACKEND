from django.contrib import admin
from .models import Announcement


@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    list_display = ['title', 'type', 'is_active', 'created_at']
    list_editable = ['is_active']
    list_filter = ['type', 'is_active']
    search_fields = ['title', 'message']
