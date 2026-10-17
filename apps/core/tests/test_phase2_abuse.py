import csv
import io
import json
from unittest import mock

from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from apps.core.client_ip import get_client_ip
from apps.core.dmc.export_service import _generate_csv, _generate_xlsx, _safe_cell
from apps.core.throttling import ScopedRateThrottle
from config.logging_config import JsonFormatter, build_logging, init_sentry


def request_with(**meta):
    return RequestFactory().get('/', **meta)


@override_settings(TRUSTED_PROXY_COUNT=1, CLIENT_IP_SHARED_SECRET='')
class ClientIPTests(SimpleTestCase):
    def test_uses_the_hop_the_trusted_proxy_appended_not_what_the_client_sent(self):
        request = request_with(HTTP_X_FORWARDED_FOR='6.6.6.6, 203.0.113.9', REMOTE_ADDR='10.0.0.1')
        self.assertEqual(get_client_ip(request), '203.0.113.9')

    def test_a_rotating_forged_header_still_resolves_to_the_same_ip(self):
        seen = {get_client_ip(request_with(HTTP_X_FORWARDED_FOR=f'1.1.1.{i}, 203.0.113.9')) for i in range(50)}
        self.assertEqual(seen, {'203.0.113.9'})

    def test_falls_back_to_remote_addr_without_the_header(self):
        self.assertEqual(get_client_ip(request_with(REMOTE_ADDR='198.51.100.4')), '198.51.100.4')

    def test_two_proxies_count_from_the_right(self):
        with override_settings(TRUSTED_PROXY_COUNT=2):
            request = request_with(HTTP_X_FORWARDED_FOR='9.9.9.9, 203.0.113.9, 10.1.1.1')
            self.assertEqual(get_client_ip(request), '203.0.113.9')

    def test_garbage_in_the_header_falls_back_to_remote_addr(self):
        request = request_with(HTTP_X_FORWARDED_FOR='not-an-ip', REMOTE_ADDR='198.51.100.4')
        self.assertEqual(get_client_ip(request), '198.51.100.4')

    @override_settings(CLIENT_IP_SHARED_SECRET='s3cret-value')
    def test_bff_ip_is_trusted_only_with_the_shared_secret(self):
        good = request_with(HTTP_X_BFF_SECRET='s3cret-value', HTTP_X_CLIENT_IP='203.0.113.50', REMOTE_ADDR='10.0.0.1')
        self.assertEqual(get_client_ip(good), '203.0.113.50')
        wrong = request_with(HTTP_X_BFF_SECRET='guess', HTTP_X_CLIENT_IP='203.0.113.50', REMOTE_ADDR='10.0.0.1')
        self.assertEqual(get_client_ip(wrong), '10.0.0.1')
        missing = request_with(HTTP_X_CLIENT_IP='203.0.113.50', REMOTE_ADDR='10.0.0.1')
        self.assertEqual(get_client_ip(missing), '10.0.0.1')

    def test_client_ip_header_is_ignored_when_no_secret_is_configured(self):
        request = request_with(HTTP_X_BFF_SECRET='', HTTP_X_CLIENT_IP='203.0.113.50', REMOTE_ADDR='10.0.0.1')
        self.assertEqual(get_client_ip(request), '10.0.0.1')


class ThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def tearDown(self):
        cache.clear()

    def login(self, forwarded='1.1.1.1, 203.0.113.9', **extra):
        return self.client.post(
            '/api/auth/login/', {'email': 'nobody@srkr.ac.in', 'password': 'wrong-password'},
            format='json', HTTP_X_FORWARDED_FOR=forwarded, **extra,
        )

    def test_login_is_limited_and_a_rotated_forwarded_header_does_not_reset_it(self):
        with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {'login': '5/minute'}):
            statuses = [self.login(forwarded=f'6.6.6.{i}, 203.0.113.9').status_code for i in range(8)]
        self.assertNotIn(429, statuses[:5])
        self.assertEqual(statuses[5:], [429, 429, 429])

    def test_a_different_real_ip_has_its_own_budget(self):
        with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {'login': '2/minute'}):
            for _ in range(3):
                self.login(forwarded='x, 203.0.113.9')
            self.assertEqual(self.login(forwarded='x, 203.0.113.9').status_code, 429)
            self.assertNotEqual(self.login(forwarded='x, 198.51.100.77').status_code, 429)

    def test_registration_is_limited_per_ip(self):
        payload = {'email': 'bad', 'password': 'x'}
        with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {'register': '3/hour'}):
            statuses = [
                self.client.post('/api/auth/register/', payload, format='json', HTTP_X_FORWARDED_FOR=f'a{i}, 203.0.113.9').status_code
                for i in range(5)
            ]
        self.assertEqual(statuses[3:], [429, 429])

    @override_settings(CLIENT_IP_SHARED_SECRET='s3cret-value')
    def test_visitors_behind_the_bff_are_counted_separately(self):
        def bff_login(visitor):
            return self.client.post(
                '/api/auth/login/', {'email': 'a@srkr.ac.in', 'password': 'x'}, format='json',
                HTTP_X_FORWARDED_FOR='10.0.0.1', HTTP_X_BFF_SECRET='s3cret-value', HTTP_X_CLIENT_IP=visitor,
            ).status_code
        with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {'login': '2/minute'}):
            for _ in range(3):
                bff_login('203.0.113.1')
            self.assertEqual(bff_login('203.0.113.1'), 429)
            self.assertNotEqual(bff_login('203.0.113.2'), 429)


class ExportFormulaInjectionTests(SimpleTestCase):
    def test_safe_cell_neutralises_formula_prefixes(self):
        for dangerous in ('=HYPERLINK("http://evil","x")', '+1+1', '-2+3', '@SUM(A1)', '\tcmd', '\rcmd'):
            self.assertEqual(_safe_cell(dangerous), "'" + dangerous)

    def test_safe_cell_leaves_normal_text_and_the_placeholder_alone(self):
        for ok in ('', 'Alice', '-', 'a=b', '2026-01-01', 'Team +1'):
            self.assertEqual(_safe_cell(ok), ok)

    def _columns(self):
        from apps.core.dmc.contracts import ColumnDefinition
        return [ColumnDefinition(key='name', label='Name')]

    def test_csv_and_xlsx_exports_escape_user_text(self):
        columns = self._columns()
        records = lambda: iter([{'name': '=1+1'}, {'name': 'Bob'}])
        rows = list(csv.reader(io.StringIO(_generate_csv(columns, records()).decode('utf-8-sig'))))
        self.assertEqual(rows[1][0], "'=1+1")
        self.assertEqual(rows[2][0], 'Bob')
        import openpyxl
        workbook = openpyxl.load_workbook(io.BytesIO(_generate_xlsx(columns, records())))
        cells = [row[0].value for row in workbook.active.iter_rows()]
        self.assertEqual(cells, ['Name', "'=1+1", 'Bob'])


class LoggingTests(SimpleTestCase):
    def test_json_formatter_emits_parseable_lines_with_exceptions(self):
        import logging
        import sys
        try:
            raise ValueError('boom')
        except ValueError:
            record = logging.LogRecord('x', logging.ERROR, __file__, 1, 'failed %s', ('thing',), sys.exc_info())
        payload = json.loads(JsonFormatter().format(record))
        self.assertEqual(payload['message'], 'failed thing')
        self.assertEqual(payload['level'], 'ERROR')
        self.assertIn('ValueError', payload['exception'])

    def test_production_uses_json_and_development_uses_text(self):
        self.assertEqual(build_logging(False)['handlers']['console']['formatter'], 'json')
        self.assertEqual(build_logging(True)['handlers']['console']['formatter'], 'text')

    def test_sentry_stays_off_without_a_dsn(self):
        with mock.patch.dict('os.environ', {'SENTRY_DSN': ''}):
            self.assertFalse(init_sentry())
