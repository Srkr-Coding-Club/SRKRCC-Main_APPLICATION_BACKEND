import re
import secrets
from datetime import datetime, date
from django.db import transaction, models, IntegrityError
from django.utils import timezone
from django.contrib.auth import get_user_model
from apps.accounts.models import MembershipStatus, UserRole, PasswordStatus
from apps.accounts.services.club_id_service import ClubIDService, InvalidClubIdError
from apps.accounts.services.referral_service import ReferralService

User = get_user_model()

FORBIDDEN_CREDENTIAL_FIELDS = {
    'password', 'pwd', 'pass', 'password_hash', 'passwd', 'credential',
    'secret', 'hash', 'passwordhash'
}

BRANCH_NORMALIZATION_MAP = {
    "cse": "CSE",
    "computer science": "CSE",
    "computer science and engineering": "CSE",
    "computer science & engineering": "CSE",
    "it": "IT",
    "information technology": "IT",
    "aids": "AIDS",
    "ai&ds": "AIDS",
    "ai & ds": "AIDS",
    "artificial intelligence and data science": "AIDS",
    "aiml": "AIML",
    "ai&ml": "AIML",
    "ai & ml": "AIML",
    "artificial intelligence and machine learning": "AIML",
    "ece": "ECE",
    "electronics and communication engineering": "ECE",
    "eee": "EEE",
    "electrical and electronics engineering": "EEE",
    "mech": "MECH",
    "mechanical": "MECH",
    "mechanical engineering": "MECH",
    "civil": "CIVIL",
    "civil engineering": "CIVIL",
    "csbs": "CSBS",
    "csd": "CSD",
}


class AccountError(Exception):
    """Base exception for User Account Service operations."""
    pass


class ClubIdImmutableError(AccountError):
    """Raised when attempting to modify an existing assigned Club ID."""
    pass


class ClubIdConflictError(AccountError):
    """Raised when an incoming Club ID already belongs to a different user."""
    pass


