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
