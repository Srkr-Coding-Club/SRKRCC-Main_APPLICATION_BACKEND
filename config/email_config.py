"""Choose the outgoing-email provider from environment variables.

``EMAIL_PROVIDER`` selects who delivers mail:

    console  prints each email to the terminal (local development)
    resend   Resend over HTTPS (staging)
    gmail    Gmail SMTP (production)

When unset it falls back to ``console`` with DEBUG=True and ``gmail`` otherwise.

Returned keys, assigned one by one in settings.py: EMAIL_PROVIDER, EMAIL_BACKEND,
EMAIL_HOST, EMAIL_PORT, EMAIL_USE_TLS, EMAIL_HOST_USER, EMAIL_HOST_PASSWORD,
EMAIL_TIMEOUT, RESEND_API_KEY, DEFAULT_FROM_EMAIL.
"""
from collections.abc import Mapping

from django.core.exceptions import ImproperlyConfigured

PROVIDERS = ('console', 'resend', 'gmail')

CONSOLE_BACKEND = 'django.core.mail.backends.console.EmailBackend'
SMTP_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
RESEND_BACKEND = 'apps.core.email_backends.ResendEmailBackend'

GMAIL_HOST = 'smtp.gmail.com'
GMAIL_PORT = 587
# Django's SMTP backend waits forever by default, which would pin a gunicorn worker.
SMTP_TIMEOUT_SECONDS = 20

DEFAULT_SENDER = 'SRKR Coding Club <noreply@srkrcc.in>'


def resolve_email_settings(env: Mapping[str, str], debug: bool) -> dict:
    provider = (env.get('EMAIL_PROVIDER') or ('console' if debug else 'gmail')).strip().lower()
    if provider not in PROVIDERS:
        raise ImproperlyConfigured(
            f"EMAIL_PROVIDER={provider!r} is not supported. Use one of: {', '.join(PROVIDERS)}."
        )

    resolved = {
        'EMAIL_PROVIDER': provider,
        'EMAIL_BACKEND': CONSOLE_BACKEND,
        'EMAIL_HOST': '',
        'EMAIL_PORT': 0,
        'EMAIL_USE_TLS': False,
        'EMAIL_HOST_USER': '',
        'EMAIL_HOST_PASSWORD': '',
        'EMAIL_TIMEOUT': None,
        'RESEND_API_KEY': '',
        'DEFAULT_FROM_EMAIL': env.get('DEFAULT_FROM_EMAIL', DEFAULT_SENDER),
    }

    if provider == 'resend':
        resolved['EMAIL_BACKEND'] = RESEND_BACKEND
        resolved['RESEND_API_KEY'] = env.get('RESEND_API_KEY', '').strip()
    elif provider == 'gmail':
        resolved.update(
            EMAIL_BACKEND=SMTP_BACKEND,
            EMAIL_HOST=GMAIL_HOST,
            EMAIL_PORT=GMAIL_PORT,
            EMAIL_USE_TLS=True,
            EMAIL_HOST_USER=env.get('EMAIL_HOST_USER', '').strip(),
            # Google displays app passwords in four groups separated by spaces.
            EMAIL_HOST_PASSWORD=''.join(env.get('EMAIL_HOST_PASSWORD', '').split()),
            EMAIL_TIMEOUT=SMTP_TIMEOUT_SECONDS,
        )
    return resolved
