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
    ColumnDefinition(key="id",                label="ID",                type="number",   category="meta",     sortable=True,  filterable=False, visible_by_default=False, renderer="text",    source="accounts.User.id"),
    ColumnDefinition(key="club_id",           label="Club ID",           type="badge",    category="common",   sortable=True,  filterable=False, visible_by_default=True,  renderer="badge",   source="accounts.User.club_id"),
    ColumnDefinition(key="name",              label="Full Name",         type="text",     category="common",   sortable=True,  filterable=False, visible_by_default=True,  renderer="text",    source="accounts.User.first_name+last_name"),
    ColumnDefinition(key="email",             label="Email",             type="email",    category="common",   sortable=True,  filterable=False, visible_by_default=True,  renderer="email",   source="accounts.User.email"),
    ColumnDefinition(key="phone_number",      label="Phone",             type="text",     category="common",   sortable=False, filterable=False, visible_by_default=True,  renderer="text",    source="accounts.User.phone_number"),
    ColumnDefinition(key="membership_status", label="Status",            type="badge",    category="common",   sortable=True,  filterable=True,  visible_by_default=True,  renderer="badge",   source="accounts.User.membership_status"),
    ColumnDefinition(key="role",              label="Role",              type="badge",    category="common",   sortable=True,  filterable=True,  visible_by_default=True,  renderer="badge",   source="accounts.User.role"),
    ColumnDefinition(key="branch",            label="Branch",            type="badge",    category="academic", sortable=True,  filterable=True,  visible_by_default=True,  renderer="badge",   source="accounts.User.branch"),
    ColumnDefinition(key="roll_number",       label="Roll Number",       type="text",     category="academic", sortable=True,  filterable=False, visible_by_default=False, renderer="text",    source="accounts.User.roll_number"),
    ColumnDefinition(key="year",              label="Year",              type="number",   category="academic", sortable=True,  filterable=True,  visible_by_default=False, renderer="text",    source="accounts.User.year"),
    ColumnDefinition(key="referred_by",       label="Referred By",       type="text",     category="common",   sortable=False, filterable=False, visible_by_default=True,  renderer="text",    source="accounts.User.referred_by_user+raw"),
    ColumnDefinition(key="registered_at",     label="Registration Date", type="datetime", category="meta",     sortable=True,  filterable=False, visible_by_default=True,  renderer="date",    source="accounts.User.registered_at"),
    ColumnDefinition(key="created_from",      label="Source Origin",     type="badge",    category="meta",     sortable=True,  filterable=True,  visible_by_default=False, renderer="badge",   source="accounts.User.created_from"),
    ColumnDefinition(key="created_at",        label="System Joined",     type="datetime", category="meta",     sortable=True,  filterable=False, visible_by_default=False, renderer="date",    source="accounts.User.created_at"),
]

ALLOWED_SORT_FIELDS = {"id", "club_id", "email", "first_name", "last_name", "role", "membership_status", "branch", "year", "roll_number", "registered_at", "created_at"}

