# Admin: Managing Users & Roles

Read [../features/role-based-access.md](../features/role-based-access.md) first for what each role means — this page covers how an Admin actually manages them.

## Viewing Members
**Admin → Users** lists every registered account — name, email, college, join date, current role, last active.

## Assigning a Role & Affiliate Promotion
1. Open a user from **Admin → Users** (or from the Member Directory via **Admin → Modules → Members**).
2. Change their role: `NON_AFFILIATE`, `AFFILIATE`, `VOLUNTEER`, `JUDGE`, `CLUB_LEAD`, or `ADMIN`.
3. **Promoting to `AFFILIATE`**:
   - Promoting any user to `AFFILIATE` strictly requires an official Club ID (e.g. `25SCC001`).
   - **Club ID formats** (`ClubIDService.parse_club_id`, mirrored by `validateAffiliateId` in the frontend): `<2-digit year>SCC<3+ digits>` (`25SCC123`, `26SCC123`) and `<1-2 digit year>CCA<3+ digits>` (`21CCA041`, `1CCA041`). Letters are case-insensitive and stored in capitals. Only `SCC` IDs are ever allocated by the system (and move the sequence counter); `CCA` IDs are accepted as given.

### Creating and editing a member as an admin

   - **Create New User** (`POST /api/auth/register/` called by an admin) takes everything the signup form does: name, email, optional roll number and phone, **college** (`college_id` from the catalogue, or `other_college_name` typed; left out = the club's home college), branch, **year of study (1 to 4, required)**, role (`AFFILIATE`, `NON_AFFILIATE`, `VOLUNTEER`), the Club ID for an affiliate, and a password. Every error comes back under its own field and is shown under that input. The response includes the new `id`.
   - **Edit** (`PATCH /api/auth/users/{id}/`) can also change the college (`college_id`, or `other_college_name`; clearing the college without giving a name is refused). College changes are audit-logged with the college names.
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

## User directory (Users and Members pages)

`apps/accounts/user_directory.py` and `views_directory.py` back the admin **Users** and **Members** pages. The older `GET /auth/users/` list is unchanged (it still serves create, search-as-you-type lookups and other callers); the pages use these routes (ADMIN or CLUB_LEAD unless noted):

| Route | What it does |
|---|---|
| `GET /auth/users/directory/` | Paged (25 by default, at most 100) and filtered list. `scope=members` lists only people holding a Club ID. Filters: `q` (every word must match name, email, Club ID, roll number, phone, branch or referrer), `role`, `membership_status` and `email` (comma lists), `email` = `verified`, `unverified`, `overdue` (past the deadline) or `disputed`, `member=yes\|no`, `password_status`, `created_from`, `branch`, `year`, `college`, `joined_from`, `joined_to`, `ordering` (`newest`, `oldest`, `name`, `email`, `club_id`, `last_login`). An unknown value matches nothing. Each row carries `email_state` (VERIFIED, UNVERIFIED, OVERDUE, DISPUTED), `email_days_left`, `email_deadline`, `last_login` and `password_status`. |
| `GET /auth/users/directory/summary/` | Counts for the tiles over the whole set (or the members set): total, verified, unverified, overdue, disputed, members, needs password, per role and per membership status. |
| `GET /auth/users/directory/recipients/` | Id and email of everyone the current filters match (at most 5000, `truncated` says if more), used for export and email broadcasts. |
| `POST /auth/users/{id}/verification/extend/ {days}` (ADMIN) | Gives an unverified person 1-60 more days, counted from the later of now and the current deadline. A verified person gets `409 ALREADY_VERIFIED`. Audited. |
| `POST /auth/users/{id}/verification/resend/` (ADMIN) | Sends the verification link again (same limits as the person's own button). Audited. |
| `POST /auth/users/{id}/verify-email/` (ADMIN) | Existing: vouch for a person. |

"Overdue" uses the stored `email_verify_deadline`, or the sign-up date plus `EMAIL_VERIFY_GRACE_DAYS` when none is stored, exactly as the verification gate does. Tests: `apps/accounts/tests/test_user_directory.py`.
