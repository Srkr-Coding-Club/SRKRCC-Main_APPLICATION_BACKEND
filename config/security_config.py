"""Host, CORS, CSRF and frontend-URL settings, resolved from environment variables.

Development stays permissive (localhost origins, any host). With ``DEBUG`` off
nothing is trusted unless it is named explicitly: there are no wildcard origins and
no catch-all host default. Nothing here is specific to one hosting platform.

Returned keys, assigned one by one in settings.py: ALLOWED_HOSTS,
CSRF_TRUSTED_ORIGINS, CORS_ALLOW_ALL_ORIGINS, CORS_ALLOWED_ORIGINS,
CORS_ALLOWED_ORIGIN_REGEXES, CORS_ALLOW_CREDENTIALS, FRONTEND_URL.
"""
import warnings
from collections.abc import Mapping
from urllib.parse import urlparse

from django.core.exceptions import ImproperlyConfigured

LOOPBACK_HOSTS = ['localhost', '127.0.0.1', '0.0.0.0', '[::1]']
DEV_ORIGINS = [
    'http://localhost:3000', 'http://127.0.0.1:3000',
    'http://localhost:3001', 'http://127.0.0.1:3001',
    'http://localhost:5173', 'http://127.0.0.1:5173',
]
DEV_CSRF_ORIGINS = DEV_ORIGINS + ['http://localhost:8000', 'http://127.0.0.1:8000']
DEV_CORS_REGEXES = [
    r'^https?:\/\/localhost(:\d+)?$',
    r'^https?:\/\/127\.0\.0\.1(:\d+)?$',
]
TRUE_VALUES = ('true', '1', 't', 'yes')


def clean_origin(url_str: str) -> str:
    """Normalise an origin to ``scheme://host[:port]`` (no path, no trailing slash)."""
    raw = (url_str or '').strip()
    if not raw:
        return ''
    if not raw.startswith(('http://', 'https://')):
        raw = f'https://{raw}'
    parsed = urlparse(raw)
    return f'{parsed.scheme}://{parsed.netloc}'.rstrip('/')


def _split(value: str) -> list[str]:
    return [item.strip() for item in (value or '').split(',') if item.strip()]


def _origins(value: str) -> list[str]:
    return [origin for origin in (clean_origin(item) for item in _split(value)) if origin]


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


DEFAULT_HSTS_SECONDS = 31536000


def _flag(value: str | None, default: bool) -> bool:
    text = (value or '').strip().lower()
    return default if not text else text in TRUE_VALUES


def resolve_hsts_settings(env: Mapping[str, str]) -> dict:
    """HTTP Strict Transport Security, applied when DEBUG is off.

    ``SECURE_HSTS_SECONDS`` is how long browsers must keep refusing plain HTTP (0 turns the header
    off). Browsers cannot be told to forget it early, so use a short value such as 3600 until the
    deployment is settled, then raise it. ``SECURE_HSTS_INCLUDE_SUBDOMAINS`` and
    ``SECURE_HSTS_PRELOAD`` default to on.
    """
    raw = (env.get('SECURE_HSTS_SECONDS') or '').strip()
    try:
        seconds = int(raw) if raw else DEFAULT_HSTS_SECONDS
    except ValueError:
        warnings.warn(
            f'SECURE_HSTS_SECONDS={raw!r} is not a whole number; keeping {DEFAULT_HSTS_SECONDS}.',
            RuntimeWarning,
            stacklevel=2,
        )
        seconds = DEFAULT_HSTS_SECONDS
    return {
        'SECURE_HSTS_SECONDS': max(seconds, 0),
        'SECURE_HSTS_INCLUDE_SUBDOMAINS': _flag(env.get('SECURE_HSTS_INCLUDE_SUBDOMAINS'), True),
        'SECURE_HSTS_PRELOAD': _flag(env.get('SECURE_HSTS_PRELOAD'), True),
    }


