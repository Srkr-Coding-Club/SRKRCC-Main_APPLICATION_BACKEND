from unittest import mock
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient
from apps.accounts.models import UserRole
from apps.notifications.models import Notification, NotificationType, NotificationCategory
from apps.notifications.services import NotificationService

User = get_user_model()


class NotificationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user1 = User.objects.create_user(
            email='alice@srkr.ac.in',
            username='alice',
            password='TestPassword123!',
            first_name='Alice',
            role=UserRole.AFFILIATE,
        )
        self.user2 = User.objects.create_user(
            email='bob@srkr.ac.in',
            username='bob',
            password='TestPassword123!',
            first_name='Bob',
            role=UserRole.AFFILIATE,
        )
        self.admin = User.objects.create_user(
            email='admin@srkr.ac.in',
            username='admin',
            password='TestPassword123!',
            first_name='Admin',
            role=UserRole.ADMIN,
        )

    def test_list_notifications_and_unread_count(self):
        # Create notifications for Alice
        n1 = NotificationService.create_notification(
            recipient=self.user1,
            title='Welcome to SRKRCC',
            message='Glad to have you with us!',
            type=NotificationType.SUCCESS,
        )
        n2 = NotificationService.create_notification(
            recipient=self.user1,
            title='Hackathon Announcement',
            message='IconCoders round 1 released',
            type=NotificationType.INFO,
        )
        # Create notification for Bob
        NotificationService.create_notification(
            recipient=self.user2,
            title='Bob only notification',
            message='Private message',
        )

        self.client.force_authenticate(user=self.user1)
        resp = self.client.get('/api/notifications/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['unread_count'], 2)
        self.assertEqual(len(resp.data['results']), 2)
        self.assertEqual(resp.data['results'][0]['id'], n2.id)

        # Quick unread count endpoint
        resp_count = self.client.get('/api/notifications/unread-count/')
        self.assertEqual(resp_count.status_code, 200)
        self.assertEqual(resp_count.data['unread_count'], 2)

    def test_mark_as_read_and_mark_all_read(self):
        n1 = NotificationService.create_notification(self.user1, 'Note 1', 'Message 1')
        n2 = NotificationService.create_notification(self.user1, 'Note 2', 'Message 2')

        self.client.force_authenticate(user=self.user1)

        # Mark single as read
        resp1 = self.client.post(f'/api/notifications/{n1.id}/read/')
        self.assertEqual(resp1.status_code, 200)
        self.assertTrue(resp1.data['notification']['is_read'])
        self.assertEqual(resp1.data['unread_count'], 1)

        # Mark all read
        resp2 = self.client.post('/api/notifications/mark-all-read/')
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.data['marked_count'], 1)
        self.assertEqual(resp2.data['unread_count'], 0)

        n2.refresh_from_db()
        self.assertTrue(n2.is_read)

    def test_delete_notification(self):
        n1 = NotificationService.create_notification(self.user1, 'Note to delete', 'Msg')
        self.client.force_authenticate(user=self.user1)

        resp = self.client.delete(f'/api/notifications/{n1.id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['deleted'])
        self.assertFalse(Notification.objects.filter(id=n1.id).exists())

    def test_isolation_between_users(self):
        n1 = NotificationService.create_notification(self.user1, 'Secret Note', 'Msg')
        self.client.force_authenticate(user=self.user2)

        # Bob cannot mark Alice's notification as read
        resp = self.client.post(f'/api/notifications/{n1.id}/read/')
        self.assertEqual(resp.status_code, 404)

        # Bob cannot delete Alice's notification
        resp_del = self.client.delete(f'/api/notifications/{n1.id}/')
        self.assertEqual(resp_del.status_code, 404)

    @mock.patch('apps.notifications.services.run_in_background')
    def test_admin_broadcast_in_app_and_email(self, mock_bg):
        self.client.force_authenticate(user=self.admin)

        payload = {
            'title': 'Grand Hackathon Announcement',
            'message': 'All members please assemble at 9 AM.',
            'type': 'URGENT',
            'category': 'HACKATHON',
            'link_url': '/hackathons/iconcoders',
            'channels': ['IN_APP', 'EMAIL'],
            'audience': 'ALL',
        }

        resp = self.client.post('/api/notifications/broadcast/', payload, format='json')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['total_recipients'], 3)
        self.assertEqual(resp.data['in_app_count'], 3)
        self.assertEqual(resp.data['email_count'], 3)
        self.assertTrue(mock_bg.called)

        # Check Alice received the in-app notification
        alice_notifs = Notification.objects.filter(recipient=self.user1)
        self.assertEqual(alice_notifs.count(), 1)
        self.assertEqual(alice_notifs.first().title, 'Grand Hackathon Announcement')
        self.assertEqual(alice_notifs.first().type, NotificationType.URGENT)

    def test_non_admin_cannot_broadcast(self):
        self.client.force_authenticate(user=self.user1)
        payload = {
            'title': 'Unauthorized Broadcast',
            'message': 'Should fail',
            'channels': ['IN_APP'],
            'audience': 'ALL',
        }
        resp = self.client.post('/api/notifications/broadcast/', payload, format='json')
        self.assertEqual(resp.status_code, 403)
