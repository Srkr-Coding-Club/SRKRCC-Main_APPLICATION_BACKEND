import uuid
from django.contrib.auth.models import AbstractUser
from django.db import models
from apps.core.models import TimeStampedModel

class UserRole(models.TextChoices):
    # AFFILIATE always has a club_id - enforced in RegisterSerializer.validate()
    # (signup + the admin "Create New User" modal, which reuses that same
    # endpoint) and in UserDetailView.perform_update (the admin role-change
    # PATCH). NON_AFFILIATE has no such requirement, though nothing stops one
    # from holding a club_id too (e.g. a pre-assigned id from an offline
    # recruitment drive, or historical data).
    AFFILIATE = 'AFFILIATE', 'Affiliate'
    NON_AFFILIATE = 'NON_AFFILIATE', 'Non-Affiliate'
    VOLUNTEER = 'VOLUNTEER', 'Volunteer'
    JUDGE = 'JUDGE', 'Judge'
    CLUB_LEAD = 'CLUB_LEAD', 'Club Lead'
    ADMIN = 'ADMIN', 'Admin'


class MembershipStatus(models.TextChoices):
    ACTIVE = 'ACTIVE', 'Active'
    INACTIVE = 'INACTIVE', 'Inactive'
    SUSPENDED = 'SUSPENDED', 'Suspended'
    ALUMNI = 'ALUMNI', 'Alumni'
    PENDING = 'PENDING', 'Pending'


class PasswordStatus(models.TextChoices):
    ACTIVE = 'ACTIVE', 'Password Active'
    NEEDS_SETUP = 'NEEDS_SETUP', 'Password Setup Required'


class User(AbstractUser, TimeStampedModel):
    # Canonical Login Identity
    email = models.EmailField(unique=True)

    # Permanent Club Membership Identity (e.g. 25SCC277 for SRKR Coding Club)
    club_id = models.CharField(
        max_length=30,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
        help_text="Permanent unique Club ID (e.g. 25SCC277). Immutable once assigned."
    )

    role = models.CharField(
        max_length=20,
        choices=UserRole.choices,
        default=UserRole.NON_AFFILIATE
    )
    membership_status = models.CharField(
        max_length=20,
        choices=MembershipStatus.choices,
        default=MembershipStatus.ACTIVE,
        db_index=True
    )
    password_status = models.CharField(
        max_length=20,
        choices=PasswordStatus.choices,
        default=PasswordStatus.ACTIVE,
        db_index=True,
        help_text="Tracks credential readiness: ACTIVE for normal users, NEEDS_SETUP for backup-imported accounts"
    )

    # Profile & Academic Details
    #
    # unique=True (added alongside the self-registration uniqueness check in
    # RegisterSerializer) - two students previously could sign up with the same
    # roll number since nothing enforced it beyond format. NULL is exempt from
    # the constraint, which legacy/admin/faculty rows without a roll number rely
    # on; blank='' is not, so callers that don't have a value must pass None
    # (apps/accounts/services/user_account_service.py already does this).
    roll_number = models.CharField(max_length=50, blank=True, null=True, unique=True, db_index=True)
    college = models.ForeignKey(
        'colleges.College',
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='students',
    )
    other_college_name = models.CharField(
        max_length=150,
        blank=True,
        default='',
        help_text="Typed by the member when their college is not in the catalogue; empty whenever `college` is set.",
    )
    branch = models.CharField(max_length=100, blank=True, null=True)
    year = models.IntegerField(blank=True, null=True)
    phone_number = models.CharField(max_length=20, blank=True, null=True)
    github_profile = models.URLField(blank=True, null=True)
    linkedin_profile = models.URLField(blank=True, null=True)

    # Historical Onboarding Date (preserves legacy join dates like Nov 2, 2025)
    registered_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Historical date when the member joined the club"
    )

    # Referral Tracking (Resolved FK + Raw historical string)
    referred_by_user = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='referred_members',
        help_text="Resolved club member who referred or onboarded this user"
    )
    referred_by_raw = models.CharField(
        max_length=150,
        null=True,
        blank=True,
        help_text="Raw unlinked referrer name from legacy imports"
    )

    # Provenance
    created_from = models.CharField(
        max_length=50,
        default='SELF_REGISTRATION',
        help_text="Origin of account creation: LEGACY_IMPORT, FORM, ADMIN, SELF_REGISTRATION"
    )

    # Email verification (see apps/accounts/services/email_verification.py). Anyone can sign up; a private feature needs a
    # verified address, and an account still unverified at its deadline is blocked from the private app until it verifies.
    email_verified_at = models.DateTimeField(
        null=True, blank=True, db_index=True,
        help_text="When the owner of this address proved they can read its mail. Empty = not verified.",
    )
    email_verify_deadline = models.DateTimeField(
        null=True, blank=True, db_index=True,
        help_text="Unverified accounts are blocked from private features after this moment. Admins can extend it.",
    )
    email_disputed_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Set when the address owner used \"This wasn't me\" in a verification email.",
    )

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']

    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if self._state.adding and self.email_verify_deadline is None and self.email_verified_at is None:
            from django.conf import settings
            from django.utils import timezone
            from datetime import timedelta
            self.email_verify_deadline = timezone.now() + timedelta(days=getattr(settings, 'EMAIL_VERIFY_GRACE_DAYS', 7))
        super().save(*args, **kwargs)

    @property
    def full_name(self) -> str:
        name = f"{self.first_name} {self.last_name}".strip()
        return name if name else self.username

    def __str__(self):
        club_tag = f" [{self.club_id}]" if self.club_id else ""
        return f"{self.email}{club_tag} ({self.get_role_display()})"


