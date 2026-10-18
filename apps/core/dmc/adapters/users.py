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
from apps.accounts.models import MembershipStatus, UserRole
from apps.core.dmc.filter_engine import Spec, apply_queryset, choices, specs_by_key, to_definitions
from apps.core.dmc.contracts import (
    CanonicalValue,
    ColumnDefinition,
    FilterClause,
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

BRANCH_OPTIONS = [FilterOption(b, b) for b in ("CSE", "IT", "AIDS", "AIML", "ECE", "EEE", "MECH", "CIVIL", "CSBS")]

SPECS = specs_by_key(
    Spec("name", "Full Name", "text", ("first_name", "last_name"), tokenize=True),
    Spec("email", "Email", "text", ("email",)),
    Spec("club_id", "Club ID", "text", ("club_id",)),
    Spec("phone_number", "Phone", "text", ("phone_number",)),
    Spec("roll_number", "Roll Number", "text", ("roll_number",)),
    Spec("membership_status", "Membership Status", "select", ("membership_status",), options=lambda: choices(MembershipStatus)),
    Spec("role", "Role", "select", ("role",), options=lambda: choices(UserRole)),
    Spec("branch", "Branch", "select", ("branch",), options=BRANCH_OPTIONS),
    Spec("year", "Year", "number", ("year",)),
    Spec("created_from", "Source Origin", "select", ("created_from",), options=[
        FilterOption("Legacy Backup Import", "LEGACY_IMPORT"),
        FilterOption("Self Registration", "SELF_REGISTRATION"),
        FilterOption("Admin Created", "ADMIN"),
        FilterOption("Form Submission", "FORM"),
    ]),
    Spec("registered_at", "Registration Date", "date", ("registered_at",)),
    Spec("created_at", "System Joined", "date", ("created_at",)),
)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class UsersAdapter(BaseDatasetAdapter):

    def get_schema(self, user: Any) -> tuple[list[ColumnDefinition], list[FilterDefinition]]:
        return COLUMNS, to_definitions(SPECS)

    def filter_specs(self):
        return SPECS

    def _apply_filters(self, qs, filters: list[FilterClause]):
        return apply_queryset(qs, filters, SPECS)

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
        qs = self._apply_filters(qs, query_req.filters)

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
            qs = self._apply_filters(qs, query_req.filters)

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
