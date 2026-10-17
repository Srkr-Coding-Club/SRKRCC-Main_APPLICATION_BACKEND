import os
import sys
from pathlib import Path
import dj_database_url
from dotenv import load_dotenv
from django.core.exceptions import ImproperlyConfigured

from config.email_config import resolve_email_settings
from config.ssm_secrets import INSECURE_DEFAULT_SECRET_KEY, load_serverless_secrets
from config.logging_config import build_logging, init_sentry
from config.security_config import resolve_security_settings

load_dotenv()
# Local and Render already have SECRET_KEY and DATABASE_URL. Lambda does not;
# load them from Parameter Store before Django reads them. No-op otherwise.
load_serverless_secrets()

BASE_DIR = Path(__file__).resolve().parent.parent

_INSECURE_DEFAULT_SECRET_KEY = INSECURE_DEFAULT_SECRET_KEY
SECRET_KEY = os.getenv('SECRET_KEY', _INSECURE_DEFAULT_SECRET_KEY)


def _env_truthy(name: str, default: str = '') -> bool:
    return os.getenv(name, default).lower() in ('true', '1', 't')


def _is_serverless() -> bool:
    """True only when Lambda (or an explicit SERVERLESS flag) is active.

    Absent both AWS_LAMBDA_FUNCTION_NAME and SERVERLESS, Render and local
    keep the historical connection and media behaviour.
    """
    return bool(os.getenv('AWS_LAMBDA_FUNCTION_NAME')) or _env_truthy('SERVERLESS')


def database_conn_max_age() -> int:
    # Persistent connections leak on Lambda because the process is frozen after
    # the response. Neon pooled URLs also expect short-lived clients.
    return 0 if _is_serverless() else 600


# Bounded waits for Lambda only. Neon can take a few seconds to wake; these
# values fail the request instead of blocking until Gunicorn's worker timeout.
# connect_timeout is seconds (libpq). statement_timeout is milliseconds.
POSTGRES_CONNECT_TIMEOUT_SECONDS = 10
POSTGRES_STATEMENT_TIMEOUT_MS = 10000


def apply_database_options(config: dict) -> dict:
    """Return a dj-database-url dict with Lambda-safe Postgres timeouts.

    Django's request-finished hook calls close_old_connections ->
    close_if_unusable_or_obsolete. When errors_occurred is set, that path
    runs PostgreSQL is_usable() -> SELECT 1 with no default query timeout,
    which hung Gunicorn workers on a stale Neon socket. CONN_HEALTH_CHECKS
    stays False so reuse-path health checks are not added. CONN_MAX_AGE is
    unchanged by this function.
    """
    config = dict(config)
    config['CONN_HEALTH_CHECKS'] = False
    if not _is_serverless():
        return config

    engine = config.get('ENGINE') or ''
    if 'postgresql' not in engine and 'postgres' not in engine:
        return config

    options = dict(config.get('OPTIONS') or {})
    options.setdefault('connect_timeout', POSTGRES_CONNECT_TIMEOUT_SECONDS)
    existing = (options.get('options') or '').strip()
    if 'statement_timeout' not in existing:
        extra = f'-c statement_timeout={POSTGRES_STATEMENT_TIMEOUT_MS}'
        options['options'] = f'{existing} {extra}'.strip()
    config['OPTIONS'] = options
    return config


def resolve_media_root() -> Path:
    # /tmp is writable on Lambda. This does not move existing files to S3;
    # backups and DMC exports still use local paths.
    return Path('/tmp/media') if _is_serverless() else (BASE_DIR / 'media')


def _use_s3() -> bool:
    return _env_truthy('USE_S3')


FILESYSTEM_STORAGE_BACKEND = 'django.core.files.storage.FileSystemStorage'
S3_STORAGE_BACKEND = 'storages.backends.s3.S3Storage'
WHITENOISE_STORAGE_BACKEND = 'whitenoise.storage.CompressedManifestStaticFilesStorage'


def default_storage_backend() -> str:
    return S3_STORAGE_BACKEND if _use_s3() else FILESYSTEM_STORAGE_BACKEND


def s3_storage_options() -> dict:
    """Bucket/region from env. Credentials come from the process role or default chain — never from this file."""
    return {
        'bucket_name': os.getenv('AWS_STORAGE_BUCKET_NAME', '').strip(),
        'region_name': os.getenv('AWS_S3_REGION_NAME', os.getenv('AWS_REGION', 'ap-south-1')).strip() or 'ap-south-1',
        'default_acl': None,
        'querystring_auth': True,
        'file_overwrite': False,
        'signature_version': 's3v4',
        'location': 'media',
        'object_parameters': {'CacheControl': 'max-age=86400'},
    }

