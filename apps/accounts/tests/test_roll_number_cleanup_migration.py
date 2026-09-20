"""
Verifies the cleanup rule the 0005_cleanup_duplicate_roll_numbers data
migration applies, ahead of 0006_roll_number_unique's DB-level unique
constraint — written after that constraint failed applying to a real
deployment because of a shared placeholder value ("xyz") in production data.
Tests the rule directly against the current model, following the same
pattern as test_role_migration.py (Django migrations aren't easily
unit-testable without extra tooling this repo doesn't have).

Complication unique to this migration: by the time these tests run, the
CURRENT schema already has the very unique constraint this cleanup exists to
protect (0006_roll_number_unique has already applied to the test database).
Simulating "two rows already share a value" therefore has to drop that
constraint first — done with a raw SQL DROP inside each test's own
transaction, which TestCase rolls back automatically at teardown, so nothing
about the schema actually changes outside the test.
"""
import importlib.util
import os

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase

User = get_user_model()

_MODULE_PATH = os.path.join(
    os.path.dirname(__file__), '..', 'migrations', '0005_cleanup_duplicate_roll_numbers.py'
)
_spec = importlib.util.spec_from_file_location('cleanup_duplicate_roll_numbers_migration', _MODULE_PATH)
_cleanup_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cleanup_module)


class _RealAppsShim:
    """Stands in for the historical-model `apps` argument RunPython receives —
    the migration's own code only calls .get_model(), so proxying straight to
    the real current model (identical shape, no schema changes since) is a
    faithful substitute without needing Django's full migration-state machinery."""

    def get_model(self, app_label, model_name):
        from django.apps import apps as real_apps
        return real_apps.get_model(app_label, model_name)


def run_cleanup():
    _cleanup_module.clear_placeholder_and_duplicate_roll_numbers(_RealAppsShim(), None)


def drop_roll_number_unique_constraint():
    """Simulates pre-0006 schema state within the current test's own
    transaction only — rolled back automatically when the test ends."""
    with connection.cursor() as cursor:
        cursor.execute('ALTER TABLE accounts_user DROP CONSTRAINT accounts_user_roll_number_d4c665ff_uniq')


class RollNumberCleanupMigrationTests(TestCase):
    def test_placeholder_value_is_cleared(self):
        user = User.objects.create_user(username='u1', email='u1@srkr.ac.in', password='pw12345!', roll_number='xyz')
        run_cleanup()
        user.refresh_from_db()
        self.assertIsNone(user.roll_number)

    def test_well_formed_unique_value_is_untouched(self):
        user = User.objects.create_user(
            username='u2', email='u2@srkr.ac.in', password='pw12345!', roll_number='21B91A0501',
        )
        run_cleanup()
        user.refresh_from_db()
        self.assertEqual(user.roll_number, '21B91A0501')

    def test_shared_placeholder_clears_every_holder(self):
        """The exact scenario that broke the real deployment: two different
        accounts sharing the literal value 'xyz'."""
        drop_roll_number_unique_constraint()
        a = User.objects.create_user(username='u3', email='u3@srkr.ac.in', password='pw12345!', roll_number='xyz')
        b = User.objects.create_user(username='u4', email='u4@srkr.ac.in', password='pw12345!', roll_number='xyz')
        run_cleanup()
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertIsNone(a.roll_number)
        self.assertIsNone(b.roll_number)

    def test_safety_net_clears_genuine_duplicate_among_well_formed_values(self):
        """Covers the second pass: two accounts share a value that DOES match
        the 10-char-alphanumeric format, so the placeholder pass alone
        wouldn't catch it — this is the unanticipated-duplicate case the
        deploy's own error log couldn't reveal (Postgres only reports the
        first violation it hits)."""
        drop_roll_number_unique_constraint()
        first = User.objects.create_user(
            username='u5', email='u5@srkr.ac.in', password='pw12345!', roll_number='21B91A0599',
        )
        second = User.objects.create_user(
            username='u6', email='u6@srkr.ac.in', password='pw12345!', roll_number='21B91A0599',
        )
        run_cleanup()
        first.refresh_from_db()
        second.refresh_from_db()
        # Lowest id (created first) is kept; the later duplicate is cleared.
        self.assertEqual(first.roll_number, '21B91A0599')
        self.assertIsNone(second.roll_number)

    def test_result_has_no_duplicates_left_for_the_unique_index(self):
        """End-to-end sanity check: whatever mess goes in, nothing collides
        on the way out — this is what 0006_roll_number_unique's AlterField
        depends on being true."""
        drop_roll_number_unique_constraint()
        User.objects.create_user(username='u7', email='u7@srkr.ac.in', password='pw12345!', roll_number='xyz')
        User.objects.create_user(username='u8', email='u8@srkr.ac.in', password='pw12345!', roll_number='xyz')
        User.objects.create_user(username='u9', email='u9@srkr.ac.in', password='pw12345!', roll_number='abc')
        User.objects.create_user(
            username='u10', email='u10@srkr.ac.in', password='pw12345!', roll_number='21B91A0501',
        )
        User.objects.create_user(
            username='u11', email='u11@srkr.ac.in', password='pw12345!', roll_number='21B91A0501',
        )
        run_cleanup()
        remaining = list(
            User.objects.exclude(roll_number__isnull=True).exclude(roll_number='').values_list('roll_number', flat=True)
        )
        self.assertEqual(len(remaining), len(set(remaining)), f'duplicates survived cleanup: {remaining}')
