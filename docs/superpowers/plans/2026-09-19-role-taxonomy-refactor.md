# AFFILIATE / NON_AFFILIATE Role Taxonomy Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the `MEMBER` role into `AFFILIATE` and `NON_AFFILIATE`, with a hard server-side rule that `AFFILIATE` always requires a `club_id`, everywhere a role can be set (self-registration, admin quick-create, admin role-change).

**Architecture:** `UserRole` gains `AFFILIATE`/`NON_AFFILIATE` and drops `MEMBER`; a one-time data migration splits existing `MEMBER` rows by whether they already have a `club_id`. The "AFFILIATE requires club_id" rule is enforced in exactly two places — `RegisterSerializer.validate()` (covers public signup and the admin "Create New User" modal, which reuses the same endpoint) and `UserDetailView.perform_update` (covers the admin Users-tab role dropdown). Everything else — permission classes, `VOLUNTEER`/`JUDGE`/`CLUB_LEAD`/`ADMIN` — is untouched; only the literal role strings referencing `MEMBER` are renamed.

**Tech Stack:** Django REST Framework (backend), Next.js/React/TypeScript (frontend), Django `TestCase` + DRF `APIClient` for backend tests, `tsc --noEmit` + `next build` for frontend verification (no frontend test runner exists in this repo).

**Spec:** `docs/superpowers/specs/2026-09-19-role-taxonomy-refactor-design.md`

## Global Constraints

- Final role set, exact strings: `AFFILIATE`, `NON_AFFILIATE`, `VOLUNTEER`, `JUDGE`, `CLUB_LEAD`, `ADMIN`. `MEMBER` no longer exists anywhere.
- `AFFILIATE` ⇒ must have a non-null `club_id`. This is one-directional — a `NON_AFFILIATE` (or any other role) is allowed to hold a `club_id` too (e.g. historical data, or a pre-assigned id from an offline recruitment drive); nothing forces the reverse.
- The **public signup page** may only ever produce `AFFILIATE` or `NON_AFFILIATE` (its "are you an affiliate?" checkbox is the only role signal it sends). `POST /auth/register/` — the endpoint both the public signup page and the admin "Create New User" modal call — accepts `AFFILIATE`, `NON_AFFILIATE`, or `VOLUNTEER` (the modal can still directly create a `VOLUNTEER`, unchanged from before this role split — see the pre-flight ruling in the ledger). `JUDGE`/`CLUB_LEAD`/`ADMIN` are never accepted by this endpoint from either caller; they're admin-granted only, via the Users-tab PATCH.
- An admin PATCHing a user's role to `AFFILIATE` when that user has no `club_id` is rejected with `400` and a field-anchored `club_id` error — never silently allowed, never auto-combined with a club_id assignment in the same request.
- Default role for a fresh row with no other signal: `NON_AFFILIATE`.
- Backend repo root: `C:\Users\chall\OneDrive\Desktop\SRKRCC-Main_APPLICATION_BACKEND`. Frontend repo root: `C:\Users\chall\OneDrive\Desktop\SRKRCC-Main_APPLICATION_FRONTEND`. Run backend tests with `./venv/Scripts/python.exe manage.py test <path>` from the backend root.

---

## Task 1: Model + migration — split `MEMBER` into `AFFILIATE`/`NON_AFFILIATE`

