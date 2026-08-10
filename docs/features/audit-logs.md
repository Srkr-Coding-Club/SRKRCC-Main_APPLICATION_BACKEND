# Feature: Audit Logs

## What It Is
An automatic, tamper-evident record of every important action taken in the Admin Panel — who did what, and when. Think of it as the platform's "black box recorder" for admin activity.

## Why It Exists
As more people get Admin/Club Lead access, accountability matters: if a form gets deleted, a role gets changed, or a hackathon's results get altered, the club needs to know who did it and when — both to fix mistakes and to build trust in a system multiple people can act on.

## What Gets Logged
- Feature flag changes (module enabled/disabled) — see [feature-flags.md](feature-flags.md)
- Role/permission changes — see [role-based-access.md](role-based-access.md)
- Form created/edited/published/deleted — see [dynamic-form-builder.md](dynamic-form-builder.md)
- Event/hackathon created/edited/cancelled
- Results published or amended
- Data exports (since they can include personal data) — see [data-export.md](data-export.md)
- Bulk email sends — see [email-notifications.md](email-notifications.md)
- Content moderation actions (blog approve/reject)

Each log entry records: **who** (which admin account), **what** (the action and what it changed), **when** (timestamp), and where relevant, **before/after values**.

## How Admins Use It
Admins can view a searchable/filterable audit trail in the Admin Panel — filter by user, action type, date range, or affected module. See [../admin/audit-logs-monitoring.md](../admin/audit-logs-monitoring.md).

## Example
> "The 'Hackathons' module flag was disabled by **Admin: Priya S.** on **2026-06-01 14:32** — previously enabled."

This means if something unexpectedly disappears or changes, any admin can check the log instead of guessing.

## Related Docs
- [../admin/audit-logs-monitoring.md](../admin/audit-logs-monitoring.md) — how to read/search the log
- [../architecture/backup-security.md](../architecture/backup-security.md)
