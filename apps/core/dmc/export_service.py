"""
dmc/export_service.py
---------------------
ExportService: the single location for all data serialization logic.

Cross-cutting concerns handled here:
  - Column allowlist enforcement (permitted_columns)
  - Null/empty formatting using four-state envelopes
  - CSV / write-only XLSX / JSON generation
  - Sync vs async job dispatch based on DMC_SYNC_EXPORT_MAX_ROWS
  - Audit logging

Adapters are NEVER responsible for formatting — they provide stream_records().
"""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from typing import Any, Generator

from django.conf import settings
from django.utils import timezone

from apps.core.dmc.contracts import CanonicalValue, ColumnDefinition, ExportRequest, QueryRequest
from apps.core.dmc.permissions import get_permitted_columns, FORBIDDEN_COLUMN_KEYS
from apps.audit.utils import log_audit_event

# Configurable threshold: exports <= this size are served synchronously
DMC_SYNC_EXPORT_MAX_ROWS: int = getattr(settings, "DMC_SYNC_EXPORT_MAX_ROWS", 1000)


# ---------------------------------------------------------------------------
# Value formatting helper
# ---------------------------------------------------------------------------

def _format_cell(cv: CanonicalValue | Any) -> str:
    """Convert a CanonicalValue envelope to a human-readable string for CSV/XLSX cells."""
    if not isinstance(cv, CanonicalValue):
        return str(cv) if cv is not None else ""
    if cv.state == "not_applicable":
        return "—"
    if cv.state == "empty":
        return ""
    if cv.state == "unknown":
        return "[Unavailable]"
    v = cv.value
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, list):
        return ", ".join(str(i) for i in v)
    if isinstance(v, dict):
        return json.dumps(v)
    return str(v) if v is not None else ""


def _format_json_cell(cv: CanonicalValue | Any) -> Any:
    """Return a JSON-serializable representation of the canonical value."""
    if not isinstance(cv, CanonicalValue):
        return cv
    return {"state": cv.state, "value": cv.value, "display_value": cv.display_value}


# ---------------------------------------------------------------------------
# CSV generator
# ---------------------------------------------------------------------------

def _generate_csv(
    columns: list[ColumnDefinition],
    records: Generator[dict[str, CanonicalValue], None, None],
) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([c.label for c in columns])
    keys = [c.key for c in columns]
    for record in records:
        writer.writerow([_format_cell(record.get(k)) for k in keys])
    return buf.getvalue().encode("utf-8-sig")


# ---------------------------------------------------------------------------
# XLSX generator (write-only mode — no full workbook in memory)
# ---------------------------------------------------------------------------

def _generate_xlsx(
    columns: list[ColumnDefinition],
    records: Generator[dict[str, CanonicalValue], None, None],
) -> bytes:
    try:
        import openpyxl
        from openpyxl import Workbook
    except ImportError as exc:
        raise RuntimeError("openpyxl is required for XLSX export. Install it: pip install openpyxl") from exc

    buf = io.BytesIO()
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("DMC Export")
    from openpyxl.styles import Font
    header = [c.label for c in columns]
    ws.append(header)
    keys = [c.key for c in columns]
    for record in records:
        ws.append([_format_cell(record.get(k)) for k in keys])
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# JSON generator
# ---------------------------------------------------------------------------

def _generate_json(
    columns: list[ColumnDefinition],
    records: Generator[dict[str, CanonicalValue], None, None],
) -> bytes:
    keys = [c.key for c in columns]
    rows = []
    for record in records:
        rows.append({c.label: _format_json_cell(record.get(k)) for c, k in zip(columns, keys)})
    return json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8")


# ---------------------------------------------------------------------------
# ExportService
# ---------------------------------------------------------------------------

