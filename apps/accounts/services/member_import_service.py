import csv
import io
try:
    import openpyxl
except ImportError:
    openpyxl = None
from datetime import timedelta
from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from apps.accounts.models import ImportJob, ImportJobStatus, User
from apps.accounts.services.user_account_service import UserAccountService, ClubIdImmutableError, ClubIdConflictError
from apps.accounts.services.club_id_service import ClubIDService, InvalidClubIdError
from apps.core.models import EmailTemplate
from apps.core.services.email_service import EmailNotificationService, MemberEmailContext

MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_ALLOWED_ROWS = 10000
MAX_ALLOWED_COLS = 50

STANDARD_HEADER_SYNONYMS = {
    "email": ["email", "e-mail", "mail", "email address", "student email"],
    "full_name": ["full name", "fullname", "name", "student name", "member name", "candidate name"],
    "phone_number": ["phone number", "phone", "mobile", "mobile number", "contact", "whatsapp"],
    "branch": ["branch", "department", "dept", "course", "stream"],
    "roll_number": ["roll number", "roll no", "rollno", "reg no", "registration number", "id number"],
    "club_id": ["club id", "clubid", "member id", "membership id", "affiliate id", "scc id"],
    "referred_by": ["member", "referred by", "referrer", "onboarded by", "lead", "coordinator"],
    "registered_at": ["registration date", "reg date", "date", "joining date", "joined date", "timestamp"],
    "membership_status": ["status", "membership status", "state", "active status"],
}


class MemberImportError(Exception):
    """Base exception for member backup imports."""
    pass