def resolve_health_check_paths(env: Mapping[str, str]) -> tuple[str, ...]:
    """Paths the hosting platform polls to see whether the app is alive.

    ``HEALTH_CHECK_PATHS`` is comma separated. These paths are answered whatever ``Host`` header
    the platform sends (health checks often use the server's private IP) and are never redirected
    to HTTPS. Every other path keeps the strict ``ALLOWED_HOSTS`` check.
    """
    raw = env.get('HEALTH_CHECK_PATHS') or '/,/api/ping,/api/ping/'
    paths = [item.strip() for item in raw.split(',') if item.strip()]
    return tuple(path if path.startswith('/') else f'/{path}' for path in paths)


def _platform_hosts(env: Mapping[str, str]) -> list[str]:
    """Hostnames the hosting platform assigns to the service, from ``PLATFORM_HOSTS``.

    Comma separated; a leading dot matches every subdomain (``.example-host.com``). They count
    as an explicit host list, so ``ALLOWED_HOSTS`` may stay empty when the platform names them.

    ``RENDER`` and ``RENDER_EXTERNAL_HOSTNAME`` are deprecated aliases kept only so services
    already running on Render keep booting without a dashboard change. New setups, on any
    platform, should set ``PLATFORM_HOSTS`` instead.
    """
    hosts = _split(env.get('PLATFORM_HOSTS', ''))
    if env.get('RENDER'):
        hosts.append('.onrender.com')
    legacy_hostname = (env.get('RENDER_EXTERNAL_HOSTNAME') or '').strip()
    if legacy_hostname:
        hosts.append(legacy_hostname)
    return hosts


def resolve_security_settings(env: Mapping[str, str], debug: bool) -> dict:
    explicit_hosts = _split(env.get('ALLOWED_HOSTS', '')) + _platform_hosts(env)

    if debug:
        hosts = explicit_hosts + LOOPBACK_HOSTS + ['testserver'] if explicit_hosts else ['*']
    else:
        if not explicit_hosts:
            raise ImproperlyConfigured(
                'ALLOWED_HOSTS is not set. With DEBUG off, list the hostnames this backend serves '
                '(comma separated), e.g. ALLOWED_HOSTS=api.example.com, or set PLATFORM_HOSTS to the '
                'hostnames your hosting platform assigns.'
            )
        hosts = explicit_hosts + LOOPBACK_HOSTS

    allow_all_origins = (env.get('CORS_ALLOW_ALL_ORIGINS') or ('True' if debug else 'False')).lower() in TRUE_VALUES
    if allow_all_origins and not debug:
        raise ImproperlyConfigured(
            'CORS_ALLOW_ALL_ORIGINS is on while DEBUG is off. Remove it and list the real frontend '
            'origin(s) in CORS_ALLOWED_ORIGINS instead.'
        )

    cors_origins = _origins(env.get('CORS_ALLOWED_ORIGINS', ''))
    csrf_origins = _origins(env.get('CSRF_TRUSTED_ORIGINS', ''))
    if debug:
        cors_origins = cors_origins or [clean_origin(origin) for origin in DEV_ORIGINS]
        csrf_origins = csrf_origins + [clean_origin(origin) for origin in DEV_CSRF_ORIGINS]

    # Falls back to localhost when unset. With DEBUG off, `manage.py check` warns about a
    # localhost value (core.W005) because every email link would point at a local machine.
    frontend_url = (env.get('FRONTEND_URL') or 'http://localhost:3000').strip().rstrip('/')

    # In production the browser talks to the same-origin frontend proxy, never to Django
    # directly, so cookies/credentials are not sent cross-origin unless explicitly enabled.
    credentials = (env.get('CORS_ALLOW_CREDENTIALS') or ('True' if debug else 'False')).lower() in TRUE_VALUES

    return {
        'ALLOWED_HOSTS': _unique(hosts),
        'CSRF_TRUSTED_ORIGINS': _unique(csrf_origins),
        'CORS_ALLOW_ALL_ORIGINS': allow_all_origins,
        'CORS_ALLOWED_ORIGINS': _unique(cors_origins),
        'CORS_ALLOWED_ORIGIN_REGEXES': list(DEV_CORS_REGEXES) if debug else [],
        'CORS_ALLOW_CREDENTIALS': credentials,
        'FRONTEND_URL': frontend_url,
    }