class ClubIDSequence(TimeStampedModel):
    """
    Thread-safe, DB-locked sequence counter for generating sequential, year-prefixed Club IDs.
    Example: prefix='SCC', year=2025, next_sequence=278 -> produces '25SCC278' for SRKR Coding Club.
    """
    prefix = models.CharField(max_length=10, default="SCC")
    year = models.PositiveIntegerField(db_index=True)
    next_sequence = models.PositiveIntegerField(default=1)

    class Meta:
        unique_together = [('prefix', 'year')]
        ordering = ['prefix', '-year']

    def __str__(self):
        return f"ClubIDSequence({self.prefix}, {self.year} -> next={self.next_sequence})"


class ReservationStatus(models.TextChoices):
    RESERVED = 'RESERVED', 'Reserved'
    CLAIMED = 'CLAIMED', 'Claimed'
    RELEASED = 'RELEASED', 'Released'


class ClubIdReservation(TimeStampedModel):
    """A Club ID held for a person who has no account (yet): nobody else can be given it, and it is attached when they register."""
    club_id = models.CharField(max_length=30, db_index=True)
    reserved_for_email = models.EmailField(blank=True, default='', help_text='Lower-cased. Empty = held for later, not for a particular person.')
    reason = models.CharField(max_length=300, blank=True, default='')
    source = models.CharField(max_length=30, default='MANUAL', help_text='MANUAL or RECONCILIATION')
    status = models.CharField(max_length=10, choices=ReservationStatus.choices, default=ReservationStatus.RESERVED, db_index=True)
    created_by = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    claimed_by = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    claimed_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(fields=['club_id'], condition=models.Q(status='RESERVED'), name='uniq_active_reservation_per_club_id'),
            models.UniqueConstraint(
                fields=['reserved_for_email'], condition=models.Q(status='RESERVED') & ~models.Q(reserved_for_email=''), name='uniq_active_reservation_per_email',
            ),
        ]

    def __str__(self):
        return f"{self.club_id} -> {self.reserved_for_email or 'held'} ({self.status})"


class ClubIdEventKind(models.TextChoices):
    ISSUED = 'ISSUED', 'Issued (next number)'
    ASSIGNED = 'ASSIGNED', 'Assigned'
    CHANGED = 'CHANGED', 'Changed'
    REASSIGNED_OUT = 'REASSIGNED_OUT', 'Moved away'
    REASSIGNED_IN = 'REASSIGNED_IN', 'Moved to'
    CLEARED = 'CLEARED', 'Cleared (retired)'
    RESERVED = 'RESERVED', 'Reserved'
    RESERVATION_RELEASED = 'RESERVATION_RELEASED', 'Reservation released'
    CLAIMED = 'CLAIMED', 'Reservation claimed'
    COUNTER_SET = 'COUNTER_SET', 'Counter moved'


class ClubIdEvent(TimeStampedModel):
    """Every change to a Club ID: who held it, who changed it, why. A CLEARED id is retired and is never handed out again by itself."""
    club_id = models.CharField(max_length=30, blank=True, default='', db_index=True)
    kind = models.CharField(max_length=24, choices=ClubIdEventKind.choices, db_index=True)
    user = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    user_email = models.EmailField(blank=True, default='')
    other_email = models.EmailField(blank=True, default='', help_text='The previous or next holder, when the id moved.')
    actor = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    actor_email = models.EmailField(blank=True, default='')
    reason = models.CharField(max_length=300, blank=True, default='')
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [models.Index(fields=['club_id', 'kind'])]

    def __str__(self):
        return f"{self.kind} {self.club_id} {self.user_email}"


