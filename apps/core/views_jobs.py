"""
apps/core/views_jobs.py
------------------------
Unified read-only view over every "bulk job" type in the app — bulk email
campaigns, DMC data exports, and CSV/backup member imports — so an admin can
see everything queued/running/done/failed in one place instead of hunting
through four separate models.

Nothing here executes or dispatches work; apps.core.tasks.run_in_background
and each domain's own service (EmailNotificationService, ExportService,
MemberImportService, the backup-import commit view) still own that. This is
purely an observability aggregation layer.
"""

from __future__ import annotations

from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import IsAdminOrClubLead

# Coarse status bucket every job type's own status maps into, so the frontend
# can render one consistent badge set regardless of job type.
_STATUS_GROUPS = {
    # EmailJob
    "PENDING": "PENDING",
    "PROCESSING": "RUNNING",
    "COMPLETED": "SUCCESS",
    "PARTIALLY_FAILED": "PARTIAL",
    "FAILED": "FAILED",
    # ExportJob
    "QUEUED": "PENDING",
    "RUNNING": "RUNNING",
    "EXPIRED": "FAILED",
    # ImportJob / ImportAttempt
    "PREVIEWED": "PENDING",
    "COMMITTED": "SUCCESS",
    "REJECTED": "FAILED",
}

_VALID_TYPES = {"email", "export", "member_import", "backup_import"}


def _status_group(raw_status: str) -> str:
    return _STATUS_GROUPS.get(raw_status, "PENDING")


def _user_summary(user) -> dict | None:
    if not user:
        return None
    name = f"{user.first_name} {user.last_name}".strip() or user.username
    return {"id": user.id, "name": name, "email": user.email}


def _serialize_email_job(job) -> dict:
    return {
        "id": str(job.id),
        "type": "email",
        "type_label": "Bulk Email",
        "title": job.campaign_name or f"Email — {job.template.name}",
        "status": job.status,
        "status_group": _status_group(job.status),
        "created_at": job.created_at,
        "completed_at": job.updated_at if job.status in ("COMPLETED", "FAILED", "PARTIALLY_FAILED") else None,
        "created_by": _user_summary(job.created_by),
        "summary": {
            "total": job.total_recipients,
            "sent": job.sent_count,
            "failed": job.failed_count,
        },
        "error_message": job.error_summary or "",
    }


def _serialize_export_job(job) -> dict:
    return {
        "id": str(job.pk),
        "type": "export",
        "type_label": "Data Export",
        "title": f"{job.dataset_id} — {job.format.upper()} export",
        "status": job.status,
        "status_group": _status_group(job.status),
        "created_at": job.created_at,
        "completed_at": job.completed_at,
        "created_by": _user_summary(job.created_by),
        "summary": {
            "rows": job.row_count,
            "file_size_bytes": job.file_size,
            "format": job.format,
        },
        "error_message": job.error_message or "",
    }


def _serialize_member_import_job(job) -> dict:
    return {
        "id": str(job.id),
        "type": "member_import",
        "type_label": "Member Import",
        "title": job.source_filename,
        "status": job.status,
        "status_group": _status_group(job.status),
        "created_at": job.created_at,
        "completed_at": job.updated_at if job.status in ("COMMITTED", "FAILED") else None,
        "created_by": _user_summary(job.created_by),
        "summary": {
            "total_rows": job.total_rows,
            "valid_rows": job.valid_rows,
            "conflict_rows": job.conflict_rows,
            "new_users": job.new_users_count,
            "updated_users": job.updated_users_count,
        },
        "error_message": "",
    }


def _serialize_backup_import_attempt(attempt) -> dict:
    return {
        "id": str(attempt.id),
        "type": "backup_import",
        "type_label": "Backup Import",
        "title": f"{attempt.backup_job.original_filename} → {attempt.target_domain}",
        "status": attempt.status,
        "status_group": _status_group(attempt.status),
        "created_at": attempt.created_at,
        "completed_at": attempt.committed_at,
        "created_by": _user_summary(attempt.committed_by),
        "summary": {
            "total_records": attempt.total_records,
            "valid_records": attempt.valid_records,
            "conflict_records": attempt.conflict_records,
            "inserted": attempt.inserted_records,
            "updated": attempt.updated_records,
        },
        "error_message": "",
    }


class BackgroundJobsOverviewView(APIView):
    """
    GET /api/admin/jobs/?type=email|export|member_import|backup_import&status=PENDING|RUNNING|SUCCESS|PARTIAL|FAILED&limit=50

    Aggregates the four bulk-job models into one sorted, optionally-filtered
    list. Each model is queried independently (they're unrelated tables, so
    there's no single SQL query to run across all of them) and merged in
    Python — fine at this app's scale (dozens to low hundreds of jobs total,
    not millions), and avoids the complexity of a heterogeneous UNION.
    """
    permission_classes = [IsAdminOrClubLead]

    def get(self, request):
        from apps.accounts.models import ImportJob
        from apps.core.dmc.models import ExportJob
        from apps.core.models import EmailJob, ImportAttempt

        type_filter = request.query_params.get("type")
        if type_filter and type_filter not in _VALID_TYPES:
            return Response({"error": f"Invalid type. Choose one of: {', '.join(sorted(_VALID_TYPES))}."}, status=400)
        status_filter = request.query_params.get("status")

        try:
            per_type_limit = max(1, min(200, int(request.query_params.get("limit", 50))))
        except ValueError:
            per_type_limit = 50

        rows: list[dict] = []

        if not type_filter or type_filter == "email":
            qs = EmailJob.objects.select_related("template", "created_by").order_by("-created_at")[:per_type_limit]
            rows.extend(_serialize_email_job(job) for job in qs)

        if not type_filter or type_filter == "export":
            qs = ExportJob.objects.select_related("created_by").order_by("-created_at")[:per_type_limit]
            rows.extend(_serialize_export_job(job) for job in qs)

        if not type_filter or type_filter == "member_import":
            qs = ImportJob.objects.select_related("created_by").order_by("-created_at")[:per_type_limit]
            rows.extend(_serialize_member_import_job(job) for job in qs)

        if not type_filter or type_filter == "backup_import":
            qs = ImportAttempt.objects.select_related("backup_job", "committed_by").order_by("-created_at")[:per_type_limit]
            rows.extend(_serialize_backup_import_attempt(attempt) for attempt in qs)

        if status_filter:
            rows = [r for r in rows if r["status_group"] == status_filter.upper()]

        rows.sort(key=lambda r: r["created_at"], reverse=True)

        stats: dict[str, int] = {}
        for r in rows:
            stats[r["status_group"]] = stats.get(r["status_group"], 0) + 1

        return Response({"results": rows, "count": len(rows), "stats": stats})
