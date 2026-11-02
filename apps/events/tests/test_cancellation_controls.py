"""Organizer control over cancelling an event registration: locked, a cutoff before the start, and what members are told."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.serializers import UserProfileDetailSerializer
from apps.events import registration
from apps.events.models import Event
from apps.forms.models import Form, FormStatus, Response

User = get_user_model()
HOUR = timezone.timedelta(hours=1)


def _user(username, role='NON_AFFILIATE'):
    return User.objects.create_user(username=username, email=f'{username}@srkr.ac.in', password='pw12345!', role=role)


class CancellationControlTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = _user('admin1', 'ADMIN')
        self.member = _user('member')
        self.form = Form.objects.create(title='Talk', slug='talk-reg', status=FormStatus.PUBLISHED)
        self.event = Event.objects.create(
            title='Talk', slug='talk', description='d', registration_form=self.form,
            start_time=timezone.now() + 30 * HOUR, end_time=timezone.now() + 32 * HOUR,
        )
        Response.objects.create(form=self.form, user=self.member)
        self.url = '/api/events/talk/my-registration/'

    def cancel(self):
        self.client.force_authenticate(self.member)
        return self.client.delete(self.url)

    def set_rules(self, **fields):
        Event.objects.filter(pk=self.event.pk).update(**fields)
        self.event.refresh_from_db()

    def test_by_default_cancelling_is_allowed_until_the_start_with_no_closing_time_to_announce(self):
        self.assertEqual(registration.cancellation_state(self.event), {'allowed': True, 'reason': None, 'closes_at': None})
        self.assertEqual(self.cancel().status_code, 204)

    def test_locked_refuses_with_a_clear_code_and_keeps_the_registration(self):
        self.set_rules(cancellation_locked=True)
        reply = self.cancel()
        self.assertEqual((reply.status_code, reply.json()['code']), (409, registration.CANCELLATION_LOCKED))
        self.assertIn('closed cancellations', reply.json()['error'])
        self.assertTrue(Response.objects.filter(form=self.form, user=self.member).exists())

    def test_a_cutoff_before_the_start_allows_cancelling_until_it_and_refuses_after(self):
        self.set_rules(cancellation_cutoff_hours=24)          # the event is 30 hours away: 6 hours left to cancel
        state = registration.cancellation_state(self.event)
        self.assertTrue(state['allowed'])
        self.assertEqual(state['closes_at'], self.event.start_time - 24 * HOUR)
        self.set_rules(cancellation_cutoff_hours=48)          # that moment is already behind us
        reply = self.cancel()
        self.assertEqual((reply.status_code, reply.json()['code']), (409, registration.CANCELLATION_CLOSED))

    def test_the_cutoff_is_inclusive_of_the_moment_it_closes(self):
        self.set_rules(cancellation_cutoff_hours=30)
        state = registration.cancellation_state(self.event, now=self.event.start_time - 30 * HOUR)
        self.assertEqual((state['allowed'], state['reason']), (False, registration.CANCELLATION_CLOSED))
        before = registration.cancellation_state(self.event, now=self.event.start_time - 30 * HOUR - timezone.timedelta(seconds=1))
        self.assertTrue(before['allowed'])

    def test_an_event_with_no_date_can_be_cancelled_unless_locked(self):
        self.set_rules(start_time=None, end_time=None, cancellation_cutoff_hours=24)
        self.assertTrue(registration.cancellation_state(self.event)['allowed'])
        self.set_rules(cancellation_locked=True)
        self.assertEqual(registration.cancellation_state(self.event)['reason'], registration.CANCELLATION_LOCKED)

    def test_started_wins_over_the_other_reasons(self):
        self.set_rules(start_time=timezone.now() - HOUR, cancellation_locked=True)
        self.assertEqual(registration.cancellation_state(self.event)['reason'], registration.EVENT_STARTED)

    def test_the_profile_tells_the_member_what_is_possible_and_until_when(self):
        self.set_rules(cancellation_cutoff_hours=24)
        item = UserProfileDetailSerializer(self.member).data['registered_events'][0]
        self.assertEqual(item['cancellation']['allowed'], True)
        self.assertEqual(item['cancellation']['closes_at'], self.event.start_time - 24 * HOUR)
        self.set_rules(cancellation_locked=True)
        locked = UserProfileDetailSerializer(self.member).data['registered_events'][0]['cancellation']
        self.assertEqual((locked['allowed'], locked['reason'], locked['closes_at']), (False, registration.CANCELLATION_LOCKED, None))

    def test_a_plain_form_registration_has_no_cancellation_info(self):
        plain = Form.objects.create(title='Survey', slug='survey', status=FormStatus.PUBLISHED)
        Response.objects.create(form=plain, user=self.member)
        items = {item['form_slug']: item for item in UserProfileDetailSerializer(self.member).data['registered_events']}
        self.assertIsNone(items['survey']['cancellation'])

    def test_an_admin_sets_the_rules_through_the_event_api_and_members_cannot(self):
        self.client.force_authenticate(self.admin)
        done = self.client.patch('/api/events/talk/', {'cancellation_locked': True, 'cancellation_cutoff_hours': 12}, format='json')
        self.assertEqual(done.status_code, 200, done.json())
        self.assertEqual((done.json()['cancellation_locked'], done.json()['cancellation_cutoff_hours']), (True, 12))
        self.client.force_authenticate(self.member)
        refused = self.client.patch('/api/events/talk/', {'cancellation_locked': False}, format='json')
        self.assertEqual(refused.status_code, 403)
        self.event.refresh_from_db()
        self.assertTrue(self.event.cancellation_locked)

    def test_the_cutoff_must_be_a_sensible_number_of_hours(self):
        self.client.force_authenticate(self.admin)
        for bad in (-1, 24 * 30 + 1, 'soon'):
            reply = self.client.patch('/api/events/talk/', {'cancellation_cutoff_hours': bad}, format='json')
            self.assertEqual(reply.status_code, 400, bad)

    def test_the_public_event_shows_the_rules_so_the_page_can_say_them(self):
        self.set_rules(cancellation_locked=True, cancellation_cutoff_hours=6)
        data = APIClient().get('/api/events/talk/').json()
        self.assertEqual((data['cancellation_locked'], data['cancellation_cutoff_hours']), (True, 6))
