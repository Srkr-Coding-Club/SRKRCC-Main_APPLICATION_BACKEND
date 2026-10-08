# Admin: Managing Users & Roles

Read [../features/role-based-access.md](../features/role-based-access.md) first for what each role means — this page covers how an Admin actually manages them.

## Viewing Members
**Admin → Users** lists every registered account — name, email, college, join date, current role, last active.

## Assigning a Role & Affiliate Promotion
1. Open a user from **Admin → Users** (or from the Member Directory via **Admin → Modules → Members**).
2. Change their role: `NON_AFFILIATE`, `AFFILIATE`, `VOLUNTEER`, `JUDGE`, `CLUB_LEAD`, or `ADMIN`.
3. **Promoting to `AFFILIATE`**:
   - Promoting any user to `AFFILIATE` strictly requires an official Club ID (e.g. `25SCC001`).
   - The admin can choose between **Manual Entry** (typing a verified Club ID with live format validation) or **Auto-Generation** (suggesting the next sequential ID via `GET /api/auth/club-ids/next/`).
   - The backend validates uniqueness and canonical format (`\d{2}[A-Z]{3}\d{3}`) before granting the promotion.
4. Save. The change takes effect immediately and is recorded in the [Audit Log](audit-logs-monitoring.md).

## Editing User Profile Details
Admins can directly edit a user's profile details from both **Admin → Users** and **Admin → Modules → Members**:
- First Name, Last Name, Phone Number
- College Roll Number (10 alphanumeric chars), Branch, Year of Study
- Club ID (with format validation and "Suggest Next ID" auto-fill)
- Platform Role & Membership Status (`ACTIVE`, `INACTIVE`, `SUSPENDED`, `ALUMNI`, `PENDING`)
- GitHub and LinkedIn profile links
Updates execute via `PATCH /api/auth/users/<id>/` and audit log any role or security changes.

## Member Directory Filtering
In **Admin → Modules → Members**:
- The roster displays official **Affiliates (Club Members)** by default, preventing unverified or pending non-affiliates from cluttering the official club directory.
- Admins can filter by role (`AFFILIATE`, `ALL`, `NON_AFFILIATE`, `VOLUNTEER`, `JUDGE`, `CLUB_LEAD`, `ADMIN`), membership status, and branch.

## Assigning a Scoped Role (e.g. Volunteer for one event, Judge for one hackathon)
Some roles aren't platform-wide — a Volunteer might only be able to view one specific event's attendee list, and a Judge only sees one specific hackathon's submissions.
1. Go to that event/hackathon's admin page.
2. Find **Team/Access** or **Volunteers/Judges** section.
3. Add the member and their scoped role there — this doesn't change their platform-wide role.

## Removing Access
Set a user's role back to **Member** / **Non-Affiliate** (or remove them from a scoped assignment) — this immediately revokes the extra permissions; it does not delete their account or their past data (registrations, submissions, posts remain intact).

## Good Practice
- Only grant **Admin** to people who genuinely need full platform control (feature flags, role management, audit logs) — most day-to-day work only needs **Club Lead**.
- Review role assignments periodically, especially after a change in club leadership each year — outgoing leads should be moved back to Member.
- Every role change is logged — see [audit-logs-monitoring.md](audit-logs-monitoring.md) if something looks off.

## Related Docs
- [../features/role-based-access.md](../features/role-based-access.md)
- [audit-logs-monitoring.md](audit-logs-monitoring.md)
