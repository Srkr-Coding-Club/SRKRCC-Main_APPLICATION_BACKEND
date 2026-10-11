"""Tests for the admin-only audit trail endpoint and its query filters."""

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APITestCase

from .models import AuditLog

User = get_user_model()


class AuditLogTrailTests(APITestCase):
    def setUp(self):
        self.member = User.objects.create_user(
            email='member@example.com', username='member', password='safe-password'
        )
        self.admin = User.objects.create_user(
            email='admin@example.com', username='admin', password='safe-password', role='ADMIN'
        )

    def make_event(self, action, target_model='Problem', target_id='1'):
        return AuditLog.objects.create(
            action=action, target_model=target_model, target_id=target_id
        )

    def test_member_is_denied(self):
        self.client.force_authenticate(self.member)
        response = self.client.get(reverse('auditlog-list'))
        self.assertEqual(response.status_code, 403)

    def test_action_prefix_scopes_to_module_events(self):
        self.make_event('codequest.problem_created')
        self.make_event('Deleted Form', target_model='Form')

        self.client.force_authenticate(self.admin)
        response = self.client.get(reverse('auditlog-list'), {'action_prefix': 'codequest.'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row['action'] for row in response.data], ['codequest.problem_created'])

    def test_target_model_filter_is_case_insensitive(self):
        self.make_event('codequest.problem_created', target_model='Problem')
        self.make_event('codequest.submission_reviewed', target_model='Submission')

        self.client.force_authenticate(self.admin)
        response = self.client.get(reverse('auditlog-list'), {'target_model': 'submission'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['target_model'], 'Submission')

    def test_limit_caps_the_list(self):
        for index in range(5):
            self.make_event(f'codequest.event_{index}')

        self.client.force_authenticate(self.admin)
        response = self.client.get(reverse('auditlog-list'), {'limit': 2})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 2)

    def test_invalid_limit_is_rejected(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get(reverse('auditlog-list'), {'limit': 'zero'})
        self.assertEqual(response.status_code, 400)
