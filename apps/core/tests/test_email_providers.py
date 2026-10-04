"""EMAIL_PROVIDER selection, the Resend HTTPS backend, and the startup checks."""
import base64
import email.message
import io
import json
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.core.mail import EmailMessage, EmailMultiAlternatives, get_connection
from django.test import SimpleTestCase, override_settings

from apps.core import checks as email_checks
from apps.core.email_backends import ResendEmailBackend, ResendError
from apps.core.services.email_service import EmailNotificationService
from config.email_config import (
    CONSOLE_BACKEND, DEFAULT_SENDER, RESEND_BACKEND, SMTP_BACKEND, resolve_email_settings,
)

RESEND_PATH = 'apps.core.email_backends.ResendEmailBackend'


class ResolveEmailSettingsTests(SimpleTestCase):
    def test_defaults_to_console_in_debug_and_gmail_otherwise(self):
        self.assertEqual(resolve_email_settings({}, debug=True)['EMAIL_PROVIDER'], 'console')
        self.assertEqual(resolve_email_settings({}, debug=False)['EMAIL_PROVIDER'], 'gmail')
        self.assertEqual(resolve_email_settings({'EMAIL_PROVIDER': ''}, debug=False)['EMAIL_PROVIDER'], 'gmail')

    def test_console_prints_to_the_terminal(self):
        resolved = resolve_email_settings({'EMAIL_PROVIDER': 'console'}, debug=False)
        self.assertEqual(resolved['EMAIL_BACKEND'], CONSOLE_BACKEND)
        self.assertEqual(resolved['RESEND_API_KEY'], '')

    def test_resend_uses_the_https_backend_and_the_api_key(self):
        resolved = resolve_email_settings({'EMAIL_PROVIDER': 'resend', 'RESEND_API_KEY': '  re_123  '}, debug=False)
        self.assertEqual(resolved['EMAIL_BACKEND'], RESEND_BACKEND)
        self.assertEqual(resolved['RESEND_API_KEY'], 're_123')
        self.assertEqual(resolved['EMAIL_HOST'], '')

    def test_gmail_is_smtp_on_587_with_tls_and_a_timeout(self):
        resolved = resolve_email_settings({
            'EMAIL_PROVIDER': 'gmail', 'EMAIL_HOST_USER': 'club@gmail.com', 'EMAIL_HOST_PASSWORD': 'abcd efgh ijkl mnop',
        }, debug=False)
        self.assertEqual(resolved['EMAIL_BACKEND'], SMTP_BACKEND)
        self.assertEqual((resolved['EMAIL_HOST'], resolved['EMAIL_PORT'], resolved['EMAIL_USE_TLS']), ('smtp.gmail.com', 587, True))
        self.assertEqual(resolved['EMAIL_HOST_USER'], 'club@gmail.com')
        self.assertEqual(resolved['EMAIL_HOST_PASSWORD'], 'abcdefghijklmnop')
        self.assertEqual(resolved['EMAIL_TIMEOUT'], 20)

    def test_provider_name_is_case_and_whitespace_insensitive(self):
        self.assertEqual(resolve_email_settings({'EMAIL_PROVIDER': ' Resend '}, debug=False)['EMAIL_PROVIDER'], 'resend')

    def test_unknown_provider_fails_loudly_and_lists_the_valid_ones(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            resolve_email_settings({'EMAIL_PROVIDER': 'gsmtp'}, debug=False)
        message = str(ctx.exception)
        self.assertIn("'gsmtp'", message)
        for name in ('console', 'resend', 'gmail'):
            self.assertIn(name, message)

    def test_sender_default_and_override(self):
        self.assertEqual(resolve_email_settings({}, debug=True)['DEFAULT_FROM_EMAIL'], DEFAULT_SENDER)
        custom = resolve_email_settings({'DEFAULT_FROM_EMAIL': 'Club <a@b.com>'}, debug=True)
        self.assertEqual(custom['DEFAULT_FROM_EMAIL'], 'Club <a@b.com>')

    def test_gmail_settings_build_a_working_smtp_connection(self):
        resolved = resolve_email_settings({'EMAIL_PROVIDER': 'gmail', 'EMAIL_HOST_USER': 'u@gmail.com', 'EMAIL_HOST_PASSWORD': 'pw'}, False)
        with override_settings(**{k: v for k, v in resolved.items() if k != 'EMAIL_PROVIDER'}):
            connection = get_connection()
        self.assertEqual(type(connection).__module__, 'django.core.mail.backends.smtp')
        self.assertEqual((connection.host, connection.port, connection.use_tls, connection.timeout),
                         ('smtp.gmail.com', 587, True, 20))
        self.assertEqual((connection.username, connection.password), ('u@gmail.com', 'pw'))


def _ok(body=None):
    response = mock.MagicMock()
    response.read.return_value = json.dumps(body or {'id': 'em_1'}).encode()
    response.__enter__.return_value = response
    return response


def _http_error(code, body=None, headers=None):
    hdrs = email.message.Message()
    for key, value in (headers or {}).items():
        hdrs[key] = value
    return urllib.error.HTTPError('https://api.resend.com/emails', code, 'Error', hdrs, io.BytesIO(json.dumps(body or {}).encode()))


@override_settings(RESEND_API_KEY='re_secret_key', DEFAULT_FROM_EMAIL='Club <noreply@srkrcc.in>')
class ResendBackendTests(SimpleTestCase):
    def setUp(self):
        sleep = mock.patch('apps.core.email_backends.time.sleep')
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)
        ResendEmailBackend._next_allowed = 0.0

    def _backend(self, **kw):
        return ResendEmailBackend(**kw)

    def _send(self, message, urlopen):
        with mock.patch('apps.core.email_backends.urllib.request.urlopen', urlopen):
            return self._backend().send_messages([message])

    def test_sends_html_and_text_with_auth_and_a_custom_user_agent(self):
        message = EmailMultiAlternatives('Hello', 'plain body', 'Club <a@srkrcc.in>', ['x@y.com'], cc=['c@y.com'],
                                         bcc=['b@y.com'], reply_to=['r@y.com'])
        message.attach_alternative('<p>html body</p>', 'text/html')
        urlopen = mock.Mock(return_value=_ok())
        self.assertEqual(self._send(message, urlopen), 1)

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.resend.com/emails')
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(request.get_header('Authorization'), 'Bearer re_secret_key')
        self.assertEqual(request.get_header('User-agent'), 'srkrcc-backend/1.0')
        self.assertEqual(json.loads(request.data), {
            'from': 'Club <a@srkrcc.in>', 'to': ['x@y.com'], 'subject': 'Hello', 'cc': ['c@y.com'], 'bcc': ['b@y.com'],
            'reply_to': ['r@y.com'], 'html': '<p>html body</p>', 'text': 'plain body',
        })

    def test_falls_back_to_the_default_sender(self):
        urlopen = mock.Mock(return_value=_ok())
        self._send(EmailMessage('S', 'b', None, ['x@y.com']), urlopen)
        self.assertEqual(json.loads(urlopen.call_args.args[0].data)['from'], 'Club <noreply@srkrcc.in>')

    def test_html_only_message(self):
        message = EmailMessage('S', '<b>hi</b>', 'a@srkrcc.in', ['x@y.com'])
        message.content_subtype = 'html'
        urlopen = mock.Mock(return_value=_ok())
        self._send(message, urlopen)
        body = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual((body['html'], 'text' in body), ('<b>hi</b>', False))

    def test_attachments_are_base64_and_custom_headers_pass_through(self):
        message = EmailMessage('S', 'b', 'a@srkrcc.in', ['x@y.com'], headers={'X-Entity-Ref-ID': '42', 'Reply-To': 'ignored@y.com'})
        message.attach('report.csv', 'a,b\n1,2\n', 'text/csv')
        urlopen = mock.Mock(return_value=_ok())
        self._send(message, urlopen)
        body = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(body['headers'], {'X-Entity-Ref-ID': '42'})
        self.assertEqual(base64.b64decode(body['attachments'][0]['content']), b'a,b\n1,2\n')
        self.assertEqual(body['attachments'][0]['filename'], 'report.csv')

    def test_messages_without_recipients_are_skipped(self):
        urlopen = mock.Mock(return_value=_ok())
        self.assertEqual(self._send(EmailMessage('S', 'b', 'a@srkrcc.in', []), urlopen), 0)
        urlopen.assert_not_called()

    def test_a_missing_api_key_raises_unless_fail_silently(self):
        message = EmailMessage('S', 'b', 'a@srkrcc.in', ['x@y.com'])
        with override_settings(RESEND_API_KEY=''):
            with self.assertRaises(ImproperlyConfigured):
                self._backend().send_messages([message])
            self.assertEqual(self._backend(fail_silently=True).send_messages([message]), 0)

    def test_a_rejected_email_raises_with_resends_reason_and_never_the_key(self):
        error = _http_error(422, {'name': 'validation_error', 'message': 'The from domain is not verified.'})
        with self.assertRaises(ResendError) as ctx:
            self._send(EmailMessage('S', 'b', 'a@srkrcc.in', ['x@y.com']), mock.Mock(side_effect=error))
        self.assertEqual(ctx.exception.status, 422)
        self.assertIn('from domain is not verified', str(ctx.exception))
        self.assertNotIn('re_secret_key', str(ctx.exception))

    def test_fail_silently_swallows_a_rejection(self):
        with mock.patch('apps.core.email_backends.urllib.request.urlopen', mock.Mock(side_effect=_http_error(500))):
            self.assertEqual(self._backend(fail_silently=True).send_messages([EmailMessage('S', 'b', 'a@srkrcc.in', ['x@y.com'])]), 0)

    def test_network_failure_is_reported_as_unreachable(self):
        with self.assertRaises(ResendError) as ctx:
            self._send(EmailMessage('S', 'b', 'a@srkrcc.in', ['x@y.com']), mock.Mock(side_effect=urllib.error.URLError('timed out')))
        self.assertIn('Could not reach Resend', str(ctx.exception))
        self.assertIsNone(ctx.exception.status)

    def test_rate_limit_is_retried_using_retry_after(self):
        urlopen = mock.Mock(side_effect=[_http_error(429, headers={'retry-after': '2'}), _ok()])
        self.assertEqual(self._send(EmailMessage('S', 'b', 'a@srkrcc.in', ['x@y.com']), urlopen), 1)
        self.assertEqual(urlopen.call_count, 2)
        self.assertIn(mock.call(2.0), self.sleep.call_args_list)

    def test_retry_wait_is_capped(self):
        urlopen = mock.Mock(side_effect=[_http_error(429, headers={'retry-after': '3600'}), _ok()])
        self._send(EmailMessage('S', 'b', 'a@srkrcc.in', ['x@y.com']), urlopen)
        self.assertIn(mock.call(5), self.sleep.call_args_list)

    def test_gives_up_after_the_retry_limit(self):
        urlopen = mock.Mock(side_effect=lambda *a, **k: (_ for _ in ()).throw(_http_error(429, {'message': 'slow down'})))
        with self.assertRaises(ResendError) as ctx:
            self._send(EmailMessage('S', 'b', 'a@srkrcc.in', ['x@y.com']), urlopen)
        self.assertEqual(urlopen.call_count, ResendEmailBackend.MAX_RETRIES + 1)
        self.assertEqual(ctx.exception.status, 429)

    def test_back_to_back_sends_are_spaced_out(self):
        clock = [100.0]
        with mock.patch('apps.core.email_backends.time.monotonic', side_effect=lambda: clock[0]):
            backend = self._backend()
            backend._throttle()
            self.sleep.assert_not_called()
            backend._throttle()
        slept = [c.args[0] for c in self.sleep.call_args_list]
        self.assertEqual(len(slept), 1)
        self.assertAlmostEqual(slept[0], ResendEmailBackend.MIN_INTERVAL_SECONDS)

    def test_a_batch_stops_at_the_first_failure_like_smtp(self):
        urlopen = mock.Mock(side_effect=[_ok(), _http_error(422, {'message': 'bad'}), _ok()])
        messages = [EmailMessage('S', 'b', 'a@srkrcc.in', [f'u{i}@y.com']) for i in range(3)]
        with mock.patch('apps.core.email_backends.urllib.request.urlopen', urlopen):
            with self.assertRaises(ResendError):
                self._backend().send_messages(messages)
        self.assertEqual(urlopen.call_count, 2)


