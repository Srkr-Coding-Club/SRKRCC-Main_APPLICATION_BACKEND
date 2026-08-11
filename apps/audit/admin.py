from django.contrib import admin
from .models import AuditLog

@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ['actor', 'action', 'target_model', 'target_id', 'created_at']
    list_filter = ['action', 'target_model']
    readonly_fields = ['actor', 'action', 'target_model', 'target_id', 'details', 'created_at']
