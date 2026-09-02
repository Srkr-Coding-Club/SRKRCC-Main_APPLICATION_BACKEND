"""dmc/urls.py"""

from django.urls import path
from apps.core.dmc.views import (
    DatasetCatalogView,
    DatasetSchemaView,
    DatasetQueryView,
    DatasetRecordDetailView,
    DatasetExportView,
    ExportJobStatusView,
    ExportJobDownloadView,
)

urlpatterns = [
    path("datasets/",                                     DatasetCatalogView.as_view(),       name="dmc-datasets"),
    path("datasets/<str:dataset_id>/schema/",             DatasetSchemaView.as_view(),         name="dmc-schema"),
    path("datasets/<str:dataset_id>/query/",              DatasetQueryView.as_view(),          name="dmc-query"),
    path("datasets/<str:dataset_id>/records/<str:record_id>/", DatasetRecordDetailView.as_view(), name="dmc-record"),
    path("datasets/<str:dataset_id>/export/",             DatasetExportView.as_view(),         name="dmc-export"),
    path("exports/<int:job_id>/",                         ExportJobStatusView.as_view(),       name="dmc-export-status"),
    path("exports/<int:job_id>/download/",                ExportJobDownloadView.as_view(),     name="dmc-export-download"),
]
