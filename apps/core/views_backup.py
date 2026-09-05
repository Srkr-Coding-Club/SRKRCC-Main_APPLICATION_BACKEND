import json
import os
from django.http import FileResponse, Http404
from rest_framework import status, permissions
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from apps.core.models import BackupJob, ImportAttempt, RawBackupArchive
from apps.core.services.backup.backup_service import UniversalBackupService, BackupError
from apps.core.services.backup.registry import BackupImporterRegistry
from apps.audit.utils import log_audit_event
from apps.core.permissions import IsAdminOrClubLead


class BackupDomainMetadataView(APIView):
    """
    GET /api/admin/backups/domains/
    Exposes domain schemas, field synonyms, and required rules to the frontend dynamically.
    """
    permission_classes = [IsAdminOrClubLead]

    def get(self, request):
        domains = BackupImporterRegistry.get_domains_metadata()
        return Response(domains)


class BackupUploadIntakeView(APIView):
    """
    POST /api/admin/backups/upload/
    Uploads a backup file (.csv or .xlsx), computes SHA-256, stores original file,
    detects headers, and returns suggested domain.
    """
    permission_classes = [IsAdminOrClubLead]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        file_obj = request.FILES.get("file")
        if not file_obj:
            return Response({"error": "No file uploaded. Please attach a .csv or .xlsx file."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            backup_job, metadata = UniversalBackupService.intake_backup_file(
                file_obj=file_obj,
                filename=file_obj.name,
                user=request.user,
            )
            log_audit_event(
                actor=request.user,
                action="Uploaded Backup File",
                target_model="BackupJob",
                target_id=str(backup_job.id),
                details={"filename": backup_job.original_filename, "size_bytes": backup_job.file_size_bytes},
            )
            return Response(metadata, status=status.HTTP_201_CREATED)
        except BackupError as be:
            return Response({"error": str(be)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as ex:
            return Response({"error": f"Upload failed: {str(ex)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class BackupListView(APIView):
    """
    GET /api/admin/backups/
    Lists all uploaded backup jobs and raw archives.
    """
    permission_classes = [IsAdminOrClubLead]

    def get(self, request):
        backups = BackupJob.objects.all().order_by('-created_at')[:100]
        data = [
            {
                "id": str(b.id),
                "original_filename": b.original_filename,
                "file_size_bytes": b.file_size_bytes,
                "file_format": b.file_format,
                "file_sha256": b.file_sha256,
                "total_rows": b.total_rows,
                "headers": b.headers,
                "suggested_domain": b.suggested_domain,
                "suggestion_confidence": str(b.suggestion_confidence),
                "status": b.status,
                "created_at": b.created_at,
                "expires_at": b.expires_at,
                "uploader": b.created_by.email if b.created_by else "System",
            }
            for b in backups
        ]
        return Response(data)


class BackupDetailView(APIView):
    """
    GET /api/admin/backups/<uuid:id>/
    Retrieves details of a backup job and its past import attempts.
    """
    permission_classes = [IsAdminOrClubLead]

    def get(self, request, id):
        backup_job = BackupJob.objects.filter(id=id).first()
        if not backup_job:
            return Response({"error": "Backup not found."}, status=status.HTTP_404_NOT_FOUND)

        attempts = backup_job.import_attempts.all().order_by('-created_at')
        attempts_data = [
            {
                "id": str(a.id),
                "target_domain": a.target_domain,
                "target_form_id": a.target_form_id,
                "required_fields_satisfied": a.required_fields_satisfied,
                "schema_confidence_percentage": str(a.schema_confidence_percentage),
                "total_records": a.total_records,
                "valid_records": a.valid_records,
                "conflict_records": a.conflict_records,
                "status": a.status,
                "committed_at": a.committed_at,
            }
            for a in attempts
        ]

        try:
            raw_archive = backup_job.raw_archive
        except RawBackupArchive.DoesNotExist:
            raw_archive = None

        return Response({
            "id": str(backup_job.id),
            "original_filename": backup_job.original_filename,
            "file_size_bytes": backup_job.file_size_bytes,
            "file_format": backup_job.file_format,
            "file_sha256": backup_job.file_sha256,
            "total_rows": backup_job.total_rows,
            "headers": backup_job.headers,
            "suggested_domain": backup_job.suggested_domain,
            "suggestion_confidence": str(backup_job.suggestion_confidence),
            "status": backup_job.status,
            "created_at": backup_job.created_at,
            "attempts": attempts_data,
            "has_raw_archive": raw_archive is not None,
            "raw_archive_id": str(raw_archive.id) if raw_archive else None,
        })


class BackupAnalyzeDomainView(APIView):
    """
    POST /api/admin/backups/<uuid:id>/analyze/
    Evaluates backup headers against a specific domain schema.
    """
    permission_classes = [IsAdminOrClubLead]

    def post(self, request, id):
        backup_job = BackupJob.objects.filter(id=id).first()
        if not backup_job:
            return Response({"error": "Backup not found."}, status=status.HTTP_404_NOT_FOUND)

        target_domain = request.data.get("target_domain", "").strip().upper()
        if not target_domain:
            return Response({"error": "target_domain is required."}, status=status.HTTP_400_BAD_REQUEST)

        target_form_id = request.data.get("target_form_id")

        try:
            analysis = UniversalBackupService.analyze_domain(
                backup_job=backup_job,
                target_domain=target_domain,
                target_form_id=target_form_id,
            )
            return Response(analysis)
        except BackupError as be:
            return Response({"error": str(be)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as ex:
            return Response({"error": f"Analysis failed: {str(ex)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class BackupPreviewView(APIView):
    """
    POST /api/admin/backups/<uuid:id>/preview/
    Row-level inspection creating an ImportAttempt and ImportRow records.
    """
    permission_classes = [IsAdminOrClubLead]

    def post(self, request, id):
        backup_job = BackupJob.objects.filter(id=id).first()
        if not backup_job:
            return Response({"error": "Backup not found."}, status=status.HTTP_404_NOT_FOUND)

        target_domain = request.data.get("target_domain", "").strip().upper()
        if not target_domain:
            return Response({"error": "target_domain is required."}, status=status.HTTP_400_BAD_REQUEST)

        custom_mapping = request.data.get("mapping")
        target_form_id = request.data.get("target_form_id")
        idempotency_key = request.headers.get("Idempotency-Key") or request.data.get("idempotency_key")

        try:
            _, preview_data = UniversalBackupService.generate_preview(
                backup_job=backup_job,
                target_domain=target_domain,
                custom_mapping=custom_mapping,
                target_form_id=target_form_id,
                idempotency_key=idempotency_key,
            )
            return Response(preview_data)
        except BackupError as be:
            return Response({"error": str(be)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as ex:
            return Response({"error": f"Preview failed: {str(ex)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class BackupCommitImportView(APIView):
    """
    POST /api/admin/backups/<uuid:id>/imports/<uuid:attempt_id>/commit/
    Executes atomic database mutations from a validated ImportAttempt.
    """
    permission_classes = [IsAdminOrClubLead]

    def post(self, request, id, attempt_id):
        options = {
            "send_welcome_email": bool(request.data.get("send_welcome_email", False)),
            "email_template_id": request.data.get("email_template_id"),
        }

        try:
            result = UniversalBackupService.commit_import(
                attempt_id=str(attempt_id),
                user=request.user,
                options=options,
            )
            log_audit_event(
                actor=request.user,
                action="Committed Backup Import",
                target_model="ImportAttempt",
                target_id=str(attempt_id),
                details={"backup_job_id": str(id), **{k: v for k, v in result.items() if isinstance(v, (str, int, float, bool))}},
            )
            return Response(result)
        except BackupError as be:
            return Response({"error": str(be)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as ex:
            return Response({"error": f"Commit failed: {str(ex)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class BackupArchiveRawView(APIView):
    """
    POST /api/admin/backups/<uuid:id>/archive-raw/
    Direct escape hatch archiving into the schemaless Raw Vault.
    """
    permission_classes = [IsAdminOrClubLead]

    def post(self, request, id):
        backup_job = BackupJob.objects.filter(id=id).first()
        if not backup_job:
            return Response({"error": "Backup not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            result = UniversalBackupService.archive_directly_to_raw_vault(
                backup_job=backup_job,
                user=request.user,
            )
            log_audit_event(
                actor=request.user,
                action="Archived Backup to Raw Vault",
                target_model="BackupJob",
                target_id=str(id),
                details={"filename": backup_job.original_filename},
            )
            return Response(result)
        except BackupError as be:
            return Response({"error": str(be)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as ex:
            return Response({"error": f"Raw archive failed: {str(ex)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class BackupRawDownloadView(APIView):
    """
    GET /api/admin/backups/<uuid:id>/raw-download/
    Streams original pristine backup file for authorized administrators.
    """
    permission_classes = [IsAdminOrClubLead]

    def get(self, request, id):
        backup_job = BackupJob.objects.filter(id=id).first()
        if not backup_job or not backup_job.file_storage_path or not os.path.exists(backup_job.file_storage_path):
            raise Http404("Backup file not found on disk.")

        log_audit_event(
            actor=request.user,
            action="Downloaded Raw Backup File",
            target_model="BackupJob",
            target_id=str(id),
            details={"filename": backup_job.original_filename},
        )
        response = FileResponse(open(backup_job.file_storage_path, 'rb'), content_type=backup_job.mime_type)
        response['Content-Disposition'] = f'attachment; filename="{backup_job.original_filename}"'
        return response
