"""
dmc/views.py
------------
DRF API views for the Data Management Center.

Endpoints:
  GET  /api/admin/dmc/datasets/              → Catalog of available datasets
  GET  /api/admin/dmc/datasets/<id>/schema/  → Column + filter definitions
  POST /api/admin/dmc/datasets/<id>/query/   → Paginated filtered query
  GET  /api/admin/dmc/datasets/<id>/records/<pk>/  → Single record detail
  POST /api/admin/dmc/datasets/<id>/export/  → Initiate export (sync or async)
  GET  /api/admin/dmc/exports/<job_id>/      → Export job status
  GET  /api/admin/dmc/exports/<job_id>/download/ → Download completed export
"""

from __future__ import annotations

import dataclasses
import os
from typing import Any

from django.http import FileResponse, HttpResponse
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.dmc.contracts import (
    ExportRequest,
    FilterClause,
    QueryRequest,
    SortClause,
)
from apps.core.dmc.export_service import ExportService
from apps.core.dmc.permissions import (
    DMCBasePermission,
    can_export_dataset,
    can_view_dataset,
    get_permitted_columns,
    sanitize_record,
)
from apps.core.dmc.registry import get_dataset, get_dataset_registry


# ---------------------------------------------------------------------------
# Serialization helpers for dataclasses → JSON-safe dicts
# ---------------------------------------------------------------------------

def _serialize_column(col) -> dict:
    return {
        "key":               col.key,
        "label":             col.label,
        "type":              col.type,
        "category":          col.category,
        "source":            col.source,
        "sortable":          col.sortable,
        "filterable":        col.filterable,
        "exportable":        col.exportable,
        "visible_by_default":col.visible_by_default,
        "renderer":          col.renderer,
        "description":       col.description,
    }


def _serialize_filter(f) -> dict:
    return {
        "key":       f.key,
        "label":     f.label,
        "type":      f.type,
        "operators": f.operators,
        "options":   [{"label": o.label, "value": o.value} for o in f.options],
    }


def _serialize_canonical_value(cv) -> dict:
    return {
        "state":         cv.state,
        "value":         cv.value,
        "display_value": cv.display_value,
        "type":          cv.type,
    }


def _serialize_record(record: dict) -> dict:
    return {k: _serialize_canonical_value(v) for k, v in record.items()}


def _serialize_dataset_def(d) -> dict:
    return {
        "id":          d.id,
        "label":       d.label,
        "description": d.description,
        "group":       d.group,
        "primary_key": d.primary_key,
        "default_sort_field":     d.default_sort_field,
        "default_sort_direction": d.default_sort_direction,
        "capabilities": dataclasses.asdict(d.capabilities),
        "health":      d.health,
    }


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

class DatasetCatalogView(APIView):
    """GET /api/admin/dmc/datasets/ — list all datasets accessible to the current user."""
    permission_classes = [DMCBasePermission]

    def get(self, request: Request) -> Response:
        registry = get_dataset_registry()
        accessible = [
            _serialize_dataset_def(d)
            for d in registry.values()
            if can_view_dataset(request.user, d.id)
        ]
        return Response({"datasets": accessible})


class DatasetSchemaView(APIView):
    """GET /api/admin/dmc/datasets/<dataset_id>/schema/"""
    permission_classes = [DMCBasePermission]

    def get(self, request: Request, dataset_id: str) -> Response:
        if not can_view_dataset(request.user, dataset_id):
            return Response({"error": "Not authorized for this dataset."}, status=status.HTTP_403_FORBIDDEN)

        dataset_def = get_dataset(dataset_id)
        if not dataset_def:
            return Response({"error": "Dataset not found."}, status=status.HTTP_404_NOT_FOUND)

        adapter = dataset_def.adapter_class()
        columns, filters = adapter.get_schema(request.user)
        permitted = get_permitted_columns(request.user, columns)

        return Response({
            "dataset_id":  dataset_id,
            "columns":     [_serialize_column(c) for c in permitted],
            "filters":     [_serialize_filter(f) for f in filters],
            "total_columns": len(permitted),
        })


