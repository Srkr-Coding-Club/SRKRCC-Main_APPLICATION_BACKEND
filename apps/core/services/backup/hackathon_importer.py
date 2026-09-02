import csv
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from django.db import transaction
from django.utils.text import slugify
from django.utils import timezone
from apps.core.models import BackupJob, ImportAttempt, ImportAttemptStatus, ImportRow, ImportRowAction
from apps.core.services.backup.base_importer import BaseBackupImporter
from apps.hackathons.models import Hackathon
from apps.accounts.services.user_account_service import UserAccountService

try:
    import openpyxl
except ImportError:
    openpyxl = None


class HackathonBackupImporter(BaseBackupImporter):
    domain_key = 'HACKATHONS'
    display_name = 'Hackathons & Sprints'
    description = '48hr build sprints, flagship hackathons, prize pools, and tracks.'

    required_fields = ['title']

    expected_fields = {
        'title': ['title', 'hackathon title', 'name', 'hackathon name', 'competition'],
        'theme': ['theme', 'track', 'problem statement', 'domain'],
        'prize_pool': ['prize pool', 'prize', 'prizes', 'cash prize', 'rewards'],
        'start_date': ['start date', 'start_date', 'start', 'from', 'date'],
        'end_date': ['end date', 'end_date', 'end', 'to', 'deadline'],
        'is_flagship': ['is flagship', 'flagship', 'iconcoders', 'flag'],
        'description': ['description', 'summary', 'about', 'overview'],
    }

    def calculate_confidence(self, headers: list[str]) -> tuple[bool, Decimal, dict[str, str], list[str]]:
        suggested_mapping = {}
        matched_canonical_fields = set()
        unmapped_headers = []

        for raw_h in headers:
            clean_h = raw_h.strip().lower()
            matched_canonical = None

            for canonical_key, aliases in self.expected_fields.items():
                if clean_h == canonical_key or clean_h in aliases:
                    matched_canonical = canonical_key
                    break

            if matched_canonical:
                suggested_mapping[raw_h] = matched_canonical
                matched_canonical_fields.add(matched_canonical)
            else:
                suggested_mapping[raw_h] = ""
                unmapped_headers.append(raw_h)

        total_expected = len(self.expected_fields)  # 7
        unique_matched = len(matched_canonical_fields)
        confidence_pct = (Decimal(unique_matched) / Decimal(total_expected)) * Decimal(100)

        required_satisfied = 'title' in matched_canonical_fields

        return required_satisfied, round(confidence_pct, 2), suggested_mapping, unmapped_headers

    def build_normalized_mapping(self, custom_mapping: dict[str, str] | None, headers: list[str]) -> dict[str, str]:
        if not custom_mapping:
            _, _, suggested, _ = self.calculate_confidence(headers)
            return suggested

        normalized = {}
        for h in headers:
            target = custom_mapping.get(h, "").strip()
            normalized[h] = target if target in self.expected_fields else ""
        return normalized

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

        req_satisfied, confidence_pct, _, unmapped_headers = self.calculate_confidence(list(mapping.keys()))

        import_attempt = ImportAttempt.objects.create(
            backup_job=backup_job,
            target_domain=self.domain_key,
            idempotency_key=idempotency_key,
            column_mapping=mapping,
            unmapped_columns=unmapped_headers,
            required_fields_satisfied=req_satisfied,
            schema_confidence_percentage=confidence_pct,
            status=ImportAttemptStatus.PREVIEWED,
        )

        raw_rows = self._read_rows(backup_job)
        valid_count = 0
        conflict_count = 0
        inserted_count = 0
        updated_count = 0
        import_row_objects = []

        for idx, r in enumerate(raw_rows, start=1):
            normalized_payload = {}
            for source_col, target_field in mapping.items():
                if target_field and target_field in self.expected_fields:
                    normalized_payload[target_field] = r.get(source_col, "")

            title = (normalized_payload.get("title") or "").strip()
            row_errors = []
            action = ImportRowAction.CREATE

            if not title:
                row_errors.append("Missing required Hackathon title.")

            start_dt = UserAccountService.parse_legacy_date(normalized_payload.get("start_date")) or timezone.now()

            # Deduplication: title + start_date
            matched_hackathon = None
            if title:
                matched_hackathon = Hackathon.objects.filter(title__iexact=title, start_date__date=start_dt.date()).first()
                if not matched_hackathon:
                    matched_hackathon = Hackathon.objects.filter(title__iexact=title).first()

            if matched_hackathon:
                action = ImportRowAction.UPDATE
            else:
                action = ImportRowAction.CREATE

            is_valid = len(row_errors) == 0
            if is_valid:
                valid_count += 1
                if action == ImportRowAction.CREATE:
                    inserted_count += 1
                else:
                    updated_count += 1
            else:
                conflict_count += 1
                action = ImportRowAction.CONFLICT

            import_row_objects.append(
                ImportRow(
                    import_attempt=import_attempt,
                    source_row_number=idx,
                    raw_data=r,
                    normalized_data=normalized_payload,
                    action=action,
                    is_valid=is_valid,
                    error_message="; ".join(row_errors),
                    target_object_id=str(matched_hackathon.id) if matched_hackathon else "",
                )
            )

        ImportRow.objects.bulk_create(import_row_objects, batch_size=500)

        import_attempt.total_records = len(raw_rows)
        import_attempt.valid_records = valid_count
        import_attempt.conflict_records = conflict_count
        import_attempt.inserted_records = inserted_count
        import_attempt.updated_records = updated_count
        import_attempt.validation_summary = {
            "unmapped_columns_count": len(unmapped_headers),
            "unmapped_columns": unmapped_headers,
        }
        import_attempt.save()

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
                "inserted_count": import_attempt.inserted_records,
                "updated_count": import_attempt.updated_records,
            }

        rows = import_attempt.rows.filter(is_valid=True).order_by('source_row_number')
        processed_hackathons = []

        with transaction.atomic():
            for row in rows:
                sid = transaction.savepoint()
                try:
                    p = row.normalized_data
                    title = p.get("title", "").strip()
                    start_dt = UserAccountService.parse_legacy_date(p.get("start_date")) or timezone.now()
                    end_dt = UserAccountService.parse_legacy_date(p.get("end_date")) or (start_dt + timedelta(days=2))
                    theme = p.get("theme", "Open Innovation").strip() or "Open Innovation"
                    prize_pool = p.get("prize_pool", "₹50,000").strip() or "₹50,000"
                    desc = p.get("description", "").strip() or f"{title} sprint organized by SRKR Coding Club."
                    flag_val = str(p.get("is_flagship", "false")).lower()
                    is_flagship = flag_val in {"true", "1", "yes", "flagship"}

                    hackathon = Hackathon.objects.filter(title__iexact=title, start_date__date=start_dt.date()).first()
                    created = False

                    if not hackathon:
                        base_slug = slugify(title) or "hackathon"
                        unique_slug = base_slug
                        c = 1
                        while Hackathon.objects.filter(slug=unique_slug).exists():
                            unique_slug = f"{base_slug}-{c}"
                            c += 1

                        hackathon = Hackathon.objects.create(
                            title=title,
                            slug=unique_slug,
                            theme=theme,
                            prize_pool=prize_pool,
                            description=desc,
                            is_flagship=is_flagship,
                            start_date=start_dt,
                            end_date=end_dt,
                        )
                        created = True
                    else:
                        hackathon.theme = theme
                        hackathon.prize_pool = prize_pool
                        hackathon.description = desc
                        hackathon.is_flagship = is_flagship
                        hackathon.end_date = end_dt
                        hackathon.save()

                    row.target_object_id = str(hackathon.id)
                    row.save(update_fields=['target_object_id'])
                    processed_hackathons.append((hackathon, created))

                    transaction.savepoint_commit(sid)
                except Exception as ex:
                    transaction.savepoint_rollback(sid)
                    row.is_valid = False
                    row.error_message = str(ex)
                    row.action = ImportRowAction.CONFLICT
                    row.save(update_fields=['is_valid', 'error_message', 'action'])

            import_attempt.status = ImportAttemptStatus.COMMITTED
            import_attempt.committed_by = user
            import_attempt.committed_at = timezone.now()
            import_attempt.save()

            import_attempt.backup_job.status = 'PARTIALLY_IMPORTED'
            import_attempt.backup_job.save(update_fields=['status', 'updated_at'])

        return {
            "success": True,
            "imported_count": len(processed_hackathons),
            "inserted_count": sum(1 for _, c in processed_hackathons if c),
            "updated_count": sum(1 for _, c in processed_hackathons if not c),
        }
