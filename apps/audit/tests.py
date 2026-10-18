from datetime import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .models import AuditLog

User = get_user_model()


def log_at(year, month, day=10, action='Something Happened'):
    entry = AuditLog.objects.create(action=action)
    when = timezone.make_aware(datetime(year, month, day, 12, 0))
    AuditLog.objects.filter(pk=entry.pk).update(created_at=when)
    return entry


class AuditMonthTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='aud_admin', email='aud_admin@srkr.ac.in', password='pw12345!', role='ADMIN')
        self.lead = User.objects.create_user(username='aud_lead', email='aud_lead@srkr.ac.in', password='pw12345!', role='CLUB_LEAD')
        self.member = User.objects.create_user(username='aud_member', email='aud_member@srkr.ac.in', password='pw12345!', role='NON_AFFILIATE')
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        log_at(2026, 8)
        log_at(2026, 9)
        log_at(2026, 9, 30)
        log_at(2026, 12, 31)

    def test_months_are_grouped_newest_first_with_counts(self):
        data = self.client.get('/api/audit/months/').json()
        self.assertEqual(data['months'], [
            {'month': '2026-12', 'count': 1}, {'month': '2026-09', 'count': 2}, {'month': '2026-08', 'count': 1},
        ])
        self.assertTrue(data['can_delete'])

    def test_list_can_be_limited_to_one_month(self):
        self.assertEqual(self.client.get('/api/audit/?month=2026-09').json()['count'], 2)
        self.assertEqual(self.client.get('/api/audit/?month=2026-12').json()['count'], 1)
        self.assertEqual(self.client.get('/api/audit/?month=bad').status_code, 400)

    def test_deleting_a_month_removes_only_that_month_and_records_it(self):
        response = self.client.delete('/api/audit/months/2026-09/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['deleted'], 2)
        self.assertEqual(AuditLog.objects.filter(action='Something Happened').count(), 2)
        months = [m['month'] for m in self.client.get('/api/audit/months/').json()['months']]
        self.assertNotIn('2026-09', months)
        record = AuditLog.objects.get(action='Deleted Audit Logs')
        self.assertEqual(record.actor, self.admin)
        self.assertEqual(record.details, {'month': '2026-09', 'deleted_entries': 2})

    def test_deleting_the_current_month_keeps_the_deletion_record(self):
        month = timezone.localtime().strftime('%Y-%m')
        self.client.delete(f'/api/audit/months/{month}/')
        self.assertTrue(AuditLog.objects.filter(action='Deleted Audit Logs', target_id=month).exists())

    def test_club_leads_can_read_but_not_delete(self):
        self.client.force_authenticate(self.lead)
        self.assertFalse(self.client.get('/api/audit/months/').json()['can_delete'])
        self.assertEqual(self.client.delete('/api/audit/months/2026-09/').status_code, 403)
        self.assertEqual(AuditLog.objects.filter(action='Something Happened').count(), 4)

    def test_members_and_anonymous_cannot_touch_audit_logs(self):
        self.client.force_authenticate(self.member)
        self.assertEqual(self.client.get('/api/audit/months/').status_code, 403)
        self.assertEqual(self.client.delete('/api/audit/months/2026-09/').status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.delete('/api/audit/months/2026-09/').status_code, 401)

    def test_bad_month_is_rejected(self):
        self.assertEqual(self.client.delete('/api/audit/months/2026-13/').status_code, 400)
        self.assertEqual(self.client.delete('/api/audit/months/all/').status_code, 400)
        self.assertEqual(AuditLog.objects.filter(action='Something Happened').count(), 4)