class UserAccountService:
    """
    Single Source of Truth for all Member and User creation, identity resolution,
    and profile synchronization across the SRKR Coding Club platform.
    """

    @classmethod
    def normalize_email(cls, email: str) -> str:
        """Strips whitespace and converts email to canonical lowercase."""
        if not email or not isinstance(email, str):
            return ""
        return email.strip().lower()

    @classmethod
    def normalize_branch(cls, branch: str | None) -> str:
        """Normalizes free-form branch strings to standard club acronyms (e.g. 'Computer Science' -> 'CSE')."""
        if not branch or not isinstance(branch, str):
            return ""
        cleaned = branch.strip()
        lookup_key = cleaned.lower()
        return BRANCH_NORMALIZATION_MAP.get(lookup_key, cleaned.upper())

    @classmethod
    def parse_legacy_date(cls, date_val: str | datetime | date | None) -> datetime | None:
        """
        Parses diverse legacy spreadsheet date formats (e.g. 'Nov 2, 2025', '2025-11-02', '02/11/2025', datetime, date).
        """
        if not date_val:
            return None
        if isinstance(date_val, datetime):
            return timezone.make_aware(date_val) if timezone.is_naive(date_val) else date_val
        if isinstance(date_val, date):
            dt = datetime.combine(date_val, datetime.min.time())
            return timezone.make_aware(dt)

        clean_str = str(date_val).strip()
        formats = [
            "%b %d, %Y",       # Nov 2, 2025
            "%B %d, %Y",       # November 2, 2025
            "%b %d %Y",        # Nov 2 2025
            "%Y-%m-%d",        # 2025-11-02
            "%Y-%m-%d %H:%M:%S", # 2025-11-02 14:30:00
            "%d/%m/%Y",        # 02/11/2025
            "%d-%m-%Y",        # 02-11-2025
            "%m/%d/%Y",        # 11/02/2025
            "%Y/%m/%d",        # 2025/11/02
        ]

        for fmt in formats:
            try:
                dt = datetime.strptime(clean_str, fmt)
                return timezone.make_aware(dt) if timezone.is_naive(dt) else dt
            except ValueError:
                continue

        return None

    @classmethod
    def find_by_email(cls, email: str) -> User | None:
        """Case-insensitive canonical email lookup."""
        canonical = cls.normalize_email(email)
        if not canonical:
            return None
        return User.objects.filter(email__iexact=canonical).first()

    @classmethod
    def find_by_club_id(cls, club_id: str) -> User | None:
        """Exact lookup by Club ID (case-insensitive)."""
        if not club_id or not isinstance(club_id, str):
            return None
        return User.objects.filter(club_id__iexact=club_id.strip()).first()

    @classmethod
    def ensure_club_id(cls, user: User, year: int | None = None) -> str:
        """
        Guarantees that a user has a permanent Club ID.
        If already present, returns existing ID without modifying.
        If missing, allocates next sequential Club ID and atomically persists.
        """
        if user.club_id:
            return user.club_id

        # Determine year: from user.registered_at or user.created_at or current year
        target_year = None
        if user.registered_at:
            target_year = user.registered_at.year
        elif user.created_at:
            target_year = user.created_at.year

        new_club_id = ClubIDService.allocate_next_club_id(year=target_year)
        user.club_id = new_club_id
        user.save(update_fields=["club_id", "updated_at"])
        return new_club_id

    @classmethod
    def resolve_referral(cls, referrer_str: str | None) -> tuple[User | None, str, bool]:
        """
        Delegates referral resolution to ReferralService with strict confidence rules.
        """
        return ReferralService.resolve_referrer(referrer_str)

    @classmethod
    def upsert_member(
        cls,
        payload: dict,
        is_backup_import: bool = False,
        source_origin: str = "LEGACY_IMPORT",
        preserve_existing_fields: bool = False,
    ) -> tuple[User, bool, list[str]]:
        """
        Canonical, atomic member upsert method used by CSV Ingestion, Forms, and Admin modals.
        
        Payload keys supported:
          - email (required)
          - full_name / name / first_name / last_name
          - phone_number / phone
          - branch
          - roll_number
          - year
          - club_id (optional: preserved or validated)
          - registered_at / registration_date
          - membership_status / status ('Active' -> 'ACTIVE')
          - referred_by / member
        
        Returns: (user, created_boolean, list_of_changed_field_names)
        Raises: ClubIdImmutableError, ClubIdConflictError, AccountError
        """
        # Purge any forbidden credential columns
        for k in list(payload.keys()):
            if str(k).strip().lower().replace(" ", "_") in FORBIDDEN_CREDENTIAL_FIELDS:
                payload.pop(k, None)

        raw_email = payload.get("email") or payload.get("Email")
        if not raw_email:
            raise AccountError("Email is required to create or update a member record.")

        email = cls.normalize_email(raw_email)
        incoming_club_id = (payload.get("club_id") or payload.get("Club ID") or "").strip().upper() or None
        
        # Name handling
        full_name = (payload.get("full_name") or payload.get("Full Name") or payload.get("name") or "").strip()
        first_name = (payload.get("first_name") or "").strip()
        last_name = (payload.get("last_name") or "").strip()

        if full_name and not (first_name or last_name):
            parts = full_name.split()
            first_name = parts[0]
            last_name = " ".join(parts[1:]) if len(parts) > 1 else ""

        # Academic / Phone / Branch
        raw_branch = payload.get("branch") or payload.get("Branch")
        branch = cls.normalize_branch(raw_branch)
        phone = (payload.get("phone_number") or payload.get("Phone Number") or payload.get("phone") or "").strip()
        roll_num = (payload.get("roll_number") or payload.get("Roll Number") or "").strip() or None
        year_val = payload.get("year") or payload.get("Year")
        try:
            year_int = int(year_val) if year_val else None
        except (ValueError, TypeError):
            year_int = None

        # Registration Date
        raw_reg_date = payload.get("registered_at") or payload.get("Registration Date") or payload.get("registration_date")
        registered_at = cls.parse_legacy_date(raw_reg_date)

        # Status
        raw_status = str(payload.get("membership_status") or payload.get("Status") or "ACTIVE").strip().upper()
        if raw_status in {"ACTIVE", "ENABLED", "TRUE", "1"}:
            status = MembershipStatus.ACTIVE
        elif raw_status in {"INACTIVE", "DISABLED", "FALSE", "0"}:
            status = MembershipStatus.INACTIVE
        elif raw_status in {"ALUMNI", "GRADUATED"}:
            status = MembershipStatus.ALUMNI
        elif raw_status in {"SUSPENDED", "BLOCKED"}:
            status = MembershipStatus.SUSPENDED
        else:
            status = MembershipStatus.ACTIVE

        # Referral
        raw_referrer = payload.get("referred_by") or payload.get("Member") or payload.get("referrer")
        ref_user, ref_raw, _ = cls.resolve_referral(raw_referrer)

        # Collision Check: Does incoming club_id belong to another existing email?
        if incoming_club_id:
            existing_club_holder = cls.find_by_club_id(incoming_club_id)
            if existing_club_holder and existing_club_holder.email.lower() != email:
                raise ClubIdConflictError(
                    f"Club ID '{incoming_club_id}' is already assigned to {existing_club_holder.email}. "
                    f"Cannot reassign to {email}."
                )

        with transaction.atomic():
            user = cls.find_by_email(email)
            created = False
            changed_fields = []

            if not user:
                # Create New User. The whole create attempt is wrapped in a savepoint:
                # two concurrent requests for the *same* new email can both pass the
                # find_by_email() check above before either commits (read-committed
                # isolation), and both then race to INSERT. The DB's unique constraint
                # on email lets only one succeed; we catch the other's IntegrityError
                # and fall through to the update path below against the winner's row,
                # instead of letting a raw 500 escape.
                created = True
                try:
                    with transaction.atomic():
                        username_base = email.split("@")[0][:140]
                        unique_username = username_base
                        counter = 1
                        while User.objects.filter(username=unique_username).exists():
                            unique_username = f"{username_base[:135]}_{counter}"
                            counter += 1

                        # If incoming club_id is provided, use it; otherwise allocate new
                        final_club_id = incoming_club_id
                        if not final_club_id:
                            target_year = registered_at.year if registered_at else timezone.now().year
                            final_club_id = ClubIDService.allocate_next_club_id(year=target_year)
                        else:
                            # Sync sequence watermark so future allocations know about this imported sequence
                            try:
                                parsed = ClubIDService.parse_club_id(final_club_id)
                                ClubIDService.sync_sequence_watermark(parsed["full_year"], parsed["sequence"], parsed["prefix"])
                            except InvalidClubIdError:
                                pass

                        user = User(
                            username=unique_username,
                            email=email,
                            first_name=first_name,
                            last_name=last_name,
                            club_id=final_club_id,
                            branch=branch,
                            phone_number=phone,
                            roll_number=roll_num,
                            year=year_int,
                            registered_at=registered_at or timezone.now(),
                            membership_status=status,
                            referred_by_user=ref_user,
                            referred_by_raw=ref_raw or None,
                            created_from=source_origin,
                            # Unconditionally AFFILIATE, not club_id-derived: this path always
                            # resolves a club_id above (either the imported row's own, or a
                            # freshly allocated one) before reaching this point.
                            role=UserRole.AFFILIATE,
                            password_status=PasswordStatus.NEEDS_SETUP if is_backup_import else PasswordStatus.ACTIVE,
                        )
                        if is_backup_import:
                            user.set_unusable_password()
                        else:
                            random_password = secrets.token_urlsafe(16)
                            user.set_password(random_password)
                        user.save()
                    changed_fields.append("created_account")
                except IntegrityError:
                    created = False
                    user = cls.find_by_email(email)
                    if not user:
                        # Not an email collision after all (e.g. username or club_id
                        # unique constraint) — re-raise the original failure shape.
                        raise

            if not created:
                # Update Existing User
                # IMMUTABILITY INVARIANT: Check if incoming Club ID conflicts with existing Club ID
                if incoming_club_id and user.club_id and user.club_id.upper() != incoming_club_id.upper():
                    raise ClubIdImmutableError(
                        f"User {user.email} already has immutable Club ID '{user.club_id}'. "
                        f"Cannot change to '{incoming_club_id}'."
                    )

                # Assign Club ID if user lacked one
                if not user.club_id:
                    if incoming_club_id:
                        user.club_id = incoming_club_id
                        try:
                            parsed = ClubIDService.parse_club_id(incoming_club_id)
                            ClubIDService.sync_sequence_watermark(parsed["full_year"], parsed["sequence"], parsed["prefix"])
                        except InvalidClubIdError:
                            pass
                    else:
                        target_year = (user.registered_at.year if user.registered_at else None) or (registered_at.year if registered_at else None)
                        user.club_id = ClubIDService.allocate_next_club_id(year=target_year)
                    changed_fields.append("club_id")

                # Update fields if not preserved or if currently empty
                update_field_list = []
                
                if first_name and (not preserve_existing_fields or not user.first_name):
                    if user.first_name != first_name:
                        user.first_name = first_name
                        update_field_list.append("first_name")

                if last_name and (not preserve_existing_fields or not user.last_name):
                    if user.last_name != last_name:
                        user.last_name = last_name
                        update_field_list.append("last_name")

                if branch and (not preserve_existing_fields or not user.branch):
                    if user.branch != branch:
                        user.branch = branch
                        update_field_list.append("branch")

                if phone and (not preserve_existing_fields or not user.phone_number):
                    if user.phone_number != phone:
                        user.phone_number = phone
                        update_field_list.append("phone_number")

                if roll_num and (not preserve_existing_fields or not user.roll_number):
                    if user.roll_number != roll_num:
                        user.roll_number = roll_num
                        update_field_list.append("roll_number")

                if registered_at and not user.registered_at:
                    user.registered_at = registered_at
                    update_field_list.append("registered_at")

                if status and user.membership_status != status and not preserve_existing_fields:
                    user.membership_status = status
                    update_field_list.append("membership_status")

                if ref_user and not user.referred_by_user:
                    user.referred_by_user = ref_user
                    update_field_list.append("referred_by_user")
                elif ref_raw and not user.referred_by_raw:
                    user.referred_by_raw = ref_raw
                    update_field_list.append("referred_by_raw")

                if "club_id" in changed_fields:
                    update_field_list.append("club_id")

                # Authoritative Password Reconciliation Invariant:
                # Never call set_unusable_password() on an existing user during import updates!
                # Stored password hash is kept byte-for-byte identical.
                if user.has_usable_password():
                    if user.password_status != PasswordStatus.ACTIVE:
                        user.password_status = PasswordStatus.ACTIVE
                        update_field_list.append("password_status")
                else:
                    if user.password_status != PasswordStatus.NEEDS_SETUP:
                        user.password_status = PasswordStatus.NEEDS_SETUP
                        update_field_list.append("password_status")

                if update_field_list:
                    update_field_list.append("updated_at")
                    user.save(update_fields=update_field_list)
                    changed_fields.extend(update_field_list)

            return user, created, changed_fields