class DatasetQueryView(APIView):
    """POST /api/admin/dmc/datasets/<dataset_id>/query/"""
    permission_classes = [DMCBasePermission]

    def post(self, request: Request, dataset_id: str) -> Response:
        if not can_view_dataset(request.user, dataset_id):
            return Response({"error": "Not authorized."}, status=status.HTTP_403_FORBIDDEN)

        dataset_def = get_dataset(dataset_id)
        if not dataset_def:
            return Response({"error": "Dataset not found."}, status=status.HTTP_404_NOT_FOUND)

        body = request.data or {}
        sort_data = body.get("sort", {})
        query_req = QueryRequest(
            page=max(1, int(body.get("page", 1))),
            page_size=min(200, max(1, int(body.get("page_size", 50)))),
            search=str(body.get("search", "")).strip(),
            sort=SortClause(
                field=str(sort_data.get("field", dataset_def.default_sort_field)),
                direction=sort_data.get("direction", dataset_def.default_sort_direction),
            ),
            filters=[
                FilterClause(field=f["field"], operator=f["operator"], value=f["value"])
                for f in body.get("filters", [])
                if "field" in f and "operator" in f and "value" in f
            ],
            columns=body.get("columns", []),
        )

        # Validate sort field against allowlist
        if query_req.sort.field not in dataset_def.allowed_sort_fields:
            query_req.sort.field = dataset_def.default_sort_field

        adapter = dataset_def.adapter_class()
        result = adapter.query(query_req, request.user)

        # Permission filter on returned columns
        schema_cols, _ = adapter.get_schema(request.user)
        permitted_keys = {c.key for c in get_permitted_columns(request.user, schema_cols)}

        return Response({
            "dataset_id": result.dataset_id,
            "total":      result.total,
            "page":       result.page,
            "page_size":  result.page_size,
            "records": [
                _serialize_record(sanitize_record(request.user, rec, permitted_keys))
                for rec in result.records
            ],
        })


class DatasetRecordDetailView(APIView):
    """GET /api/admin/dmc/datasets/<dataset_id>/records/<record_id>/"""
    permission_classes = [DMCBasePermission]

    def get(self, request: Request, dataset_id: str, record_id: str) -> Response:
        if not can_view_dataset(request.user, dataset_id):
            return Response({"error": "Not authorized."}, status=status.HTTP_403_FORBIDDEN)

        dataset_def = get_dataset(dataset_id)
        if not dataset_def:
            return Response({"error": "Dataset not found."}, status=status.HTTP_404_NOT_FOUND)

        if not dataset_def.capabilities.record_detail:
            return Response({"error": "Record detail not supported for this dataset."}, status=status.HTTP_400_BAD_REQUEST)

        adapter = dataset_def.adapter_class()
        record = adapter.get_record(record_id, request.user)
        if record is None:
            return Response({"error": "Record not found."}, status=status.HTTP_404_NOT_FOUND)

        schema_cols, _ = adapter.get_schema(request.user)
        permitted_keys = {c.key for c in get_permitted_columns(request.user, schema_cols)}
        safe = sanitize_record(request.user, record, permitted_keys)
        return Response({"dataset_id": dataset_id, "record": _serialize_record(safe)})