class MemberImportService:
    """
    Orchestrates the 3-stage Member Directory Backup Ingestion Pipeline:
    Upload & Parse -> Validation & Snapshot (ImportJob) -> Atomic Commit & Email Automation.
    """

    @classmethod
    def auto_detect_mapping(cls, headers: list[str]) -> dict[str, str]:
        """Maps uploaded sheet headers to canonical user model fields."""
        mapping = {}
        used_fields = set()

        for header in headers:
            clean_h = str(header).strip().lower()
            matched = False
            for target_field, synonyms in STANDARD_HEADER_SYNONYMS.items():
                if target_field not in used_fields and (clean_h == target_field or clean_h in synonyms):
                    mapping[header] = target_field
                    used_fields.add(target_field)
                    matched = True
                    break
            if not matched:
                mapping[header] = ""  # ignore

        return mapping

    @classmethod
    def parse_file(cls, file_obj, filename: str) -> tuple[list[str], list[dict[str, str]]]:
        """
        Parses CSV or modern XLSX file and returns (headers, rows_as_dicts).
        Enforces 10MB size limit, 10,000 row limit, and 50 column limit.
        """
        if hasattr(file_obj, "size") and file_obj.size > MAX_FILE_SIZE_BYTES:
            raise MemberImportError(f"File exceeds maximum allowed size of 10MB ({file_obj.size} bytes).")

        filename_lower = filename.lower()
        rows = []
        headers = []

        if filename_lower.endswith(".csv"):
            content = file_obj.read()
            if isinstance(content, bytes):
                content = content.decode("utf-8-sig", errors="replace")
            reader = csv.reader(io.StringIO(content))
            raw_headers = next(reader, None)
            if not raw_headers:
                raise MemberImportError("CSV file is empty or missing header row.")

            headers = [str(h).strip() for h in raw_headers][:MAX_ALLOWED_COLS]
            for row_idx, raw_row in enumerate(reader):
                if row_idx >= MAX_ALLOWED_ROWS:
                    break
                if not any(raw_row):
                    continue
                row_dict = {}
                for col_idx, h in enumerate(headers):
                    val = raw_row[col_idx].strip() if col_idx < len(raw_row) else ""
                    row_dict[h] = val
                rows.append(row_dict)

        elif filename_lower.endswith(".xlsx"):
            wb = openpyxl.load_workbook(file_obj, data_only=True, read_only=True)
            sheet = wb.active
            iter_rows = sheet.iter_rows(values_only=True)
            raw_headers = next(iter_rows, None)
            if not raw_headers:
                raise MemberImportError("Excel sheet is empty.")

            headers = [str(h).strip() for h in raw_headers if h is not None][:MAX_ALLOWED_COLS]
            for row_idx, raw_row in enumerate(iter_rows):
                if row_idx >= MAX_ALLOWED_ROWS:
                    break
                if not any(raw_row):
                    continue
                row_dict = {}
                for col_idx, h in enumerate(headers):
                    val = str(raw_row[col_idx]).strip() if col_idx < len(raw_row) and raw_row[col_idx] is not None else ""
                    row_dict[h] = val
                rows.append(row_dict)
        else:
            raise MemberImportError("Unsupported file format. Please upload a .csv or .xlsx file.")

        return headers, rows

    @classmethod
    def preview_import(
        cls,
        file_obj,
        filename: str,
        user=None,
        custom_mapping: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[ImportJob, dict]:
        """
        Stage 2: Validates uploaded dataset, builds preview snapshot, checks collisions,
        and saves an ImportJob record (valid for 24 hours).
        """
        headers, raw_rows = cls.parse_file(file_obj, filename)
        mapping = custom_mapping or cls.auto_detect_mapping(headers)

        # In-file duplicate tracking
        seen_emails: dict[str, int] = {}
        seen_club_ids: dict[str, int] = {}

        preview_rows = []
        cleaned_rows = []
        
        new_count = 0
        update_count = 0
        conflict_count = 0

        for idx, r in enumerate(raw_rows, start=1):
            mapped_payload = {}
            for raw_h, target_f in mapping.items():
                if target_f:
                    mapped_payload[target_f] = r.get(raw_h, "").strip()

            email = UserAccountService.normalize_email(mapped_payload.get("email", ""))
            incoming_cid = (mapped_payload.get("club_id", "")).strip().upper() or None
            
            row_errors = []
            action_type = "CREATE"
            matched_user_id = None

            if not email:
                row_errors.append("Missing required email address.")
            elif "@" not in email:
                row_errors.append(f"Invalid email format '{email}'.")
            else:
                # Check duplicate email within file
                if email in seen_emails:
                    row_errors.append(f"Duplicate email '{email}' in file (row {seen_emails[email]} and row {idx}).")
                else:
                    seen_emails[email] = idx

            # Check duplicate Club ID within file
            if incoming_cid:
                if incoming_cid in seen_club_ids:
                    row_errors.append(f"Duplicate Club ID '{incoming_cid}' in file (row {seen_club_ids[incoming_cid]} and row {idx}).")
                else:
                    seen_club_ids[incoming_cid] = idx

                # Check Club ID format
                if not ClubIDService.validate_club_id_format(incoming_cid):
                    row_errors.append(f"Malformed Club ID '{incoming_cid}'. Expected format like '25SCC277'.")

            # Check against Database
            if email and not row_errors:
                existing_user = UserAccountService.find_by_email(email)
                if existing_user:
                    action_type = "UPDATE"
                    matched_user_id = existing_user.id
                    # Immutability Check
                    if incoming_cid and existing_user.club_id and existing_user.club_id.upper() != incoming_cid:
                        row_errors.append(
                            f"Club ID conflict: User already has immutable ID '{existing_user.club_id}', cannot assign '{incoming_cid}'."
                        )
                else:
                    action_type = "CREATE"

                # Check if incoming Club ID is taken by another user
                if incoming_cid:
                    other_holder = UserAccountService.find_by_club_id(incoming_cid)
                    if other_holder and other_holder.email.lower() != email:
                        row_errors.append(
                            f"Club ID '{incoming_cid}' is already registered to {other_holder.email}."
                        )

            # Resolve referral ambiguity
            ref_raw = mapped_payload.get("referred_by", "")
            ref_user, _, is_ambiguous = UserAccountService.resolve_referral(ref_raw)
            ref_display = ref_user.email if ref_user else ref_raw

            is_valid = len(row_errors) == 0
            if is_valid:
                if action_type == "CREATE":
                    new_count += 1
                else:
                    update_count += 1
            else:
                conflict_count += 1

            preview_item = {
                "row_index": idx,
                "email": email or "N/A",
                "full_name": mapped_payload.get("full_name", ""),
                "phone_number": mapped_payload.get("phone_number", ""),
                "branch": UserAccountService.normalize_branch(mapped_payload.get("branch", "")),
                "club_id": incoming_cid or "Auto-generated",
                "referred_by": ref_display,
                "is_referral_ambiguous": is_ambiguous,
                "registered_at": str(mapped_payload.get("registered_at", "")),
                "membership_status": mapped_payload.get("membership_status", "ACTIVE").upper(),
                "action": action_type if is_valid else "ERROR",
                "errors": row_errors,
                "is_valid": is_valid,
            }
            preview_rows.append(preview_item)

            if is_valid:
                cleaned_rows.append(mapped_payload)

        # Create or update ImportJob
        import_job = ImportJob.objects.create(
            source_filename=filename,
            source_type="MEMBER_BACKUP",
            idempotency_key=idempotency_key,
            status=ImportJobStatus.PREVIEWED,
            mapping_snapshot=mapping,
            validation_snapshot={
                "total_rows": len(raw_rows),
                "valid_rows": len(cleaned_rows),
                "conflict_rows": conflict_count,
                "new_users_count": new_count,
                "updated_users_count": update_count,
            },
            rows_data=cleaned_rows,
            total_rows=len(raw_rows),
            valid_rows=len(cleaned_rows),
            conflict_rows=conflict_count,
            new_users_count=new_count,
            updated_users_count=update_count,
            created_by=user,
            expires_at=timezone.now() + timedelta(hours=24),
        )

        preview_response = {
            "job_id": str(import_job.id),
            "filename": filename,
            "headers": headers,
            "mapping": mapping,
            "total_rows": len(raw_rows),
            "valid_rows": len(cleaned_rows),
            "conflict_rows": conflict_count,
            "new_users_count": new_count,
            "updated_users_count": update_count,
            "preview_sample": preview_rows[:100], # Send up to 100 preview rows
        }

        return import_job, preview_response

    @classmethod
    def commit_import(
        cls,
        job_id: str,
        user=None,
        send_welcome_email: bool = False,
        email_template_id: int | None = None,
    ) -> dict:
        """
        Stage 3: Commits the validated ImportJob snapshot into the database atomically
        with per-row savepoints. Optionally queues welcome emails for new accounts.
        """
        job = ImportJob.objects.filter(id=job_id).first()
        if not job:
            raise MemberImportError("Import job not found.")

        if job.status == ImportJobStatus.COMMITTED:
            # Idempotent response if already committed
            return {
                "success": True,
                "message": "Import job was already successfully committed.",
                "job_id": str(job.id),
                "imported_count": job.valid_rows,
                "new_users_count": job.new_users_count,
                "updated_users_count": job.updated_users_count,
            }

        if timezone.now() > job.expires_at:
            raise MemberImportError("Import job has expired. Please re-upload and validate the file.")

        created_users = []
        updated_users = []
        failed_rows = []

        # Find template if welcome email requested
        email_template = None
        if send_welcome_email and email_template_id:
            email_template = EmailTemplate.objects.filter(id=email_template_id, is_active=True).first()

        email_dispatch_tuples = []

        with transaction.atomic():
            for idx, row_payload in enumerate(job.rows_data, start=1):
                sid = transaction.savepoint()
                try:
                    user_obj, created, _ = UserAccountService.upsert_member(
                        row_payload,
                        is_backup_import=True,
                        source_origin="LEGACY_BACKUP",
                    )
                    transaction.savepoint_commit(sid)

                    if created:
                        created_users.append(user_obj)
                        if email_template:
                            ctx = MemberEmailContext.build_for_user(user_obj)
                            email_dispatch_tuples.append((user_obj.email, user_obj, ctx))
                    else:
                        updated_users.append(user_obj)

                except (ClubIdImmutableError, ClubIdConflictError, Exception) as ex:
                    transaction.savepoint_rollback(sid)
                    failed_rows.append({"row": idx, "email": row_payload.get("email"), "error": str(ex)})

            job.status = ImportJobStatus.COMMITTED
            job.save(update_fields=["status", "updated_at"])

        # Asynchronously dispatch welcome emails if requested
        if email_dispatch_tuples and email_template:
            try:
                email_job = EmailNotificationService.create_email_job(
                    template=email_template,
                    recipient_tuples=email_dispatch_tuples,
                    campaign_name=f"Backup Import Welcome - {job.source_filename}",
                    created_by=user,
                )
                EmailNotificationService.process_email_job(email_job)
            except Exception as e:
                print(f"[Email Batch Trigger Error]: {e}")

        return {
            "success": True,
            "job_id": str(job.id),
            "filename": job.source_filename,
            "imported_count": len(created_users) + len(updated_users),
            "new_users_count": len(created_users),
            "updated_users_count": len(updated_users),
            "failed_count": len(failed_rows),
            "failed_rows": failed_rows,
            "emails_queued": len(email_dispatch_tuples),
        }
