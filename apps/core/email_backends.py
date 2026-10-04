"""Django email backend that delivers through the Resend HTTPS API.

Used when EMAIL_PROVIDER=resend (staging). HTTPS means it keeps working on hosts that
block outbound SMTP. Standard library only, so no extra dependency.
"""
import base64
import json
import threading
import time
import urllib.error
import urllib.request

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.mail.backends.base import BaseEmailBackend

# Headers Resend builds itself from the dedicated payload fields.
_RESERVED_HEADERS = {'from', 'to', 'cc', 'bcc', 'subject', 'reply-to'}


class ResendError(Exception):
    """Resend rejected the request or could not be reached. Never contains the API key."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class ResendEmailBackend(BaseEmailBackend):
    API_URL = 'https://api.resend.com/emails'
    USER_AGENT = 'srkrcc-backend/1.0'
    TIMEOUT_SECONDS = 15
    MAX_RETRIES = 3
    MAX_RETRY_WAIT_SECONDS = 5
    # Resend allows 2 requests per second per team. Django builds a fresh backend for
    # every message, so the spacing is tracked on the class.
    MIN_INTERVAL_SECONDS = 0.6

    _throttle_lock = threading.Lock()
    _next_allowed = 0.0

    def __init__(self, api_key: str | None = None, fail_silently: bool = False, **kwargs):
        super().__init__(fail_silently=fail_silently, **kwargs)
        self.api_key = api_key if api_key is not None else getattr(settings, 'RESEND_API_KEY', '')

    def send_messages(self, email_messages):
        if not email_messages:
            return 0
        if not self.api_key:
            if self.fail_silently:
                return 0
            raise ImproperlyConfigured('RESEND_API_KEY is not set, so Resend cannot send email.')

        sent = 0
        for message in email_messages:
            if not message.recipients():
                continue
            try:
                self._post(self._payload(message))
                sent += 1
            except Exception:
                if not self.fail_silently:
                    raise
        return sent

    def _payload(self, message) -> dict:
        payload = {
            'from': message.from_email or settings.DEFAULT_FROM_EMAIL,
            'to': list(message.to),
            'subject': message.subject,
        }
        for field, value in (('cc', message.cc), ('bcc', message.bcc), ('reply_to', message.reply_to)):
            if value:
                payload[field] = list(value)

        html = message.body if message.content_subtype == 'html' else None
        text = None if html is not None else message.body
        for content, mimetype in getattr(message, 'alternatives', []):
            if mimetype == 'text/html':
                html = content
        if html is not None:
            payload['html'] = html
        if text:
            payload['text'] = text

        headers = {k: v for k, v in message.extra_headers.items() if k.lower() not in _RESERVED_HEADERS}
        if headers:
            payload['headers'] = headers
        if message.attachments:
            payload['attachments'] = [self._attachment(a) for a in message.attachments]
        return payload

    @staticmethod
    def _attachment(attachment) -> dict:
        if not isinstance(attachment, tuple):
            raise ValueError('The Resend backend only supports attachments added with message.attach(filename, content, mimetype).')
        filename, content, _mimetype = attachment
        raw = content.encode('utf-8') if isinstance(content, str) else content
        return {'filename': filename, 'content': base64.b64encode(raw).decode('ascii')}

    def _throttle(self):
        cls = type(self)
        with cls._throttle_lock:
            wait = cls._next_allowed - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            cls._next_allowed = time.monotonic() + self.MIN_INTERVAL_SECONDS

    def _post(self, payload: dict) -> dict:
        request = urllib.request.Request(
            self.API_URL,
            data=json.dumps(payload).encode('utf-8'),
            method='POST',
            headers={
                'Authorization': f'Bearer {self.api_key}',
                'Content-Type': 'application/json',
                # Resend sits behind Cloudflare, which rejects the default Python user agent.
                'User-Agent': self.USER_AGENT,
            },
        )
        for attempt in range(self.MAX_RETRIES + 1):
            self._throttle()
            try:
                with urllib.request.urlopen(request, timeout=self.TIMEOUT_SECONDS) as response:
                    return json.loads(response.read() or b'{}')
            except urllib.error.HTTPError as error:
                if error.code == 429 and attempt < self.MAX_RETRIES:
                    time.sleep(self._retry_wait(error, attempt))
                    continue
                raise ResendError(f'Resend rejected the email ({error.code}): {self._error_detail(error)}', error.code) from None
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                raise ResendError(f'Could not reach Resend: {error}') from error
        raise ResendError('Resend kept rate limiting the request.', 429)

    def _retry_wait(self, error: urllib.error.HTTPError, attempt: int) -> float:
        try:
            wait = float(error.headers.get('retry-after', ''))
        except (TypeError, ValueError):
            wait = 2 ** attempt
        return min(max(wait, 0), self.MAX_RETRY_WAIT_SECONDS)

    @staticmethod
    def _error_detail(error: urllib.error.HTTPError) -> str:
        try:
            body = json.loads(error.read() or b'{}')
            return str(body.get('message') or body.get('error') or error.reason)
        except (ValueError, AttributeError):
            return str(error.reason)
