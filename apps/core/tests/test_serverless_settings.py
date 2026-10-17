"""Serverless flags must not change Render/local defaults when unset."""
import os
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from config.settings import (
    BASE_DIR,
    POSTGRES_CONNECT_TIMEOUT_SECONDS,
    POSTGRES_STATEMENT_TIMEOUT_MS,
    _is_serverless,
    apply_database_options,
    database_conn_max_age,
    resolve_media_root,
)


def _without(key: str) -> dict:
    return {k: v for k, v in os.environ.items() if k != key}


class ServerlessSettingsTests(SimpleTestCase):
    def test_not_serverless_when_flags_absent(self):
        env = {k: v for k, v in os.environ.items() if k not in ('SERVERLESS', 'AWS_LAMBDA_FUNCTION_NAME')}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(_is_serverless())
            self.assertEqual(database_conn_max_age(), 600)
            self.assertEqual(resolve_media_root(), BASE_DIR / 'media')

    def test_serverless_flag_disables_persistent_connections(self):
        env = _without('AWS_LAMBDA_FUNCTION_NAME')
        env['SERVERLESS'] = '1'
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertTrue(_is_serverless())
            self.assertEqual(database_conn_max_age(), 0)
            self.assertEqual(resolve_media_root(), Path('/tmp/media'))

    def test_lambda_function_name_implies_serverless(self):
        with mock.patch.dict(os.environ, {'AWS_LAMBDA_FUNCTION_NAME': 'srkrcc-prod-api'}, clear=False):
            self.assertTrue(_is_serverless())
            self.assertEqual(database_conn_max_age(), 0)
            self.assertEqual(resolve_media_root(), Path('/tmp/media'))

    def test_loaded_settings_match_non_serverless_defaults(self):
        """The Django test process does not set SERVERLESS, so imported settings
        must keep the Render/local connection and media paths."""
        self.assertNotIn(os.getenv('SERVERLESS', '').lower(), ('true', '1', 't'))
        self.assertFalse(os.getenv('AWS_LAMBDA_FUNCTION_NAME'))
        self.assertEqual(settings.DATABASES['default']['CONN_MAX_AGE'], 600)
        self.assertFalse(settings.DATABASES['default'].get('CONN_HEALTH_CHECKS', False))
        self.assertEqual(Path(settings.MEDIA_ROOT), BASE_DIR / 'media')


class DatabaseTimeoutSettingsTests(SimpleTestCase):
    _POSTGRES = {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'srkrcc_db',
        'CONN_MAX_AGE': 0,
        'OPTIONS': {},
    }

    def test_non_serverless_does_not_add_postgres_timeouts(self):
        env = {k: v for k, v in os.environ.items() if k not in ('SERVERLESS', 'AWS_LAMBDA_FUNCTION_NAME')}
        with mock.patch.dict(os.environ, env, clear=True):
            config = apply_database_options({**self._POSTGRES, 'CONN_MAX_AGE': 600})
        self.assertEqual(config['CONN_MAX_AGE'], 600)
        self.assertFalse(config['CONN_HEALTH_CHECKS'])
        self.assertNotIn('connect_timeout', config.get('OPTIONS') or {})

    def test_serverless_adds_connect_and_statement_timeouts(self):
        env = {k: v for k, v in os.environ.items() if k != 'AWS_LAMBDA_FUNCTION_NAME'}
        env['SERVERLESS'] = '1'
        with mock.patch.dict(os.environ, env, clear=True):
            config = apply_database_options(dict(self._POSTGRES))
        self.assertEqual(config['CONN_MAX_AGE'], 0)
        self.assertFalse(config['CONN_HEALTH_CHECKS'])
        self.assertEqual(config['OPTIONS']['connect_timeout'], POSTGRES_CONNECT_TIMEOUT_SECONDS)
        self.assertIn(
            f'statement_timeout={POSTGRES_STATEMENT_TIMEOUT_MS}',
            config['OPTIONS']['options'],
        )

    def test_serverless_does_not_duplicate_existing_statement_timeout(self):
        env = {k: v for k, v in os.environ.items() if k != 'AWS_LAMBDA_FUNCTION_NAME'}
        env['SERVERLESS'] = '1'
        incoming = {
            **self._POSTGRES,
            'OPTIONS': {'options': '-c statement_timeout=2000', 'connect_timeout': 3},
        }
        with mock.patch.dict(os.environ, env, clear=True):
            config = apply_database_options(incoming)
        self.assertEqual(config['OPTIONS']['connect_timeout'], 3)
        self.assertEqual(config['OPTIONS']['options'].count('statement_timeout'), 1)
        self.assertIn('statement_timeout=2000', config['OPTIONS']['options'])

    def test_serverless_sqlite_is_left_without_postgres_options(self):
        env = {k: v for k, v in os.environ.items() if k != 'AWS_LAMBDA_FUNCTION_NAME'}
        env['SERVERLESS'] = '1'
        sqlite = {'ENGINE': 'django.db.backends.sqlite3', 'NAME': '/tmp/build.db', 'CONN_MAX_AGE': 0}
        with mock.patch.dict(os.environ, env, clear=True):
            config = apply_database_options(sqlite)
        self.assertFalse(config['CONN_HEALTH_CHECKS'])
        self.assertNotIn('connect_timeout', config.get('OPTIONS') or {})
