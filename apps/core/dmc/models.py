"""
dmc/models.py
-------------
Django model for tracking asynchronous export jobs.
"""

from django.db import models
from django.conf import settings


class ExportJob(models.Model):
    STATUS_QUEUED     = "QUEUED"
    STATUS_RUNNING    = "RUNNING"
    STATUS_COMPLETED  = "COMPLETED"
    STATUS_FAILED     = "FAILED"
    STATUS_EXPIRED    = "EXPIRED"
    STATUS_CHOICES = [
        (STATUS_QUEUED,    "Queued"),
        (STATUS_RUNNING,   "Running"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED,    "Failed"),
        (STATUS_EXPIRED,   "Expired"),
    ]

    FORMAT_CHOICES = [("csv", "CSV"), ("xlsx", "XLSX"), ("json", "JSON")]

    # Identity
    dataset_id = models.CharField(max_length=100)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="dmc_export_jobs",
    )

    # Request snapshot — frozen at export initiation time
    format     = models.CharField(max_length=10, choices=FORMAT_CHOICES, default="csv")
    row_scope  = models.CharField(max_length=20, default="all_filtered")   # "selected" | "all_filtered"
    column_scope = models.CharField(max_length=20, default="visible")      # "visible" | "all_permitted"
    selected_record_ids = models.JSONField(default=list, blank=True)       # list[str] when row_scope="selected"
    filter_snapshot     = models.JSONField(default=dict, blank=True)       # Serialized QueryRequest at initiation
    column_keys         = models.JSONField(default=list, blank=True)       # List of column keys to export

    # Result
    status        = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_QUEUED)
    row_count     = models.PositiveIntegerField(null=True, blank=True)
    file_size     = models.PositiveIntegerField(null=True, blank=True)     # Bytes
    file_path     = models.CharField(max_length=500, blank=True)           # Server temp file path
    error_message = models.TextField(blank=True)

    # Timestamps
    created_at   = models.DateTimeField(auto_now_add=True)
    started_at   = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    expires_at   = models.DateTimeField(null=True, blank=True)             # Auto-set to created_at + 24h

    class Meta:
        app_label = "core"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["created_by", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"ExportJob #{self.pk} [{self.dataset_id}] {self.format.upper()} — {self.status}"

    @property
    def is_downloadable(self) -> bool:
        from django.utils import timezone
        if self.status != self.STATUS_COMPLETED:
            return False
        if self.expires_at and timezone.now() > self.expires_at:
            return False
        return bool(self.file_path)
