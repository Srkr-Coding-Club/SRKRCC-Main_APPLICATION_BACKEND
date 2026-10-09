from datetime import timedelta

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from .models import Difficulty, Problem


class ProblemVisibilityTests(APITestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.today_problem = Problem.objects.create(
            title='Today challenge',
            slug='today-challenge',
            difficulty=Difficulty.EASY,
            statement='Available today.',
            scheduled_date=self.today,
        )
        self.previous_problem = Problem.objects.create(
            title='Previous challenge',
            slug='previous-challenge',
            difficulty=Difficulty.EASY,
            statement='Available in the archive.',
            scheduled_date=self.today - timedelta(days=1),
        )
        self.future_problem = Problem.objects.create(
            title='Future challenge',
            slug='future-challenge',
            difficulty=Difficulty.MEDIUM,
            statement='Not available yet.',
            scheduled_date=self.today + timedelta(days=1),
        )
        user_model = get_user_model()
        self.member = user_model.objects.create_user(
            email='member@example.com', username='member', password='safe-password'
        )
        self.admin = user_model.objects.create_user(
            email='admin@example.com', username='admin', password='safe-password', role='ADMIN'
        )

    def test_public_and_member_lists_include_today_and_the_archive(self):
        response = self.client.get(reverse('codequest-problem-list'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {item['slug'] for item in response.data},
            {'today-challenge', 'previous-challenge'},
        )

        self.client.force_authenticate(self.member)
        response = self.client.get(reverse('codequest-problem-list'))
        self.assertEqual(
            {item['slug'] for item in response.data},
            {'today-challenge', 'previous-challenge'},
        )

    def test_admin_can_see_scheduling_calendar(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get(reverse('codequest-problem-list'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {item['slug'] for item in response.data},
            {'today-challenge', 'previous-challenge', 'future-challenge'},
        )

    def test_member_cannot_submit_a_future_problem(self):
        self.client.force_authenticate(self.member)
        response = self.client.post(
            reverse('codequest-submission-list'),
            {'problem': self.future_problem.pk, 'code': 'print(1)', 'language': 'python'},
            format='json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('problem', response.data)

    def test_batch_schedule_route_is_not_captured_as_a_problem_slug(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            reverse('codequest-batch-schedule'),
            {
                'problems': [{
                    'title': 'Batch route challenge',
                    'difficulty': 'MEDIUM',
                    'statement': 'A complete scheduled challenge.',
                    'scheduled_date': (self.today + timedelta(days=2)).isoformat(),
                    'external_url': 'https://leetcode.com/problems/two-sum/',
                }],
            },
            format='json',
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['problems'][0]['title'], 'Batch route challenge')


class BatchScheduleValidationTests(APITestCase):
    def setUp(self):
        self.today = timezone.localdate()
        user_model = get_user_model()
        self.admin = user_model.objects.create_user(
            email='batch-admin@example.com', username='batch-admin', password='safe-password', role='ADMIN'
        )
        self.client.force_authenticate(self.admin)
        self.url = reverse('codequest-batch-schedule')

    def payload(self, **overrides):
        entry = {
            'title': 'Batch validation challenge',
            'difficulty': 'MEDIUM',
            'statement': 'A complete scheduled challenge.',
            'scheduled_date': (self.today + timedelta(days=3)).isoformat(),
            'external_url': 'https://leetcode.com/problems/two-sum/',
        }
        entry.update(overrides)
        return {'problems': [entry]}

    def post_batch(self, payload):
        return self.client.post(self.url, payload, format='json')

    def assertRejected(self, response, needle, expected_problems=0):
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn(needle, str(response.data))
        self.assertEqual(Problem.objects.count(), expected_problems)

    def test_empty_external_url_is_rejected(self):
        response = self.post_batch(self.payload(external_url=''))
        self.assertRejected(response, 'external_url')

    def test_https_external_url_is_accepted(self):
        url = 'https://leetcode.com/problems/two-sum/'
        response = self.post_batch(self.payload(external_url=url))
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Problem.objects.get().external_url, url)

    def test_http_external_url_rejects_the_batch(self):
        response = self.post_batch(self.payload(external_url='http://leetcode.com/problems/two-sum/'))
        self.assertRejected(response, 'external_url')

    def test_malformed_external_url_rejects_the_batch(self):
        response = self.post_batch(self.payload(external_url='not-a-url'))
        self.assertRejected(response, 'external_url')

    def test_non_https_schemes_reject_the_batch(self):
        for index, url in enumerate(('javascript:alert(1)', 'ftp://example.com/problem')):
            with self.subTest(url=url):
                response = self.post_batch(self.payload(
                    scheduled_date=(self.today + timedelta(days=4 + index)).isoformat(),
                    external_url=url,
                ))
                self.assertRejected(response, 'external_url')

    def test_previous_date_rejects_the_batch(self):
        yesterday = self.today - timedelta(days=1)
        response = self.post_batch(self.payload(scheduled_date=yesterday.isoformat()))
        self.assertRejected(response, yesterday.isoformat())
        self.assertIn('cannot be before today', str(response.data))

    def test_today_is_accepted(self):
        response = self.post_batch(self.payload(scheduled_date=self.today.isoformat()))
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Problem.objects.get().scheduled_date, self.today)

    def test_future_date_is_accepted(self):
        response = self.post_batch(self.payload(scheduled_date=(self.today + timedelta(days=1)).isoformat()))
        self.assertEqual(response.status_code, 201, response.data)

    def test_already_scheduled_date_rejects_the_batch(self):
        taken = self.today + timedelta(days=5)
        Problem.objects.create(
            title='Existing challenge',
            slug='existing-challenge',
            difficulty=Difficulty.EASY,
            statement='Already booked.',
            scheduled_date=taken,
        )
        response = self.post_batch(self.payload(scheduled_date=taken.isoformat()))
        self.assertRejected(response, taken.isoformat(), expected_problems=1)

    def test_duplicate_dates_inside_the_batch_are_rejected(self):
        date = (self.today + timedelta(days=6)).isoformat()
        response = self.post_batch({
            'problems': [
                {'title': 'First duplicate date', 'difficulty': 'EASY', 'statement': 'First.', 'scheduled_date': date, 'external_url': 'https://leetcode.com/problems/a/'},
                {'title': 'Second duplicate date', 'difficulty': 'HARD', 'statement': 'Second.', 'scheduled_date': date, 'external_url': 'https://leetcode.com/problems/b/'},
            ],
        })
        self.assertRejected(response, 'unique')

    def test_valid_batch_is_created(self):
        response = self.post_batch({
            'problems': [
                {'title': 'First batch problem', 'difficulty': 'EASY', 'statement': 'First.', 'scheduled_date': (self.today + timedelta(days=1)).isoformat(), 'external_url': 'https://leetcode.com/problems/a/'},
                {'title': 'Second batch problem', 'difficulty': 'MEDIUM', 'statement': 'Second.', 'scheduled_date': (self.today + timedelta(days=2)).isoformat(), 'external_url': 'https://leetcode.com/problems/b/'},
                {'title': 'Third batch problem', 'difficulty': 'HARD', 'statement': 'Third.', 'scheduled_date': (self.today + timedelta(days=3)).isoformat(), 'external_url': 'https://www.geeksforgeeks.org/problems/example/'},
            ],
        })
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Problem.objects.count(), 3)

    def test_mixed_valid_and_invalid_batch_creates_nothing(self):
        response = self.post_batch({
            'problems': [
                {'title': 'Valid batch problem', 'difficulty': 'EASY', 'statement': 'Valid.', 'scheduled_date': (self.today + timedelta(days=7)).isoformat(), 'external_url': 'https://leetcode.com/problems/two-sum/'},
                {'title': 'Invalid batch problem', 'difficulty': 'HARD', 'statement': 'Invalid.', 'scheduled_date': (self.today + timedelta(days=8)).isoformat(), 'external_url': 'http://leetcode.com/problems/two-sum/'},
            ],
        })
        self.assertRejected(response, 'external_url')


class ProblemSerializerValidationTests(APITestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.admin = get_user_model().objects.create_user(
            email='serializer-admin@example.com', username='serializer-admin', password='safe-password', role='ADMIN'
        )
        self.client.force_authenticate(self.admin)
        self.list_url = reverse('codequest-problem-list')

    def payload(self, **overrides):
        data = {
            'title': 'Validator problem',
            'difficulty': 'EASY',
            'statement': 'A challenge needing proper scheduling rules.',
            'scheduled_date': (self.today + timedelta(days=10)).isoformat(),
            'external_url': 'https://leetcode.com/problems/two-sum/',
        }
        data.update(overrides)
        return data

    def test_create_rejects_a_past_scheduled_date(self):
        yesterday = (self.today - timedelta(days=3)).isoformat()
        response = self.client.post(self.list_url, self.payload(scheduled_date=yesterday), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('cannot be before today', str(response.data['scheduled_date']))

    def test_create_rejects_a_date_that_already_has_a_problem(self):
        taken = self.today + timedelta(days=10)
        Problem.objects.create(title='Occupied', slug='occupied-date', difficulty='EASY', statement='x', scheduled_date=taken)
        response = self.client.post(self.list_url, self.payload(scheduled_date=taken.isoformat()), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('already scheduled', str(response.data['scheduled_date']))

    def test_create_rejects_http_external_url(self):
        response = self.client.post(self.list_url, self.payload(external_url='http://leetcode.com/problems/two-sum/'), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('external_url', response.data)

    def test_create_rejects_an_empty_external_url(self):
        response = self.client.post(self.list_url, self.payload(external_url=''), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('external_url', response.data)

    def test_create_rejects_a_missing_external_url(self):
        payload = self.payload()
        payload.pop('external_url')
        response = self.client.post(self.list_url, payload, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('external_url', response.data)

    def test_create_accepts_a_future_date_with_https_url(self):
        response = self.client.post(self.list_url, self.payload(), format='json')
        self.assertEqual(response.status_code, 201, response.data)

    def test_update_can_keep_an_existing_past_date(self):
        past = self.today - timedelta(days=1)
        problem = Problem.objects.create(title='Yesterday challenge', slug='yesterday-challenge', statement='x', scheduled_date=past)
        url = reverse('codequest-problem-detail', kwargs={'slug': problem.slug})
        response = self.client.patch(url, {'scheduled_date': past.isoformat(), 'statement': 'Updated statement'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)

    def test_update_rejects_moving_a_problem_to_a_past_date(self):
        problem = Problem.objects.create(title='Mover', slug='mover', statement='x', scheduled_date=self.today + timedelta(days=1))
        url = reverse('codequest-problem-detail', kwargs={'slug': problem.slug})
        response = self.client.patch(url, {'scheduled_date': (self.today - timedelta(days=1)).isoformat()}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('cannot be before today', str(response.data['scheduled_date']))

    def test_update_rejects_moving_to_an_occupied_date(self):
        target = self.today + timedelta(days=5)
        Problem.objects.create(title='Holder', slug='holder', statement='x', scheduled_date=target)
        problem = Problem.objects.create(title='Mover two', slug='mover-two', statement='x', scheduled_date=self.today + timedelta(days=6))
        url = reverse('codequest-problem-detail', kwargs={'slug': problem.slug})
        response = self.client.patch(url, {'scheduled_date': target.isoformat()}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('already scheduled', str(response.data['scheduled_date']))