FILTERS: list[FilterDefinition] = [
    FilterDefinition(
        key="membership_status", label="Membership Status", type="select", operators=["eq", "neq"],
        options=[
            FilterOption("Active", "ACTIVE"),
            FilterOption("Inactive", "INACTIVE"),
            FilterOption("Alumni", "ALUMNI"),
            FilterOption("Suspended", "SUSPENDED"),
        ],
    ),
    FilterDefinition(
        key="role", label="Role", type="select", operators=["eq", "neq"],
        options=[
            FilterOption("Affiliate", "AFFILIATE"),
            FilterOption("Non-Affiliate", "NON_AFFILIATE"),
            FilterOption("Volunteer", "VOLUNTEER"),
            FilterOption("Judge", "JUDGE"),
            FilterOption("Club Lead", "CLUB_LEAD"),
            FilterOption("Admin", "ADMIN"),
        ],
    ),
    FilterDefinition(
        key="branch", label="Branch", type="select", operators=["eq", "neq"],
        options=[
            FilterOption("CSE", "CSE"), FilterOption("IT", "IT"), FilterOption("AIDS", "AIDS"),
            FilterOption("AIML", "AIML"), FilterOption("ECE", "ECE"), FilterOption("EEE", "EEE"),
            FilterOption("MECH", "MECH"), FilterOption("CIVIL", "CIVIL"), FilterOption("CSBS", "CSBS"),
        ],
    ),
    FilterDefinition(
        key="created_from", label="Source Origin", type="select", operators=["eq", "neq"],
        options=[
            FilterOption("Legacy Backup Import", "LEGACY_IMPORT"),
            FilterOption("Self Registration", "SELF_REGISTRATION"),
            FilterOption("Admin Created", "ADMIN"),
            FilterOption("Form Submission", "FORM"),
        ],
    ),
]


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class UsersAdapter(BaseDatasetAdapter):

    def get_schema(self, user: Any) -> tuple[list[ColumnDefinition], list[FilterDefinition]]:
        return COLUMNS, FILTERS

    def query(self, query_req: QueryRequest, user: Any) -> QueryResult:
        qs = User.objects.all().select_related('referred_by_user')

        # Search: name, email, roll_number, club_id, phone_number
        if query_req.search:
            q = query_req.search.strip()
            qs = qs.filter(
                Q(first_name__icontains=q) |
                Q(last_name__icontains=q) |
                Q(email__icontains=q) |
                Q(club_id__icontains=q) |
                Q(phone_number__icontains=q) |
                Q(roll_number__icontains=q)
            )

        # Filters
        for f in query_req.filters:
            if f.field == "role" and f.operator == "eq":
                qs = qs.filter(role=f.value)
            elif f.field == "role" and f.operator == "neq":
                qs = qs.exclude(role=f.value)
            elif f.field == "membership_status" and f.operator == "eq":
                qs = qs.filter(membership_status=f.value)
            elif f.field == "branch" and f.operator == "eq":
                qs = qs.filter(branch=f.value)
            elif f.field == "branch" and f.operator == "neq":
                qs = qs.exclude(branch=f.value)
            elif f.field == "created_from" and f.operator == "eq":
                qs = qs.filter(created_from=f.value)

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
            u = User.objects.select_related('referred_by_user').get(pk=record_id)
        except (User.DoesNotExist, ValueError):
            return None
        return self._normalize(u)

    def stream_records(
        self,
        query_req: QueryRequest,
        user: Any,
        selected_ids: list[str] | None = None,
    ) -> Generator[dict[str, CanonicalValue], None, None]:
        qs = User.objects.all().select_related('referred_by_user')

        if selected_ids is not None:
            qs = qs.filter(pk__in=selected_ids)
        else:
            if query_req.search:
                q = query_req.search.strip()
                qs = qs.filter(
                    Q(first_name__icontains=q) | Q(last_name__icontains=q) |
                    Q(email__icontains=q) | Q(club_id__icontains=q) |
                    Q(phone_number__icontains=q) | Q(roll_number__icontains=q)
                )
            for f in query_req.filters:
                if f.field == "role" and f.operator == "eq":
                    qs = qs.filter(role=f.value)
                elif f.field == "membership_status" and f.operator == "eq":
                    qs = qs.filter(membership_status=f.value)
                elif f.field == "branch" and f.operator == "eq":
                    qs = qs.filter(branch=f.value)
                elif f.field == "created_from" and f.operator == "eq":
                    qs = qs.filter(created_from=f.value)

        for u in qs.iterator(chunk_size=500):
            yield self._normalize(u)

    # ---------------------------------------------------------------------------

    def _normalize(self, u: Any) -> dict[str, CanonicalValue]:
        name = f"{u.first_name} {u.last_name}".strip()
        ref_display = ""
        if u.referred_by_user:
            ref_display = f"{u.referred_by_user.first_name} {u.referred_by_user.last_name}".strip() or u.referred_by_user.email
        elif u.referred_by_raw:
            ref_display = u.referred_by_raw

        reg_date_str = u.registered_at.isoformat() if u.registered_at else (u.created_at.isoformat() if u.created_at else None)

        return {
            "id":                self._val(str(u.pk),          "number",   "accounts.User.id"),
            "club_id":           self._val(u.club_id,          "badge",    "accounts.User.club_id"),
            "name":              self._val(name or None,       "text",     "accounts.User.first_name+last_name"),
            "email":             self._val(u.email,            "email",    "accounts.User.email"),
            "phone_number":      self._val(u.phone_number,     "text",     "accounts.User.phone_number"),
            "membership_status": self._val(u.membership_status,"badge",    "accounts.User.membership_status"),
            "role":              self._val(u.role,             "badge",    "accounts.User.role"),
            "branch":            self._val(u.branch,           "badge",    "accounts.User.branch"),
            "roll_number":       self._val(u.roll_number,      "text",     "accounts.User.roll_number"),
            "year":              self._val(u.year,             "number",   "accounts.User.year"),
            "referred_by":       self._val(ref_display or None,"text",     "accounts.User.referred_by"),
            "registered_at":     self._val(reg_date_str,       "datetime", "accounts.User.registered_at"),
            "created_from":      self._val(u.created_from,     "badge",    "accounts.User.created_from"),
            "created_at":        self._val(u.created_at.isoformat() if u.created_at else None, "datetime", "accounts.User.created_at"),
        }
