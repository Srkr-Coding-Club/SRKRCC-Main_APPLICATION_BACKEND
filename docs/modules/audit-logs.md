# Audit Logs

Every sensitive admin action is recorded as an `AuditLog` row (`apps/audit`, written through
`log_audit_event`). Admins and club leads read them in **Admin → Audit Logs**.

## Monthly view and deletion

- `GET /api/audit/months/` returns the number of entries per month (newest first, months in the
  project timezone, Asia/Kolkata) plus `can_delete` for the caller.
- `GET /api/audit/?month=YYYY-MM` limits the list to one month; it combines with `search`.
- `DELETE /api/audit/months/<YYYY-MM>/` permanently deletes that month's entries.

The Audit Logs page shows one chip per month with its count. Clicking a chip filters the table to that
month; the trash icon beside it asks for confirmation, then deletes the month.

## Rules

- **Who:** only admins (ADMIN role, staff or superuser) can delete. Club leads can read but get 403 on
  delete, and the page hides the trash icon for them (`can_delete`).
- **Whole months only:** there is no delete-everything or delete-by-range endpoint. A malformed month is a 400.
- **The deletion is audited:** a `Deleted Audit Logs` entry (month, number deleted, who) is written
  *after* the delete, so it survives even when the current month is deleted.
- Deleted entries are gone; export what you need to keep before deleting.
