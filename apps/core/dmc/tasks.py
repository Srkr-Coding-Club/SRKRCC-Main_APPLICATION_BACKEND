"""
dmc/tasks.py
------------
Background job function for large (async) DMC exports — run on a plain
Python thread via apps.core.tasks.run_in_background, not a task queue.

ExportService._run_async() creates a QUEUED ExportJob and dispatches this via
run_in_background(lambda: run_export_job(job.pk)). This function looks
everything up fresh from `job_id` rather than receiving live objects, since it
runs on its own thread/connection.
"""

from django.utils import timezone

from apps.core.dmc.contracts import ExportRequest, FilterClause, QueryRequest, SortClause


def run_export_job(job_id):
    from apps.core.dmc.export_service import ExportService
    from apps.core.dmc.models import ExportJob
    from apps.core.dmc.registry import get_dataset

    job = ExportJob.objects.select_related('created_by').get(pk=job_id)
    dataset_def = get_dataset(job.dataset_id)
    adapter = dataset_def.adapter_class()
    user = job.created_by

    schema_cols, _ = adapter.get_schema(user)
    export_cols = [c for c in schema_cols if c.key in job.column_keys]

    snap = job.filter_snapshot or {}
    query_req = QueryRequest(
        page=1,
        page_size=1,
        search=snap.get('search', ''),
        sort=SortClause(
            field=snap.get('sort_field', dataset_def.default_sort_field),
            direction=snap.get('sort_direction', dataset_def.default_sort_direction),
        ),
        filters=[
            FilterClause(field=f['field'], operator=f['operator'], value=f['value'])
            for f in snap.get('filters', [])
        ],
    )
    export_req = ExportRequest(
        format=job.format,
        row_scope=job.row_scope,
        column_scope=job.column_scope,
        selected_record_ids=job.selected_record_ids or [],
        visible_column_keys=job.column_keys,
    )

    filename = f"{dataset_def.id}_export_{timezone.now().strftime('%Y%m%d_%H%M%S')}.{job.format}"

    svc = ExportService(dataset_def, adapter, user)
    svc._execute_job(job, export_cols, export_req, query_req, filename)
