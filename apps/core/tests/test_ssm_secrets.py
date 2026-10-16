"""Parameter Store loading is serverless-only and never records secret values."""
import os
import re
from pathlib import Path
from unittest import mock

from botocore.exceptions import ClientError
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from config.settings import BASE_DIR
from config.ssm_secrets import INSECURE_DEFAULT_SECRET_KEY, load_serverless_secrets

SECRET_NAME = '/srkrcc/dev/backend/secret_key'
DATABASE_NAME = '/srkrcc/dev/backend/database_url'
SECRET_VALUE = 'lambda-only-secret-key-value'
DATABASE_VALUE = 'postgres://neon-user:neon-pass@ep.example/neondb'


def _env(**overrides) -> dict:
    env = {
        'AWS_REGION': 'ap-south-1',
    }
    env.update(overrides)
    return env


def _response(parameters=None, invalid=None):
    return {
        'Parameters': parameters or [],
        'InvalidParameters': invalid or [],
    }


class SsmSecretLoadingTests(SimpleTestCase):
    def test_non_serverless_does_not_call_ssm(self):
        env = _env(SECRET_KEY='', DATABASE_URL='')
        with mock.patch('config.ssm_secrets.boto3.client') as client:
            load_serverless_secrets(env)
        client.assert_not_called()
        self.assertEqual(env['SECRET_KEY'], '')
        self.assertEqual(env['DATABASE_URL'], '')

    def test_serverless_with_both_values_does_not_call_ssm(self):
        env = _env(
            SERVERLESS='1',
            SECRET_KEY='already-set-secret',
            DATABASE_URL='postgres://local/db',
        )
        with mock.patch('config.ssm_secrets.boto3.client') as client:
            load_serverless_secrets(env)
        client.assert_not_called()
        self.assertEqual(env['SECRET_KEY'], 'already-set-secret')
        self.assertEqual(env['DATABASE_URL'], 'postgres://local/db')

    def test_lambda_function_name_loads_missing_secrets(self):
        env = _env(AWS_LAMBDA_FUNCTION_NAME='srkrcc-dev-backend')
        client = mock.Mock()
        client.get_parameters.return_value = _response([
            {'Name': SECRET_NAME, 'Value': SECRET_VALUE},
            {'Name': DATABASE_NAME, 'Value': DATABASE_VALUE},
        ])
        with mock.patch('config.ssm_secrets.boto3.client', return_value=client):
            load_serverless_secrets(env)

        client.get_parameters.assert_called_once_with(
            Names=[SECRET_NAME, DATABASE_NAME],
            WithDecryption=True,
        )
        self.assertEqual(env['SECRET_KEY'], SECRET_VALUE)
        self.assertEqual(env['DATABASE_URL'], DATABASE_VALUE)

    def test_insecure_default_secret_key_is_treated_as_missing(self):
        env = _env(
            SERVERLESS='1',
            SECRET_KEY=INSECURE_DEFAULT_SECRET_KEY,
            DATABASE_URL='postgres://local/db',
        )
        client = mock.Mock()
        client.get_parameters.return_value = _response([
            {'Name': SECRET_NAME, 'Value': SECRET_VALUE},
        ])
        with mock.patch('config.ssm_secrets.boto3.client', return_value=client):
            load_serverless_secrets(env)

        client.get_parameters.assert_called_once_with(
            Names=[SECRET_NAME],
            WithDecryption=True,
        )
        self.assertEqual(env['SECRET_KEY'], SECRET_VALUE)
        self.assertEqual(env['DATABASE_URL'], 'postgres://local/db')

    def test_custom_parameter_names_are_used(self):
        env = _env(
            SERVERLESS='1',
            SSM_SECRET_KEY_NAME='/srkrcc/prod/backend/secret_key',
            SSM_DATABASE_URL_NAME='/srkrcc/prod/backend/database_url',
        )
        client = mock.Mock()
        client.get_parameters.return_value = _response([
            {'Name': '/srkrcc/prod/backend/secret_key', 'Value': SECRET_VALUE},
            {'Name': '/srkrcc/prod/backend/database_url', 'Value': DATABASE_VALUE},
        ])
        with mock.patch('config.ssm_secrets.boto3.client', return_value=client):
            load_serverless_secrets(env)
        names = client.get_parameters.call_args.kwargs['Names']
        self.assertEqual(
            names,
            ['/srkrcc/prod/backend/secret_key', '/srkrcc/prod/backend/database_url'],
        )

    def test_missing_parameter_fails_without_writing_secrets(self):
        env = _env(SERVERLESS='1')
        client = mock.Mock()
        client.get_parameters.return_value = _response(
            invalid=[SECRET_NAME, DATABASE_NAME],
        )
        with mock.patch('config.ssm_secrets.boto3.client', return_value=client):
            with self.assertRaises(ImproperlyConfigured) as raised:
                load_serverless_secrets(env)
        self.assertNotIn('SECRET_KEY', env)
        self.assertNotIn('DATABASE_URL', env)
        self.assertNotIn(SECRET_VALUE, str(raised.exception))
        self.assertIn('SECRET_KEY', str(raised.exception))
        self.assertIn('DATABASE_URL', str(raised.exception))

    def test_access_denied_does_not_leak_exception_body(self):
        env = _env(SERVERLESS='1')
        client = mock.Mock()
        client.get_parameters.side_effect = ClientError(
            {
                'Error': {
                    'Code': 'AccessDeniedException',
                    'Message': f'denied while reading {DATABASE_VALUE}',
                }
            },
            'GetParameters',
        )
        with mock.patch('config.ssm_secrets.boto3.client', return_value=client):
            with self.assertRaises(ImproperlyConfigured) as raised:
                load_serverless_secrets(env)
        message = str(raised.exception)
        self.assertNotIn(DATABASE_VALUE, message)
        self.assertNotIn('neon-pass', message)
        self.assertNotIn('AccessDeniedException', message)
        self.assertIsNone(raised.exception.__cause__)

    def test_empty_parameter_value_is_rejected(self):
        env = _env(SERVERLESS='1')
        client = mock.Mock()
        client.get_parameters.return_value = _response([
            {'Name': SECRET_NAME, 'Value': '   '},
            {'Name': DATABASE_NAME, 'Value': DATABASE_VALUE},
        ])
        with mock.patch('config.ssm_secrets.boto3.client', return_value=client):
            with self.assertRaises(ImproperlyConfigured) as raised:
                load_serverless_secrets(env)
        self.assertNotIn(DATABASE_VALUE, str(raised.exception))
        self.assertNotIn('SECRET_KEY', env)