class ReconciliationOutcome(models.TextChoices):
    MATCH = 'MATCH', 'Matches'
    NAME_DIFFERS = 'NAME_DIFFERS', 'Matches, name differs'
    ID_MISSING_IN_APP = 'ID_MISSING_IN_APP', 'Account has no Club ID'
    ID_MISMATCH = 'ID_MISMATCH', 'Different Club ID in the app'
    ID_HELD_BY_OTHER = 'ID_HELD_BY_OTHER', 'Club ID belongs to someone else'
    EMAIL_MISMATCH = 'EMAIL_MISMATCH', 'Club ID is under another email'
    ID_RESERVED = 'ID_RESERVED', 'Club ID is reserved for someone else'
    ID_RETIRED = 'ID_RETIRED', 'Club ID was retired'
    NO_ACCOUNT = 'NO_ACCOUNT', 'No account'
    NO_ACCOUNT_NO_ID = 'NO_ACCOUNT_NO_ID', 'No account and no Club ID'
    NO_ID_IN_SHEET = 'NO_ID_IN_SHEET', 'No Club ID in the sheet'
    INVALID = 'INVALID', 'Cannot be read'
    DUPLICATE = 'DUPLICATE', 'Repeated in the sheet'


class ReconciliationResolution(models.TextChoices):
    PENDING = 'PENDING', 'Open'
    LINKED = 'LINKED', 'Club ID linked'
    KEPT_APP = 'KEPT_APP', 'Kept the app value'
    USED_SHEET = 'USED_SHEET', 'Used the sheet value'
    FOLLOW_UP = 'FOLLOW_UP', 'Marked for follow-up'
    INVITED = 'INVITED', 'Invited'
    DISMISSED = 'DISMISSED', 'Dismissed'


class ReconciliationRun(TimeStampedModel):
    """One uploaded past-member sheet compared with the accounts. Rows keep what the sheet said and what was decided about each."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_filename = models.CharField(max_length=255)
    sheet_name = models.CharField(max_length=255, blank=True, default='')
    mapping = models.JSONField(default=dict, help_text='Which sheet column is the name, the email and the Club ID.')
    total_rows = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.source_filename} ({self.total_rows} rows)"


class ReconciliationRow(models.Model):
    run = models.ForeignKey(ReconciliationRun, on_delete=models.CASCADE, related_name='rows')
    row_number = models.PositiveIntegerField(help_text='The row in the sheet (the header is row 1).')
    sheet_name = models.CharField(max_length=200, blank=True, default='')
    sheet_email = models.CharField(max_length=254, blank=True, default='')
    sheet_club_id = models.CharField(max_length=60, blank=True, default='')
    raw = models.JSONField(default=dict, blank=True)
    outcome = models.CharField(max_length=20, choices=ReconciliationOutcome.choices, db_index=True)
    detail = models.CharField(max_length=500, blank=True, default='')
    matched_user = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    other_user = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    resolution = models.CharField(max_length=12, choices=ReconciliationResolution.choices, default=ReconciliationResolution.PENDING, db_index=True)
    note = models.CharField(max_length=300, blank=True, default='')
    resolved_by = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    resolved_at = models.DateTimeField(null=True, blank=True)
    invited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['row_number']
        constraints = [models.UniqueConstraint(fields=['run', 'row_number'], name='uniq_reconciliation_row_number')]
        indexes = [models.Index(fields=['run', 'outcome', 'resolution'])]

    def __str__(self):
        return f"row {self.row_number}: {self.outcome}"


class MembershipRequestStatus(models.TextChoices):
    PENDING = 'PENDING', 'Waiting for an organizer'
    APPROVED = 'APPROVED', 'Approved'
    REJECTED = 'REJECTED', 'Not approved'
    CANCELLED = 'CANCELLED', 'Withdrawn'


class ClubMembershipRequest(TimeStampedModel):
    """Somebody asking to become a club member. Approving gives them the next Club ID (and the affiliate role)."""
    user = models.ForeignKey('accounts.User', on_delete=models.CASCADE, related_name='membership_requests')
    status = models.CharField(max_length=10, choices=MembershipRequestStatus.choices, default=MembershipRequestStatus.PENDING, db_index=True)
    message = models.CharField(max_length=300, blank=True, default='')
    note = models.CharField(max_length=300, blank=True, default='', help_text='The reason shown to the person when a request is not approved.')
    club_id = models.CharField(max_length=30, blank=True, default='', help_text='The Club ID given when approved.')
    decided_by = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [models.UniqueConstraint(fields=['user'], condition=models.Q(status='PENDING'), name='uniq_pending_membership_request_per_user')]

    def __str__(self):
        return f"{self.user_id}: {self.status}"


class ImportJobStatus(models.TextChoices):
    PREVIEWED = 'PREVIEWED', 'Previewed'
    COMMITTED = 'COMMITTED', 'Committed'
    FAILED = 'FAILED', 'Failed'


class ImportJob(TimeStampedModel):
    """
    Snapshot of a 3-stage spreadsheet backup import (Upload -> Preview -> Commit).
    Stores parsed mapping and validation snapshot for safe, idempotent commit.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_filename = models.CharField(max_length=255)
    source_type = models.CharField(max_length=50, default='MEMBER_BACKUP')
    idempotency_key = models.CharField(max_length=128, unique=True, null=True, blank=True, db_index=True)
    status = models.CharField(max_length=20, choices=ImportJobStatus.choices, default=ImportJobStatus.PREVIEWED)
    
    mapping_snapshot = models.JSONField(default=dict)
    validation_snapshot = models.JSONField(default=dict)
    rows_data = models.JSONField(default=list, help_text="Parsed cleaned rows for atomic commit")

    total_rows = models.IntegerField(default=0)
    valid_rows = models.IntegerField(default=0)
    conflict_rows = models.IntegerField(default=0)
    new_users_count = models.IntegerField(default=0)
    updated_users_count = models.IntegerField(default=0)

    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"ImportJob({self.id} | {self.source_filename} | {self.status} | {self.valid_rows}/{self.total_rows} valid)"


