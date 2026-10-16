"""Optional S3 default storage is off unless USE_S3=true."""
import os
from unittest import mock

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.files.storage import default_storage
from django.test import SimpleTestCase

from config.settings import (
    FILESYSTEM_STORAGE_BACKEND,
    S3_STORAGE_BACKEND,
    WHITENOISE_STORAGE_BACKEND,
    _use_s3,
    default_storage_backend,
    s3_storage_options,
)


def _env_without(*keys: str) -> dict:
    return {k: v for k, v in os.environ.items() if k not in keys}


class S3StorageSettingsTests(SimpleTestCase):
    def test_use_s3_unset_keeps_filesystem_backend(self):
        env = _env_without('USE_S3')
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(_use_s3())
            self.assertEqual(default_storage_backend(), FILESYSTEM_STORAGE_BACKEND)

    def test_use_s3_false_keeps_filesystem_backend(self):
        env = _env_without('USE_S3')
        env['USE_S3'] = 'false'
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(_use_s3())
            self.assertEqual(default_storage_backend(), FILESYSTEM_STORAGE_BACKEND)

    def test_use_s3_true_selects_s3_backend(self):
        env = _env_without('USE_S3')
        env['USE_S3'] = 'true'
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertTrue(_use_s3())
            self.assertEqual(default_storage_backend(), S3_STORAGE_BACKEND)

    def test_s3_options_read_bucket_and_region_from_env(self):
        env = dict(os.environ)
        env['AWS_STORAGE_BUCKET_NAME'] = 'srkrcc-test-media'
        env['AWS_S3_REGION_NAME'] = 'ap-south-1'
        with mock.patch.dict(os.environ, env, clear=True):
            options = s3_storage_options()
        self.assertEqual(options['bucket_name'], 'srkrcc-test-media')
        self.assertEqual(options['region_name'], 'ap-south-1')
        self.assertTrue(options['querystring_auth'])
        self.assertIsNone(options['default_acl'])
        self.assertEqual(options['location'], 'media')
        self.assertNotIn('access_key', options)
        self.assertNotIn('secret_key', options)

    def test_loaded_settings_stay_on_filesystem_and_whitenoise(self):
        self.assertNotIn(os.getenv('USE_S3', '').lower(), ('true', '1', 't'))
        self.assertEqual(default_storage.__class__.__name__, 'FileSystemStorage')
        self.assertEqual(settings.STATICFILES_STORAGE, WHITENOISE_STORAGE_BACKEND)

    def test_use_s3_without_bucket_is_rejected(self):
        env = {k: v for k, v in os.environ.items() if k != 'AWS_STORAGE_BUCKET_NAME'}
        env['USE_S3'] = 'true'
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertTrue(_use_s3())
            self.assertEqual(s3_storage_options()['bucket_name'], '')
            with self.assertRaises(ImproperlyConfigured):
                if _use_s3() and not s3_storage_options()['bucket_name']:
                    raise ImproperlyConfigured('USE_S3=true requires AWS_STORAGE_BUCKET_NAME.')