class _FakeResend(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        type(self).seen.append({'path': self.path, 'headers': dict(self.headers), 'body': json.loads(body)})
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"id": "em_live"}')

    def log_message(self, *args):
        pass


@override_settings(EMAIL_BACKEND=RESEND_PATH, RESEND_API_KEY='re_live_key', DEFAULT_FROM_EMAIL='Club <noreply@srkrcc.in>')
class ResendOverRealHttpTests(SimpleTestCase):
    """The Django message API end to end, over a genuine socket to a local stand-in for Resend."""

    def setUp(self):
        _FakeResend.seen = []
        self.server = HTTPServer(('127.0.0.1', 0), _FakeResend)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        patch = mock.patch.object(ResendEmailBackend, 'API_URL', f'http://127.0.0.1:{self.server.server_port}/emails')
        patch.start()
        self.addCleanup(patch.stop)
        ResendEmailBackend._next_allowed = 0.0
        self.addCleanup(setattr, ResendEmailBackend, '_next_allowed', 0.0)

    def test_the_notification_service_delivers_through_the_resend_backend(self):
        from apps.core.models import EmailTemplate
        template = EmailTemplate(name='t', subject_template='Hi {{first_name}}', html_template='<p>Hello {{first_name}}</p>',
                                 text_template='Hello {{first_name}}', allowed_parameters=['first_name'])
        sent = EmailNotificationService.send_single_email('ravi@srkr.ac.in', template, {'first_name': 'Ravi'})
        self.assertTrue(sent)
        self.assertEqual(len(_FakeResend.seen), 1)
        request = _FakeResend.seen[0]
        self.assertEqual(request['path'], '/emails')
        self.assertEqual(request['headers']['Authorization'], 'Bearer re_live_key')
        self.assertEqual(request['headers']['Content-Type'], 'application/json')
        self.assertEqual(request['headers']['User-Agent'], 'srkrcc-backend/1.0')
        self.assertEqual(request['body']['to'], ['ravi@srkr.ac.in'])
        self.assertEqual(request['body']['subject'], 'Hi Ravi')
        self.assertEqual(request['body']['html'], '<p>Hello Ravi</p>')
        self.assertEqual(request['body']['text'], 'Hello Ravi')

    def test_send_mail_uses_the_configured_backend(self):
        from django.core.mail import send_mail
        self.assertEqual(send_mail('Subject', 'Body', None, ['a@b.com']), 1)
        self.assertEqual(_FakeResend.seen[0]['body']['from'], 'Club <noreply@srkrcc.in>')


