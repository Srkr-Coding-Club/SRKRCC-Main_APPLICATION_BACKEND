import csv
import io
from decimal import Decimal
from typing import Any
from django.db import transaction
from django.utils import timezone
from apps.core.models import BackupJob, ImportAttempt, ImportAttemptStatus, ImportRow, ImportRowAction
from apps.core.services.backup.base_importer import BaseBackupImporter
from apps.accounts.services.user_account_service import UserAccountService, ClubIdImmutableError, ClubIdConflictError
from apps.accounts.services.club_id_service import ClubIDService
from apps.accounts.models import User, MembershipStatus
from apps.core.models import EmailTemplate
from apps.core.services.email_service import EmailNotificationService, MemberEmailContext

try:
    import openpyxl
except ImportError:
    openpyxl = None


class UserBackupImporter(BaseBackupImporter):
    domain_key = 'USERS'
    display_name = 'Club Member Directory'
    description = 'Members, students, and user accounts with permanent Club IDs (25SCC...) and referral attribution.'

    required_fields = ['email']

    expected_fields = {
        'full_name': ['full name', 'fullname', 'name', 'student name', 'member name', 'candidate name'],
        'email': ['email', 'e-mail', 'mail', 'email address', 'student email'],
        'phone_number': ['phone number', 'phone', 'mobile', 'mobile number', 'contact', 'whatsapp'],
        'branch': ['branch', 'department', 'dept', 'course', 'stream'],
        'club_id': ['club id', 'clubid', 'member id', 'membership id', 'scc id'],
        'referred_by': ['member', 'referred by', 'referrer', 'onboarded by', 'lead', 'coordinator'],
        'registered_at': ['registration date', 'reg date', 'date', 'joining date', 'joined date', 'timestamp'],
        'membership_status': ['status', 'membership status', 'state', 'active status'],
    }

    FORBIDDEN_CREDENTIAL_FIELDS = {
        'password', 'pwd', 'pass', 'password_hash', 'passwd', 'credential',
        'secret', 'hash', 'passwordhash'
    }

    def calculate_confidence(self, headers: list[str]) -> tuple[bool, Decimal, dict[str, str], list[str]]:
        clean_headers = [h.strip().lower() for h in headers if h and h.strip()]
        suggested_mapping = {}
        matched_canonical_fields = set()
        unmapped_headers = []

        for raw_h in headers:
            clean_h = raw_h.strip().lower().replace(" ", "_")
            if clean_h in self.FORBIDDEN_CREDENTIAL_FIELDS:
                # Systematically ignore forbidden credential columns
                suggested_mapping[raw_h] = ""
                unmapped_headers.append(raw_h)
                continue
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

        total_expected = len(self.expected_fields)  # 8
        unique_matched = len(matched_canonical_fields)
        confidence_pct = (Decimal(unique_matched) / Decimal(total_expected)) * Decimal(100)

        # Required fields check: at least email or club_id must be recognized
        required_satisfied = 'email' in matched_canonical_fields or 'club_id' in matched_canonical_fields

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

        # Check existing attempt with same idempotency key
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
        seen_emails: dict[str, int] = {}
        seen_club_ids: dict[str, int] = {}

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

            email = UserAccountService.normalize_email(normalized_payload.get("email", ""))
            incoming_cid = (normalized_payload.get("club_id", "")).strip().upper() or None

            row_errors = []
            action = ImportRowAction.CREATE

            # Identity rules:
            # Existing user: club_id OR email
            # New user: email strictly required
            matched_user = None
            if incoming_cid:
                matched_user = UserAccountService.find_by_club_id(incoming_cid)
            if not matched_user and email:
                matched_user = UserAccountService.find_by_email(email)

            if matched_user:
                action = ImportRowAction.UPDATE
                # Immutability Check
                if incoming_cid and matched_user.club_id and matched_user.club_id.upper() != incoming_cid:
                    row_errors.append(
                        f"Club ID conflict: User already has permanent ID '{matched_user.club_id}', cannot assign '{incoming_cid}'."
                    )
            else:
                action = ImportRowAction.CREATE
                if not email:
                    row_errors.append("Missing required email address for new member account creation.")
                elif "@" not in email:
                    row_errors.append(f"Invalid email format '{email}'.")

            # Duplicate checking within the file
            if email:
                if email in seen_emails:
                    row_errors.append(f"Duplicate email '{email}' in file (row {seen_emails[email]} and row {idx}).")
                else:
                    seen_emails[email] = idx

            if incoming_cid:
                if incoming_cid in seen_club_ids:
                    row_errors.append(f"Duplicate Club ID '{incoming_cid}' in file (row {seen_club_ids[incoming_cid]} and row {idx}).")
                else:
                    seen_club_ids[incoming_cid] = idx

                if not ClubIDService.validate_club_id_format(incoming_cid):
                    row_errors.append(f"Malformed Club ID '{incoming_cid}'. Expected format like '25SCC277'.")

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
                    raw_data=r,  # Preserves all raw unmapped columns
                    normalized_data=normalized_payload,
                    action=action,
                    is_valid=is_valid,
                    error_message="; ".join(row_errors),
                    target_object_id=str(matched_user.id) if matched_user else "",
                )
            )

        ImportRow.objects.bulk_create(import_row_objects, batch_size=500)

        import_attempt.total_records = len(raw_rows)
        import_attempt.valid_records = valid_count
        import_attempt.conflict_records = conflict_count
        import_attempt.inserted_records = inserted_count
        import_attempt.updated_records = updated_count
        import_attempt.validation_summary = {
            "duplicate_emails_in_file": len(raw_rows) - len(seen_emails),
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
        options = options or {}
        send_welcome_email = bool(options.get("send_welcome_email", False))
        email_template_id = options.get("email_template_id")

        if import_attempt.status == ImportAttemptStatus.COMMITTED:
            return {
                "success": True,
                "already_committed": True,
                "imported_count": import_attempt.valid_records,
                "new_users_count": import_attempt.inserted_records,
                "updated_users_count": import_attempt.updated_records,
            }

        rows = import_attempt.rows.filter(is_valid=True).order_by('source_row_number')
        imported_users = []
        max_imported_year = None
        max_imported_seq = 0

        with transaction.atomic():
            for row in rows:
                sid = transaction.savepoint()
                try:
                    payload = dict(row.normalized_data)
                    user_obj, created, _ = UserAccountService.upsert_member(
                        payload=payload,
                        is_backup_import=True,
                        source_origin="LEGACY_IMPORT",
                    )
                    imported_users.append((user_obj, created))
                    row.target_object_id = str(user_obj.id)
                    row.save(update_fields=['target_object_id'])

                    if user_obj.club_id:
                        try:
                            parsed_cid = ClubIDService.parse_club_id(user_obj.club_id)
                            cid_year = parsed_cid["full_year"]
                            cid_seq = parsed_cid["sequence"]
                            if max_imported_year is None or cid_year >= max_imported_year:
                                max_imported_year = cid_year
                                if cid_seq > max_imported_seq:
                                    max_imported_seq = cid_seq
                        except Exception:
                            pass

                    transaction.savepoint_commit(sid)
                except Exception as ex:
                    transaction.savepoint_rollback(sid)
                    row.is_valid = False
                    row.error_message = str(ex)
                    row.action = ImportRowAction.CONFLICT
                    row.save(update_fields=['is_valid', 'error_message', 'action'])

            # Fast-forward sequence watermark past highest imported Club ID
            if max_imported_year and max_imported_seq > 0:
                ClubIDService.sync_sequence_watermark(
                    year=max_imported_year,
                    max_seen_sequence=max_imported_seq,
                    prefix="SCC",
                )

            import_attempt.status = ImportAttemptStatus.COMMITTED
            import_attempt.committed_by = user
            import_attempt.committed_at = timezone.now()
            import_attempt.save()

            import_attempt.backup_job.status = 'PARTIALLY_IMPORTED'
            import_attempt.backup_job.save(update_fields=['status', 'updated_at'])

        # Optional Welcome Email Broadcast
        if send_welcome_email and imported_users:
            template = None
            if email_template_id:
                template = EmailTemplate.objects.filter(id=email_template_id, is_active=True).first()
            if not template:
                template = EmailTemplate.objects.filter(name='member_welcome', is_active=True).first()

            if template:
                recipient_tuples = [
                    (u.email, u, MemberEmailContext.build_for_user(u))
                    for u, _ in imported_users if u.email
                ]
                job = EmailNotificationService.create_email_job(
                    template=template,
                    recipient_tuples=recipient_tuples,
                    campaign_name=f"Welcome - Backup Import {import_attempt.backup_job.original_filename}",
                    created_by=user,
                )
                EmailNotificationService.process_email_job(job)

        return {
            "success": True,
            "imported_count": len(imported_users),
            "new_users_count": sum(1 for _, c in imported_users if c),
            "updated_users_count": sum(1 for _, c in imported_users if not c),
        }
