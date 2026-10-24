"""Rate limits that must not punish a whole campus for sharing one address."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from apps.core.throttling import ScopedRateThrottle
from apps.forms.models import FieldType, FormStatus
from apps.forms.tests.factories import add_field, make_form
from config.throttle_config import DEFAULT_RATES, resolve_throttle_rates

User = get_user_model()
SHARED = {'HTTP_X_FORWARDED_FOR': 'x, 203.0.113.9'}   # everyone behind one campus address


class CampusDefaults(SimpleTestCase):
    def per_minute(self, scope):
        count, period = DEFAULT_RATES[scope].split('/')
        return int(count) * {'s': 60, 'm': 1, 'h': 1 / 60, 'd': 1 / 1440}[period[0]]

    def test_address_wide_limits_leave_room_for_a_lab(self):
        self.assertGreaterEqual(self.per_minute('anon'), 300)
        self.assertGreaterEqual(self.per_minute('login'), 60)
        self.assertGreaterEqual(self.per_minute('college_search'), 300)
        self.assertGreaterEqual(int(DEFAULT_RATES['register'].split('/')[0]), 60)

    def test_guessing_one_account_is_still_tight(self):
        self.assertLessEqual(self.per_minute('login_account'), 10)

    def test_new_scopes_can_be_tuned_by_environment(self):
        rates = resolve_throttle_rates({'THROTTLE_LOGIN_ACCOUNT': '3/minute', 'THROTTLE_FORM_SUBMIT_ANON': '5/minute'})
        self.assertEqual((rates['login_account'], rates['form_submit_anon']), ('3/minute', '5/minute'))


class SigninIsCountedPerAccountAndAddress(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = APIClient()

    def login(self, email, forwarded='x, 203.0.113.9'):
        return self.client.post('/api/auth/login/', {'email': email, 'password': 'wrong'}, format='json', HTTP_X_FORWARDED_FOR=forwarded).status_code

    def test_one_account_is_stopped_but_a_labmate_on_the_same_address_is_not(self):
        with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {'login_account': '3/minute'}):
            for _ in range(3):
                self.assertNotEqual(self.login('dana@srkr.ac.in'), 429)
            self.assertEqual(self.login('dana@srkr.ac.in'), 429)
            self.assertNotEqual(self.login('lee@srkr.ac.in'), 429)

    def test_a_stranger_elsewhere_cannot_lock_someone_out(self):
        with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {'login_account': '3/minute'}):
            for _ in range(5):
                self.login('dana@srkr.ac.in', forwarded='x, 198.51.100.66')
            self.assertNotEqual(self.login('dana@srkr.ac.in', forwarded='x, 203.0.113.9'), 429)

    def test_the_account_is_matched_ignoring_case_and_spaces(self):
        with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {'login_account': '2/minute'}):
            self.login('Dana@SRKR.ac.in')
            self.login(' dana@srkr.ac.in ')
            self.assertEqual(self.login('DANA@srkr.ac.in'), 429)

    def test_a_missing_or_odd_email_does_not_crash_the_throttle(self):
        for body in ({}, {'email': None}, {'email': ''}, {'email': ['a']}, {'email': {'a': 1}}, {'password': 'x'}):
            resp = self.client.post('/api/auth/login/', body, format='json', **SHARED)
            self.assertLess(resp.status_code, 500, body)

    def test_a_throttled_answer_says_how_long_to_wait(self):
        with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {'login_account': '1/minute'}):
            self.login('dana@srkr.ac.in')
            resp = self.client.post('/api/auth/login/', {'email': 'dana@srkr.ac.in', 'password': 'x'}, format='json', **SHARED)
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.data['code'], 'THROTTLED')
        self.assertGreaterEqual(resp.data['retry_after'], 1)
        self.assertLessEqual(resp.data['retry_after'], 60)
        self.assertIn('wait', resp.data['detail'])
        self.assertTrue(resp.has_header('Retry-After'))


class SubmissionsAreCountedPerPersonNotPerAddress(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.form = make_form(status=FormStatus.PUBLISHED, allow_multiple_responses=True, max_responses_per_user=100)
        self.name = add_field(self.form, FieldType.TEXT, label='Name', required=True)

    def submit(self, user=None):
        client = APIClient()
        if user:
            client.force_authenticate(user)
        return client.post('/api/forms/submissions/', {'form': self.form.id, 'answers': [{'field': self.name.id, 'value': 'x'}]}, format='json', **SHARED).status_code

    def test_signed_in_people_on_one_address_each_get_their_own_budget(self):
        users = [User.objects.create_user(username=f'u{i}', email=f'u{i}@srkr.ac.in', password='x') for i in range(5)]
        with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {'form_submit': '2/minute'}):
            for user in users:
                self.assertEqual([self.submit(user), self.submit(user)], [201, 201], user.username)
            self.assertEqual(self.submit(users[0]), 429)
            self.assertEqual(self.submit(User.objects.create_user(username='late', email='late@srkr.ac.in', password='x')), 201)

    def test_anonymous_callers_share_the_address_rate_which_is_separate_and_higher(self):
        with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {'form_submit': '1/minute', 'form_submit_anon': '3/minute'}):
            self.assertNotIn(429, [self.submit() for _ in range(3)])
            self.assertEqual(self.submit(), 429)
            user = User.objects.create_user(username='member', email='member@srkr.ac.in', password='x')
            self.assertEqual(self.submit(user), 201)


class NothingElseBroke(TestCase):
    def test_other_errors_keep_their_shape(self):
        resp = APIClient().get('/api/forms/does-not-exist/')
        self.assertIn(resp.status_code, (401, 403, 404))
        self.assertNotIn('retry_after', getattr(resp, 'data', {}) or {})