class EmailChecksTests(SimpleTestCase):
    def _ids(self, **overrides):
        base = dict(EMAIL_PROVIDER='console', EMAIL_BACKEND=CONSOLE_BACKEND, DEBUG=True, RESEND_API_KEY='',
                    EMAIL_HOST_USER='', EMAIL_HOST_PASSWORD='', DEFAULT_FROM_EMAIL='Club <noreply@srkrcc.in>',
                    FRONTEND_URL='https://srkrcc.in')
        base.update(overrides)
        with override_settings(**base):
            return [m.id for m in email_checks.check_email_provider(None)]

    def test_console_is_fine_locally_but_flagged_in_production(self):
        self.assertEqual(self._ids(), [])
        self.assertEqual(self._ids(DEBUG=False), ['core.W004'])
        # Django swaps in an in-memory backend while testing, which must not trip the warning.
        self.assertEqual(self._ids(DEBUG=False, EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend'), [])

    def test_resend_needs_an_api_key(self):
        self.assertEqual(self._ids(EMAIL_PROVIDER='resend'), ['core.W001'])
        self.assertEqual(self._ids(EMAIL_PROVIDER='resend', RESEND_API_KEY='re_x'), [])

    def test_resend_rejects_free_mailbox_senders(self):
        ids = self._ids(EMAIL_PROVIDER='resend', RESEND_API_KEY='re_x', DEFAULT_FROM_EMAIL='Club <club@gmail.com>')
        self.assertEqual(ids, ['core.W002'])
        ok = self._ids(EMAIL_PROVIDER='resend', RESEND_API_KEY='re_x', DEFAULT_FROM_EMAIL='onboarding@resend.dev')
        self.assertEqual(ok, [])

    def test_a_real_provider_with_a_localhost_frontend_url_is_flagged(self):
        for url in ('http://localhost:3000', 'http://127.0.0.1:3000'):
            ids = self._ids(EMAIL_PROVIDER='resend', RESEND_API_KEY='re_x', DEBUG=False, FRONTEND_URL=url)
            self.assertEqual(ids, ['core.W005'], url)
        # In development a localhost frontend is correct.
        self.assertEqual(self._ids(EMAIL_PROVIDER='resend', RESEND_API_KEY='re_x', FRONTEND_URL='http://localhost:3000'), [])
        self.assertEqual(self._ids(FRONTEND_URL='http://localhost:3000'), [])  # console: links are only printed

    def test_nothing_is_checked_while_testing_with_the_in_memory_backend(self):
        ids = self._ids(EMAIL_PROVIDER='resend', RESEND_API_KEY='', DEBUG=False, FRONTEND_URL='http://localhost:3000',
                        EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
        self.assertEqual(ids, [])

    def test_gmail_needs_credentials(self):
        self.assertEqual(self._ids(EMAIL_PROVIDER='gmail', DEBUG=False), ['core.W003'])
        self.assertEqual(self._ids(EMAIL_PROVIDER='gmail', DEBUG=False, EMAIL_HOST_USER='club@gmail.com'), ['core.W003'])

    def test_gmail_notes_when_the_sender_differs_from_the_account(self):
        base = dict(EMAIL_PROVIDER='gmail', DEBUG=False, EMAIL_HOST_USER='club@gmail.com', EMAIL_HOST_PASSWORD='pw')
        self.assertEqual(self._ids(**base, DEFAULT_FROM_EMAIL='Club <noreply@srkrcc.in>'), ['core.I001'])
        self.assertEqual(self._ids(**base, DEFAULT_FROM_EMAIL='SRKR Coding Club <CLUB@gmail.com>'), [])
