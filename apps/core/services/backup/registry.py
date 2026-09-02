from decimal import Decimal
from typing import Any
from apps.core.services.backup.base_importer import BaseBackupImporter
from apps.core.services.backup.user_importer import UserBackupImporter
from apps.core.services.backup.event_importer import EventBackupImporter
from apps.core.services.backup.hackathon_importer import HackathonBackupImporter
from apps.core.services.backup.form_importer import FormSubmissionImporter
from apps.core.services.backup.raw_vault_importer import RawVaultImporter


class BackupImporterRegistry:
    """
    Central registry and discovery engine for all backup domain interpreters.
    """
    _importers: dict[str, BaseBackupImporter] = {}

    @classmethod
    def initialize(cls):
        if not cls._importers:
            cls.register(UserBackupImporter())
            cls.register(EventBackupImporter())
            cls.register(HackathonBackupImporter())
            cls.register(FormSubmissionImporter())
            cls.register(RawVaultImporter())

    @classmethod
    def register(cls, importer: BaseBackupImporter):
        cls._importers[importer.domain_key] = importer

    @classmethod
    def get_importer(cls, domain_key: str) -> BaseBackupImporter:
        cls.initialize()
        importer = cls._importers.get(domain_key)
        if not importer:
            # Fallback to RawVaultImporter
            return cls._importers['UNKNOWN_RAW']
        return importer

    @classmethod
    def suggest_domain(cls, headers: list[str]) -> tuple[str, Decimal, str]:
        """
        Evaluates source headers against all structured domains and determines
        the best-matching candidate.
        
        Returns: (domain_key, confidence_pct, reason)
        """
        cls.initialize()
        best_domain = 'UNKNOWN_RAW'
        best_confidence = Decimal('0.00')
        best_reason = 'No structured schema matched above 50% threshold. Recommended for Raw Vault preservation.'

        # Only evaluate structured domains (USERS, EVENTS, HACKATHONS)
        candidate_keys = ['USERS', 'EVENTS', 'HACKATHONS']

        for key in candidate_keys:
            importer = cls._importers[key]
            req_satisfied, conf, _, _ = importer.calculate_confidence(headers)

            if req_satisfied and conf >= Decimal('50.00'):
                if conf > best_confidence:
                    best_domain = key
                    best_confidence = conf
                    best_reason = f"Recognized {int((conf / 100) * len(importer.expected_fields))} of {len(importer.expected_fields)} expected canonical fields ({conf}% confidence)."

        return best_domain, best_confidence, best_reason

    @classmethod
    def get_domains_metadata(cls) -> list[dict[str, Any]]:
        """
        Exposes domain schemas, field synonyms, and required fields for the frontend.
        """
        cls.initialize()
        metadata = []
        for key, imp in cls._importers.items():
            metadata.append({
                "key": imp.domain_key,
                "display_name": imp.display_name,
                "description": imp.description,
                "required_fields": imp.required_fields,
                "expected_fields": imp.expected_fields,
                "is_schemaless": imp.domain_key == 'UNKNOWN_RAW',
            })
        return metadata