class MigrationStartupGuardTests(SimpleTestCase):
    def test_lambda_entrypoint_migrates_only_when_startup_tasks_are_enabled(self):
        script = Path(BASE_DIR, 'entrypoint.sh').read_text(encoding='utf-8')
        dockerfile = Path(BASE_DIR, 'Dockerfile').read_text(encoding='utf-8')
        render_build = Path(BASE_DIR, 'build.sh').read_text(encoding='utf-8')
        locals_tf = Path(BASE_DIR, 'infra', 'terraform', 'locals.tf').read_text(encoding='utf-8')
        iam_tf = Path(BASE_DIR, 'infra', 'terraform', 'iam.tf').read_text(encoding='utf-8')

        self.assertLess(script.index('RUN_STARTUP_TASKS'), script.index('migrate --noinput'))
        self.assertIn('ENTRYPOINT ["/app/entrypoint.sh"]', dockerfile)
        self.assertIn('python manage.py migrate --noinput', render_build)

        lambda_env = locals_tf.split('lambda_environment', 1)[1].split('}', 1)[0]
        self.assertIsNone(re.search(r'(?<!SSM_)DATABASE_URL', lambda_env))
        self.assertIsNone(re.search(r'(?<![A-Z_])SECRET_KEY\s*=', lambda_env))
        self.assertIsNone(re.search(r'RUN_STARTUP_TASKS\s*=', lambda_env))
        self.assertIn('SSM_SECRET_KEY_NAME', lambda_env)
        self.assertIn('SSM_DATABASE_URL_NAME', lambda_env)

        self.assertIn('ssm:GetParameters', iam_tf)
        self.assertIn('kms:Decrypt', iam_tf)
        self.assertIn('kms:ViaService', iam_tf)
        self.assertIn('kms:EncryptionContext:PARAMETER_ARN', iam_tf)

    def test_loaded_process_is_not_serverless(self):
        self.assertNotIn(os.getenv('SERVERLESS', '').lower(), ('true', '1', 't'))
        self.assertFalse(os.getenv('AWS_LAMBDA_FUNCTION_NAME'))
