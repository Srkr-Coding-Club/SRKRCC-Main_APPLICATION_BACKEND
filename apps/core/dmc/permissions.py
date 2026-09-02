"""
dmc/permissions.py
------------------
Scoped RBAC for the Data Management Center.

Permission evaluation follows a strict chain:
  1. Authentication
  2. Base admin/lead role check
  3. Organization / club scope check
  4. Dataset-level permission
  5. Column-level allowlist (sensitive field exclusion)
  6. Export-level authorization

Nothing reaches the adapters without passing every layer above it.
"""

from __future__ import annotations
from typing import TYPE_CHECKING

from rest_framework import permissions as drf_permissions

if TYPE_CHECKING:
    from apps.core.dmc.contracts import ColumnDefinition


# ---------------------------------------------------------------------------
# Sensitive columns that are NEVER exposed through DMC regardless of role
# ---------------------------------------------------------------------------

FORBIDDEN_COLUMN_KEYS: frozenset[str] = frozenset([
    "password",
    "password_hash",
    "last_login",        # Django internal
    "is_superuser",
    "is_staff",
    "user_permissions",
    "groups",
    "token",
    "access_token",
    "refresh_token",
    "session_key",
    "secret",
    "private_key",
])

# Datasets that require ADMIN role (not just CLUB_LEAD)
ADMIN_ONLY_DATASETS: frozenset[str] = frozenset([
    "users",
    "career_applications",
])


# ---------------------------------------------------------------------------
# DRF permission class — base gate for all DMC views
# ---------------------------------------------------------------------------

class DMCBasePermission(drf_permissions.BasePermission):
    """
    Allows access only to authenticated users with at minimum ADMIN or CLUB_LEAD role.
    Fine-grained dataset/column/export checks are performed via the helper functions below.
    """

    def has_permission(self, request, view) -> bool:
        if not (request.user and request.user.is_authenticated):
            return False
        if request.user.is_staff or request.user.is_superuser:
            return True
        user_role = getattr(request.user, "role", None)
        return user_role in ("ADMIN", "CLUB_LEAD")


# ---------------------------------------------------------------------------
# Scoped permission helpers
# ---------------------------------------------------------------------------

def can_view_dataset(user, dataset_id: str) -> bool:
    """
    Returns True if `user` is permitted to read the given dataset.

    Super Admins / Django staff: full access.
    ADMIN role: full access to all datasets.
    CLUB_LEAD role: restricted — cannot access admin-only datasets.
    """
    if not (user and user.is_authenticated):
        return False
    if user.is_staff or user.is_superuser:
        return True

    user_role = getattr(user, "role", None)
    if user_role == "ADMIN":
        return True
    if user_role == "CLUB_LEAD":
        return dataset_id not in ADMIN_ONLY_DATASETS
    return False


def can_export_dataset(user, dataset_id: str) -> bool:
    """
    Export authorization — currently mirrors dataset view access.
    Can be made stricter (e.g. require export-specific role grants) as needed.
    """
    return can_view_dataset(user, dataset_id)


def get_permitted_columns(user, columns: list[ColumnDefinition]) -> list[ColumnDefinition]:
    """
    Filters a list of ColumnDefinitions to only those the user is allowed to see.

    Rules:
      - FORBIDDEN_COLUMN_KEYS are always stripped.
      - Columns with `exportable=False` are excluded when used in export context
        (callers must pass only exportable columns if needed).
      - Columns with a specific `permission` key are checked against the user role.
    """
    user_role = getattr(user, "role", None)
    is_superuser = getattr(user, "is_superuser", False) or getattr(user, "is_staff", False)

    permitted = []
    for col in columns:
        if col.key in FORBIDDEN_COLUMN_KEYS:
            continue
        if col.permission:
            if not is_superuser and user_role not in col.permission.split(","):
                continue
        permitted.append(col)
    return permitted


def sanitize_record(user, record: dict, permitted_column_keys: set[str]) -> dict:
    """
    Strips any keys from a canonical record that are not in the permitted column set.
    Also removes FORBIDDEN_COLUMN_KEYS unconditionally.

    The returned dict is safe to serialize and expose to the client.
    """
    return {
        k: v
        for k, v in record.items()
        if k in permitted_column_keys and k not in FORBIDDEN_COLUMN_KEYS
    }
