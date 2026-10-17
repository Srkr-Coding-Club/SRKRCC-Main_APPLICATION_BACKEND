"""Serverless flags must not change Render/local defaults when unset."""
import os
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from config.settings import (
    BASE_DIR,
    _is_serverless,
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
        self.assertEqual(Path(settings.MEDIA_ROOT), BASE_DIR / 'media')
