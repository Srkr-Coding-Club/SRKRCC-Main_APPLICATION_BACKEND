import uuid
from django.contrib.auth.models import AbstractUser
from django.db import models
from apps.core.models import TimeStampedModel

class UserRole(models.TextChoices):
    # AFFILIATE always has a club_id — enforced in RegisterSerializer.validate()
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
    # RegisterSerializer) — two students previously could sign up with the same
    # roll number since nothing enforced it beyond format. NULL is exempt from
    # the constraint, which legacy/admin/faculty rows without a roll number rely
    # on; blank='' is not, so callers that don't have a value must pass None
    # (apps/accounts/services/user_account_service.py already does this).
    roll_number = models.CharField(max_length=50, blank=True, null=True, unique=True, db_index=True)
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

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']

    class Meta:
        ordering = ['-created_at']

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
