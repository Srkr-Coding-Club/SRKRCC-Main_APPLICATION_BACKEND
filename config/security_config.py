"""Host, CORS, CSRF and frontend-URL settings, resolved from environment variables.

Development stays permissive (localhost origins, any host). With ``DEBUG`` off
nothing is trusted unless it is named explicitly: there are no wildcard origins,
no catch-all host default, and the frontend URL must be a real public address.

Returned keys, assigned one by one in settings.py: ALLOWED_HOSTS,
CSRF_TRUSTED_ORIGINS, CORS_ALLOW_ALL_ORIGINS, CORS_ALLOWED_ORIGINS,
CORS_ALLOWED_ORIGIN_REGEXES, CORS_ALLOW_CREDENTIALS, FRONTEND_URL.
"""
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
LOCAL_FRONTEND_HOSTS = {'localhost', '127.0.0.1', '0.0.0.0', '[::1]', '::1'}
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


def resolve_security_settings(env: Mapping[str, str], debug: bool) -> dict:
    explicit_hosts = _split(env.get('ALLOWED_HOSTS', ''))
    render_hostname = (env.get('RENDER_EXTERNAL_HOSTNAME') or '').strip()
    if render_hostname:
        explicit_hosts.append(render_hostname)

    if debug:
        hosts = explicit_hosts + LOOPBACK_HOSTS + ['testserver'] if explicit_hosts else ['*']
    else:
        if not explicit_hosts and not env.get('RENDER'):
            raise ImproperlyConfigured(
                'ALLOWED_HOSTS is not set. With DEBUG off, list the hostnames this backend serves '
                '(comma separated), e.g. ALLOWED_HOSTS=api.srkrcc.in.'
            )
        hosts = explicit_hosts + LOOPBACK_HOSTS
        if env.get('RENDER'):
            # Render's own hostnames (including the health checker's) end in .onrender.com.
            hosts.append('.onrender.com')

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

    frontend_url = (env.get('FRONTEND_URL') or 'http://localhost:3000').strip().rstrip('/')
    if not debug:
        host = (urlparse(frontend_url).hostname or '').lower()
        if not env.get('FRONTEND_URL') or host in LOCAL_FRONTEND_HOSTS:
            raise ImproperlyConfigured(
                'FRONTEND_URL must be the public address of the frontend (e.g. https://srkrcc.in) '
                'when DEBUG is off: it is used for every link in outgoing emails.'
            )

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
