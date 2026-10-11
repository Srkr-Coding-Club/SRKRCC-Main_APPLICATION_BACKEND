from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from apps.events.models import Event, EventStatus
from apps.feature_flags.models import FeatureFlag

User = get_user_model()


class PublicCacheControlTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_superuser(
            username='cacheadmin',
            email='cacheadmin@srkr.ac.in',
            password='Password@123',
            role='ADMIN',
        )
        self.event = Event.objects.create(
            title='Caching Tech Workshop',
            slug='caching-tech-workshop',
            status=EventStatus.LIVE,
        )
        self.flag = FeatureFlag.objects.create(
            key='events',
            name='Events Module',
            is_enabled=True,
        )

    def test_anonymous_get_receives_public_cache_control(self):
        response = self.client.get('/api/events/')
        self.assertEqual(response.status_code, 200)
        cache_control = response.headers.get('Cache-Control', '')
        self.assertIn('public', cache_control)
        self.assertIn('max-age=60', cache_control)
        self.assertIn('stale-while-revalidate=120', cache_control)

    def test_anonymous_feature_flag_receives_public_cache_control(self):
        response = self.client.get('/api/feature-flags/')
        self.assertEqual(response.status_code, 200)
        cache_control = response.headers.get('Cache-Control', '')
        self.assertIn('public', cache_control)
        self.assertIn('max-age=60', cache_control)

    def test_authenticated_get_receives_private_no_cache(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.get('/api/events/')
        self.assertEqual(response.status_code, 200)
        cache_control = response.headers.get('Cache-Control', '')
        self.assertEqual(cache_control, 'private, no-cache')

    def test_post_does_not_receive_public_cache_control(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.post('/api/events/caching-tech-workshop/close/')
        self.assertEqual(response.status_code, 200)
        cache_control = response.headers.get('Cache-Control', '')
        self.assertNotIn('public', cache_control)
