from django.urls import path
from apps.core.views_backup import (
    BackupDomainMetadataView,
    BackupUploadIntakeView,
    BackupListView,
    BackupDetailView,
    BackupAnalyzeDomainView,
    BackupPreviewView,
    BackupCommitImportView,
    BackupArchiveRawView,
    BackupRawDownloadView,
)

urlpatterns = [
    path('domains/', BackupDomainMetadataView.as_view(), name='backup-domains-metadata'),
    path('upload/', BackupUploadIntakeView.as_view(), name='backup-upload-intake'),
    path('', BackupListView.as_view(), name='backup-list'),
    path('<uuid:id>/', BackupDetailView.as_view(), name='backup-detail'),
    path('<uuid:id>/analyze/', BackupAnalyzeDomainView.as_view(), name='backup-analyze'),
    path('<uuid:id>/preview/', BackupPreviewView.as_view(), name='backup-preview'),
    path('<uuid:id>/imports/<uuid:attempt_id>/commit/', BackupCommitImportView.as_view(), name='backup-commit'),
    path('<uuid:id>/archive-raw/', BackupArchiveRawView.as_view(), name='backup-archive-raw'),
    path('<uuid:id>/raw-download/', BackupRawDownloadView.as_view(), name='backup-raw-download'),
]
