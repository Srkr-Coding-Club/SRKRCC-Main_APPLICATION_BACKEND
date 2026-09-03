import hashlib
import io
import os
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any
from django.conf import settings
from django.utils import timezone
from apps.core.models import BackupJob, BackupJobStatus, ImportAttempt
from apps.core.services.backup.registry import BackupImporterRegistry

try:
    import openpyxl
except ImportError:
    openpyxl = None
import csv

MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_ALLOWED_ROWS = 10000
MAX_ALLOWED_COLS = 50
MAX_CELL_LENGTH = 10000

BACKUP_STORAGE_DIR = os.path.join(settings.MEDIA_ROOT, 'backups')


class BackupError(Exception):
    """Base exception for universal backup operations."""
    pass


class UniversalBackupService:
    """
    Orchestrates backup intake, SHA-256 hashing, file storage, domain analysis, and preview/commit.
    """

    @classmethod
    def intake_backup_file(
        cls,
        file_obj: Any,
        filename: str,
        user: Any = None,
    ) -> tuple[BackupJob, dict[str, Any]]:
        """
        Stage 1: Intake & Preserve
        Computes SHA-256, verifies limits, stores the original file artifact,
        detects headers and raw row count, and suggests a domain.
        """
        if not os.path.exists(BACKUP_STORAGE_DIR):
            os.makedirs(BACKUP_STORAGE_DIR, exist_ok=True)

        file_bytes = file_obj.read()
        file_size = len(file_bytes)

        if file_size > MAX_FILE_SIZE_BYTES:
            raise BackupError(f"File exceeds maximum allowed limit of 10 MB ({file_size / (1024*1024):.2f} MB).")

        # Compute SHA-256 Hash
        sha256_hash = hashlib.sha256(file_bytes).hexdigest()

        # Deduplication check
        existing_backup = BackupJob.objects.filter(file_sha256=sha256_hash).first()
        is_duplicate = existing_backup is not None

        ext = os.path.splitext(filename)[1].lower()
        if ext not in {'.csv', '.xlsx'}:
            raise BackupError(f"Unsupported file format '{ext}'. Only .csv and .xlsx files are supported.")

        file_format = 'CSV' if ext == '.csv' else 'XLSX'
        mime_type = 'text/csv' if file_format == 'CSV' else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

        # Store isolated original file
        storage_filename = f"{uuid.uuid4().hex}{ext}"
        storage_path = os.path.join(BACKUP_STORAGE_DIR, storage_filename)
        with open(storage_path, 'wb') as dest:
            dest.write(file_bytes)

        # Parse headers and count rows
        headers = []
        row_count = 0

        if file_format == 'CSV':
            try:
                decoded_str = file_bytes.decode('utf-8-sig', errors='replace')
                reader = csv.reader(decoded_str.splitlines())
                for idx, r in enumerate(reader):
                    if idx == 0:
                        headers = [h.strip() for h in r if h.strip()]
                    else:
                        if any(cell.strip() for cell in r if cell):
                            row_count += 1
            except Exception as ex:
                raise BackupError(f"Failed parsing CSV: {str(ex)}")
        elif file_format == 'XLSX':
            if not openpyxl:
                raise BackupError("openpyxl is required to parse Excel spreadsheets.")
            try:
                wb = openpyxl.load_workbook(filename=io.BytesIO(file_bytes), data_only=True, read_only=True)
                sheet = wb.active
                for idx, row in enumerate(sheet.iter_rows(values_only=True)):
                    if idx == 0:
                        headers = [str(c).strip() for c in row if c is not None and str(c).strip()]
                    else:
                        if any(row):
                            row_count += 1
                wb.close()
            except Exception as ex:
                raise BackupError(f"Failed parsing XLSX: {str(ex)}")

        if not headers:
            raise BackupError("The uploaded file does not contain any column headers.")

        if len(headers) > MAX_ALLOWED_COLS:
            raise BackupError(f"Spreadsheet contains {len(headers)} columns, exceeding the maximum allowed {MAX_ALLOWED_COLS} columns.")

        if row_count > MAX_ALLOWED_ROWS:
            raise BackupError(f"Spreadsheet contains {row_count} rows, exceeding the maximum allowed {MAX_ALLOWED_ROWS} rows.")

        # Suggest domain based on headers
        suggested_domain, confidence, reason = BackupImporterRegistry.suggest_domain(headers)

        backup_job = BackupJob.objects.create(
            original_filename=filename,
            file_sha256=sha256_hash,
            file_size_bytes=file_size,
            file_storage_path=storage_path,
            mime_type=mime_type,
            file_format=file_format,
            headers=headers,
            total_rows=row_count,
            suggested_domain=suggested_domain,
            suggestion_confidence=confidence,
            status=BackupJobStatus.AWAITING_DOMAIN_SELECTION,
            created_by=user if hasattr(user, 'is_authenticated') and user.is_authenticated else None,
            expires_at=timezone.now() + timedelta(hours=24),
        )

        metadata = {
            "backup_id": str(backup_job.id),
            "filename": filename,
            "file_size": file_size,
            "sha256": sha256_hash,
            "headers": headers,
            "total_rows": row_count,
            "suggested_domain": suggested_domain,
            "confidence_percentage": str(confidence),
            "reason": reason,
            "is_duplicate_hash": is_duplicate,
            "existing_backup_id": str(existing_backup.id) if existing_backup else None,
        }

        return backup_job, metadata

    @classmethod
    def analyze_domain(
        cls,
        backup_job: BackupJob,
        target_domain: str,
        target_form_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Stage 2: Schema-Level Analysis
        Calculates column mapping and 50% confidence score for a specific selected domain.
        """
        importer = BackupImporterRegistry.get_importer(target_domain)
        headers = backup_job.headers

        if target_domain == 'FORMS' and target_form_id:
            from apps.forms.models import Form
            form = Form.objects.filter(id=target_form_id).first()
            if not form:
                raise BackupError(f"Target Form #{target_form_id} does not exist.")
            from apps.core.services.backup.form_importer import FormSubmissionImporter
            if isinstance(importer, FormSubmissionImporter):
                req_sat, conf, mapping, unmapped = importer.calculate_form_confidence(form, headers)
                form_fields = form.fields.filter(is_deleted=False).exclude(type='SECTION')
                expected_fields = {str(f.id): [f.label] for f in form_fields}
            else:
                req_sat, conf, mapping, unmapped = False, Decimal('0.00'), {}, headers
                expected_fields = {}
        else:
            req_sat, conf, mapping, unmapped = importer.calculate_confidence(headers)
            expected_fields = importer.expected_fields

        return {
            "backup_id": str(backup_job.id),
            "target_domain": target_domain,
            "domain_display": importer.display_name,
            "required_fields_satisfied": req_sat,
            "schema_confidence_percentage": str(conf),
            "is_eligible_for_structured_import": req_sat and (conf >= Decimal('50.00')),
            "suggested_mapping": mapping,
            "unmapped_headers": unmapped,
            "headers": headers,
            "expected_fields": expected_fields,
            "required_fields": importer.required_fields,
        }

    @classmethod
    def generate_preview(
        cls,
        backup_job: BackupJob,
        target_domain: str,
        custom_mapping: dict[str, str] | None = None,
        target_form_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[ImportAttempt, dict[str, Any]]:
        """
        Stage 3: Row-Level Preview
        Reads all rows, verifies collisions, and creates ImportRow provenance records.
        """
        importer = BackupImporterRegistry.get_importer(target_domain)
        normalized_mapping = importer.build_normalized_mapping(custom_mapping, backup_job.headers)

        context = {
            "idempotency_key": idempotency_key,
            "target_form_id": target_form_id,
        }

        import_attempt = importer.preview(
            backup_job=backup_job,
            mapping=normalized_mapping,
            context=context,
        )

        rows_sample = list(import_attempt.rows.all()[:50].values(
            'source_row_number', 'raw_data', 'normalized_data', 'action', 'is_valid', 'error_message'
        ))

        preview_data = {
            "attempt_id": str(import_attempt.id),
            "backup_id": str(backup_job.id),
            "target_domain": target_domain,
            "total_records": import_attempt.total_records,
            "valid_records": import_attempt.valid_records,
            "conflict_records": import_attempt.conflict_records,
            "inserted_records": import_attempt.inserted_records,
            "updated_records": import_attempt.updated_records,
            "required_fields_satisfied": import_attempt.required_fields_satisfied,
            "schema_confidence_percentage": str(import_attempt.schema_confidence_percentage),
            "unmapped_columns": import_attempt.unmapped_columns,
            "rows_sample": rows_sample,
            "status": import_attempt.status,
        }

        return import_attempt, preview_data

    @classmethod
    def commit_import(
        cls,
        attempt_id: str,
        user: Any,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Stage 4: Confirmed Commit
        Transforms validated ImportAttempt into application domain models.
        """
        import_attempt = ImportAttempt.objects.filter(id=attempt_id).select_related('backup_job').first()
        if not import_attempt:
            raise BackupError(f"Import attempt #{attempt_id} not found.")

        importer = BackupImporterRegistry.get_importer(import_attempt.target_domain)
        result = importer.commit(import_attempt=import_attempt, user=user, options=options)
        return result

    @classmethod
    def archive_directly_to_raw_vault(
        cls,
        backup_job: BackupJob,
        user: Any,
    ) -> dict[str, Any]:
        """
        Direct escape hatch: Archives the backup into the schemaless Raw Vault.
        """
        from apps.core.services.backup.raw_vault_importer import RawVaultImporter
        importer = RawVaultImporter()
        import_attempt = importer.preview(backup_job=backup_job, mapping={h: h for h in backup_job.headers})
        result = importer.commit(import_attempt=import_attempt, user=user)
        return result
