import csv
from decimal import Decimal
from typing import Any
from django.db import transaction
from django.utils import timezone
from apps.core.models import BackupJob, ImportAttempt, ImportAttemptStatus, ImportRow, ImportRowAction
from apps.core.services.backup.base_importer import BaseBackupImporter
from apps.forms.models import Form, FormField, Response as FormResponse, Answer as FormAnswer
from apps.accounts.services.user_account_service import UserAccountService

try:
    import openpyxl
except ImportError:
    openpyxl = None


class FormSubmissionImporter(BaseBackupImporter):
    domain_key = 'FORMS'
    display_name = 'Form Submissions & Registrations'
    description = 'Historical responses and survey answers linked to a published club form.'

    required_fields = ['target_form_id']
    expected_fields = {}  # Dynamic per form

    def calculate_confidence(self, headers: list[str]) -> tuple[bool, Decimal, dict[str, str], list[str]]:
        # Without target form, confidence cannot be statically asserted
        return False, Decimal('0.00'), {h: "" for h in headers}, headers

    def calculate_form_confidence(self, form: Form, headers: list[str]) -> tuple[bool, Decimal, dict[str, str], list[str]]:
        form_fields = list(form.fields.filter(is_deleted=False).exclude(type='SECTION'))
        if not form_fields:
            return True, Decimal('100.00'), {}, headers

        field_labels = {f.label.strip().lower(): str(f.id) for f in form_fields}
        suggested_mapping = {}
        matched_canonical_fields = set()
        unmapped_headers = []

        for raw_h in headers:
            clean_h = raw_h.strip().lower()
            matched_id = None

            for label, f_id in field_labels.items():
                if clean_h == label or clean_h in label:
                    matched_id = f_id
                    break

            if matched_id:
                suggested_mapping[raw_h] = matched_id
                matched_canonical_fields.add(matched_id)
            else:
                suggested_mapping[raw_h] = ""
                unmapped_headers.append(raw_h)

        total_expected = len(form_fields)
        unique_matched = len(matched_canonical_fields)
        confidence_pct = (Decimal(unique_matched) / Decimal(total_expected)) * Decimal(100) if total_expected > 0 else Decimal('100.00')

        return True, round(confidence_pct, 2), suggested_mapping, unmapped_headers

    def build_normalized_mapping(self, custom_mapping: dict[str, str] | None, headers: list[str]) -> dict[str, str]:
        return custom_mapping or {h: "" for h in headers}

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
        target_form_id = context.get('target_form_id') or context.get('target_form')

        if idempotency_key:
            existing_attempt = ImportAttempt.objects.filter(idempotency_key=idempotency_key).first()
            if existing_attempt:
                return existing_attempt

        target_form = None
        if target_form_id:
            target_form = Form.objects.filter(id=target_form_id).first()

        if not target_form:
            raise ValueError("A valid published Form must be selected to import Form Submissions.")

        req_satisfied, confidence_pct, _, unmapped_headers = self.calculate_form_confidence(
            target_form, list(mapping.keys())
        )

        import_attempt = ImportAttempt.objects.create(
            backup_job=backup_job,
            target_domain=self.domain_key,
            target_form=target_form,
            idempotency_key=idempotency_key,
            column_mapping=mapping,
            unmapped_columns=unmapped_headers,
            required_fields_satisfied=req_satisfied,
            schema_confidence_percentage=confidence_pct,
            status=ImportAttemptStatus.PREVIEWED,
        )

        raw_rows = self._read_rows(backup_job)
        valid_count = 0
        import_row_objects = []

        for idx, r in enumerate(raw_rows, start=1):
            normalized_payload = {}
            for source_col, target_field_id in mapping.items():
                if target_field_id:
                    normalized_payload[target_field_id] = r.get(source_col, "")

            is_valid = len(normalized_payload) > 0
            if is_valid:
                valid_count += 1

            import_row_objects.append(
                ImportRow(
                    import_attempt=import_attempt,
                    source_row_number=idx,
                    raw_data=r,
                    normalized_data=normalized_payload,
                    action=ImportRowAction.CREATE if is_valid else ImportRowAction.SKIP,
                    is_valid=is_valid,
                    error_message="" if is_valid else "No mapped fields populated in this row",
                )
            )

        ImportRow.objects.bulk_create(import_row_objects, batch_size=500)

        import_attempt.total_records = len(raw_rows)
        import_attempt.valid_records = valid_count
        import_attempt.inserted_records = valid_count
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
            }

        target_form = import_attempt.target_form
        if not target_form:
            raise ValueError("Target form is missing from import attempt.")

        rows = import_attempt.rows.filter(is_valid=True).order_by('source_row_number')
        created_responses = []

        with transaction.atomic():
            for row in rows:
                sid = transaction.savepoint()
                try:
                    payload = row.normalized_data
                    # Check if user email exists in raw payload to link user
                    user_email = row.raw_data.get("email") or row.raw_data.get("Email")
                    linked_user = None
                    if user_email:
                        linked_user = UserAccountService.find_by_email(user_email)

                    resp = FormResponse.objects.create(
                        form=target_form,
                        user=linked_user,
                        is_manual_entry=True,
                        created_by_admin=user if hasattr(user, 'is_authenticated') and user.is_authenticated else None,
                    )

                    answer_objects = []
                    for field_id_str, val in payload.items():
                        try:
                            f_id = int(field_id_str)
                            answer_objects.append(
                                FormAnswer(
                                    response=resp,
                                    field_id=f_id,
                                    value=val,
                                )
                            )
                        except (ValueError, TypeError):
                            pass

                    if answer_objects:
                        FormAnswer.objects.bulk_create(answer_objects)

                    row.target_object_id = str(resp.id)
                    row.save(update_fields=['target_object_id'])
                    created_responses.append(resp)

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
            "imported_count": len(created_responses),
            "inserted_count": len(created_responses),
            "updated_count": 0,
        }
