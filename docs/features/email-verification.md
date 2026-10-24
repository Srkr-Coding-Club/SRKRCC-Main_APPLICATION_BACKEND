# Email Verification

## What It Is
Anyone can sign up with any email address. Nothing is sent at sign-up. A person clicks **Verify email** when they need to and receives a one-time link, the same way a password reset works. A verified address unlocks the private features; an account that is still unverified when its deadline passes is **blocked** from the private app until it verifies. Public pages stay open to everyone, always.

## Why It Exists
The email address is the account's identity and where every confirmation goes. Without proof of ownership, someone can sign up with another person's address (blocking the real owner), a typo can create an account nobody can recover, and form submissions or team invites can be tied to an address nobody has proved they read.

## The Rules
| | |
|---|---|
| Who | **Everyone**, including admins and club leads. Nobody is exempt, and nobody is grandfathered. |
| Email | Any address. There is no domain restriction. |
| Link | One-time, valid 24 hours, opened with a button (page load never uses it: mail scanners open links). |
| Public pages | Always open, even to a blocked person. |
| Unverified, before the deadline | Can sign in, read and edit their profile, browse, and read forms. |
| Needs a verified email | Filling in or submitting a form (including saving a draft), event and hackathon registration, teams and invites, attendance badges, certificates, CodeQuest submissions, and **all admin-panel actions** (for admins and club leads from day 1). |
| After the deadline | Blocked from every private page. Still reachable: `/auth/me`, the verification endpoints, sign out, password reset, token refresh, notifications, public pages. |
| Deadline | New accounts: sign-up + `EMAIL_VERIFY_GRACE_DAYS` (7). Existing accounts when this shipped: in daily waves (see below). Changing the address never restarts it. |

## The Switch: `EMAIL_VERIFICATION_MODE`
| Value | Effect |
|---|---|
| `off` (default) | Nothing happens. |
| `banner` | People see their status and can verify. Nothing is restricted. |
| `features` | The verified-only features above are enforced. |
| `block` | As `features`, and an account past its deadline is blocked. |

Roll out in that order. Going back is just changing the value; to give people more time while email is having trouble, use `extend_verification_deadlines`.

## How It Works
- **State**: `User.email_verified_at`, `email_verify_deadline`, `email_disputed_at`. `GET /api/auth/me/` returns `email_verification` (`mode`, `verified`, `days_left`, `blocked`, masked addresses).
- **One gate** (`apps/core/verification_gate.py`) runs after authentication on every API request and asks `gate_decision` (`apps/accounts/services/email_verification.py`). An endpoint is *public* if an anonymous visitor would be let through it (so "anyone may read, staff may write" endpoints stay readable). A view can say `verification_exempt` (needed to verify or leave), `verification_reads_need_verified` (a read-only private feature, e.g. a certificate) or `verification_writes_ok_in_grace` (profile edits until the deadline). Refusals are `403` with `{code: "EMAIL_VERIFICATION_REQUIRED", reason: "feature" | "staff" | "deadline", blocked, days_left}`.
- **Form submissions** are open to read but a write needs a signed-in, verified account (`require_verified`), so anonymous API submissions are refused once `features` is on.
- **People are matched by email only if verified**: hackathon teammate search and Club ID creation from a form's email ignore unverified accounts, because anyone can sign up with anybody's address.

## The Link Flow
1. `POST /api/auth/email/verification/request/` (signed in): sends the link to the account's address. Limits: 60 s cooldown, 5 per hour per account, 6 per day per address. A failed send is recorded (`send_status`), reported as `503 SEND_FAILED`, and does not use the hourly budget.
2. The email links to `/account/verify-email?token=…`. `GET /api/auth/email/verification/status/?token=` only *looks* (never uses the link). `POST /api/auth/email/verification/confirm/ {token}` confirms. Pressing twice, or on a second device, is harmless.
3. `POST /api/auth/email/change/ {new_email, password}`: the new address gets its own link; the address on the account changes only when it is confirmed. A verified person stays verified meanwhile.
4. A completed password reset or setup through an emailed link also verifies the address.
5. An admin can vouch for someone who cannot receive mail: `POST /api/auth/users/<id>/verify-email/` (ADMIN role, audited), the Django admin action, or `python manage.py verify_email_manually <email>`.

## "This wasn't me"
Every verification email has a second link (valid 14 days, `POST /api/auth/email/verification/dispute/`). It is for someone whose address was used to sign up without them:
- A sign-up with **no history** is removed and the address is free again. One **with history** (it answered a form, joined a team...; only signing in does not count, because the address owner disowned it) is switched off for an admin to review.
- Disputing an **email change** only cancels that change.
- Verification mail to the address stops for 30 days, so it cannot be used to flood their inbox.

## Existing Members (the rollout)
Nobody is grandfathered. The migration gives every existing unverified account a deadline in **daily waves** (`EMAIL_VERIFY_EXISTING_START_DAYS` after the rollout, then `EMAIL_VERIFY_EXISTING_BATCH` accounts per day, most recently active first), so the whole membership does not ask the mail provider for mail on the same day. Re-plan with `schedule_verification_deadlines` just before announcing the block.

## Operations
| Command | Use |
|---|---|
| `report_unverifiable_emails [--staff-only]` | Lists accounts whose address cannot receive mail (reserved or malformed). Run it **before** `block`, and fix admins and club leads first. |
| `verify_email_manually <email>` | Break-glass: verify one account (audited). Works even if every admin is blocked. |
| `extend_verification_deadlines --days N [--only-blocked]` | More time for everyone, e.g. while email is down. |
| `schedule_verification_deadlines [--start-days --batch --only-unscheduled]` | Re-plan deadlines in daily waves. |
| `purge_unverified_accounts [--days 30] [--dry-run]` | Remove never-verified, never-used accounts 30 days after their deadline (frees addresses held by squatters). Staff and any account with history are kept. |

The Django admin site (`/admin/`) is a separate login that this gate does not touch, so it is the last resort.

## Settings
`EMAIL_VERIFICATION_MODE`, `EMAIL_VERIFY_GRACE_DAYS` (7), `EMAIL_VERIFY_LINK_HOURS` (24), `EMAIL_VERIFY_DISPUTE_DAYS` (14), `EMAIL_VERIFY_SUPPRESS_DAYS` (30), `EMAIL_VERIFY_EXISTING_START_DAYS` (7), `EMAIL_VERIFY_EXISTING_BATCH` (100). Rate scope `email_verify` (30/hour per account; `THROTTLE_EMAIL_VERIFY`).

## Sending Mail on Gmail SMTP
- Gmail allows about 500 recipients a day on a free account and about 2000 on Google Workspace. Mail is only sent when someone clicks, which keeps the load low, and the staggered deadlines spread the rollout. Keep bulk campaigns off the account that sends system mail.
- Gmail rewrites the From address to the real mailbox unless a "Send mail as" alias is verified. Use the club mailbox as `DEFAULT_FROM_EMAIL`.
- A failed send never fails a request; the person is told and can retry. If Gmail becomes a problem, `EMAIL_PROVIDER=resend` is a configuration change.

## Known Limits
- Any address is allowed, so a throwaway inbox can verify. This proves ownership of an address, not identity.
- Gmail dot and `+tag` variants of one mailbox count as different addresses.
- An unverified account is not found by hackathon teammate search, and a team leader cannot email-invite an address that already has an account; the owner must verify (or use "Forgot / Set Up?") first.