# Defaults to False (secure-by-default) if the env var is entirely absent, e.g. a
# fresh deploy with no .env configured. Local dev sets DEBUG=True explicitly in .env.
DEBUG = os.getenv('DEBUG', 'False').lower() in ('true', '1', 't')
TESTING = 'test' in sys.argv

if not DEBUG and SECRET_KEY == _INSECURE_DEFAULT_SECRET_KEY:
    raise ImproperlyConfigured(
        "SECRET_KEY is using the insecure development default while DEBUG=False. "
        "Set a unique SECRET_KEY environment variable before running in production."
    )

# Hosts, CORS, CSRF and the frontend URL are resolved together in config/security_config.py:
# permissive on localhost in development, explicit-only (no wildcards) with DEBUG off.
_security = resolve_security_settings(os.environ, DEBUG)
ALLOWED_HOSTS = _security['ALLOWED_HOSTS']
CSRF_TRUSTED_ORIGINS = _security['CSRF_TRUSTED_ORIGINS']

if not DEBUG:
    # Render, Railway, and Heroku terminate SSL at their edge load balancer.
    # Enabling SECURE_SSL_REDIRECT internally causes health checks (which connect over HTTP)
    # to fail with 301 redirects, breaking port detection.
    SECURE_SSL_REDIRECT = os.getenv('SECURE_SSL_REDIRECT', 'False').lower() in ('true', '1', 't')
    SECURE_REDIRECT_EXEMPT = [
        r'^$',
        r'^api/ping/?$',
        r'^api/health/?$',
    ]
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.getenv('SECURE_HSTS_SECONDS', '31536000'))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Third party packages
    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'corsheaders',

    # Custom Platform Apps
    'apps.core',
    'apps.colleges',
    'apps.accounts',
    'apps.feature_flags',
    'apps.forms',
    'apps.attendance',
    'apps.events',
    'apps.hackathons',
    'apps.codequest',
    'apps.career',
    'apps.blogs',
    'apps.audit',
    'apps.announcements',
    'apps.notifications',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'

# Database Configuration: Uses local PostgreSQL by default.
# DATABASE_URL still comes only from the environment (or the local default).
DATABASES = {
    'default': apply_database_options(
        dj_database_url.config(
            default=os.getenv('DATABASE_URL', 'postgres://postgres:postgres@localhost:5432/srkrcc_db'),
            conn_max_age=database_conn_max_age(),
            conn_health_checks=False,
        )
    )
}

# Custom User Model
AUTH_USER_MODEL = 'accounts.User'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
    # Character-class mix (upper/lower/digit/special). Lives in AUTH_PASSWORD_VALIDATORS
    # rather than the signup serializer so it also covers the password-setup link flow.
    {'NAME': 'apps.accounts.validators.ComplexPasswordValidator'},
]

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True

# Static & Media Files
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'
WHITENOISE_MANIFEST_STRICT = False

MEDIA_URL = 'media/'
MEDIA_ROOT = resolve_media_root()

# Optional S3 for Django's default storage (FileField / ImageField) only.
# Backups and DMC exports use open() on MEDIA_ROOT / tempfile — they do not
# follow this backend. Static files stay on WhiteNoise, never S3.
# Objects are private (presigned URLs). Do not set a public CloudFront domain
# here; this app has no FileFields today and backups must not be world-readable.
if _use_s3():
    _s3_opts = s3_storage_options()
    if not _s3_opts['bucket_name']:
        raise ImproperlyConfigured('USE_S3=true requires AWS_STORAGE_BUCKET_NAME.')
    AWS_STORAGE_BUCKET_NAME = _s3_opts['bucket_name']
    AWS_S3_REGION_NAME = _s3_opts['region_name']
    AWS_S3_SIGNATURE_VERSION = 's3v4'
    AWS_DEFAULT_ACL = None
    AWS_QUERYSTRING_AUTH = True
    AWS_S3_FILE_OVERWRITE = False
    AWS_LOCATION = 'media'
    if 'storages' not in INSTALLED_APPS:
        INSTALLED_APPS.append('storages')
    STORAGES = {
        'default': {
            'BACKEND': S3_STORAGE_BACKEND,
            'OPTIONS': _s3_opts,
        },
        'staticfiles': {
            'BACKEND': WHITENOISE_STORAGE_BACKEND,
        },
    }

# Dynamic-form FILE / MULTI_FILE answers are captured inline as base64 data URLs
# (there is no binary-upload endpoint), so a submission body can carry a few MB
# of image data. Raise the form-post ceiling from Django's 2.5 MB default.
DATA_UPLOAD_MAX_MEMORY_SIZE = 30 * 1024 * 1024  # 30 MB
FILE_UPLOAD_MAX_MEMORY_SIZE = 30 * 1024 * 1024

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# --- Client IP / throttling ---------------------------------------------------------------
# How many reverse proxies in front of Django append to X-Forwarded-For (Render alone = 1).
TRUSTED_PROXY_COUNT = int(os.getenv('TRUSTED_PROXY_COUNT', '1'))
# Shared with the Next.js BFF so it can pass the visitor's real IP (otherwise every visitor looks like
# the frontend host). Unset = header ignored.
CLIENT_IP_SHARED_SECRET = os.getenv('CLIENT_IP_SHARED_SECRET', '')

