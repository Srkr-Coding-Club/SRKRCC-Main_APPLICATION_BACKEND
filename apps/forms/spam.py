"""
Keeping bots out of a public form.

Two layers, both cheap for a real person:

* A honeypot - a hidden text box named ``HONEYPOT_FIELD`` that people never see or fill but
  form-filling bots do. A submission that fills it is answered with a normal-looking success and
  dropped, so the bot learns nothing.
* An optional Cloudflare Turnstile check, per form (``Form.captcha_enabled``). The server verifies the
  token the browser got from the widget; it needs ``TURNSTILE_SECRET_KEY`` (and the site needs
  ``NEXT_PUBLIC_TURNSTILE_SITE_KEY`` on the frontend). A form that asks for it with no secret
  configured refuses submissions rather than silently skipping the check.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

# Not a word browsers or password managers associate with a value to fill in (website, url, email, phone, name...).
HONEYPOT_FIELD = 'hp_q7z4'
CAPTCHA_TOKEN_FIELD = 'captcha_token'
TURNSTILE_VERIFY_URL = 'https://challenges.cloudflare.com/turnstile/v0/siteverify'
MAX_TOKEN_LENGTH = 2048

OK = 'ok'
MISSING = 'missing'
FAILED = 'failed'
NOT_CONFIGURED = 'not_configured'
UNAVAILABLE = 'unavailable'


def honeypot_filled(data) -> bool:
    value = data.get(HONEYPOT_FIELD) if hasattr(data, 'get') else None
    return bool(value) and bool(str(value).strip())


def captcha_configured() -> bool:
    return bool(getattr(settings, 'TURNSTILE_SECRET_KEY', ''))


CAPTCHA_NOT_CONFIGURED_MESSAGE = (
    "The bot check (CAPTCHA) is switched on for this form, but the server has no TURNSTILE_SECRET_KEY, "
    "so nobody could submit it."
)


def verify_captcha(token, remote_ip: str | None = None) -> str:
    """One of OK / MISSING / FAILED / NOT_CONFIGURED / UNAVAILABLE. Never raises."""
    secret = getattr(settings, 'TURNSTILE_SECRET_KEY', '')
    if not secret:
        return NOT_CONFIGURED
    if not isinstance(token, str) or not token.strip():
        return MISSING
    if len(token) > MAX_TOKEN_LENGTH:
        return FAILED
    fields = {'secret': secret, 'response': token}
    if remote_ip:
        fields['remoteip'] = remote_ip
    request = urllib.request.Request(
        TURNSTILE_VERIFY_URL, data=urllib.parse.urlencode(fields).encode(),
        headers={'Content-Type': 'application/x-www-form-urlencoded'}, method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=getattr(settings, 'TURNSTILE_TIMEOUT_SECONDS', 5)) as reply:  # noqa: S310 - fixed https URL
            body = json.loads(reply.read(65536).decode('utf-8'))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        logger.warning('Turnstile verification could not be reached', exc_info=True)
        return UNAVAILABLE
    return OK if body.get('success') is True else FAILED
