"""
dmc/contracts.py
-----------------
Formal dataclass contracts that form the central data exchange specification
between the Dataset Registry, Dataset Adapters, Views, and ExportService.

These are plain Python dataclasses — no Django models, no serializers.
They define what every layer of the DMC must produce and accept.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Literal


# ---------------------------------------------------------------------------
# Four-State Canonical Value Envelope
# ---------------------------------------------------------------------------

ValueState = Literal["value", "not_applicable", "empty", "unknown"]

ColumnType = Literal["text", "email", "number", "date", "datetime", "url", "badge", "file", "rating", "boolean", "multiline"]
ColumnCategory = Literal["common", "academic", "hackathon", "form_questions", "career", "codequest", "meta"]
ColumnRenderer = Literal["text", "link", "badge", "file_download", "star_rating", "date", "boolean", "email"]

FilterType = Literal["select", "text", "date_range", "boolean", "number_range"]
FilterOperator = Literal["eq", "neq", "contains", "starts_with", "between", "gte", "lte"]

ExportFormat = Literal["csv", "xlsx", "json"]
ExportRowScope = Literal["selected", "all_filtered"]
ExportColumnScope = Literal["visible", "all_permitted"]


@dataclass
class CanonicalValue:
    """
    Represents a single field value within a normalized DMC record.

    States:
        value          → Valid data; rendered via type-specific formatter.
        not_applicable → Field does not exist for this dataset/entity (renders as '—').
        empty          → Field exists but was left blank by the respondent (renders as '(empty)').
        unknown        → Expected relation/data could not be resolved (renders as '[Unavailable]').
    """
    state: ValueState
    value: Any                    # Raw Python value (str, int, list, None …)
    display_value: str            # Pre-formatted string the frontend can use as fallback
    type: ColumnType = "text"
    source: str | None = None     # Field provenance e.g. "accounts.User.email"


# ---------------------------------------------------------------------------
# Column & Filter Definitions
# ---------------------------------------------------------------------------

@dataclass
class ColumnDefinition:
    """Describes a single column in a DMC dataset schema."""
    key: str                                     # e.g. "email", "problem_statement"
    label: str                                   # e.g. "Email Address"
    type: ColumnType = "text"
    category: ColumnCategory = "common"
    source: str = ""                             # e.g. "accounts.User.email"
    sortable: bool = True
    filterable: bool = False
    exportable: bool = True
    visible_by_default: bool = True
    permission: str | None = None                # Specific column-level permission key
    renderer: ColumnRenderer = "text"
    description: str = ""


@dataclass
class FilterOption:
    label: str
    value: Any


@dataclass
class FilterDefinition:
    """Describes a filterable field and how the frontend should render its controls."""
    key: str
    label: str
    type: FilterType = "text"
    operators: list[FilterOperator] = field(default_factory=lambda: ["eq"])
    options: list[FilterOption] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Dataset Capabilities
# ---------------------------------------------------------------------------

@dataclass
class DatasetCapabilities:
    read: bool = True
    search: bool = True
    filter: bool = True
    sort: bool = True
    export_csv: bool = True
    export_xlsx: bool = True
    export_json: bool = True
    record_detail: bool = True


# ---------------------------------------------------------------------------
# Dataset Definition — registered in the DATASET_REGISTRY
# ---------------------------------------------------------------------------

@dataclass
class DatasetDefinition:
    """
    Formal specification for a single queryable dataset within the DMC.

    Each dataset:
      - Has a stable `id` (never a URL slug exposed to the frontend as an identifier).
      - Maps to exactly one domain entity type (1 row = 1 entity invariant).
      - Exposes capabilities so the frontend knows what actions are allowed.
      - Has a health field the backend can update to signal adapter degradation.
    """
    id: str                                        # e.g. "users", "hackathon_teams"
    label: str                                     # Human-readable name
    description: str
    group: str                                     # Sidebar group: "Users" | "Forms" | etc.
    primary_key: str = "id"                        # Record identifier used by selection/export
    default_sort_field: str = "created_at"
    default_sort_direction: str = "desc"
    allowed_sort_fields: list[str] = field(default_factory=lambda: ["created_at"])
    capabilities: DatasetCapabilities = field(default_factory=DatasetCapabilities)
    health: Literal["OK", "DEGRADED", "UNAVAILABLE"] = "OK"
    adapter_class: type | None = None              # Set by registry after adapter import


# ---------------------------------------------------------------------------
# Query / Response contract
# ---------------------------------------------------------------------------

@dataclass
class FilterClause:
    """A single applied filter from the client query."""
    field: str
    operator: FilterOperator
    value: Any


@dataclass
class SortClause:
    field: str
    direction: Literal["asc", "desc"] = "desc"


@dataclass
class QueryRequest:
    """Structured request payload for POST /datasets/<id>/query/"""
    page: int = 1
    page_size: int = 50
    search: str = ""
    sort: SortClause = field(default_factory=lambda: SortClause(field="created_at"))
    filters: list[FilterClause] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)   # Requested visible column keys


@dataclass
class QueryResult:
    """Paginated result returned by adapter.query()"""
    records: list[dict[str, CanonicalValue]]           # list of {col_key: CanonicalValue}
    total: int
    page: int
    page_size: int
    dataset_id: str


# ---------------------------------------------------------------------------
# Export contract
# ---------------------------------------------------------------------------

@dataclass
class ExportRequest:
    """Payload for POST /datasets/<id>/export/"""
    format: ExportFormat
    row_scope: ExportRowScope = "all_filtered"
    column_scope: ExportColumnScope = "visible"
    selected_record_ids: list[str] = field(default_factory=list)
    visible_column_keys: list[str] = field(default_factory=list)
    # Query snapshot — must be captured server-side at export initiation
    query_snapshot: QueryRequest | None = None
