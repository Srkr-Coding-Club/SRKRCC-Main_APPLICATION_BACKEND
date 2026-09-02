"""
dmc/adapters/base.py
--------------------
Abstract base class that every DMC Dataset Adapter must implement.

Responsibilities:
  - get_schema()       → Column definitions, filter specs, field provenance
  - query()            → Filtered, sorted, paginated canonical records
  - get_record()       → Single canonical record for the detail drawer
  - stream_records()   → Generator of records for ExportService consumption

Adapters MUST NOT handle CSV/XLSX/JSON formatting — that belongs in ExportService.
Adapters MUST NOT store/send sensitive columns — permission filtering is applied
  by the calling view layer before returning anything to the client.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Generator, Any

from apps.core.dmc.contracts import (
    CanonicalValue,
    ColumnDefinition,
    FilterDefinition,
    QueryRequest,
    QueryResult,
)


class BaseDatasetAdapter(ABC):
    """
    Contract that every DMC adapter must satisfy.

    Instantiated per-request (or per-view call) to keep state clean.
    """

    @abstractmethod
    def get_schema(self, user: Any) -> tuple[list[ColumnDefinition], list[FilterDefinition]]:
        """
        Returns:
            (columns, filters) — column and filter definitions for this dataset.

        Implementations must respect the caller's `user` for any column-level
        permission checks that are dataset-specific (e.g. hiding an internal note
        column from non-superusers). Broad FORBIDDEN_COLUMN_KEYS filtering is
        always applied by the permission layer on top of this.
        """
        raise NotImplementedError

    @abstractmethod
    def query(self, query_req: QueryRequest, user: Any) -> QueryResult:
        """
        Execute a server-side paginated query and return canonical records.

        Query execution order (MANDATORY to prevent N+1 and memory issues):
          1. Build base queryset.
          2. Apply search (DB LIKE / icontains).
          3. Apply filters (DB WHERE clauses).
          4. Validate and apply sort (against allowed_sort_fields allowlist).
          5. DB-level COUNT for total.
          6. DB-level LIMIT/OFFSET pagination on primary IDs.
          7. Prefetch relations ONLY for the paginated slice.
          8. Normalize to canonical records.
        """
        raise NotImplementedError

    @abstractmethod
    def get_record(self, record_id: str, user: Any) -> dict[str, CanonicalValue] | None:
        """
        Return a single sanitized canonical record for the detail drawer.

        Returns None if the record does not exist or is not accessible.
        The returned dict must never include forbidden/sensitive fields.
        """
        raise NotImplementedError

    @abstractmethod
    def stream_records(
        self,
        query_req: QueryRequest,
        user: Any,
        selected_ids: list[str] | None = None,
    ) -> Generator[dict[str, CanonicalValue], None, None]:
        """
        Yields canonical records for consumption by ExportService.

        When selected_ids is provided, only those specific records are yielded
        (after verifying they belong to the user's scope).
        When None, yields all records matching the query filters.

        Must use DB-level iteration (iterator(), chunked fetching) to avoid
        loading the entire dataset into memory.
        """
        raise NotImplementedError

    # ---------------------------------------------------------------------------
    # Utility helpers available to all adapters
    # ---------------------------------------------------------------------------

    @staticmethod
    def _val(
        value: Any,
        col_type: str = "text",
        source: str = "",
        *,
        not_applicable: bool = False,
    ) -> CanonicalValue:
        """
        Build a CanonicalValue envelope from a raw Python value.

        Usage:
            self._val("Rajesh", "text", "accounts.User.first_name")
            self._val(None, "text", source="", not_applicable=True)
        """
        if not_applicable:
            return CanonicalValue(
                state="not_applicable",
                value=None,
                display_value="—",
                type=col_type,
                source=None,
            )
        if value is None or value == "":
            return CanonicalValue(
                state="empty",
                value=value,
                display_value="(empty)",
                type=col_type,
                source=source or None,
            )
        return CanonicalValue(
            state="value",
            value=value,
            display_value=str(value),
            type=col_type,
            source=source or None,
        )

    @staticmethod
    def _unknown(col_type: str = "text", source: str = "") -> CanonicalValue:
        """Convenience factory for the 'unknown' state."""
        return CanonicalValue(
            state="unknown",
            value=None,
            display_value="[Unavailable]",
            type=col_type,
            source=source or None,
        )
