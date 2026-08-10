# Admin: Managing Users & Roles

Read [../features/role-based-access.md](../features/role-based-access.md) first for what each role means — this page covers how an Admin actually manages them.

## Viewing Members
**Admin → Users** lists every registered account — name, email, college, join date, current role, last active.

## Assigning a Role
1. Open a user from **Admin → Users**.
2. Change their role: Member, Contributor, Volunteer, Club Lead, Admin, or a context-specific role like Judge (assigned per hackathon, not platform-wide).
3. Save. The change takes effect immediately and is recorded in the [Audit Log](audit-logs-monitoring.md).

## Assigning a Scoped Role (e.g. Volunteer for one event, Judge for one hackathon)
Some roles aren't platform-wide — a Volunteer might only be able to view one specific event's attendee list, and a Judge only sees one specific hackathon's submissions.
1. Go to that event/hackathon's admin page.
2. Find **Team/Access** or **Volunteers/Judges** section.
3. Add the member and their scoped role there — this doesn't change their platform-wide role.

## Removing Access
Set a user's role back to **Member** (or remove them from a scoped assignment) — this immediately revokes the extra permissions; it does not delete their account or their past data (registrations, submissions, posts remain intact).

## Good Practice
- Only grant **Admin** to people who genuinely need full platform control (feature flags, role management, audit logs) — most day-to-day work only needs **Club Lead**.
- Review role assignments periodically, especially after a change in club leadership each year — outgoing leads should be moved back to Member.
- Every role change is logged — see [audit-logs-monitoring.md](audit-logs-monitoring.md) if something looks off.

## Related Docs
- [../features/role-based-access.md](../features/role-based-access.md)
- [audit-logs-monitoring.md](audit-logs-monitoring.md)
