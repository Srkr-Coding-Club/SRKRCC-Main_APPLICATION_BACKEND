import re
from datetime import datetime
from django.db import transaction
from django.utils import timezone
from apps.accounts.models import ClubIDSequence

CLUB_ID_REGEX = re.compile(r"^(\d{2})([A-Za-z]{3})(\d{3,})$")

class ClubIdError(Exception):
    """Base exception for Club ID operations."""
    pass

class InvalidClubIdError(ClubIdError):
    """Raised when a Club ID string is malformed or violates expected prefix/year."""
    pass

class ClubIdAllocationError(ClubIdError):
    """Raised when Club ID sequence allocation fails."""
    pass


class ClubIDService:
    """
    Centralized, thread-safe, concurrency-locked service for generating, validating,
    and allocating unique sequential Club IDs (e.g. 25SCC001 -> 25SCC277 -> 25SCC278).
    """

    DEFAULT_PREFIX = "SCC"

    @classmethod
    def get_current_two_digit_year(cls, dt: datetime | None = None) -> int:
        """Returns 2-digit integer representation of year (e.g. 2025 -> 25)."""
        target = dt or timezone.now()
        return target.year % 100

    @classmethod
    def get_full_year_from_two_digit(cls, two_digit_year: int) -> int:
        """Converts 2-digit year (25) to full 4-digit year (2025)."""
        return 2000 + two_digit_year

    @classmethod
    def parse_club_id(cls, club_id: str, expected_prefix: str = DEFAULT_PREFIX) -> dict:
        """
        Parses and validates a Club ID string.
        Returns dict with:
          - year_2digit: int (e.g. 25)
          - full_year: int (e.g. 2025)
          - prefix: str (e.g. 'SCC')
          - sequence: int (e.g. 277)
          - canonical_id: str (e.g. '25SCC277')
        Raises InvalidClubIdError if invalid format or unexpected prefix.
        """
        if not club_id or not isinstance(club_id, str):
            raise InvalidClubIdError("Club ID must be a non-empty string.")

        clean_id = club_id.strip().upper()
        match = CLUB_ID_REGEX.match(clean_id)
        if not match:
            raise InvalidClubIdError(
                f"Invalid Club ID format '{club_id}'. Expected format '<YY><PREFIX><SEQUENCE>', e.g. '25SCC277'."
            )

        two_digit_year = int(match.group(1))
        prefix = match.group(2)
        sequence = int(match.group(3))

        if expected_prefix and prefix != expected_prefix.upper():
            raise InvalidClubIdError(
                f"Club ID prefix mismatch: expected '{expected_prefix.upper()}', got '{prefix}' in '{club_id}'."
            )

        return {
            "year_2digit": two_digit_year,
            "full_year": cls.get_full_year_from_two_digit(two_digit_year),
            "prefix": prefix,
            "sequence": sequence,
            "canonical_id": clean_id,
        }

    @classmethod
    def validate_club_id_format(cls, club_id: str, expected_prefix: str = DEFAULT_PREFIX) -> bool:
        """Returns True if club_id conforms to the expected format and prefix, False otherwise."""
        try:
            cls.parse_club_id(club_id, expected_prefix)
            return True
        except InvalidClubIdError:
            return False

    @classmethod
    def allocate_next_club_id(cls, year: int | None = None, prefix: str = DEFAULT_PREFIX) -> str:
        """
        Atomically allocates the next sequential Club ID for the given full year (e.g. 2025)
        using a row-locked database counter (select_for_update).
        
        Example:
          - First call in 2025 -> '25SCC001'
          - Next call in 2025 -> '25SCC002'
          - If max seen sequence was 277 -> returns '25SCC278'
        """
        target_year = year or timezone.now().year
        clean_prefix = prefix.strip().upper()
        two_digit_year = target_year % 100

        with transaction.atomic():
            seq_obj, created = ClubIDSequence.objects.select_for_update().get_or_create(
                prefix=clean_prefix,
                year=target_year,
                defaults={"next_sequence": 1}
            )

            current_seq = seq_obj.next_sequence
            # Increment and save
            seq_obj.next_sequence = current_seq + 1
            seq_obj.save(update_fields=["next_sequence", "updated_at"])

        # Format sequence zero-padded to at least 3 digits (e.g. 1 -> '001', 278 -> '278', 1000 -> '1000')
        seq_str = f"{current_seq:03d}" if current_seq < 1000 else str(current_seq)
        return f"{two_digit_year:02d}{clean_prefix}{seq_str}"

    @classmethod
    def sync_sequence_watermark(cls, year: int, max_seen_sequence: int, prefix: str = DEFAULT_PREFIX) -> int:
        """
        Ensures the sequence counter for the given year is at least `max_seen_sequence + 1`.
        Used when importing legacy backup datasets (e.g. importing '25SCC277' fast-forwards next_sequence to 278)
        so that subsequent automatic allocations will never collide with legacy IDs.
        """
        target_year = year if year > 100 else cls.get_full_year_from_two_digit(year)
        clean_prefix = prefix.strip().upper()
        required_next = max_seen_sequence + 1

        with transaction.atomic():
            seq_obj, _ = ClubIDSequence.objects.select_for_update().get_or_create(
                prefix=clean_prefix,
                year=target_year,
                defaults={"next_sequence": 1}
            )

            if seq_obj.next_sequence <= max_seen_sequence:
                seq_obj.next_sequence = required_next
                seq_obj.save(update_fields=["next_sequence", "updated_at"])
                return required_next
            return seq_obj.next_sequence

    @classmethod
    def preview_next_club_id(cls, year: int | None = None, prefix: str = DEFAULT_PREFIX) -> str:
        """Reads the next Club ID without incrementing the sequence counter."""
        target_year = year or timezone.now().year
        clean_prefix = prefix.strip().upper()
        two_digit_year = target_year % 100

        seq_obj = ClubIDSequence.objects.filter(prefix=clean_prefix, year=target_year).first()
        current_seq = seq_obj.next_sequence if seq_obj else 1
        seq_str = f"{current_seq:03d}" if current_seq < 1000 else str(current_seq)
        return f"{two_digit_year:02d}{clean_prefix}{seq_str}"
