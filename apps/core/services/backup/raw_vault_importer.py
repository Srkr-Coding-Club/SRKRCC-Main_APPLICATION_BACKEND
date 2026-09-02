import csv
from decimal import Decimal
from typing import Any
from django.db import transaction
from django.utils import timezone
from apps.core.models import (
    BackupJob,
    ImportAttempt,
    ImportAttemptStatus,
    ImportRow,
    ImportRowAction,
    RawBackupArchive,
    RawBackupRow,
)
from apps.core.services.backup.base_importer import BaseBackupImporter

try:
    import openpyxl
except ImportError:
    openpyxl = None


class RawVaultImporter(BaseBackupImporter):
    domain_key = 'UNKNOWN_RAW'
    display_name = 'Unknown / Schemaless Raw Vault'
    description = 'Preserves arbitrary tabular backups, unmapped spreadsheets, or legacy archives with zero schema restrictions.'

    required_fields = []
    expected_fields = {}

    def calculate_confidence(self, headers: list[str]) -> tuple[bool, Decimal, dict[str, str], list[str]]:
        # Schemaless Raw Vault is always 100% eligible with no requirements
        return True, Decimal('100.00'), {h: h for h in headers}, []

    def build_normalized_mapping(self, custom_mapping: dict[str, str] | None, headers: list[str]) -> dict[str, str]:
        return {h: h for h in headers}

    def _read_rows(self, backup_job: BackupJob) -> list[dict[str, Any]]:
        storage_path = backup_job.file_storage_path
        if not storage_path:
            return []

        rows = []
        if backup_job.file_format == 'CSV':
            with open(storage_path, 'r', encoding='utf-8-sig', errors='replace') as f:
                reader = csv.DictReader(f)
                for r in reader:
                    rows.append({k: v.strip() if isinstance(v, str) else v for k, v in r.items() if k})
        elif backup_job.file_format == 'XLSX' and openpyxl:
            wb = openpyxl.load_workbook(storage_path, data_only=True, read_only=True)
            sheet = wb.active
            headers = []
            for idx, row in enumerate(sheet.iter_rows(values_only=True)):
                if idx == 0:
                    headers = [str(c).strip() if c is not None else f"Column_{i}" for i, c in enumerate(row)]
                else:
                    if not any(row):
                        continue
                    row_dict = {}
                    for col_idx, cell in enumerate(row):
                        if col_idx < len(headers):
                            row_dict[headers[col_idx]] = str(cell).strip() if cell is not None else ""
                    rows.append(row_dict)
            wb.close()
        return rows

    def preview(
        self,
        backup_job: BackupJob,
        mapping: dict[str, str],
        context: dict[str, Any] | None = None,
    ) -> ImportAttempt:
        context = context or {}
        idempotency_key = context.get('idempotency_key')

        if idempotency_key:
            existing_attempt = ImportAttempt.objects.filter(idempotency_key=idempotency_key).first()
            if existing_attempt:
                return existing_attempt

        raw_rows = self._read_rows(backup_job)
        headers = backup_job.headers or (list(raw_rows[0].keys()) if raw_rows else [])

        import_attempt = ImportAttempt.objects.create(
            backup_job=backup_job,
            target_domain=self.domain_key,
            idempotency_key=idempotency_key,
            column_mapping={h: h for h in headers},
            unmapped_columns=[],
            required_fields_satisfied=True,
            schema_confidence_percentage=Decimal('100.00'),
            total_records=len(raw_rows),
            valid_records=len(raw_rows),
            inserted_records=len(raw_rows),
            status=ImportAttemptStatus.PREVIEWED,
        )

        import_row_objects = [
            ImportRow(
                import_attempt=import_attempt,
                source_row_number=idx,
                raw_data=r,
                normalized_data=r,
                action=ImportRowAction.CREATE,
                is_valid=True,
                error_message="",
            )
            for idx, r in enumerate(raw_rows, start=1)
        ]
        ImportRow.objects.bulk_create(import_row_objects, batch_size=500)

        backup_job.status = 'READY'
        backup_job.save(update_fields=['status', 'updated_at'])

        return import_attempt

    def commit(
        self,
        import_attempt: ImportAttempt,
        user: Any,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if import_attempt.status == ImportAttemptStatus.COMMITTED:
            return {
                "success": True,
                "already_committed": True,
                "imported_count": import_attempt.valid_records,
            }

        backup_job = import_attempt.backup_job
        rows = import_attempt.rows.all().order_by('source_row_number')

        with transaction.atomic():
            archive, created = RawBackupArchive.objects.get_or_create(
                backup_job=backup_job,
                defaults={
                    "title": f"Archive - {backup_job.original_filename}",
                    "category": "UNKNOWN_RAW",
                    "headers": backup_job.headers,
                    "total_rows": len(rows),
                    "created_by": user if hasattr(user, 'is_authenticated') and user.is_authenticated else None,
                }
            )

            raw_row_objects = [
                RawBackupRow(
                    archive=archive,
                    row_number=row.source_row_number,
                    row_data=row.raw_data,
                )
                for row in rows
            ]
            RawBackupRow.objects.bulk_create(raw_row_objects, batch_size=500)

            import_attempt.status = ImportAttemptStatus.COMMITTED
            import_attempt.committed_by = user
            import_attempt.committed_at = timezone.now()
            import_attempt.save()

            backup_job.status = 'ARCHIVED_RAW'
            backup_job.save(update_fields=['status', 'updated_at'])

        return {
            "success": True,
            "archive_id": str(archive.id),
            "imported_count": len(raw_row_objects),
            "is_raw_vault": True,
        }
