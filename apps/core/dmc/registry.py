"""
dmc/registry.py
---------------
The single source of truth for all queryable DMC datasets.

To add a new dataset, add a DatasetDefinition entry to DATASET_REGISTRY
and map the adapter class. No other files need to change.
"""

from __future__ import annotations
from apps.core.dmc.contracts import DatasetDefinition, DatasetCapabilities


def _build_registry() -> dict[str, DatasetDefinition]:
    """
    Lazily resolves adapter classes to avoid circular imports at module load.
    Called once on first access.
    """
    from apps.core.dmc.adapters.users import UsersAdapter
    from apps.core.dmc.adapters.forms import FormsAllAdapter
    from apps.core.dmc.adapters.hackathons import (
        HackathonParticipantsAdapter,
        HackathonTeamsAdapter,
        HackathonSubmissionsAdapter,
    )
    from apps.core.dmc.adapters.events_codequest_careers import (
        EventRegistrationsAdapter,
        CodequestSubmissionsAdapter,
        CareerApplicationsAdapter,
        CareerJobsAdapter,
    )

    entries = [
        # -----------------------------------------------------------------------
        # Users & Members
        # -----------------------------------------------------------------------
        DatasetDefinition(
            id="users",
            label="Users & Members",
            description="All registered platform accounts with academic and profile details.",
            group="Users",
            primary_key="id",
            default_sort_field="created_at",
            default_sort_direction="desc",
            allowed_sort_fields=["id", "email", "first_name", "last_name", "role", "branch", "year", "roll_number", "is_active", "created_at"],
            capabilities=DatasetCapabilities(read=True, search=True, filter=True, sort=True, export_csv=True, export_xlsx=True, export_json=True, record_detail=True),
            health="OK",
            adapter_class=UsersAdapter,
        ),

        # -----------------------------------------------------------------------
        # Dynamic Forms
        # -----------------------------------------------------------------------
        DatasetDefinition(
            id="forms_all",
            label="All Form Submissions",
            description="Cross-form view with common identity columns across all form responses.",
            group="Forms",
            primary_key="id",
            default_sort_field="submitted_at",
            default_sort_direction="desc",
            allowed_sort_fields=["id", "submitted_at", "is_manual_entry"],
            capabilities=DatasetCapabilities(),
            health="OK",
            adapter_class=FormsAllAdapter,
        ),

        # -----------------------------------------------------------------------
        # Hackathons
        # -----------------------------------------------------------------------
        DatasetDefinition(
            id="hackathon_participants",
            label="Hackathon Participants",
            description="One row per participant (leader or member) across all hackathon teams.",
            group="Hackathons",
            primary_key="id",
            default_sort_field="created_at",
            default_sort_direction="desc",
            allowed_sort_fields=["created_at", "email"],
            capabilities=DatasetCapabilities(record_detail=False),
            health="OK",
            adapter_class=HackathonParticipantsAdapter,
        ),
        DatasetDefinition(
            id="hackathon_teams",
            label="Hackathon Teams",
            description="One row per registered hackathon team with project submission summary.",
            group="Hackathons",
            primary_key="id",
            default_sort_field="created_at",
            default_sort_direction="desc",
            allowed_sort_fields=["id", "team_name", "submission_score", "created_at"],
            capabilities=DatasetCapabilities(),
            health="OK",
            adapter_class=HackathonTeamsAdapter,
        ),
        DatasetDefinition(
            id="hackathon_submissions",
            label="Hackathon Submissions",
            description="One row per project submission with repo, demo, and score.",
            group="Hackathons",
            primary_key="id",
            default_sort_field="created_at",
            default_sort_direction="desc",
            allowed_sort_fields=["id", "score", "created_at"],
            capabilities=DatasetCapabilities(),
            health="OK",
            adapter_class=HackathonSubmissionsAdapter,
        ),

        # -----------------------------------------------------------------------
        # Events
        # -----------------------------------------------------------------------
        DatasetDefinition(
            id="event_registrations",
            label="Event Registrations",
            description="One row per event attendee registration via the linked registration form.",
            group="Events",
            primary_key="id",
            default_sort_field="submitted_at",
            default_sort_direction="desc",
            allowed_sort_fields=["id", "submitted_at", "event_date"],
            capabilities=DatasetCapabilities(),
            health="OK",
            adapter_class=EventRegistrationsAdapter,
        ),

        # -----------------------------------------------------------------------
        # CodeQuest
        # -----------------------------------------------------------------------
        DatasetDefinition(
            id="codequest_submissions",
            label="CodeQuest Submissions",
            description="One row per problem code submission with language and pass/fail status.",
            group="CodeQuest",
            primary_key="id",
            default_sort_field="created_at",
            default_sort_direction="desc",
            allowed_sort_fields=["id", "is_correct", "created_at"],
            capabilities=DatasetCapabilities(),
            health="OK",
            adapter_class=CodequestSubmissionsAdapter,
        ),

        # -----------------------------------------------------------------------
        # Careers
        # -----------------------------------------------------------------------
        DatasetDefinition(
            id="career_applications",
            label="Career Applications",
            description="One row per job/internship application (linked form response).",
            group="Careers",
            primary_key="id",
            default_sort_field="submitted_at",
            default_sort_direction="desc",
            allowed_sort_fields=["id", "submitted_at"],
            capabilities=DatasetCapabilities(),
            health="OK",
            adapter_class=CareerApplicationsAdapter,
        ),
        DatasetDefinition(
            id="career_jobs",
            label="Job Listings",
            description="One row per active job or internship posting.",
            group="Careers",
            primary_key="id",
            default_sort_field="created_at",
            default_sort_direction="desc",
            allowed_sort_fields=["id", "title", "deadline", "created_at"],
            capabilities=DatasetCapabilities(),
            health="OK",
            adapter_class=CareerJobsAdapter,
        ),
    ]

    return {d.id: d for d in entries}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_registry: dict[str, DatasetDefinition] | None = None


def get_dataset_registry() -> dict[str, DatasetDefinition]:
    global _registry
    if _registry is None:
        _registry = _build_registry()
    return _registry


def get_dataset(dataset_id: str) -> DatasetDefinition | None:
    return get_dataset_registry().get(dataset_id)
