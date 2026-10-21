"""Serverless flags must not change Render/local defaults when unset."""
import os
import socket
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
    rewrite_direct_database_url,
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

    def test_serverless_pooler_omits_statement_timeout(self):
        env = {k: v for k, v in os.environ.items() if k != 'AWS_LAMBDA_FUNCTION_NAME'}
        env['SERVERLESS'] = '1'
        incoming = {
            **self._POSTGRES,
            'HOST': 'ep-example-pooler.ap-southeast-1.aws.neon.tech',
            'PORT': 5432,
        }
        fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('13.228.184.177', 5432))]
        with mock.patch.dict(os.environ, env, clear=True), mock.patch('config.settings.socket.getaddrinfo', return_value=fake):
            config = apply_database_options(incoming)
        self.assertEqual(config['OPTIONS']['connect_timeout'], POSTGRES_CONNECT_TIMEOUT_SECONDS)
        self.assertNotIn('options', config['OPTIONS'])
        self.assertEqual(config['OPTIONS']['hostaddr'], '13.228.184.177')

    def test_direct_flag_rewrites_pooler_host_before_timeouts(self):
        env = {k: v for k, v in os.environ.items() if k != 'AWS_LAMBDA_FUNCTION_NAME'}
        env['SERVERLESS'] = '1'
        env['DATABASE_USE_DIRECT'] = '1'
        incoming = {
            **self._POSTGRES,
            'HOST': 'ep-example-pooler.ap-southeast-1.aws.neon.tech',
        }
        fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('52.220.170.93', 5432))]
        with mock.patch.dict(os.environ, env, clear=True), mock.patch('config.settings.socket.getaddrinfo', return_value=fake) as lookup:
            config = apply_database_options(incoming)
        self.assertEqual(config['HOST'], 'ep-example.ap-southeast-1.aws.neon.tech')
        self.assertIn('statement_timeout', config['OPTIONS']['options'])
        self.assertEqual(lookup.call_args.args[0], 'ep-example.ap-southeast-1.aws.neon.tech')

    def test_serverless_pins_postgres_to_ipv4(self):
        env = {k: v for k, v in os.environ.items() if k != 'AWS_LAMBDA_FUNCTION_NAME'}
        env['SERVERLESS'] = '1'
        incoming = {**self._POSTGRES, 'HOST': 'ep-example-pooler.ap-southeast-1.aws.neon.tech', 'PORT': 5432}
        fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('13.228.184.177', 5432))]
        with mock.patch.dict(os.environ, env, clear=True), mock.patch('config.settings.socket.getaddrinfo', return_value=fake) as lookup:
            config = apply_database_options(incoming)
        lookup.assert_called_once()
        self.assertEqual(config['HOST'], 'ep-example-pooler.ap-southeast-1.aws.neon.tech')
        self.assertEqual(config['OPTIONS']['hostaddr'], '13.228.184.177')

    def test_non_serverless_does_not_pin_ipv4(self):
        env = {k: v for k, v in os.environ.items() if k not in ('SERVERLESS', 'AWS_LAMBDA_FUNCTION_NAME')}
        incoming = {**self._POSTGRES, 'HOST': 'ep-example.ap-southeast-1.aws.neon.tech', 'CONN_MAX_AGE': 600}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch('config.settings.socket.getaddrinfo') as lookup:
            config = apply_database_options(incoming)
        lookup.assert_not_called()
        self.assertNotIn('hostaddr', config.get('OPTIONS') or {})

    def test_serverless_sqlite_is_left_without_postgres_options(self):
        env = {k: v for k, v in os.environ.items() if k != 'AWS_LAMBDA_FUNCTION_NAME'}
        env['SERVERLESS'] = '1'
        sqlite = {'ENGINE': 'django.db.backends.sqlite3', 'NAME': '/tmp/build.db', 'CONN_MAX_AGE': 0}
        with mock.patch.dict(os.environ, env, clear=True):
            config = apply_database_options(sqlite)
        self.assertFalse(config['CONN_HEALTH_CHECKS'])
        self.assertNotIn('connect_timeout', config.get('OPTIONS') or {})


class DirectDatabaseUrlTests(SimpleTestCase):
    _POOLER = (
        'postgresql://neondb_owner:secret@ep-example-pooler.ap-southeast-1.aws.neon.tech/neondb'
        '?sslmode=require&channel_binding=require'
    )

    def test_direct_flag_strips_only_the_pooler_suffix(self):
        with mock.patch.dict(os.environ, {'DATABASE_USE_DIRECT': '1'}, clear=False):
            rewritten = rewrite_direct_database_url(self._POOLER)
        self.assertIn('neondb_owner:secret@ep-example.ap-southeast-1.aws.neon.tech/neondb', rewritten)
        self.assertNotIn('-pooler', rewritten)
        self.assertIn('sslmode=require', rewritten)
        self.assertIn('channel_binding=require', rewritten)

    def test_without_flag_pooler_url_is_unchanged(self):
        env = {k: v for k, v in os.environ.items() if k != 'DATABASE_USE_DIRECT'}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(rewrite_direct_database_url(self._POOLER), self._POOLER)

    def test_direct_flag_leaves_non_pooler_urls_unchanged(self):
        local = 'postgres://postgres:postgres@localhost:5432/srkrcc_db'
        with mock.patch.dict(os.environ, {'DATABASE_USE_DIRECT': '1'}, clear=False):
            self.assertEqual(rewrite_direct_database_url(local), local)