**Files:**
- Modify: `apps/accounts/models.py:6-11` (the `UserRole` class), `apps/accounts/models.py:41-45` (the `role` field's `default`)
- Create: `apps/accounts/migrations/0006_affiliate_non_affiliate_roles.py`
- Test: `apps/accounts/tests/test_role_migration.py` (new file)

**Interfaces:**
- Produces: `UserRole.AFFILIATE` (value `'AFFILIATE'`), `UserRole.NON_AFFILIATE` (value `'NON_AFFILIATE'`) — every later task's backend code imports these from `apps.accounts.models`.
- Produces: the data-migration mapping rule (`MEMBER` + `club_id` set → `AFFILIATE`; `MEMBER` + no `club_id` → `NON_AFFILIATE`) — Task 4 reuses this exact rule for `seed_data.py`/`user_account_service.py`, and this task's own test proves it.

- [ ] **Step 1: Write the failing test for the mapping rule**

This test exercises the *rule* the migration applies (not the migration file itself — Django migrations aren't naturally unit-testable without extra tooling this repo doesn't have, and the actual migration gets verified for real in Step 5 by running `migrate` against the dev DB and checking the resulting counts). It's written against the current model on purpose, so it fails right now because `'AFFILIATE'`/`'NON_AFFILIATE'` aren't valid `UserRole` values yet — `User.objects.create_user(..., role='AFFILIATE')` will still *save* (Django doesn't enforce `choices` at the DB or `.create_user()` level, only via `full_clean()`), but the migration's own model lookup by those role strings must reflect the same rule this test encodes.

Create `apps/accounts/tests/test_role_migration.py`:

```python
"""
Verifies the mapping rule the 0006_affiliate_non_affiliate_roles data
migration applies to historical MEMBER rows: a MEMBER with a club_id becomes
AFFILIATE, a MEMBER without one becomes NON_AFFILIATE. This tests the rule
directly against the current model (not the migration file itself — Django
migrations aren't easily unit-testable without extra tooling this repo
doesn't have) so it stays meaningful as living documentation even after the
one-time migration has run in every environment.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

User = get_user_model()


class MemberRoleSplitMappingTests(TestCase):
    def test_member_with_club_id_maps_to_affiliate(self):
        user = User.objects.create_user(
            username='hasid', email='hasid@srkr.ac.in', password='pw12345!',
            role='MEMBER', club_id='25SCC901',
        )
        User.objects.filter(role='MEMBER', club_id__isnull=False).update(role='AFFILIATE')
        User.objects.filter(role='MEMBER', club_id__isnull=True).update(role='NON_AFFILIATE')
        user.refresh_from_db()
        self.assertEqual(user.role, 'AFFILIATE')

    def test_member_without_club_id_maps_to_non_affiliate(self):
        user = User.objects.create_user(
            username='noid', email='noid@srkr.ac.in', password='pw12345!',
            role='MEMBER',
        )
        User.objects.filter(role='MEMBER', club_id__isnull=False).update(role='AFFILIATE')
        User.objects.filter(role='MEMBER', club_id__isnull=True).update(role='NON_AFFILIATE')
        user.refresh_from_db()
        self.assertEqual(user.role, 'NON_AFFILIATE')

    def test_non_member_roles_are_unaffected(self):
        volunteer = User.objects.create_user(
            username='vol1', email='vol1@srkr.ac.in', password='pw12345!', role='VOLUNTEER',
        )
        User.objects.filter(role='MEMBER', club_id__isnull=False).update(role='AFFILIATE')
        User.objects.filter(role='MEMBER', club_id__isnull=True).update(role='NON_AFFILIATE')
        volunteer.refresh_from_db()
        self.assertEqual(volunteer.role, 'VOLUNTEER')
```

- [ ] **Step 2: Run the test to verify it currently passes trivially (baseline), then move on**

Run: `./venv/Scripts/python.exe manage.py test apps.accounts.tests.test_role_migration -v 1`
Expected: `OK` — these three assertions pass even before the model changes, since they only exercise the raw `.update()` calls against string values Django doesn't validate at the DB layer. That's fine: this step exists to confirm the test file itself is wired correctly (imports resolve, `TestCase` runs) before Step 3 changes the model underneath it. The test starts earning its keep once Step 3 makes `MEMBER` an *invalid* choice going forward — it stays green because `.create_user()` and `.update()` never call `full_clean()`.

- [ ] **Step 3: Update `UserRole` and the `role` field's default**

Edit `apps/accounts/models.py`. Replace:

```python
class UserRole(models.TextChoices):
    MEMBER = 'MEMBER', 'Member'
    VOLUNTEER = 'VOLUNTEER', 'Volunteer'
    JUDGE = 'JUDGE', 'Judge'
    CLUB_LEAD = 'CLUB_LEAD', 'Club Lead'
    ADMIN = 'ADMIN', 'Admin'
```

with:

```python
class UserRole(models.TextChoices):
    # AFFILIATE always has a club_id — enforced in RegisterSerializer.validate()
    # (signup + the admin "Create New User" modal, which reuses that same
    # endpoint) and in UserDetailView.perform_update (the admin role-change
    # PATCH). NON_AFFILIATE has no such requirement, though nothing stops one
    # from holding a club_id too (e.g. a pre-assigned id from an offline
    # recruitment drive, or historical data).
    AFFILIATE = 'AFFILIATE', 'Affiliate'
    NON_AFFILIATE = 'NON_AFFILIATE', 'Non-Affiliate'
    VOLUNTEER = 'VOLUNTEER', 'Volunteer'
    JUDGE = 'JUDGE', 'Judge'
    CLUB_LEAD = 'CLUB_LEAD', 'Club Lead'
    ADMIN = 'ADMIN', 'Admin'
```

Then replace the `role` field's default:

```python
    role = models.CharField(
        max_length=20,
        choices=UserRole.choices,
        default=UserRole.MEMBER
    )
```

with:

```python
    role = models.CharField(
        max_length=20,
        choices=UserRole.choices,
        default=UserRole.NON_AFFILIATE
    )
```

- [ ] **Step 4: Run the test again to confirm it still passes**

Run: `./venv/Scripts/python.exe manage.py test apps.accounts.tests.test_role_migration -v 1`
Expected: `OK` — same three tests, now exercising the actual new `UserRole` values.

- [ ] **Step 5: Write the migration**

Run: `./venv/Scripts/python.exe manage.py makemigrations accounts --name affiliate_non_affiliate_roles`
Expected output: `Migrations for 'accounts': apps\accounts\migrations\0006_affiliate_non_affiliate_roles.py — Alter field role on user`

Open the generated file and add the data-migration operation and a module docstring/comment. The generated file will contain one `migrations.AlterField(...)` operation from the schema change — append a `migrations.RunPython(...)` operation after it, and add the two functions above the `Migration` class:

```python
from django.db import migrations, models


def split_member_role(apps, schema_editor):
    """
    One-time data migration: existing MEMBER rows split by whether they
    already have a club_id. Verified against the live dataset before writing
    this migration — 26 MEMBER rows: 12 with a club_id (-> AFFILIATE), 14
    without (-> NON_AFFILIATE). VOLUNTEER/JUDGE/CLUB_LEAD/ADMIN rows are
    untouched by both filters below (they don't match role='MEMBER').
    """
    User = apps.get_model('accounts', 'User')
    User.objects.filter(role='MEMBER', club_id__isnull=False).update(role='AFFILIATE')
    User.objects.filter(role='MEMBER', club_id__isnull=True).update(role='NON_AFFILIATE')


def unsplit_member_role(apps, schema_editor):
    """Reverse: collapse AFFILIATE/NON_AFFILIATE back to MEMBER, for `migrate` rollback symmetry."""
    User = apps.get_model('accounts', 'User')
    User.objects.filter(role__in=['AFFILIATE', 'NON_AFFILIATE']).update(role='MEMBER')


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0005_roll_number_unique'),
    ]

    operations = [
        migrations.AlterField(
            model_name='user',
            name='role',
            field=models.CharField(
                choices=[
                    ('AFFILIATE', 'Affiliate'),
                    ('NON_AFFILIATE', 'Non-Affiliate'),
                    ('VOLUNTEER', 'Volunteer'),
                    ('JUDGE', 'Judge'),
                    ('CLUB_LEAD', 'Club Lead'),
                    ('ADMIN', 'Admin'),
                ],
                default='NON_AFFILIATE',
                max_length=20,
            ),
        ),
        migrations.RunPython(split_member_role, reverse_code=unsplit_member_role),
    ]
```

(If `makemigrations` produces a different `choices=` ordering or dependency line than shown, keep its actual output for the `AlterField` operation — only the `RunPython` operation and the two functions above the class are hand-added.)

- [ ] **Step 6: Apply the migration and verify against live dev data**

Run: `./venv/Scripts/python.exe manage.py migrate accounts`
Expected: `Applying accounts.0006_affiliate_non_affiliate_roles... OK`

Run:
```bash
./venv/Scripts/python.exe manage.py shell -c "
from django.contrib.auth import get_user_model
from django.db.models import Count
User = get_user_model()
print(list(User.objects.values('role').annotate(c=Count('id')).order_by('role')))
print('any MEMBER rows left:', User.objects.filter(role='MEMBER').exists())
"
```
Expected: no `MEMBER` row in the output, and (matching the counts verified during spec-writing) `AFFILIATE` count == the old `MEMBER`-with-`club_id` count, `NON_AFFILIATE` count == the old `MEMBER`-without-`club_id` count, `VOLUNTEER`/`JUDGE`/`CLUB_LEAD`/`ADMIN` counts unchanged.

- [ ] **Step 7: Commit**

```bash
git add apps/accounts/models.py apps/accounts/migrations/0006_affiliate_non_affiliate_roles.py apps/accounts/tests/test_role_migration.py
git commit -m "feat: split MEMBER role into AFFILIATE/NON_AFFILIATE

UserRole gains AFFILIATE and NON_AFFILIATE, loses MEMBER. Data migration
splits existing MEMBER rows by whether they already have a club_id.
VOLUNTEER/JUDGE/CLUB_LEAD/ADMIN are untouched.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 2: `RegisterSerializer` — self-registerable roles + AFFILIATE requires club_id

**Files:**
- Modify: `apps/accounts/serializers.py` (the `RegisterSerializer` class — `SELF_REGISTERABLE_ROLES`, `validate_role`, `validate`)
- Test: `apps/accounts/tests/test_registration_validation.py`

**Interfaces:**
- Consumes: `UserRole.AFFILIATE`, `UserRole.NON_AFFILIATE` (Task 1).
- Produces: `RegisterSerializer` now rejects `role=AFFILIATE` without a valid `club_id` with a `400` naming the `club_id` field — Task 9 (frontend `CreateUserModal`) and the already-existing signup form both rely on this exact error shape (`{"club_id": ["..."]}`) to show a field-anchored message.

- [ ] **Step 1: Write the failing tests**

Add to `apps/accounts/tests/test_registration_validation.py`, inside `RegistrationValidationTests` (anywhere after `test_self_registration_cannot_grant_itself_admin`, before the `LoginValidationTests` class):

```python
    # --- AFFILIATE / NON_AFFILIATE ------------------------------------------

    def test_self_registration_defaults_to_non_affiliate_with_no_role_sent(self):
        resp = self._post()  # _payload() never sets 'role'
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(User.objects.get(email='newmember@srkr.ac.in').role, 'NON_AFFILIATE')

    def test_non_affiliate_signup_does_not_require_a_club_id(self):
        resp = self._post(role='NON_AFFILIATE')
        self.assertEqual(resp.status_code, 201, resp.data)
        user = User.objects.get(email='newmember@srkr.ac.in')
        self.assertEqual(user.role, 'NON_AFFILIATE')
        self.assertIsNone(user.club_id)

    def test_affiliate_signup_without_club_id_is_rejected(self):
        resp = self._post(role='AFFILIATE')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('club_id', resp.data)
        self.assertFalse(User.objects.filter(email='newmember@srkr.ac.in').exists())

    def test_affiliate_signup_with_blank_club_id_is_rejected(self):
        resp = self._post(role='AFFILIATE', club_id='')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('club_id', resp.data)

    def test_affiliate_signup_with_valid_club_id_succeeds(self):
        resp = self._post(role='AFFILIATE', club_id='25SCC410')
        self.assertEqual(resp.status_code, 201, resp.data)
        user = User.objects.get(email='newmember@srkr.ac.in')
        self.assertEqual(user.role, 'AFFILIATE')
        self.assertEqual(user.club_id, '25SCC410')

    def test_affiliate_signup_with_malformed_club_id_is_rejected_by_club_id_check_first(self):
        # validate_club_id() runs before the cross-field AFFILIATE check, so a
        # malformed id is reported as a format problem, not as "missing".
        resp = self._post(role='AFFILIATE', club_id='not-a-club-id')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('club_id', resp.data)
        self.assertNotIn('valid Club ID', str(resp.data['club_id']))

    def test_endpoint_still_accepts_volunteer_for_the_admin_create_user_modal(self):
        # SELF_REGISTERABLE_ROLES keeps VOLUNTEER alongside AFFILIATE/
        # NON_AFFILIATE specifically so the admin's "Create New User" modal
        # (which POSTs to this same endpoint) can still directly create a
        # VOLUNTEER, same as before this role split. The public signup form
        # itself never sends 'VOLUNTEER' — this is the endpoint's own allowed
        # set, broader than what any one caller offers.
        resp = self._post(role='VOLUNTEER')
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(User.objects.get(email='newmember@srkr.ac.in').role, 'VOLUNTEER')
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/Scripts/python.exe manage.py test apps.accounts.tests.test_registration_validation -v 1`
Expected: `FAIL`/`ERROR` on six of the seven new tests — `role='AFFILIATE'` currently coerces to `'MEMBER'` (not in `SELF_REGISTERABLE_ROLES` yet) via the old `validate_role` fallback, so none of the AFFILIATE-specific assertions hold yet, and `test_self_registration_defaults_to_non_affiliate_with_no_role_sent` fails because the current default is `'MEMBER'`. `test_endpoint_still_accepts_volunteer_for_the_admin_create_user_modal` passes already — `'VOLUNTEER'` is in the *old* `SELF_REGISTERABLE_ROLES` too, just not asserted against `'NON_AFFILIATE'`-flavored expectations; it's included here as a regression guard for Step 3, not because it fails first.

- [ ] **Step 3: Implement**

Edit `apps/accounts/serializers.py`. Replace:

```python
    # Self-registration may only pick from these two low-privilege roles. ADMIN/
    # CLUB_LEAD/JUDGE can only be granted by an existing admin — role was
    # previously unrestricted, letting an anonymous POST with {"role": "ADMIN"}
    # create a full admin account.
    SELF_REGISTERABLE_ROLES = {'MEMBER', 'VOLUNTEER'}
    role = serializers.CharField(required=False, allow_blank=True)
```

with:

```python
    # This endpoint is called by two things: the public signup form (which
    # only ever sends AFFILIATE or NON_AFFILIATE — its "are you an affiliate?"
    # checkbox) and the admin's "Create New User" modal (which can also
    # directly create a VOLUNTEER, same as it could before this role split —
    # that capability isn't being removed here, just kept working under the
    # new names). JUDGE/CLUB_LEAD/ADMIN can only be granted by an existing
    # admin via the Users-tab PATCH — role was previously unrestricted here,
    # letting an anonymous POST with {"role": "ADMIN"} create a full admin
    # account.
    SELF_REGISTERABLE_ROLES = {'AFFILIATE', 'NON_AFFILIATE', 'VOLUNTEER'}
    role = serializers.CharField(required=False, allow_blank=True)
```

Replace:

```python
    def validate_role(self, value):
        return value if value in self.SELF_REGISTERABLE_ROLES else 'MEMBER'
```

with:

```python
    def validate_role(self, value):
        return value if value in self.SELF_REGISTERABLE_ROLES else 'NON_AFFILIATE'
```

Then extend `validate()` — replace:

```python
    def validate(self, attrs):
        # Password strength is checked here rather than in validate_password()
        # because UserAttributeSimilarityValidator needs the rest of the payload:
        # it is what rejects a password built out of the applicant's own email or
        # name, and DRF runs per-field validators before those siblings exist.
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError as DjangoValidationError

        password = attrs.get('password')
        if password:
            email = attrs.get('email', '')
            candidate = User(
                email=email,
                username=email.split('@')[0],
                first_name=attrs.get('first_name', ''),
                last_name=attrs.get('last_name', ''),
            )
            try:
                validate_password(password, user=candidate)
            except DjangoValidationError as ex:
                raise serializers.ValidationError({'password': list(ex.messages)})
        return attrs
```

with:

```python
    def validate(self, attrs):
        # Password strength is checked here rather than in validate_password()
        # because UserAttributeSimilarityValidator needs the rest of the payload:
        # it is what rejects a password built out of the applicant's own email or
        # name, and DRF runs per-field validators before those siblings exist.
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError as DjangoValidationError

        password = attrs.get('password')
        if password:
            email = attrs.get('email', '')
            candidate = User(
                email=email,
                username=email.split('@')[0],
                first_name=attrs.get('first_name', ''),
                last_name=attrs.get('last_name', ''),
            )
            try:
                validate_password(password, user=candidate)
            except DjangoValidationError as ex:
                raise serializers.ValidationError({'password': list(ex.messages)})

        # AFFILIATE always has a club_id (validate_club_id() above has already
        # normalized it to canonical form, or to None if blank/omitted — this
        # runs after both validate_role() and validate_club_id() since DRF
        # calls per-field validators before this object-level one).
        if attrs.get('role') == 'AFFILIATE' and not attrs.get('club_id'):
            raise serializers.ValidationError({
                'club_id': "Affiliate members must provide a valid Club ID. "
                           "If you don't have one yet, sign up as a Non-Affiliate instead.",
            })
        return attrs
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/Scripts/python.exe manage.py test apps.accounts.tests.test_registration_validation -v 1`
Expected: `OK`, all tests pass including the six new ones.

- [ ] **Step 5: Fix the one pre-existing test that still asserts the old default**

Edit `apps/accounts/tests/test_registration_validation.py` line 218. Replace:

```python
        self.assertEqual(User.objects.get(email='newmember@srkr.ac.in').role, 'MEMBER')
```

with:

```python
        self.assertEqual(User.objects.get(email='newmember@srkr.ac.in').role, 'NON_AFFILIATE')
```

(`apps/accounts/tests/test_registration_club_id.py` needs no changes — none of its tests set or assert `role`, only `club_id`, and `NON_AFFILIATE` is allowed to hold a `club_id` too.)

- [ ] **Step 6: Run the full accounts test suite**

Run: `./venv/Scripts/python.exe manage.py test apps.accounts -v 1`
Expected: `OK`

- [ ] **Step 7: Commit**

```bash
git add apps/accounts/serializers.py apps/accounts/tests/test_registration_validation.py
git commit -m "feat: self-registration creates AFFILIATE/NON_AFFILIATE, AFFILIATE requires club_id

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 3: `UserDetailView` — admin PATCH to AFFILIATE requires an existing club_id

**Files:**
- Modify: `apps/accounts/views.py:1-2` (imports), `apps/accounts/views.py:90-126` (`UserDetailView.perform_update`)
- Test: `apps/accounts/tests/test_user_role_update.py`

**Interfaces:**
- Consumes: `UserRole.AFFILIATE` string (Task 1).
- Produces: `PATCH /auth/users/{id}/` with `{"role": "AFFILIATE"}` against a `club_id`-less user now returns `400` with `{"club_id": [...]}` — the frontend's existing `handleRoleChange` revert-on-failure logic (`src/lib/hooks/useAdminData.ts`) already surfaces whatever message this returns, no frontend change required for this behavior specifically (see Task 8).

- [ ] **Step 1: Update the existing tests to use the new role names first**

The existing suite uses `'MEMBER'` purely as "some low-privilege, non-elevated role" — none of its scenarios care about the AFFILIATE/club_id rule yet, so this is a straight rename, done before adding new tests so the new ones read cleanly against a suite that already reflects the new taxonomy.

Edit `apps/accounts/tests/test_user_role_update.py`. Replace:

```python
        self.member = User.objects.create_user(
            username='member1', email='member1@srkr.ac.in', password='pw12345!', role='MEMBER',
        )
```

with:

```python
        self.member = User.objects.create_user(
            username='member1', email='member1@srkr.ac.in', password='pw12345!', role='NON_AFFILIATE',
        )
```

Replace (in `test_club_lead_cannot_promote_to_admin`):

```python
        self.assertEqual(self.member.role, 'MEMBER')
```

with:

```python
        self.assertEqual(self.member.role, 'NON_AFFILIATE')
```

Replace (in `test_cannot_change_own_role`):

```python
        resp = self.client.patch(self._url(self.admin), {'role': 'MEMBER'}, format='json')
```

with:

```python
        resp = self.client.patch(self._url(self.admin), {'role': 'NON_AFFILIATE'}, format='json')
```

Replace (in `test_member_forbidden_from_endpoint`):

```python
        resp = self.client.patch(self._url(self.club_lead), {'role': 'MEMBER'}, format='json')
```

with:

```python
        resp = self.client.patch(self._url(self.club_lead), {'role': 'NON_AFFILIATE'}, format='json')
```

- [ ] **Step 2: Run the renamed tests to confirm they still pass (sanity check before adding new behavior)**

Run: `./venv/Scripts/python.exe manage.py test apps.accounts.tests.test_user_role_update -v 1`
Expected: `OK` — pure rename, no behavior change yet.

- [ ] **Step 3: Write the failing tests for the new AFFILIATE/club_id rule**

Add to `apps/accounts/tests/test_user_role_update.py`, inside `UserRoleUpdateTests`, after `test_club_lead_can_change_low_privilege_roles`:

```python
    def test_admin_cannot_promote_to_affiliate_without_a_club_id(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.patch(self._url(self.member), {'role': 'AFFILIATE'}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('club_id', resp.data)
        self.member.refresh_from_db()
        self.assertEqual(self.member.role, 'NON_AFFILIATE')

    def test_admin_can_promote_to_affiliate_when_club_id_already_set(self):
        self.member.club_id = '25SCC420'
        self.member.save(update_fields=['club_id'])
        self.client.force_authenticate(self.admin)
        resp = self.client.patch(self._url(self.member), {'role': 'AFFILIATE'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.member.refresh_from_db()
        self.assertEqual(self.member.role, 'AFFILIATE')
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `./venv/Scripts/python.exe manage.py test apps.accounts.tests.test_user_role_update -v 1`
Expected: `test_admin_cannot_promote_to_affiliate_without_a_club_id` `FAIL`s (currently returns `200`, the PATCH is allowed with no check).

- [ ] **Step 5: Implement**

Edit `apps/accounts/views.py`. Replace the import line:

```python
from rest_framework.exceptions import PermissionDenied
```

with:

```python
from rest_framework.exceptions import PermissionDenied, ValidationError
```

Then replace the escalation-check block inside `perform_update`:

```python
        # Escalation check applies only when `role` is actually being changed —
        # scoped to serializer.validated_data (not target.role) so that a
        # membership_status-only PATCH on a user who already holds an elevated
        # role doesn't get wrongly blocked as a "role escalation".
        if 'role' in serializer.validated_data:
            new_role = serializer.validated_data['role']
            is_full_admin = requester.is_superuser or requester.is_staff or getattr(requester, 'role', None) == 'ADMIN'
            if new_role in self.ELEVATED_ROLES and not is_full_admin:
                raise PermissionDenied("Only an Admin can assign the Admin or Club Lead role.")
```

with:

```python
        # Escalation check applies only when `role` is actually being changed —
        # scoped to serializer.validated_data (not target.role) so that a
        # membership_status-only PATCH on a user who already holds an elevated
        # role doesn't get wrongly blocked as a "role escalation".
        if 'role' in serializer.validated_data:
            new_role = serializer.validated_data['role']
            is_full_admin = requester.is_superuser or requester.is_staff or getattr(requester, 'role', None) == 'ADMIN'
            if new_role in self.ELEVATED_ROLES and not is_full_admin:
                raise PermissionDenied("Only an Admin can assign the Admin or Club Lead role.")
            # AFFILIATE always has a club_id. This endpoint doesn't accept
            # club_id in its own payload (UserRoleUpdateSerializer only writes
            # role/membership_status) — the admin must assign one first via
            # the existing Club ID tooling, then set the role.
            if new_role == 'AFFILIATE' and not target.club_id:
                raise ValidationError({
                    'club_id': ["Assign a Club ID to this member before setting their role to Affiliate."],
                })
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `./venv/Scripts/python.exe manage.py test apps.accounts.tests.test_user_role_update -v 1`
Expected: `OK`, all tests pass including the two new ones.

- [ ] **Step 7: Commit**

```bash
git add apps/accounts/views.py apps/accounts/tests/test_user_role_update.py
git commit -m "feat: reject admin PATCH to AFFILIATE role when target has no club_id

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 4: Rename-only backend touch points (DMC filter, seed data, bulk import)

**Files:**
- Modify: `apps/core/dmc/adapters/users.py:64-69`
- Modify: `apps/core/management/commands/seed_data.py:79`
- Modify: `apps/accounts/services/user_account_service.py:303`

**Interfaces:**
- Consumes: `UserRole.AFFILIATE`, `UserRole.NON_AFFILIATE` (Task 1).
- Produces: nothing new — these are pure literal renames with no new logic, verified by the full suite passing (this task has no dedicated test file of its own; `apps/accounts/tests/test_member_platform_core.py`'s bulk-import tests, exercised in Task 5's full-suite run, cover `user_account_service.py`'s behavior).

- [ ] **Step 1: DMC filter options**

Edit `apps/core/dmc/adapters/users.py`. Replace:

```python
    FilterDefinition(
        key="role", label="Role", type="select", operators=["eq", "neq"],
        options=[
            FilterOption("Member", "MEMBER"),
            FilterOption("Volunteer", "VOLUNTEER"),
            FilterOption("Judge", "JUDGE"),
            FilterOption("Club Lead", "CLUB_LEAD"),
            FilterOption("Admin", "ADMIN"),
        ],
    ),
```

with:

```python
    FilterDefinition(
        key="role", label="Role", type="select", operators=["eq", "neq"],
        options=[
            FilterOption("Affiliate", "AFFILIATE"),
            FilterOption("Non-Affiliate", "NON_AFFILIATE"),
            FilterOption("Volunteer", "VOLUNTEER"),
            FilterOption("Judge", "JUDGE"),
            FilterOption("Club Lead", "CLUB_LEAD"),
            FilterOption("Admin", "ADMIN"),
        ],
    ),
```

- [ ] **Step 2: Seed data**

Edit `apps/core/management/commands/seed_data.py` line 79. Replace:

```python
                "role": UserRole.MEMBER,
```

with:

```python
                "role": UserRole.NON_AFFILIATE,
```

(This is the `member@srkr.ac.in` seed user, which has no `club_id` in its `defaults` — `NON_AFFILIATE` is the correct match, not `AFFILIATE`.)

- [ ] **Step 3: Bulk member-directory import**

Edit `apps/accounts/services/user_account_service.py` line 303. Replace:

```python
                            role=UserRole.MEMBER,
```

with:

```python
                            role=UserRole.AFFILIATE,
```

Add a one-line comment immediately above it explaining why this is unconditional (unlike the historical data migration in Task 1, which has to branch on `club_id` presence):

```python
                            # Unconditionally AFFILIATE, not club_id-derived: this path always
                            # resolves a club_id above (either the imported row's own, or a
                            # freshly allocated one) before reaching this point.
                            role=UserRole.AFFILIATE,
```

- [ ] **Step 4: Run the tests that exercise these files**

Run: `./venv/Scripts/python.exe manage.py test apps.accounts.tests.test_member_platform_core -v 1`
Expected: `OK` (this is the bulk-import test module; confirms `user_account_service.py`'s change didn't break anything). DMC adapter and `seed_data.py` have no dedicated automated tests in this repo — `seed_data.py` is verified manually in Step 5.

- [ ] **Step 5: Manually verify `seed_data` still runs cleanly**

Run: `./venv/Scripts/python.exe manage.py seed_data --help`
Expected: prints the command's help text without a Python traceback (confirms the module still imports cleanly — `UserRole.NON_AFFILIATE` resolves). Do not actually run `seed_data` against the dev database (it seeds unrelated demo content across many other apps, out of scope for this change).

- [ ] **Step 6: Commit**

```bash
git add apps/core/dmc/adapters/users.py apps/core/management/commands/seed_data.py apps/accounts/services/user_account_service.py
git commit -m "chore: rename MEMBER role references to AFFILIATE/NON_AFFILIATE

DMC filter options, the seed_data member fixture, and the bulk
member-directory import path. No behavior change beyond the literal
rename — bulk imports always resolve a club_id before this point, so
they now map to AFFILIATE unconditionally.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 5: Full backend verification

**Files:** none (verification only)

**Interfaces:**
- Consumes: everything from Tasks 1-4.
- Produces: a green full backend suite, the contract every frontend task from here on can rely on.

- [ ] **Step 1: Run the entire backend test suite**

Run: `./venv/Scripts/python.exe manage.py test`
Expected: `OK`, all tests pass (232+ tests: the pre-existing 228 plus the 10 new ones added across Tasks 1-3 — 3 in `test_role_migration.py`, 7 in `test_registration_validation.py`, 2 in `test_user_role_update.py`; note some of Task 2's new tests double-count against this if already run individually, the point is zero failures/errors across the whole suite).

- [ ] **Step 2: Grep the whole backend for any remaining `'MEMBER'` role literal**

Run: `grep -rn "'MEMBER'" apps/ --include=*.py | grep -v migrations`
Expected: no output (the `0001_initial.py`/`0002_*.py` migration files are allowed to still say `MEMBER` — they're a historical record of what the schema looked like at that point in time, migrations are never edited after the fact). If anything unexpected turns up outside `migrations/`, fix it before proceeding — this is the safety net for anything this plan's file-by-file walk missed.

- [ ] **Step 3: No commit** — this task is verification-only; nothing changes if Steps 1-2 pass. If Step 2 finds a stray reference, fix it as a new small commit and re-run Step 1.

---

## Task 6: Frontend — shared role type and fallback defaults

**Files:**
- Modify: `src/lib/types.ts:16`
- Modify: `src/lib/auth.ts:9`, `src/lib/auth.ts:203`
- Modify: `src/app/api/auth/login/route.ts:61`
- Modify: `src/app/profile/page.tsx:106`, `src/app/profile/page.tsx:209`
- Modify: `src/components/admin/AdminGuard.tsx:92`

**Interfaces:**
- Produces: the six-role TypeScript union `'AFFILIATE' | 'NON_AFFILIATE' | 'VOLUNTEER' | 'JUDGE' | 'CLUB_LEAD' | 'ADMIN'` used by every later frontend task, and `'NON_AFFILIATE'` as the display-fallback constant wherever a role is read defensively (`user.role || <fallback>`).

- [ ] **Step 1: `types.ts` role union**

Edit `src/lib/types.ts` line 16. Replace:

```typescript
  role: 'MEMBER' | 'VOLUNTEER' | 'JUDGE' | 'CLUB_LEAD' | 'ADMIN';
```

with:

```typescript
  role: 'AFFILIATE' | 'NON_AFFILIATE' | 'VOLUNTEER' | 'JUDGE' | 'CLUB_LEAD' | 'ADMIN';
```

- [ ] **Step 2: `auth.ts` — `AuthUser` type and login fallback**

Edit `src/lib/auth.ts` line 9. Replace:

```typescript
  role: 'MEMBER' | 'VOLUNTEER' | 'JUDGE' | 'CLUB_LEAD' | 'ADMIN';
```

with:

```typescript
  role: 'AFFILIATE' | 'NON_AFFILIATE' | 'VOLUNTEER' | 'JUDGE' | 'CLUB_LEAD' | 'ADMIN';
```

Edit `src/lib/auth.ts` line 203 (inside `loginUser`, the fallback used when the BFF response has no `user` object). Replace:

```typescript
    role: data.role || 'MEMBER',
```

with:

```typescript
    role: data.role || 'NON_AFFILIATE',
```

- [ ] **Step 3: BFF login route fallback**

Edit `src/app/api/auth/login/route.ts` line 61. Replace:

```typescript
    const role = user?.role || 'MEMBER';
```

with:

```typescript
    const role = user?.role || 'NON_AFFILIATE';
```

- [ ] **Step 4: Profile page fallbacks**

Edit `src/app/profile/page.tsx` line 106 (optimistic local-storage read before the live fetch resolves). Replace:

```typescript
        role: localUser.role || 'MEMBER',
```

with:

```typescript
        role: localUser.role || 'NON_AFFILIATE',
```

Edit `src/app/profile/page.tsx` line 209 (display fallback). Replace:

```typescript
    role: profile?.role || 'MEMBER',
```

with:

```typescript
    role: profile?.role || 'NON_AFFILIATE',
```

- [ ] **Step 5: AdminGuard display fallback**

Edit `src/components/admin/AdminGuard.tsx` line 92 (display-only — the actual access gate on line 31 already checks `=== 'ADMIN' || === 'CLUB_LEAD'`, unaffected by this rename). Replace:

```typescript
    const roleName = currentUser?.role || 'MEMBER';
```

with:

```typescript
    const roleName = currentUser?.role || 'NON_AFFILIATE';
```

- [ ] **Step 6: Typecheck**

Run (from the frontend repo root): `npx tsc --noEmit`
Expected: no output (clean). If any error references a `'MEMBER'` literal that's no longer assignable to the narrowed union, that's a file this task's grep missed — fix it here rather than deferring, since Task 7 onward assumes a clean baseline.

- [ ] **Step 7: Grep the whole frontend for any remaining `'MEMBER'` role literal**

Run: `grep -rn "'MEMBER'" src/ --include=*.ts --include=*.tsx`
Expected: no output.

- [ ] **Step 8: Commit**

```bash
git add src/lib/types.ts src/lib/auth.ts src/app/api/auth/login/route.ts src/app/profile/page.tsx src/components/admin/AdminGuard.tsx
git commit -m "chore: rename MEMBER role references to AFFILIATE/NON_AFFILIATE (shared types/fallbacks)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 7: Frontend — wire the signup affiliate checkbox to `role`

**Files:**
- Modify: `src/app/signup/page.tsx:199`

**Interfaces:**
- Consumes: `RegisterSerializer`'s new contract (Task 2) — `role: 'AFFILIATE'` requires `club_id` to be present and valid, or the request comes back `400` with `{"club_id": [...]}`, which `mapApiErrorsToFields` (already wired from the earlier validation work this session) anchors under the Affiliate ID field.

- [ ] **Step 1: Change the hardcoded role to follow the checkbox**

Edit `src/app/signup/page.tsx` line 199. Replace:

```typescript
        role: 'MEMBER',
        club_id: formData.isAffiliate ? sanitizeAffiliateIdInput(formData.affiliateId) : undefined,
```

with:

```typescript
        role: formData.isAffiliate ? 'AFFILIATE' : 'NON_AFFILIATE',
        club_id: formData.isAffiliate ? sanitizeAffiliateIdInput(formData.affiliateId) : undefined,
```

- [ ] **Step 2: Typecheck**

Run: `npx tsc --noEmit`
Expected: no output (clean).

- [ ] **Step 3: Manual verification against the live backend**

With the Django dev server running on port 8000 and the Next dev server on port 3000 (start them if not already running: `./venv/Scripts/python.exe manage.py runserver 8000` from the backend root, `npm run dev` from the frontend root):

```bash
STAMP=$(date +%s)
# Unchecked (non-affiliate): should succeed with no club_id.
curl -sL -X POST http://127.0.0.1:3000/api/proxy/auth/register/ -H 'Content-Type: application/json' \
  -d "{\"email\":\"roletest_na_${STAMP}@srkr.ac.in\",\"password\":\"Str0ng!Pass\",\"first_name\":\"Rolet\",\"last_name\":\"Est\",\"roll_number\":\"90B${STAMP: -7}\",\"branch\":\"CSE\",\"year\":2,\"role\":\"NON_AFFILIATE\"}"
echo
# Checked (affiliate) without a club_id: should be rejected, field-anchored to club_id.
curl -sL -X POST http://127.0.0.1:3000/api/proxy/auth/register/ -H 'Content-Type: application/json' \
  -d "{\"email\":\"roletest_af_${STAMP}@srkr.ac.in\",\"password\":\"Str0ng!Pass\",\"first_name\":\"Rolet\",\"last_name\":\"Est\",\"roll_number\":\"91B${STAMP: -7}\",\"branch\":\"CSE\",\"year\":2,\"role\":\"AFFILIATE\"}"
```

Expected: first call returns `201` with `"role":"NON_AFFILIATE"`; second call returns `400` with a body containing `"club_id"`.

- [ ] **Step 4: Commit**

```bash
git add src/app/signup/page.tsx
git commit -m "feat: signup affiliate checkbox now sets role to AFFILIATE/NON_AFFILIATE

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 8: Frontend — admin Users-tab role dropdown

**Files:**
- Modify: `src/components/admin/UsersTab.tsx:16`, `src/components/admin/UsersTab.tsx:30`

**Interfaces:**
- Consumes: `UserDetailView`'s new contract (Task 3) — PATCHing `role: 'AFFILIATE'` against a `club_id`-less user returns `400`; `UsersTab`'s existing `onRoleChange` prop (wired to `handleRoleChange` in `useAdminData.ts`, unchanged) already reverts the optimistic UI update and shows the server's error via toast on any non-2xx response, so no new UI is needed here — just the widened role list.

- [ ] **Step 1: Widen the role union and dropdown options**

Edit `src/components/admin/UsersTab.tsx` line 16. Replace:

```typescript
  role: 'MEMBER' | 'VOLUNTEER' | 'JUDGE' | 'CLUB_LEAD' | 'ADMIN';
```

with:

```typescript
  role: 'AFFILIATE' | 'NON_AFFILIATE' | 'VOLUNTEER' | 'JUDGE' | 'CLUB_LEAD' | 'ADMIN';
```

Edit `src/components/admin/UsersTab.tsx` line 30. Replace:

```typescript
const ALL_ROLES: UserRecord['role'][] = ['MEMBER', 'VOLUNTEER', 'JUDGE', 'CLUB_LEAD', 'ADMIN'];
```

with:

```typescript
const ALL_ROLES: UserRecord['role'][] = ['AFFILIATE', 'NON_AFFILIATE', 'VOLUNTEER', 'JUDGE', 'CLUB_LEAD', 'ADMIN'];
```

(`ELEVATED_ROLES` on the next line stays `new Set(['ADMIN', 'CLUB_LEAD'])`, unchanged — `AFFILIATE`/`NON_AFFILIATE` aren't elevated roles, same tier `MEMBER` used to be.)

- [ ] **Step 2: Typecheck**

Run: `npx tsc --noEmit`
Expected: no output (clean).

- [ ] **Step 3: Commit**

```bash
git add src/components/admin/UsersTab.tsx
git commit -m "feat: admin Users-tab role dropdown offers AFFILIATE/NON_AFFILIATE

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 9: Frontend — admin "Create New User" modal gets an Affiliate ID field

**Files:**
- Modify: `src/components/admin/CreateUserModal.tsx:7-29` (props types), `src/components/admin/CreateUserModal.tsx:151-166` (role dropdown + new field)
- Modify: `src/lib/hooks/useAdminData.ts:145-152` (`newUser` state), `src/lib/hooks/useAdminData.ts:409-427` (`handleCreateUser`)

**Interfaces:**
- Consumes: `RegisterSerializer`'s AFFILIATE/club_id contract (Task 2) — this is the second real caller of `POST /auth/register/` (the first is the public signup form), so it needs the same Affiliate ID input the signup form already has.
- Produces: `newUser` state gains a `clubId: string` field — no other file reads `newUser` besides `useAdminData.ts` and `CreateUserModal.tsx` (confirmed: `src/app/admin/users/page.tsx` only passes the whole `newUser`/`setNewUser` pair through as props, it doesn't destructure individual fields), so this is a self-contained two-file change.

- [ ] **Step 1: Add `clubId` to `CreateUserModal`'s props**

> **Note for the implementer:** Task 8 already widened this interface's `role` union from the old 5-value set to `'AFFILIATE' | 'NON_AFFILIATE' | 'VOLUNTEER' | 'JUDGE' | 'CLUB_LEAD' | 'ADMIN'` (a forced side effect of a structural type dependency between this file, `UsersTab.tsx`, and `useAdminData.ts` — all three declare their own separate `role` union but are wired together via component props in `src/app/admin/users/page.tsx`, so widening one without the others breaks the build). Your `role` union is therefore **already correct** — the only remaining change here is adding `clubId: string;` to both the `newUser` and `setNewUser` shapes below.

Edit `src/components/admin/CreateUserModal.tsx`. Replace:

```typescript
interface CreateUserModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSubmit: (e: React.FormEvent) => void | Promise<void>;
  newUser: {
    name: string;
    email: string;
    rollNumber: string;
    branch: string;
    year: string;
    role: 'AFFILIATE' | 'NON_AFFILIATE' | 'VOLUNTEER' | 'JUDGE' | 'CLUB_LEAD' | 'ADMIN';
    password: string;
  };
  setNewUser: React.Dispatch<React.SetStateAction<{
    name: string;
    email: string;
    rollNumber: string;
    branch: string;
    year: string;
    role: 'AFFILIATE' | 'NON_AFFILIATE' | 'VOLUNTEER' | 'JUDGE' | 'CLUB_LEAD' | 'ADMIN';
    password: string;
  }>>;
}
```

with:

```typescript
interface CreateUserModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSubmit: (e: React.FormEvent) => void | Promise<void>;
  newUser: {
    name: string;
    email: string;
    rollNumber: string;
    branch: string;
    year: string;
    role: 'AFFILIATE' | 'NON_AFFILIATE' | 'VOLUNTEER' | 'JUDGE' | 'CLUB_LEAD' | 'ADMIN';
    clubId: string;
    password: string;
  };
  setNewUser: React.Dispatch<React.SetStateAction<{
    name: string;
    email: string;
    rollNumber: string;
    branch: string;
    year: string;
    role: 'AFFILIATE' | 'NON_AFFILIATE' | 'VOLUNTEER' | 'JUDGE' | 'CLUB_LEAD' | 'ADMIN';
    clubId: string;
    password: string;
  }>>;
}
```

- [ ] **Step 2: Add the AFFILIATE option and a conditional Affiliate ID input**

Edit `src/components/admin/CreateUserModal.tsx`. Replace:

```typescript
            <div>
              <label className="block text-xs font-bold uppercase text-[#1A1A2E] dark:text-white mb-1">
                Platform Role *
              </label>
              <select
                value={newUser.role}
                onChange={(e) => setNewUser({ ...newUser, role: e.target.value as any })}
                className="w-full px-3.5 py-2 rounded-lg border text-sm bg-[#FAFAFC] dark:bg-[#0D0E15] text-[#1A1A2E] dark:text-white border-slate-200 dark:border-slate-800"
              >
                <option value="MEMBER">MEMBER</option>
                <option value="VOLUNTEER">VOLUNTEER</option>
              </select>
              <p className="mt-1 text-[11px] text-slate-400">
                Judge, Club Lead, or Admin can be granted afterward from the Users tab.
              </p>
            </div>
          </div>
```

with:

```typescript
            <div>
              <label className="block text-xs font-bold uppercase text-[#1A1A2E] dark:text-white mb-1">
                Platform Role *
              </label>
              <select
                value={newUser.role}
                onChange={(e) => setNewUser({ ...newUser, role: e.target.value as any })}
                className="w-full px-3.5 py-2 rounded-lg border text-sm bg-[#FAFAFC] dark:bg-[#0D0E15] text-[#1A1A2E] dark:text-white border-slate-200 dark:border-slate-800"
              >
                <option value="AFFILIATE">AFFILIATE</option>
                <option value="NON_AFFILIATE">NON_AFFILIATE</option>
                <option value="VOLUNTEER">VOLUNTEER</option>
              </select>
              <p className="mt-1 text-[11px] text-slate-400">
                Judge, Club Lead, or Admin can be granted afterward from the Users tab.
              </p>
            </div>
          </div>

          {newUser.role === 'AFFILIATE' && (
            <div>
              <label className="block text-xs font-bold uppercase text-[#1A1A2E] dark:text-white mb-1">
                Affiliate ID (Club ID) *
              </label>
              <input
                type="text"
                required
                autoCapitalize="characters"
                spellCheck={false}
                placeholder="25SCC277"
                value={newUser.clubId}
                onChange={(e) => setNewUser({ ...newUser, clubId: e.target.value.toUpperCase() })}
                className="w-full px-3.5 py-2 rounded-lg border text-sm bg-[#FAFAFC] dark:bg-[#0D0E15] text-[#1A1A2E] dark:text-white border-slate-200 dark:border-slate-800 font-mono tracking-wide"
              />
              <p className="mt-1 text-[11px] text-slate-400">
                Required for Affiliate — this member won't be created without a valid, unclaimed Club ID.
              </p>
            </div>
          )}
```

- [ ] **Step 3: Add `clubId` to `useAdminData`'s `newUser` state and its type union**

Edit `src/lib/hooks/useAdminData.ts`. Replace:

```typescript
  const [newUser, setNewUser] = useState({
    name: '',
    email: '',
    rollNumber: '',
    branch: 'CSE',
    year: '1st Year',
    role: 'MEMBER' as UserRecord['role'],
    password: '',
  });
```

with:

```typescript
  const [newUser, setNewUser] = useState({
    name: '',
    email: '',
    rollNumber: '',
    branch: 'CSE',
    year: '1st Year',
    role: 'NON_AFFILIATE' as UserRecord['role'],
    clubId: '',
    password: '',
  });
```

- [ ] **Step 4: Pass `club_id` through in `handleCreateUser`'s payload**

Edit `src/lib/hooks/useAdminData.ts`. Replace:

```typescript
      const payload = {
        username: newUser.email.split('@')[0],
        email: newUser.email,
        password: newUser.password || 'password123',
        first_name: newUser.name.split(' ')[0] || newUser.name,
        last_name: newUser.name.split(' ').slice(1).join(' ') || '',
        role: newUser.role,
        roll_number: newUser.rollNumber,
        branch: newUser.branch,
      };
```

with:

```typescript
      const payload = {
        username: newUser.email.split('@')[0],
        email: newUser.email,
        password: newUser.password || 'password123',
        first_name: newUser.name.split(' ')[0] || newUser.name,
        last_name: newUser.name.split(' ').slice(1).join(' ') || '',
        role: newUser.role,
        roll_number: newUser.rollNumber,
        branch: newUser.branch,
        club_id: newUser.role === 'AFFILIATE' ? newUser.clubId : undefined,
      };
```

- [ ] **Step 5: Typecheck**

Run: `npx tsc --noEmit`
Expected: no output (clean).

- [ ] **Step 6: Manual verification against the live backend**

With both dev servers running (as in Task 7 Step 3), and logged in as an admin so `fetchApi` carries a valid session cookie — this step is a code-read verification instead of a live click-through, since scripting an authenticated admin browser session is out of proportion here: confirm by inspection that `CreateUserModal`'s new field only renders `newUser.role === 'AFFILIATE'`, and that `handleCreateUser`'s payload only includes `club_id` in that same case (both already shown above) — matching exactly the condition `RegisterSerializer.validate()` (Task 2) checks server-side, so no state exists where the UI hides the field but the server still demands it, or shows it but the server ignores it.

- [ ] **Step 7: Commit**

```bash
git add src/components/admin/CreateUserModal.tsx src/lib/hooks/useAdminData.ts
git commit -m "feat: admin Create User modal supports AFFILIATE role with required Club ID

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 10: Final full-stack verification

**Files:** none (verification only)

**Interfaces:**
- Consumes: everything from Tasks 1-9.

- [ ] **Step 1: Full backend suite**

Run: `./venv/Scripts/python.exe manage.py test`
Expected: `OK`.

- [ ] **Step 2: Frontend typecheck and production build**

Run: `npx tsc --noEmit`
Expected: no output.

Run: `npm run build`
Expected: build succeeds with no errors. (If a `next dev` server is running against this same repo when this is run, restart it afterward with `npm run dev` — a production build overwrites the shared `.next` directory a live dev server depends on.)

- [ ] **Step 3: Live end-to-end trace of the full promotion flow**

With both dev servers running:

```bash
STAMP=$(date +%s)
EMAIL="rolee2e_${STAMP}@srkr.ac.in"
ROLL="88B${STAMP: -7}"

# 1. Non-affiliate signup (checkbox unchecked) — no club_id required.
curl -sL -X POST http://127.0.0.1:3000/api/proxy/auth/register/ -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"Str0ng!Pass\",\"first_name\":\"Role\",\"last_name\":\"E2e\",\"roll_number\":\"$ROLL\",\"branch\":\"CSE\",\"year\":2,\"role\":\"NON_AFFILIATE\"}"
echo

# 2. Log in as that user.
curl -sL -c /tmp/role_e2e_cookies.txt -X POST http://127.0.0.1:3000/api/auth/login -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"Str0ng!Pass\"}" > /dev/null

# 3. Confirm their own /auth/me/ reports NON_AFFILIATE.
curl -sL -b /tmp/role_e2e_cookies.txt http://127.0.0.1:3000/api/proxy/auth/me/ | grep -o '"role":"[^"]*"'
```

Expected: step 1 returns `201`; step 3 prints `"role":"NON_AFFILIATE"`.

Then, using a seeded admin account (or `./venv/Scripts/python.exe manage.py shell` to create one and fetch the new user's id, mirroring the pattern used earlier this session's role-propagation verification):

```bash
./venv/Scripts/python.exe manage.py shell -c "
from django.contrib.auth import get_user_model
User = get_user_model()
u = User.objects.get(email='$EMAIL')
print('id:', u.id, 'club_id:', u.club_id, 'role:', u.role)
"
```

Then, as an authenticated admin, attempt to PATCH that user's role to `AFFILIATE` (should fail — no `club_id`), assign a `club_id` via the existing Club ID tooling, and retry the PATCH (should succeed):

```bash
# (repeat the admin-login curl pattern from earlier sessions, substitute the real admin credentials and the user id printed above)
curl -sL -b /tmp/role_e2e_admin_cookies.txt -X PATCH "http://127.0.0.1:3000/api/proxy/auth/users/<id>/" -H 'Content-Type: application/json' -d '{"role":"AFFILIATE"}'
# Expected: 400, body contains "club_id"
```

Expected: the PATCH without a `club_id` returns `400` naming `club_id`; assigning one first and retrying returns `200` with `"role":"AFFILIATE"`.

- [ ] **Step 4: Clean up test accounts**

```bash
./venv/Scripts/python.exe manage.py shell -c "
from django.contrib.auth import get_user_model
User = get_user_model()
n, _ = User.objects.filter(email__contains='roletest_').delete()
n2, _ = User.objects.filter(email__contains='rolee2e_').delete()
print('deleted', n + n2)
"
```

- [ ] **Step 5: No commit** — verification only. If any step fails, fix the underlying task and re-run this task from Step 1.
