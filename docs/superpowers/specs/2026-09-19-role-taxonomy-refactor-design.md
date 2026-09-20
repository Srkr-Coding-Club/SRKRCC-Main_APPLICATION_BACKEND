# Role Taxonomy Refactor — AFFILIATE / NON_AFFILIATE Split

**Status:** Approved, ready for implementation planning
**Date:** 2026-09-19
**Repos affected:** SRKRCC-Main_APPLICATION_BACKEND (primary), SRKRCC-Main_APPLICATION_FRONTEND

## 1. Problem

`User.role` currently has five values: `MEMBER`, `VOLUNTEER`, `JUDGE`, `CLUB_LEAD`, `ADMIN`. `MEMBER` conflates two different things — a general club member with a permanent Club ID (an "affiliate") and someone who has just signed up with no Club ID yet — with no way to tell them apart except by separately checking whether `club_id` is set. There is no rule anywhere that an affiliate-type member actually *has* a Club ID; an admin (or a bug) can leave that inconsistent indefinitely.

## 2. Goal

Split `MEMBER` into two explicit roles — `AFFILIATE` and `NON_AFFILIATE` — and make it a hard invariant, enforced server-side on every path that can set a role, that **`AFFILIATE` requires a `club_id`**. `VOLUNTEER`, `JUDGE`, `CLUB_LEAD`, `ADMIN` are unchanged in meaning; they just join the enum alongside the new pair.

Final role set: `AFFILIATE`, `NON_AFFILIATE`, `VOLUNTEER`, `JUDGE`, `CLUB_LEAD`, `ADMIN`.

## 3. Decisions (from brainstorming)

These were explicitly chosen over alternatives during design:

1. **VOLUNTEER stays as a real role**, not folded into AFFILIATE and not demoted to a separate capability flag. It keeps gating `apps.attendance.permissions.IsVolunteerOrAbove` (attendance QR scanning) exactly as today.
2. **Public signup only ever creates `AFFILIATE` or `NON_AFFILIATE`.** `VOLUNTEER`/`JUDGE`/`CLUB_LEAD`/`ADMIN` remain admin-granted only, same as `JUDGE`/`CLUB_LEAD`/`ADMIN` already are today.
3. **An admin PATCHing a user's role to `AFFILIATE` without a `club_id` is rejected outright** (400, field-anchored error) rather than silently allowed or auto-combined into one request. The admin must assign a Club ID first (existing Club ID tooling), then set the role.

## 4. Data model

`apps/accounts/models.py` — `UserRole` gains `AFFILIATE = 'AFFILIATE', 'Affiliate'` and `NON_AFFILIATE = 'NON_AFFILIATE', 'Non-Affiliate'`, loses `MEMBER`. `User.role`'s `default` changes from `UserRole.MEMBER` to `UserRole.NON_AFFILIATE` (the correct baseline for a row with no Club ID).

`role` is a plain `CharField(choices=...)` — Postgres has no enum constraint to migrate, so this is metadata-only at the schema level. No column type change.

## 5. Data migration

One-time `RunPython` migration, run once on deploy:

```text
UPDATE users SET role = 'AFFILIATE'     WHERE role = 'MEMBER' AND club_id IS NOT NULL;
UPDATE users SET role = 'NON_AFFILIATE' WHERE role = 'MEMBER' AND club_id IS NULL;
```

