"""Load SECRET_KEY and DATABASE_URL from Parameter Store on Lambda.

Local development and Render keep supplying those values as environment
variables. This module calls SSM only when the process is serverless and a
required value is missing. Parameter values are written into the process
environment and are never logged.
"""
import os

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from django.core.exceptions import ImproperlyConfigured

INSECURE_DEFAULT_SECRET_KEY = 'django-insecure-srkrcc-dev-secret-key-change-in-prod'

_REQUIRED = ('SECRET_KEY', 'DATABASE_URL')

_NAME_ENV = {
    'SECRET_KEY': 'SSM_SECRET_KEY_NAME',
    'DATABASE_URL': 'SSM_DATABASE_URL_NAME',
}

_DEFAULT_NAMES = {
    'SECRET_KEY': '/srkrcc/dev/backend/secret_key',
    'DATABASE_URL': '/srkrcc/dev/backend/database_url',
}


def _is_serverless(env: dict) -> bool:
    if env.get('AWS_LAMBDA_FUNCTION_NAME', '').strip():
        return True
    return env.get('SERVERLESS', '').strip().lower() in ('true', '1', 't')


def _is_configured(key: str, env: dict) -> bool:
    value = env.get(key, '').strip()
    if not value:
        return False
    if key == 'SECRET_KEY' and value == INSECURE_DEFAULT_SECRET_KEY:
        return False
    return True


def _parameter_name(key: str, env: dict) -> str:
    configured = env.get(_NAME_ENV[key], '').strip()
    return configured or _DEFAULT_NAMES[key]


def _region(env: dict) -> str:
    return (env.get('AWS_REGION') or env.get('AWS_DEFAULT_REGION') or 'ap-south-1').strip() or 'ap-south-1'


def _unavailable(keys: tuple[str, ...] | list[str]) -> ImproperlyConfigured:
    # Names of missing settings only. Never include parameter values or the
    # AWS error body, which can echo a request.
    return ImproperlyConfigured(
        'Required configuration is unavailable: ' + ', '.join(keys)
    )


def _fetch(names: list[str], region: str) -> dict[str, str]:
    client = boto3.client('ssm', region_name=region)
    try:
        response = client.get_parameters(Names=names, WithDecryption=True)
    except (BotoCoreError, ClientError):
        raise _unavailable(_REQUIRED) from None

    if response.get('InvalidParameters'):
        raise _unavailable(_REQUIRED)

    found: dict[str, str] = {}
    for parameter in response.get('Parameters') or []:
        name = parameter.get('Name') or ''
        value = (parameter.get('Value') or '').strip()
        if not name or not value:
            raise _unavailable(_REQUIRED)
        found[name] = value

    if set(found) != set(names):
        raise _unavailable(_REQUIRED)
    return found


def load_serverless_secrets(environ: dict | None = None) -> None:
    """Fill missing secrets from SSM. No-op for local and Render."""
    env = os.environ if environ is None else environ
    if not _is_serverless(env):
        return

    missing = [key for key in _REQUIRED if not _is_configured(key, env)]
    if not missing:
        return

    names = {key: _parameter_name(key, env) for key in missing}
    fetched = _fetch(list(names.values()), _region(env))
    for key, name in names.items():
        env[key] = fetched[name]
