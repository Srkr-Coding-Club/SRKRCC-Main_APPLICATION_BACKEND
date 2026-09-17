import os
from pathlib import Path
import dj_database_url
from dotenv import load_dotenv
from django.core.exceptions import ImproperlyConfigured

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

_INSECURE_DEFAULT_SECRET_KEY = 'django-insecure-srkrcc-dev-secret-key-change-in-prod'
SECRET_KEY = os.getenv('SECRET_KEY', _INSECURE_DEFAULT_SECRET_KEY)

# Defaults to False (secure-by-default) if the env var is entirely absent, e.g. a
# fresh deploy with no .env configured. Local dev sets DEBUG=True explicitly in .env.
DEBUG = os.getenv('DEBUG', 'False').lower() in ('true', '1', 't')

if not DEBUG and SECRET_KEY == _INSECURE_DEFAULT_SECRET_KEY:
    raise ImproperlyConfigured(
        "SECRET_KEY is using the insecure development default while DEBUG=False. "
        "Set a unique SECRET_KEY environment variable before running in production."
    )

raw_hosts = os.getenv('ALLOWED_HOSTS', '*').split(',')
default_hosts = [
    'localhost',
    '127.0.0.1',
    '0.0.0.0',
    'testserver',
    '[::1]',
    '.onrender.com',
]
render_hostname = os.getenv('RENDER_EXTERNAL_HOSTNAME')
if render_hostname:
    default_hosts.append(render_hostname)

ALLOWED_HOSTS = list(set([h.strip() for h in raw_hosts if h.strip()] + default_hosts))

from urllib.parse import urlparse

def clean_origin(url_str: str) -> str:
    """Normalizes an origin URL to scheme + netloc (stripping paths and trailing slashes)."""
    raw = url_str.strip()
    if not raw:
        return ''
    if not raw.startswith(('http://', 'https://')):
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    return f"{parsed.scheme}://{parsed.netloc}".rstrip('/')

default_csrf_origins = [
    'http://localhost:3000',
    'http://127.0.0.1:3000',
    'http://localhost:3001',
    'http://127.0.0.1:3001',
    'http://localhost:5173',
    'http://127.0.0.1:5173',
    'http://localhost:8000',
    'http://127.0.0.1:8000',
    'https://*.vercel.app',
    'https://*.onrender.com',
]
raw_csrf = os.getenv('CSRF_TRUSTED_ORIGINS', '')
custom_csrf = [clean_origin(o) for o in raw_csrf.split(',') if clean_origin(o)]
CSRF_TRUSTED_ORIGINS = list(set([clean_origin(o) for o in default_csrf_origins] + custom_csrf))

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

# Database Configuration: Uses local PostgreSQL by default
DATABASES = {
    'default': dj_database_url.config(
        default=os.getenv('DATABASE_URL', 'postgres://postgres:postgres@localhost:5432/srkrcc_db'),
        conn_max_age=600,
    )
}

# Custom User Model
AUTH_USER_MODEL = 'accounts.User'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
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
MEDIA_ROOT = BASE_DIR / 'media'

# Dynamic-form FILE / MULTI_FILE answers are captured inline as base64 data URLs
# (there is no binary-upload endpoint), so a submission body can carry a few MB
# of image data. Raise the form-post ceiling from Django's 2.5 MB default.
DATA_UPLOAD_MAX_MEMORY_SIZE = 30 * 1024 * 1024  # 30 MB
FILE_UPLOAD_MAX_MEMORY_SIZE = 30 * 1024 * 1024

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# REST Framework Settings
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticatedOrReadOnly',
    ),
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '100/minute',
        'user': '1000/minute',
        # Public form submission — anonymous-writable, so scope-limit it below
        # the global anon rate to blunt flooding of a single form.
        'form_submit': '20/minute',
    },
}

# CORS Policy
from corsheaders.defaults import default_headers, default_methods

# Allow all origins by default in local dev (DEBUG=True) to eliminate developer friction
CORS_ALLOW_ALL_ORIGINS = os.getenv('CORS_ALLOW_ALL_ORIGINS', 'True' if DEBUG else 'False').lower() in ('true', '1', 't')

raw_cors = os.getenv('CORS_ALLOWED_ORIGINS', 'http://localhost:3000,http://127.0.0.1:3000,http://localhost:3001,http://127.0.0.1:3001,http://localhost:5173,http://127.0.0.1:5173')
CORS_ALLOWED_ORIGINS = list(set([
    clean_origin(origin)
    for origin in raw_cors.split(',')
    if clean_origin(origin)
]))

# Regex matching for dynamic preview environments (Vercel previews, Render, localhost on any port)
CORS_ALLOWED_ORIGIN_REGEXES = [
    r"^https?:\/\/localhost(:\d+)?$",
    r"^https?:\/\/127\.0\.0\.1(:\d+)?$",
    r"^https:\/\/.*\.vercel\.app$",
    r"^https:\/\/.*\.onrender\.com$",
]

CORS_ALLOW_CREDENTIALS = True

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

# Background jobs run on plain Python threads (apps/core/tasks.py), not Celery —
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

# Email Backend Configuration
EMAIL_BACKEND = os.getenv('EMAIL_BACKEND', 'django.core.mail.backends.console.EmailBackend' if DEBUG else 'django.core.mail.backends.smtp.EmailBackend')
EMAIL_HOST = os.getenv('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = int(os.getenv('EMAIL_PORT', '587'))
EMAIL_USE_TLS = os.getenv('EMAIL_USE_TLS', 'True').lower() == 'true'
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'SRKR Coding Club <noreply@srkrcc.in>')

