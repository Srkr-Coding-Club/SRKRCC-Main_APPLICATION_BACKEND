"""
dmc/adapters/users.py
---------------------
DMC Adapter for the Users & Members Master Directory dataset.

Dataset: "users"
Entity:  accounts.User
1 row  = 1 User account (primary_key: "id")
"""

from __future__ import annotations
from typing import Any, Generator

from django.contrib.auth import get_user_model
from django.db.models import Q

from apps.core.dmc.adapters.base import BaseDatasetAdapter
from apps.core.dmc.contracts import (
    CanonicalValue,
    ColumnDefinition,
    FilterDefinition,
    FilterOption,
    QueryRequest,
    QueryResult,
)

User = get_user_model()

# ---------------------------------------------------------------------------
# Column schema
# ---------------------------------------------------------------------------

COLUMNS: list[ColumnDefinition] = [
    ColumnDefinition(key="id",              label="ID",              type="number",   category="meta",     sortable=True,  filterable=False, visible_by_default=False, renderer="text",  source="accounts.User.id"),
    ColumnDefinition(key="name",            label="Full Name",       type="text",     category="common",   sortable=True,  filterable=False, visible_by_default=True,  renderer="text",  source="accounts.User.first_name+last_name"),
    ColumnDefinition(key="email",           label="Email",           type="email",    category="common",   sortable=True,  filterable=False, visible_by_default=True,  renderer="email", source="accounts.User.email"),
    ColumnDefinition(key="phone_number",    label="Phone",           type="text",     category="common",   sortable=False, filterable=False, visible_by_default=False, renderer="text",  source="accounts.User.phone_number"),
    ColumnDefinition(key="role",            label="Role",            type="badge",    category="common",   sortable=True,  filterable=True,  visible_by_default=True,  renderer="badge", source="accounts.User.role"),
    ColumnDefinition(key="roll_number",     label="Roll Number",     type="text",     category="academic", sortable=True,  filterable=False, visible_by_default=True,  renderer="text",  source="accounts.User.roll_number"),
    ColumnDefinition(key="branch",          label="Branch",          type="badge",    category="academic", sortable=True,  filterable=True,  visible_by_default=True,  renderer="badge", source="accounts.User.branch"),
    ColumnDefinition(key="year",            label="Year",            type="number",   category="academic", sortable=True,  filterable=True,  visible_by_default=True,  renderer="text",  source="accounts.User.year"),
    ColumnDefinition(key="github_profile",  label="GitHub",          type="url",      category="common",   sortable=False, filterable=False, visible_by_default=False, renderer="link",  source="accounts.User.github_profile"),
    ColumnDefinition(key="linkedin_profile",label="LinkedIn",        type="url",      category="common",   sortable=False, filterable=False, visible_by_default=False, renderer="link",  source="accounts.User.linkedin_profile"),
    ColumnDefinition(key="is_active",       label="Active",          type="boolean",  category="meta",     sortable=True,  filterable=True,  visible_by_default=True,  renderer="boolean", source="accounts.User.is_active"),
    ColumnDefinition(key="created_at",      label="Joined Date",     type="datetime", category="meta",     sortable=True,  filterable=False, visible_by_default=True,  renderer="date",  source="accounts.User.created_at"),
]

ALLOWED_SORT_FIELDS = {"id", "email", "first_name", "last_name", "role", "branch", "year", "roll_number", "is_active", "created_at"}