class DatasetExportView(APIView):
    """POST /api/admin/dmc/datasets/<dataset_id>/export/"""
    permission_classes = [DMCBasePermission]

    ALLOWED_FORMATS = {"csv", "xlsx", "json"}
    ALLOWED_ROW_SCOPES = {"selected", "all_filtered"}
    ALLOWED_COL_SCOPES = {"visible", "all_permitted"}

    def post(self, request: Request, dataset_id: str) -> Response:
        if not can_export_dataset(request.user, dataset_id):
            return Response({"error": "Export not authorized."}, status=status.HTTP_403_FORBIDDEN)

        dataset_def = get_dataset(dataset_id)
        if not dataset_def:
            return Response({"error": "Dataset not found."}, status=status.HTTP_404_NOT_FOUND)

        body = request.data or {}
        fmt = str(body.get("format", "csv")).lower()
        if fmt not in self.ALLOWED_FORMATS:
            return Response({"error": f"Unsupported format. Choose: {', '.join(self.ALLOWED_FORMATS)}"}, status=status.HTTP_400_BAD_REQUEST)

        # Validate capabilities
        cap_map = {"csv": dataset_def.capabilities.export_csv, "xlsx": dataset_def.capabilities.export_xlsx, "json": dataset_def.capabilities.export_json}
        if not cap_map.get(fmt, False):
            return Response({"error": f"{fmt.upper()} export not supported for this dataset."}, status=status.HTTP_400_BAD_REQUEST)

        sort_data = body.get("sort", {})
        query_req = QueryRequest(
            page=1,
            page_size=9999999,
            search=str(body.get("search", "")).strip(),
            sort=SortClause(field=sort_data.get("field", dataset_def.default_sort_field), direction=sort_data.get("direction", "desc")),
            filters=[
                FilterClause(field=f["field"], operator=f["operator"], value=f["value"])
                for f in body.get("filters", [])
                if "field" in f and "operator" in f and "value" in f
            ],
        )

        export_req = ExportRequest(
            format=fmt,
            row_scope=body.get("row_scope", "all_filtered"),
            column_scope=body.get("column_scope", "visible"),
            selected_record_ids=[str(i) for i in body.get("selected_record_ids", [])],
            visible_column_keys=body.get("visible_column_keys", []),
            query_snapshot=query_req,
        )

        adapter = dataset_def.adapter_class()
        svc = ExportService(dataset_def, adapter, request.user)
        result = svc.run(export_req, query_req)

        if result["mode"] == "sync":
            http_resp = HttpResponse(
                content=result["bytes"],
                content_type=result["content_type"],
                status=200,
            )
            http_resp["Content-Disposition"] = f'attachment; filename="{result["filename"]}"'
            http_resp["X-Export-Mode"] = "sync"
            return http_resp

        # Async
        return Response(
            {"mode": "async", "job_id": result["job_id"], "message": "Export queued. Poll /exports/<job_id>/ for status."},
            status=status.HTTP_202_ACCEPTED,
        )


class ExportJobStatusView(APIView):
    """GET /api/admin/dmc/exports/<job_id>/"""
    permission_classes = [DMCBasePermission]

    def get(self, request: Request, job_id: int) -> Response:
        from apps.core.dmc.models import ExportJob
        try:
            job = ExportJob.objects.get(pk=job_id, created_by=request.user)
        except ExportJob.DoesNotExist:
            return Response({"error": "Job not found."}, status=status.HTTP_404_NOT_FOUND)

        return Response({
            "job_id":      job.pk,
            "dataset_id":  job.dataset_id,
            "format":      job.format,
            "status":      job.status,
            "row_count":   job.row_count,
            "file_size":   job.file_size,
            "created_at":  job.created_at.isoformat() if job.created_at else None,
            "completed_at":job.completed_at.isoformat() if job.completed_at else None,
            "expires_at":  job.expires_at.isoformat() if job.expires_at else None,
            "downloadable":job.is_downloadable,
            "error_message":job.error_message or None,
        })


class ExportJobDownloadView(APIView):
    """GET /api/admin/dmc/exports/<job_id>/download/"""
    permission_classes = [DMCBasePermission]

    def get(self, request: Request, job_id: int) -> Response:
        from apps.core.dmc.models import ExportJob
        try:
            job = ExportJob.objects.get(pk=job_id, created_by=request.user)
        except ExportJob.DoesNotExist:
            return Response({"error": "Job not found."}, status=status.HTTP_404_NOT_FOUND)

        if not job.is_downloadable:
            return Response({"error": "File not ready or expired."}, status=status.HTTP_404_NOT_FOUND)

        try:
            f = open(job.file_path, "rb")
        except OSError:
            job.status = ExportJob.STATUS_EXPIRED
            job.save(update_fields=["status"])
            return Response({"error": "Export file has been removed."}, status=status.HTTP_410_GONE)

        content_type_map = {"csv": "text/csv", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "json": "application/json"}
        resp = FileResponse(f, content_type=content_type_map.get(job.format, "application/octet-stream"))
        resp["Content-Disposition"] = f'attachment; filename="{os.path.basename(job.file_path)}"'
        return resp