Verified against the current live dataset: 26 `MEMBER` rows → 12 `AFFILIATE` (have a `club_id`), 14 `NON_AFFILIATE` (don't). `VOLUNTEER` (0 rows today), `JUDGE`, `CLUB_LEAD`, `ADMIN` rows are untouched. The reverse migration (`AFFILIATE`/`NON_AFFILIATE` → `MEMBER`) is provided for `migrate` rollback symmetry, per Django convention, even though it's not expected to be used.

No in-flight-JWT concern: `request.user.role` is always a fresh DB read per request (`JWTAuthentication` resolves the user by ID, not from token claims), so a token issued before the migration with a stale `role` claim in its payload is inert for authorization — confirmed in the earlier role-propagation investigation this session.

## 6. Validation — "AFFILIATE requires club_id"

Enforced in exactly two places, matching the two mutation paths:

**`RegisterSerializer.validate()`** (`apps/accounts/serializers.py`) — covers both public self-registration and the admin "Create New User" modal, which already reuses this same `POST /auth/register/` endpoint. If the resolved `role` is `AFFILIATE` and `club_id` is blank/invalid, raise a validation error naming `club_id` explicitly (not a generic non-field error), e.g. *"Affiliate members must provide a valid Club ID."* `validate_role()`'s self-registerable set shrinks from `{'MEMBER', 'VOLUNTEER'}` to `{'AFFILIATE', 'NON_AFFILIATE'}`.

**`UserDetailView.perform_update`** (`apps/accounts/views.py`) — covers the admin Users-tab role dropdown. When `role` is being changed to `AFFILIATE` and the target user's `club_id` is `None`, raise a `ValidationError` (400) naming the problem before calling `serializer.save()`. This sits next to the existing elevation check (`ADMIN`/`CLUB_LEAD` escalation), same shape.

No change needed to `ProfileView`/`UserProfileDetailSerializer` — `role` is already read-only there (self-service profile edits can't touch it).

## 7. Backend — other touch points (rename only, no new logic)

- `apps/core/permissions.py` — `IsAdminOrClubLead`, `IsJudgeOrAdmin`, `IsOwnerOrAdminOrClubLead`: no changes. They only special-case `ADMIN`/`CLUB_LEAD`/`JUDGE`, none of which are renamed.
- `apps/attendance/permissions.py` — `IsVolunteerOrAbove`: no changes, `VOLUNTEER` is unchanged.
- `apps/core/dmc/adapters/users.py` — role filter options: `FilterOption("Member", "MEMBER")` → `FilterOption("Affiliate", "AFFILIATE")` + `FilterOption("Non-Affiliate", "NON_AFFILIATE")`.
- `apps/core/management/commands/seed_data.py` — one seed user (`member@srkr.ac.in`, line ~79) uses `UserRole.MEMBER` with no `club_id` in its `defaults`; becomes `UserRole.NON_AFFILIATE`. (`apps/accounts/management/commands/setup_admin.py` only ever sets `ADMIN` — no change needed there.)
- `apps/accounts/services/user_account_service.py` (bulk member-directory import, line ~303) — currently hardcodes `role=UserRole.MEMBER`. Checked the surrounding code: this path *always* resolves a `club_id` before constructing the `User` (uses the imported row's own Club ID, or allocates a fresh one via `ClubIDService.allocate_next_club_id` when none was supplied) — there is no code path here that leaves `club_id` unset. So the replacement is unconditional: `role=UserRole.AFFILIATE`, not a club_id-derived branch (unlike the one-time data migration in §5, which has to handle historical `MEMBER` rows that legitimately have no `club_id`).

## 8. Frontend

**Signup** (`src/app/signup/page.tsx`) — no new UI. The existing "Are you an affiliate?" checkbox + conditional Affiliate ID field (built earlier this session) now actually drives `role`: checked → `role: 'AFFILIATE'`, unchecked → `role: 'NON_AFFILIATE'`, replacing today's hardcoded `role: 'MEMBER'`. Affiliate ID stays required-when-checked, which it already is.

**Admin Users tab** (`src/components/admin/UsersTab.tsx`) — `ALL_ROLES` becomes `['AFFILIATE', 'NON_AFFILIATE', 'VOLUNTEER', 'JUDGE', 'CLUB_LEAD', 'ADMIN']`. No new UI for the club_id-required rejection — it already reverts the optimistic change and shows the backend's error via toast (`handleRoleChange` in `useAdminData.ts`), which is sufficient given the backend now returns a clear, field-anchored message.

**Admin Create User modal** (`src/components/admin/CreateUserModal.tsx`) — role dropdown becomes `AFFILIATE` / `NON_AFFILIATE` / `VOLUNTEER` (JUDGE/CLUB_LEAD/ADMIN still granted afterward via the Users tab, per today's note in the modal). Gains a conditional Affiliate ID input, shown only when `AFFILIATE` is selected, mirroring the signup page's field — this is new UI, since the modal has no such field today and would otherwise hit the same backend validation with no way to satisfy it.

**Type definitions & defaults** — `src/lib/types.ts`'s role union, `src/app/profile/page.tsx`'s `'MEMBER'` fallback defaults, and any other `'MEMBER' | 'VOLUNTEER' | ...` literal unions get updated to the new six-value set.

## 9. Testing plan

- Rewrite `apps/accounts/tests/test_registration_club_id.py`, `test_registration_validation.py`, `test_user_role_update.py` assertions that reference `MEMBER`/role defaults to use `AFFILIATE`/`NON_AFFILIATE` as appropriate.
- New: signup with the affiliate checkbox checked but no/invalid Club ID → rejected, field-anchored to `club_id`.
- New: signup with the affiliate checkbox unchecked → `role == 'NON_AFFILIATE'`, no Club ID required.
- New: admin PATCHes a `club_id`-less user's role to `AFFILIATE` → 400, rejected, DB unchanged.
- New: admin PATCHes a user who already has a `club_id` to `AFFILIATE` → 200, succeeds.
- New: data migration test — seed `MEMBER` rows with/without `club_id`, run the migration, assert the split.
- Full existing suite (228 tests as of this session) must stay green, updated only where it asserted the old role literals.

## 10. Out of scope

- No change to `VOLUNTEER`/`JUDGE`/`CLUB_LEAD`/`ADMIN` semantics or permissions.
- No UI for admins to set `role` and `club_id` in the same Users-tab request (decision #3 above) — that's a possible future enhancement, not part of this refactor.
- No retroactive backfill of Club IDs for the 14 `NON_AFFILIATE` users who don't have one — they simply become (and stay) `NON_AFFILIATE` until someone assigns them a Club ID and an admin promotes them.