FILTERS: list[FilterDefinition] = [
    FilterDefinition(
        key="role", label="Role", type="select", operators=["eq", "neq"],
        options=[
            FilterOption("Member", "MEMBER"),
            FilterOption("Volunteer", "VOLUNTEER"),
            FilterOption("Judge", "JUDGE"),
            FilterOption("Club Lead", "CLUB_LEAD"),
            FilterOption("Admin", "ADMIN"),
        ],
    ),
    FilterDefinition(
        key="branch", label="Branch", type="select", operators=["eq", "neq"],
        options=[
            FilterOption("CSE", "CSE"), FilterOption("IT", "IT"), FilterOption("ECE", "ECE"),
            FilterOption("EEE", "EEE"), FilterOption("MECH", "MECH"), FilterOption("CIVIL", "CIVIL"),
        ],
    ),
    FilterDefinition(key="year",      label="Year",   type="select", operators=["eq", "neq"],
        options=[FilterOption("1st Year", "1"), FilterOption("2nd Year", "2"), FilterOption("3rd Year", "3"), FilterOption("4th Year", "4")],
    ),
    FilterDefinition(key="is_active", label="Status", type="boolean", operators=["eq"]),
]


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class UsersAdapter(BaseDatasetAdapter):

    def get_schema(self, user: Any) -> tuple[list[ColumnDefinition], list[FilterDefinition]]:
        return COLUMNS, FILTERS

    def query(self, query_req: QueryRequest, user: Any) -> QueryResult:
        qs = User.objects.all()

        # Search: name, email, roll_number
        if query_req.search:
            q = query_req.search.strip()
            qs = qs.filter(
                Q(first_name__icontains=q) |
                Q(last_name__icontains=q) |
                Q(email__icontains=q) |
                Q(roll_number__icontains=q)
            )

        # Filters
        for f in query_req.filters:
            if f.field == "role" and f.operator == "eq":
                qs = qs.filter(role=f.value)
            elif f.field == "role" and f.operator == "neq":
                qs = qs.exclude(role=f.value)
            elif f.field == "branch" and f.operator == "eq":
                qs = qs.filter(branch=f.value)
            elif f.field == "branch" and f.operator == "neq":
                qs = qs.exclude(branch=f.value)
            elif f.field == "year" and f.operator == "eq":
                qs = qs.filter(year=f.value)
            elif f.field == "is_active" and f.operator == "eq":
                qs = qs.filter(is_active=bool(f.value))

        # Sort (allowlisted)
        sort_field = query_req.sort.field if query_req.sort.field in ALLOWED_SORT_FIELDS else "created_at"
        sort_prefix = "-" if query_req.sort.direction == "desc" else ""
        qs = qs.order_by(f"{sort_prefix}{sort_field}")

        # Pagination
        total = qs.count()
        offset = (query_req.page - 1) * query_req.page_size
        page_qs = qs[offset: offset + query_req.page_size]

        records = [self._normalize(u) for u in page_qs]
        return QueryResult(
            records=records,
            total=total,
            page=query_req.page,
            page_size=query_req.page_size,
            dataset_id="users",
        )

    def get_record(self, record_id: str, user: Any) -> dict[str, CanonicalValue] | None:
        try:
            u = User.objects.get(pk=record_id)
        except (User.DoesNotExist, ValueError):
            return None
        return self._normalize(u)

    def stream_records(
        self,
        query_req: QueryRequest,
        user: Any,
        selected_ids: list[str] | None = None,
    ) -> Generator[dict[str, CanonicalValue], None, None]:
        qs = User.objects.all()

        if selected_ids is not None:
            qs = qs.filter(pk__in=selected_ids)
        else:
            if query_req.search:
                q = query_req.search.strip()
                qs = qs.filter(
                    Q(first_name__icontains=q) | Q(last_name__icontains=q) |
                    Q(email__icontains=q) | Q(roll_number__icontains=q)
                )
            for f in query_req.filters:
                if f.field == "role" and f.operator == "eq":
                    qs = qs.filter(role=f.value)
                elif f.field == "branch" and f.operator == "eq":
                    qs = qs.filter(branch=f.value)
                elif f.field == "year" and f.operator == "eq":
                    qs = qs.filter(year=f.value)
                elif f.field == "is_active" and f.operator == "eq":
                    qs = qs.filter(is_active=bool(f.value))

        for u in qs.iterator(chunk_size=500):
            yield self._normalize(u)

    # ---------------------------------------------------------------------------

    def _normalize(self, u: Any) -> dict[str, CanonicalValue]:
        name = f"{u.first_name} {u.last_name}".strip()
        return {
            "id":               self._val(str(u.pk),          "number",   "accounts.User.id"),
            "name":             self._val(name or None,        "text",     "accounts.User.first_name+last_name"),
            "email":            self._val(u.email,             "email",    "accounts.User.email"),
            "phone_number":     self._val(u.phone_number,      "text",     "accounts.User.phone_number"),
            "role":             self._val(u.role,              "badge",    "accounts.User.role"),
            "roll_number":      self._val(u.roll_number,       "text",     "accounts.User.roll_number"),
            "branch":           self._val(u.branch,            "badge",    "accounts.User.branch"),
            "year":             self._val(u.year,              "number",   "accounts.User.year"),
            "github_profile":   self._val(u.github_profile,    "url",      "accounts.User.github_profile"),
            "linkedin_profile": self._val(u.linkedin_profile,  "url",      "accounts.User.linkedin_profile"),
            "is_active":        self._val(u.is_active,         "boolean",  "accounts.User.is_active"),
            "created_at":       self._val(u.created_at.isoformat() if u.created_at else None, "datetime", "accounts.User.created_at"),
        }