class PasswordSetupToken(TimeStampedModel):
    """
    Cryptographic single-use setup token for members restored or imported from backups.
    Stores SHA-256 token hash (never plain text) with a strict 24-hour expiration window.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='password_setup_tokens')
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    is_used = models.BooleanField(default=False, db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)
    created_ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['token_hash', 'is_used']),
            models.Index(fields=['user', 'is_used']),
        ]

    def __str__(self):
        return f"PasswordSetupToken(user_id={self.user_id}, used={self.is_used}, expires={self.expires_at})"


class VerificationPurpose(models.TextChoices):
    VERIFY = 'VERIFY', 'Verify the current address'
    CHANGE = 'CHANGE', 'Change to a new address'


class VerificationStatus(models.TextChoices):
    PENDING = 'PENDING', 'Waiting for the link to be used'
    USED = 'USED', 'Confirmed'
    SUPERSEDED = 'SUPERSEDED', 'Replaced by a newer link'
    DISPUTED = 'DISPUTED', 'The address owner said it was not them'


class SendStatus(models.TextChoices):
    SENT = 'SENT', 'Sent'
    FAILED = 'FAILED', 'Could not be sent'


class EmailVerificationToken(TimeStampedModel):
    """
    A single-use link that proves the owner of ``email`` can read its mail. Only SHA-256 hashes are stored.
    The same email also carries a longer-lived "This wasn't me" link (``dispute_hash``).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='email_verification_tokens')
    purpose = models.CharField(max_length=10, choices=VerificationPurpose.choices, default=VerificationPurpose.VERIFY)
    email = models.EmailField(help_text="The address this link verifies (lower case).")
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    dispute_hash = models.CharField(max_length=64, unique=True, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    dispute_expires_at = models.DateTimeField()
    status = models.CharField(max_length=12, choices=VerificationStatus.choices, default=VerificationStatus.PENDING, db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)
    send_status = models.CharField(max_length=8, choices=SendStatus.choices, default=SendStatus.SENT)
    send_error = models.CharField(max_length=300, blank=True, default='')
    created_ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['user', 'purpose', 'status'])]

    def __str__(self):
        return f"EmailVerificationToken(user_id={self.user_id}, {self.purpose}, {self.status})"


class EmailVerificationSuppression(TimeStampedModel):
    """An address that said "This wasn't me": no verification mail is sent to it until ``until``."""
    email = models.EmailField(unique=True, help_text="Lower case.")
    until = models.DateTimeField(db_index=True)
    reason = models.CharField(max_length=100, blank=True, default='')

    def __str__(self):
        return f"{self.email} suppressed until {self.until:%Y-%m-%d}"