# Throttle counters must be shared by all gunicorn workers and survive restarts, so production uses the
# database cache (python manage.py createcachetable). Tests and DEBUG (local) runs use per-process LocMem.
if TESTING or DEBUG:
    CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
else:
    CACHES = {'default': {'BACKEND': 'django.core.cache.backends.db.DatabaseCache', 'LOCATION': 'django_cache_table'}}

LOGGING = build_logging(DEBUG)
init_sentry()

# REST Framework Settings
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticatedOrReadOnly',
    ),
    # apps.core.throttling keys on apps.core.client_ip (spoof-resistant), not DRF's own X-Forwarded-For handling.
    'DEFAULT_THROTTLE_CLASSES': [
        'apps.core.throttling.AnonRateThrottle',
        'apps.core.throttling.UserRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '100/minute',
        'user': '1000/minute',
        # Credential stuffing / mass sign-up. Relaxed under the test runner, which shares one client IP.
        'login': '100000/minute' if TESTING else '10/minute',
        'register': '100000/hour' if TESTING else '10/hour',
        # Public form submission - anonymous-writable, so scope-limit it below
        # the global anon rate to blunt flooding of a single form.
        'form_submit': '20/minute',
        # Hackathon teammate lookup by exact email - authenticated, but
        # limited so it can't be used to enumerate which emails have accounts.
        'hackathon_lookup': '30/minute',
        # Team leaders emailing people who have no account yet: it sends mail to arbitrary
        # addresses, so it is kept low.
        'hackathon_email_invite': '20/hour',
        # Public college dropdown on the signup page: one request per debounced
        # keystroke burst, so it needs headroom above a form submit but stays bounded.
        'college_search': '120/minute',
    },
}

# CORS Policy
from corsheaders.defaults import default_headers, default_methods

CORS_ALLOW_ALL_ORIGINS = _security['CORS_ALLOW_ALL_ORIGINS']
CORS_ALLOWED_ORIGINS = _security['CORS_ALLOWED_ORIGINS']
CORS_ALLOWED_ORIGIN_REGEXES = _security['CORS_ALLOWED_ORIGIN_REGEXES']
CORS_ALLOW_CREDENTIALS = _security['CORS_ALLOW_CREDENTIALS']

CORS_ALLOW_HEADERS = list(default_headers) + [
    'idempotency-key',
    'x-idempotency-key',
    'cache-control',
    'pragma',
]

CORS_ALLOW_METHODS = list(default_methods)

CORS_EXPOSE_HEADERS = [
    'content-type',
    'content-disposition',
    'content-length',
]

# Background jobs run on plain Python threads (apps/core/tasks.py), not Celery -
# no broker/worker process to configure.

# SimpleJWT Authentication Lifetimes & Configuration
from datetime import timedelta
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=int(os.getenv('JWT_ACCESS_TOKEN_LIFETIME_MINUTES', '60'))),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=int(os.getenv('JWT_REFRESH_TOKEN_LIFETIME_DAYS', '7'))),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'UPDATE_LAST_LOGIN': True,
    'ALGORITHM': 'HS256',
    'SIGNING_KEY': SECRET_KEY,
    'AUTH_HEADER_TYPES': ('Bearer',),
    'USER_ID_FIELD': 'id',
    'USER_ID_CLAIM': 'user_id',
}

# Public URL of the frontend. Links inside emails (password setup, confirmations,
# hackathon invites) are built from it.
FRONTEND_URL = _security['FRONTEND_URL']

# Email: EMAIL_PROVIDER picks console (local), resend (staging) or gmail (production).
# See config/email_config.py.
_email = resolve_email_settings(os.environ, DEBUG)
EMAIL_PROVIDER = _email['EMAIL_PROVIDER']
EMAIL_BACKEND = _email['EMAIL_BACKEND']
EMAIL_HOST = _email['EMAIL_HOST']
EMAIL_PORT = _email['EMAIL_PORT']
EMAIL_USE_TLS = _email['EMAIL_USE_TLS']
EMAIL_HOST_USER = _email['EMAIL_HOST_USER']
EMAIL_HOST_PASSWORD = _email['EMAIL_HOST_PASSWORD']
EMAIL_TIMEOUT = _email['EMAIL_TIMEOUT']
RESEND_API_KEY = _email['RESEND_API_KEY']
DEFAULT_FROM_EMAIL = _email['DEFAULT_FROM_EMAIL']