class ExportService:
    """
    Orchestrates data export from a dataset adapter.

    Usage:
        svc = ExportService(dataset_def, adapter, user)
        response_or_job = svc.run(export_request, query_request)
    """

    def __init__(self, dataset_def: Any, adapter: Any, user: Any):
        self.dataset_def = dataset_def
        self.adapter = adapter
        self.user = user

    def run(self, export_req: ExportRequest, query_req: QueryRequest):
        """
        Execute the export.

        Returns:
            dict with:
              - 'mode': 'sync' | 'async'
              - 'bytes': bytes content (sync only)
              - 'content_type': str (sync only)
              - 'filename': str
              - 'job_id': int (async only)
        """
        # --- Resolve columns ---
        schema_cols, _ = self.adapter.get_schema(self.user)
        permitted = get_permitted_columns(self.user, schema_cols)
        permitted_keys = {c.key for c in permitted}

        if export_req.column_scope == "visible" and export_req.visible_column_keys:
            export_cols = [c for c in permitted if c.key in export_req.visible_column_keys and c.exportable]
        else:
            export_cols = [c for c in permitted if c.exportable]

        # --- Estimate row count ---
        test_result = self.adapter.query(QueryRequest(page=1, page_size=1, search=query_req.search, sort=query_req.sort, filters=query_req.filters), self.user)
        total = test_result.total

        if export_req.row_scope == "selected":
            total = len(export_req.selected_record_ids)

        filename = f"{self.dataset_def.id}_export_{timezone.now().strftime('%Y%m%d_%H%M%S')}.{export_req.format}"

        if total <= DMC_SYNC_EXPORT_MAX_ROWS:
            return self._run_sync(export_req, query_req, export_cols, filename, total)
        else:
            return self._run_async(export_req, query_req, export_cols, filename, total)

    def _run_sync(self, export_req, query_req, export_cols, filename, total) -> dict:
        records = list(self.adapter.stream_records(
            query_req,
            self.user,
            selected_ids=export_req.selected_record_ids if export_req.row_scope == "selected" else None,
        ))

        data, content_type = self._serialize(export_cols, iter(records), export_req.format)

        log_audit_event(
            actor=self.user,
            action=f"DMC Export ({export_req.format.upper()})",
            target_model="DMC",
            target_id=self.dataset_def.id,
            details={"dataset": self.dataset_def.id, "format": export_req.format, "rows": len(records), "columns": len(export_cols), "scope": export_req.row_scope},
        )

        return {"mode": "sync", "bytes": data, "content_type": content_type, "filename": filename}

    def _run_async(self, export_req, query_req, export_cols, filename, total) -> dict:
        from apps.core.dmc.models import ExportJob

        job = ExportJob.objects.create(
            dataset_id=self.dataset_def.id,
            created_by=self.user,
            format=export_req.format,
            row_scope=export_req.row_scope,
            column_scope=export_req.column_scope,
            selected_record_ids=export_req.selected_record_ids,
            filter_snapshot={
                "search": query_req.search,
                "sort_field": query_req.sort.field,
                "sort_direction": query_req.sort.direction,
                "filters": [{"field": f.field, "operator": f.operator, "value": f.value} for f in query_req.filters],
            },
            column_keys=[c.key for c in export_cols],
            expires_at=timezone.now() + timezone.timedelta(hours=24),
        )

        # Dispatch background task
        try:
            from apps.core.dmc.tasks import run_export_job
            run_export_job.delay(job.pk)
        except Exception:
            # If Celery not available, run synchronously as fallback
            self._execute_job(job, export_cols, export_req, query_req, filename)

        return {"mode": "async", "job_id": job.pk, "filename": filename}

    def _serialize(self, export_cols, records_iter, fmt: str) -> tuple[bytes, str]:
        if fmt == "csv":
            return _generate_csv(export_cols, records_iter), "text/csv"
        elif fmt == "xlsx":
            return _generate_xlsx(export_cols, records_iter), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        elif fmt == "json":
            return _generate_json(export_cols, records_iter), "application/json"
        raise ValueError(f"Unsupported export format: {fmt}")

    def _execute_job(self, job: Any, export_cols: list, export_req: ExportRequest, query_req: QueryRequest, filename: str):
        """Synchronous fallback execution — also used by the Celery task."""
        from apps.core.dmc.models import ExportJob

        job.status = ExportJob.STATUS_RUNNING
        job.started_at = timezone.now()
        job.save(update_fields=["status", "started_at"])

        try:
            records = self.adapter.stream_records(
                query_req,
                self.user,
                selected_ids=export_req.selected_record_ids if export_req.row_scope == "selected" else None,
            )
            data, _ = self._serialize(export_cols, records, job.format)

            tmp_dir = getattr(settings, "DMC_EXPORT_TEMP_DIR", tempfile.gettempdir())
            os.makedirs(tmp_dir, exist_ok=True)
            file_path = os.path.join(tmp_dir, filename)
            with open(file_path, "wb") as f:
                f.write(data)

            row_count = data.count(b"\n") if job.format == "csv" else 0
            job.status = ExportJob.STATUS_COMPLETED
            job.file_path = file_path
            job.file_size = len(data)
            job.row_count = row_count
            job.completed_at = timezone.now()

        except Exception as exc:
            job.status = ExportJob.STATUS_FAILED
            job.error_message = str(exc)

        job.save(update_fields=["status", "file_path", "file_size", "row_count", "completed_at", "error_message"])

        log_audit_event(
            actor=self.user,
            action=f"DMC Export Job ({job.format.upper()}) — {job.status}",
            target_model="ExportJob",
            target_id=str(job.pk),
            details={"dataset": job.dataset_id, "format": job.format, "rows": job.row_count, "status": job.status},
        )
