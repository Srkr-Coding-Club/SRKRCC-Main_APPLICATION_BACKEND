from django.contrib import admin
from .models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'recipient', 'type', 'category', 'is_read', 'created_at')
    list_filter = ('type', 'category', 'is_read', 'created_at')
    search_fields = ('title', 'message', 'recipient__email', 'recipient__username')
    readonly_fields = ('created_at', 'updated_at', 'read_at')
