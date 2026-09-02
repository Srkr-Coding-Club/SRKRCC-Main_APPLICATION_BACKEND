from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any
from django.contrib.auth import get_user_model
from apps.core.models import BackupJob, ImportAttempt

User = get_user_model()


class BaseBackupImporter(ABC):
    """
    Abstract contract for all domain-specific backup interpreters (USERS, EVENTS, etc.).
    """
    domain_key: str
    display_name: str
    description: str
    required_fields: list[str]
    expected_fields: dict[str, list[str]]  # Canonical key -> list of synonyms/aliases

    @abstractmethod
    def calculate_confidence(self, headers: list[str]) -> tuple[bool, Decimal, dict[str, str], list[str]]:
        """
        Evaluates source headers against domain schema.
        
        Exact Formula:
          Confidence = (Unique Recognized Canonical Fields Matched / Total Expected Canonical Fields) * 100
        
        Returns:
          (required_satisfied, confidence_pct, suggested_mapping, unmapped_headers)
        """
        pass

    @abstractmethod
    def build_normalized_mapping(self, custom_mapping: dict[str, str] | None, headers: list[str]) -> dict[str, str]:
        """
        Locks and normalizes user-provided or auto-detected column mappings.
        """
        pass

    @abstractmethod
    def preview(
        self,
        backup_job: BackupJob,
        mapping: dict[str, str],
        context: dict[str, Any] | None = None,
    ) -> ImportAttempt:
        """
        Row-level inspection: Reads raw rows, checks duplicates/collisions,
        computes action stats, and saves ImportRow provenance records.
        """
        pass

    @abstractmethod
    def commit(
        self,
        import_attempt: ImportAttempt,
        user: Any,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Executes atomic database mutations under transaction savepoints.
        """
        pass
