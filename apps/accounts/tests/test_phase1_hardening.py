import logging
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.audit.utils import log_audit_event
from apps.core.dev_guard import DevelopmentOnlyError, require_development

User = get_user_model()
STRONG = 'Tr0ub4dor&3-horse-battery'


def run_setup_admin(*args, **env):
    out, err = StringIO(), StringIO()
    with mock.patch.dict('os.environ', env, clear=False):
        call_command('setup_admin', *args, stdout=out, stderr=err)
    return out.getvalue() + err.getvalue()


class SetupAdminTests(TestCase):
    @override_settings(DEBUG=False)
    def test_production_refuses_to_run_without_a_password(self):
        with mock.patch.dict('os.environ', {}, clear=False):
            import os
            os.environ.pop('DJANGO_SUPERUSER_PASSWORD', None)
            with self.assertRaisesMessage(CommandError, 'DJANGO_SUPERUSER_PASSWORD'):
                call_command('setup_admin', stdout=StringIO())
        self.assertFalse(User.objects.filter(is_superuser=True).exists())

    @override_settings(DEBUG=False)
    def test_production_refuses_well_known_passwords(self):
        for weak in ('Admin@123', 'password123', 'ADMIN123'):
            with self.assertRaises(CommandError):
                run_setup_admin(DJANGO_SUPERUSER_PASSWORD=weak)

    @override_settings(DEBUG=False)
    def test_production_creates_the_admin_with_the_configured_password(self):
        run_setup_admin(DJANGO_SUPERUSER_PASSWORD=STRONG)
        admin = User.objects.get(email='admin@srkr.ac.in')
        self.assertTrue(admin.check_password(STRONG))
        self.assertTrue(admin.is_superuser)

    @override_settings(DEBUG=True)
    def test_development_falls_back_with_a_warning(self):
        import os
        os.environ.pop('DJANGO_SUPERUSER_PASSWORD', None)
        output = run_setup_admin()
        self.assertIn('development password', output)
        self.assertTrue(User.objects.get(email='admin@srkr.ac.in').check_password('Admin@123'))

    @override_settings(DEBUG=False)
    def test_an_existing_admins_password_survives_redeploys(self):
        run_setup_admin(DJANGO_SUPERUSER_PASSWORD=STRONG)
        admin = User.objects.get(email='admin@srkr.ac.in')
        admin.set_password('Changed-by-admin-9!x')
        admin.save()
        run_setup_admin(DJANGO_SUPERUSER_PASSWORD=STRONG)
        admin.refresh_from_db()
        self.assertTrue(admin.check_password('Changed-by-admin-9!x'))

    @override_settings(DEBUG=False)
    def test_an_existing_admin_does_not_fail_a_redeploy_over_a_weak_environment_password(self):
        run_setup_admin(DJANGO_SUPERUSER_PASSWORD=STRONG)
        admin = User.objects.get(email='admin@srkr.ac.in')
        output = run_setup_admin(DJANGO_SUPERUSER_PASSWORD='Admin@123')
        self.assertIn('already exists', output)
        admin.refresh_from_db()
        self.assertTrue(admin.check_password(STRONG))
        self.assertEqual(User.objects.filter(is_superuser=True).count(), 1)

    @override_settings(DEBUG=False)
    def test_an_existing_admin_does_not_need_a_password_in_the_environment(self):
        import os
        run_setup_admin(DJANGO_SUPERUSER_PASSWORD=STRONG)
        os.environ.pop('DJANGO_SUPERUSER_PASSWORD', None)
        self.assertIn('already exists', run_setup_admin())

    @override_settings(DEBUG=False)
    def test_resetting_still_rejects_a_weak_password(self):
        run_setup_admin(DJANGO_SUPERUSER_PASSWORD=STRONG)
        with self.assertRaises(CommandError):
            run_setup_admin('--reset-password', '--password', 'Admin@123')
        self.assertTrue(User.objects.get(email='admin@srkr.ac.in').check_password(STRONG))

    @override_settings(DEBUG=False)
    def test_reset_password_flag_replaces_it_on_request(self):
        run_setup_admin(DJANGO_SUPERUSER_PASSWORD=STRONG)
        run_setup_admin('--reset-password', '--password', 'An0ther-Strong-pass-77!')
        self.assertTrue(User.objects.get(email='admin@srkr.ac.in').check_password('An0ther-Strong-pass-77!'))


class DevOnlySeedersTests(TestCase):
    @override_settings(DEBUG=False)
    def test_guard_refuses_in_production(self):
        with self.assertRaises(DevelopmentOnlyError):
            require_development('a seeder')

    @override_settings(DEBUG=True)
    def test_guard_allows_development(self):
        require_development('a seeder')

    @override_settings(DEBUG=False)
    def test_seed_data_command_refuses_in_production(self):
        with self.assertRaises(CommandError):
            call_command('seed_data', stdout=StringIO())
        self.assertFalse(User.objects.exists())


class PasswordSetupConfirmLeakTests(TestCase):
    def test_unexpected_errors_are_logged_not_returned(self):
        client = APIClient()
        secret = 'psycopg2 OperationalError: connection to 10.0.0.5 refused'
        with mock.patch(
            'apps.accounts.views_password_setup.PasswordSetupService.confirm_password_setup',
            side_effect=RuntimeError(secret),
        ), self.assertLogs('apps.accounts.views_password_setup', level='ERROR'):
            response = client.post('/api/auth/setup-password/confirm/', {'token': 't', 'password': STRONG}, format='json')
        self.assertEqual(response.status_code, 500)
        self.assertNotIn('10.0.0.5', str(response.data))
        self.assertNotIn('psycopg2', str(response.data))


class AuditFailureLoggingTests(TestCase):
    def test_a_failed_audit_write_is_logged_and_does_not_raise(self):
        with mock.patch('apps.audit.utils.AuditLog.objects.create', side_effect=RuntimeError('db down')), \
                self.assertLogs('apps.audit.utils', level='ERROR') as logs:
            result = log_audit_event(action='Did Something', target_model='Thing', target_id=7)
        self.assertIsNone(result)
        self.assertTrue(any('Did Something' in line for line in logs.output))
