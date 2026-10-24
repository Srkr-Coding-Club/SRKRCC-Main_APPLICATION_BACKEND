import warnings

from django.test import SimpleTestCase, override_settings

from config.security_config import resolve_health_check_paths, resolve_hsts_settings
from config.throttle_config import DEFAULT_RATES, resolve_throttle_rates


class ThrottleRateTests(SimpleTestCase):
    def test_defaults_are_the_campus_friendly_ones(self):
        self.assertEqual(resolve_throttle_rates({}), DEFAULT_RATES)
        self.assertEqual(DEFAULT_RATES['login'], '120/minute')
        self.assertEqual(DEFAULT_RATES['register'], '120/hour')
        self.assertEqual(DEFAULT_RATES['anon'], '600/minute')
        self.assertEqual(DEFAULT_RATES['login_account'], '10/minute')

    def test_environment_overrides_one_rate_and_leaves_the_rest(self):
        rates = resolve_throttle_rates({'THROTTLE_REGISTER': '200/hour'})
        self.assertEqual(rates['register'], '200/hour')
        self.assertEqual(rates['login'], DEFAULT_RATES['login'])

    def test_every_scope_has_a_variable(self):
        env = {f'THROTTLE_{scope.upper()}': '7/day' for scope in DEFAULT_RATES}
        self.assertTrue(all(rate == '7/day' for rate in resolve_throttle_rates(env).values()))

    def test_blank_and_whitespace_keep_the_default(self):
        rates = resolve_throttle_rates({'THROTTLE_LOGIN': '', 'THROTTLE_ANON': '   '})
        self.assertEqual(rates['login'], DEFAULT_RATES['login'])
        self.assertEqual(rates['anon'], DEFAULT_RATES['anon'])

    def test_a_value_drf_cannot_parse_is_ignored_with_a_warning(self):
        for bad in ('fast', '10', '10/', '/minute', 'ten/minute', '10/week'):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter('always')
                rates = resolve_throttle_rates({'THROTTLE_LOGIN': bad})
            self.assertEqual(rates['login'], DEFAULT_RATES['login'], bad)
            self.assertTrue(any('THROTTLE_LOGIN' in str(w.message) for w in caught), bad)

    def test_the_test_runner_lifts_sign_in_and_sign_up_limits(self):
        rates = resolve_throttle_rates({}, testing=True)
        self.assertEqual(rates['login'], '100000/minute')
        self.assertEqual(rates['register'], '100000/hour')
        self.assertEqual(rates['anon'], DEFAULT_RATES['anon'])

    def test_the_test_runner_ignores_the_environment(self):
        env = {f'THROTTLE_{scope.upper()}': '1/day' for scope in DEFAULT_RATES}
        self.assertEqual(resolve_throttle_rates(env, testing=True), resolve_throttle_rates({}, testing=True))


class HstsSettingTests(SimpleTestCase):
    def test_defaults_are_the_campus_friendly_ones(self):
        self.assertEqual(
            resolve_hsts_settings({}),
            {'SECURE_HSTS_SECONDS': 31536000, 'SECURE_HSTS_INCLUDE_SUBDOMAINS': True, 'SECURE_HSTS_PRELOAD': True},
        )

    def test_blank_values_keep_the_defaults(self):
        resolved = resolve_hsts_settings(
            {'SECURE_HSTS_SECONDS': '', 'SECURE_HSTS_INCLUDE_SUBDOMAINS': '', 'SECURE_HSTS_PRELOAD': ''}
        )
        self.assertEqual(resolved['SECURE_HSTS_SECONDS'], 31536000)
        self.assertTrue(resolved['SECURE_HSTS_INCLUDE_SUBDOMAINS'])
        self.assertTrue(resolved['SECURE_HSTS_PRELOAD'])

    def test_each_part_can_be_turned_down(self):
        resolved = resolve_hsts_settings(
            {'SECURE_HSTS_SECONDS': '3600', 'SECURE_HSTS_INCLUDE_SUBDOMAINS': 'False', 'SECURE_HSTS_PRELOAD': 'false'}
        )
        self.assertEqual(resolved['SECURE_HSTS_SECONDS'], 3600)
        self.assertFalse(resolved['SECURE_HSTS_INCLUDE_SUBDOMAINS'])
        self.assertFalse(resolved['SECURE_HSTS_PRELOAD'])

    def test_zero_turns_the_header_off(self):
        self.assertEqual(resolve_hsts_settings({'SECURE_HSTS_SECONDS': '0'})['SECURE_HSTS_SECONDS'], 0)

    def test_a_negative_value_is_treated_as_off(self):
        self.assertEqual(resolve_hsts_settings({'SECURE_HSTS_SECONDS': '-5'})['SECURE_HSTS_SECONDS'], 0)

    def test_a_value_that_is_not_a_number_keeps_the_default_with_a_warning(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            resolved = resolve_hsts_settings({'SECURE_HSTS_SECONDS': 'a year'})
        self.assertEqual(resolved['SECURE_HSTS_SECONDS'], 31536000)
        self.assertTrue(any('SECURE_HSTS_SECONDS' in str(w.message) for w in caught))


class HealthCheckPathSettingTests(SimpleTestCase):
    def test_default_covers_the_root_and_the_ping_route(self):
        self.assertEqual(resolve_health_check_paths({}), ('/', '/api/ping', '/api/ping/'))

    def test_custom_paths_get_a_leading_slash(self):
        self.assertEqual(
            resolve_health_check_paths({'HEALTH_CHECK_PATHS': 'healthz, /ready/'}), ('/healthz', '/ready/'),
        )


@override_settings(
    ALLOWED_HOSTS=['api.example.com', 'localhost'],
    HEALTH_CHECK_PATHS=('/api/ping/',),
)
class HealthCheckHostMiddlewareTests(SimpleTestCase):
    PRIVATE_IP_HOST = '10.0.3.17:8000'

    def test_the_health_check_is_answered_for_a_private_ip_host(self):
        response = self.client.get('/api/ping/', HTTP_HOST=self.PRIVATE_IP_HOST)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'ok')

    def test_a_head_request_is_also_answered(self):
        self.assertEqual(self.client.head('/api/ping/', HTTP_HOST=self.PRIVATE_IP_HOST).status_code, 200)

    def test_other_paths_still_reject_an_unlisted_host(self):
        self.assertEqual(self.client.get('/api/auth/login/', HTTP_HOST=self.PRIVATE_IP_HOST).status_code, 400)

    def test_a_write_to_the_health_path_is_not_exempt(self):
        self.assertEqual(self.client.post('/api/ping/', HTTP_HOST=self.PRIVATE_IP_HOST).status_code, 400)

    def test_a_listed_host_works_as_before(self):
        self.assertEqual(self.client.get('/api/ping/', HTTP_HOST='api.example.com').status_code, 200)

    def test_a_forwarded_host_header_cannot_override_the_safe_host(self):
        response = self.client.get(
            '/api/ping/', HTTP_HOST=self.PRIVATE_IP_HOST, HTTP_X_FORWARDED_HOST='evil.example',
        )
        self.assertEqual(response.status_code, 200)
