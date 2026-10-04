from email.utils import parseaddr
from urllib.parse import urlparse

from django.conf import settings
from django.core import checks

from config.email_config import CONSOLE_BACKEND

LOCMEM_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'

# Resend only sends from domains you have verified with it, never from free mailboxes.
_FREE_MAIL_DOMAINS = {'gmail.com', 'googlemail.com', 'yahoo.com', 'outlook.com', 'hotmail.com'}


def _sender_address() -> str:
    return parseaddr(settings.DEFAULT_FROM_EMAIL)[1].lower()


@checks.register()
def check_email_provider(app_configs, **kwargs):
    """Surface email misconfiguration at startup instead of as silently failed sends."""
    # Django swaps in the in-memory backend while testing, so nothing real is configured then.
    if settings.EMAIL_BACKEND == LOCMEM_BACKEND:
        return []
    provider = settings.EMAIL_PROVIDER
    found = []

    if provider == 'resend':
        if not settings.RESEND_API_KEY:
            found.append(checks.Warning(
                'EMAIL_PROVIDER=resend but RESEND_API_KEY is not set, so no email can be sent.',
                hint='Create an API key at resend.com and set RESEND_API_KEY.',
                id='core.W001',
            ))
        if _sender_address().rpartition('@')[2] in _FREE_MAIL_DOMAINS:
            found.append(checks.Warning(
                f'DEFAULT_FROM_EMAIL ({settings.DEFAULT_FROM_EMAIL}) uses a free mailbox domain, which Resend rejects.',
                hint='Verify your own domain in Resend, or use onboarding@resend.dev while testing.',
                id='core.W002',
            ))
    elif provider == 'gmail':
        if not (settings.EMAIL_HOST_USER and settings.EMAIL_HOST_PASSWORD):
            found.append(checks.Warning(
                'EMAIL_PROVIDER=gmail but EMAIL_HOST_USER or EMAIL_HOST_PASSWORD is not set, so no email can be sent.',
                hint='Set EMAIL_HOST_USER to the Gmail address and EMAIL_HOST_PASSWORD to a Google app password.',
                id='core.W003',
            ))
        elif _sender_address() != settings.EMAIL_HOST_USER.lower():
            found.append(checks.Info(
                'Gmail replaces the From address with the signed-in account unless the sender is a verified "Send mail as" alias.',
                hint=f'DEFAULT_FROM_EMAIL is {settings.DEFAULT_FROM_EMAIL}; EMAIL_HOST_USER is {settings.EMAIL_HOST_USER}.',
                id='core.I001',
            ))
    elif settings.EMAIL_BACKEND == CONSOLE_BACKEND and not settings.DEBUG:
        found.append(checks.Warning(
            'EMAIL_PROVIDER=console with DEBUG=False: emails are printed to the log and never delivered.',
            hint='Set EMAIL_PROVIDER to resend (staging) or gmail (production).',
            id='core.W004',
        ))
    # Locally the frontend really is on localhost, so only flag it outside development.
    if provider in ('resend', 'gmail') and not settings.DEBUG and urlparse(settings.FRONTEND_URL).hostname in ('localhost', '127.0.0.1'):
        found.append(checks.Warning(
            f'FRONTEND_URL is {settings.FRONTEND_URL}, so every link in an email will point at a local machine.',
            hint='Set FRONTEND_URL to the public address of the frontend, e.g. https://srkrcc.in.',
            id='core.W005',
        ))
    return found
