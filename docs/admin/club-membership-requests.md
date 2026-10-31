# Club membership requests

A person asks to become a club member; an Admin approves (which gives them the next Club ID and the affiliate role) or declines with a reason. Code: `apps/accounts/services/club_membership.py`, `views_membership.py`; model `ClubMembershipRequest` (migration `0015_club_membership_requests`). Tests: `apps/accounts/tests/test_club_membership.py`.

## Who may ask

`blockers(user)` lists why somebody cannot (empty = they can):

| Code | When |
|---|---|
| `ALREADY_MEMBER` | They already hold a Club ID |
| `SUSPENDED` | Membership status is suspended |
| `EMAIL_NOT_VERIFIED` | Email not verified (not required when `EMAIL_VERIFICATION_MODE=off`) |
| `PROFILE_INCOMPLETE` | First name, last name, roll number, branch, year, phone or college missing (the response lists which) |
| `PENDING` | A request is already waiting (one at a time, enforced by a partial unique constraint) |
| `COOLDOWN` | A request was declined less than 7 days ago (withdrawing has no wait) |

An id **reserved for the person's email** skips the queue: asking attaches it at once (`auto_approved: true`) and the request is recorded as approved.

## Deciding

- **Approve** (`allocate_for`): the next free number of the current year (reserved, held and retired numbers are skipped), recorded in the Club ID history with the reason "Club membership approved"; `NON_AFFILIATE` becomes `AFFILIATE` (staff roles are kept), status becomes `ACTIVE`, `registered_at` is set if empty; the person gets an in-app notification, an email and the `account.club_member` celebration. If the person was given an id some other way in the meantime, approval is refused (`409 ALREADY_MEMBER`) and the request stays open.
- **Decline**: a reason is required and is shown to the person (notification, email, profile card), with the date they may ask again.
- A request is decided once (`409 NOT_PENDING` with `status_now`). Everything is audited.
- Every new request notifies all Admins and Club Leads.

## API (`/api/auth/club-membership/`)

| Route | Who | Notes |
|---|---|---|
| `GET me/` | signed in | `club_id`, `can_request`, `blockers`, the latest request |
| `POST request/` `{message?}` | signed in | `201` with `auto_approved`; refusals use the blocker codes |
| `POST cancel/` | signed in | Withdraw the waiting request |
| `GET requests/?status=&q=&page=` | Admin, Club Lead | 25 per page, with `counts` by status; waiting ones oldest first |
| `POST requests/{id}/approve/` | Admin | |
| `POST requests/{id}/reject/` `{reason}` | Admin | |
| `POST requests/bulk-approve/` `{ids}` (1-100) | Admin | Each on its own; `{done, failed: [{id, error, code}]}` |
