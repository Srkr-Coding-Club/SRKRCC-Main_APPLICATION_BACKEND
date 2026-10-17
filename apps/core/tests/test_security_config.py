from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from config.security_config import clean_origin, resolve_security_settings

PROD = {
    'ALLOWED_HOSTS': 'api.srkrcc.in',
    'FRONTEND_URL': 'https://srkrcc.in/',
    'CORS_ALLOWED_ORIGINS': 'https://srkrcc.in/, https://www.srkrcc.in',
    'CSRF_TRUSTED_ORIGINS': 'https://srkrcc.in',
}


def prod(**overrides):
    return resolve_security_settings({**PROD, **overrides}, debug=False)


class CleanOriginTests(SimpleTestCase):
    def test_normalises_scheme_path_and_slash(self):
        self.assertEqual(clean_origin('https://srkrcc.in/some/path/'), 'https://srkrcc.in')
        self.assertEqual(clean_origin('srkrcc.in'), 'https://srkrcc.in')
        self.assertEqual(clean_origin('  '), '')


class ProductionSettingsTests(SimpleTestCase):
    def test_only_the_named_hosts_and_loopback_are_allowed(self):
        resolved = prod()
        self.assertIn('api.srkrcc.in', resolved['ALLOWED_HOSTS'])
        self.assertNotIn('*', resolved['ALLOWED_HOSTS'])
        self.assertNotIn('.onrender.com', resolved['ALLOWED_HOSTS'])

    def test_missing_allowed_hosts_fails_fast(self):
        with self.assertRaisesMessage(ImproperlyConfigured, 'ALLOWED_HOSTS'):
            resolve_security_settings({'FRONTEND_URL': 'https://srkrcc.in'}, debug=False)

    def test_render_adds_its_hostname_suffix_without_a_wildcard_default(self):
        resolved = resolve_security_settings({'FRONTEND_URL': 'https://srkrcc.in', 'RENDER': 'true'}, debug=False)
        self.assertIn('.onrender.com', resolved['ALLOWED_HOSTS'])

    def test_cors_and_csrf_use_only_listed_origins(self):
        resolved = prod()
        self.assertEqual(resolved['CORS_ALLOWED_ORIGINS'], ['https://srkrcc.in', 'https://www.srkrcc.in'])
        self.assertEqual(resolved['CSRF_TRUSTED_ORIGINS'], ['https://srkrcc.in'])
        self.assertEqual(resolved['CORS_ALLOWED_ORIGIN_REGEXES'], [])
        everything = ' '.join(resolved['CORS_ALLOWED_ORIGINS'] + resolved['CSRF_TRUSTED_ORIGINS'])
        self.assertNotIn('vercel', everything)
        self.assertNotIn('onrender', everything)
        self.assertNotIn('localhost', everything)

    def test_credentialed_cors_is_off_unless_asked_for(self):
        self.assertFalse(prod()['CORS_ALLOW_CREDENTIALS'])
        self.assertTrue(prod(CORS_ALLOW_CREDENTIALS='true')['CORS_ALLOW_CREDENTIALS'])

    def test_allow_all_origins_is_refused(self):
        with self.assertRaisesMessage(ImproperlyConfigured, 'CORS_ALLOW_ALL_ORIGINS'):
            prod(CORS_ALLOW_ALL_ORIGINS='True')

    def test_frontend_url_must_be_public(self):
        self.assertEqual(prod()['FRONTEND_URL'], 'https://srkrcc.in')
        for bad in ({'FRONTEND_URL': 'http://localhost:3000'}, {'FRONTEND_URL': 'http://127.0.0.1:3000'}):
            with self.assertRaisesMessage(ImproperlyConfigured, 'FRONTEND_URL'):
                prod(**bad)
        env = dict(PROD)
        del env['FRONTEND_URL']
        with self.assertRaisesMessage(ImproperlyConfigured, 'FRONTEND_URL'):
            resolve_security_settings(env, debug=False)


class DevelopmentSettingsTests(SimpleTestCase):
    def resolve(self, **env):
        return resolve_security_settings(env, debug=True)

    def test_everything_local_works_out_of_the_box(self):
        resolved = self.resolve()
        self.assertEqual(resolved['ALLOWED_HOSTS'], ['*'])
        self.assertIn('http://localhost:3000', resolved['CORS_ALLOWED_ORIGINS'])
        self.assertIn('http://localhost:3000', resolved['CSRF_TRUSTED_ORIGINS'])
        self.assertTrue(resolved['CORS_ALLOW_ALL_ORIGINS'])
        self.assertTrue(resolved['CORS_ALLOW_CREDENTIALS'])
        self.assertEqual(resolved['FRONTEND_URL'], 'http://localhost:3000')
        self.assertTrue(resolved['CORS_ALLOWED_ORIGIN_REGEXES'])

    def test_explicit_hosts_are_kept_alongside_loopback(self):
        hosts = self.resolve(ALLOWED_HOSTS='dev.local')['ALLOWED_HOSTS']
        self.assertIn('dev.local', hosts)
        self.assertIn('testserver', hosts)
        self.assertNotIn('*', hosts)
