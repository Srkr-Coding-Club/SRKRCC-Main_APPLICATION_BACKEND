from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.attendance.models import AttendanceBadge, AttendanceRecord, AttendanceSession, SessionLabel
from apps.audit.models import AuditLog
from apps.events import registration
from apps.events.models import Event
from apps.forms.models import Form, FormStatus, Response

User = get_user_model()


def _user(username):
    return User.objects.create_user(username=username, email=f'{username}@srkr.ac.in', password='pw12345!', role='NON_AFFILIATE')


class CancelRegistrationTests(TestCase):
    """DELETE /api/events/{slug}/my-registration/ - a member cancels their own registration."""

    def setUp(self):
        self.client = APIClient()
        self.member = _user('member')
        self.other = _user('other')
        self.form = Form.objects.create(title='Talk', slug='talk-reg', status=FormStatus.PUBLISHED)
        soon = timezone.now() + timezone.timedelta(days=3)
        self.event = Event.objects.create(title='Talk', slug='talk', description='d', registration_form=self.form, start_time=soon, end_time=soon + timezone.timedelta(hours=2))
        self.mine = Response.objects.create(form=self.form, user=self.member)
        self.theirs = Response.objects.create(form=self.form, user=self.other)
        self.url = f'/api/events/{self.event.slug}/my-registration/'

    def _scan(self, response):
        past = timezone.now().date()
        session = AttendanceSession.objects.create(form=self.form, day_index=0, session_label=SessionLabel.MORNING, date=past)
        AttendanceRecord.objects.create(badge=AttendanceBadge.objects.create(response=response), session=session)

    def test_requires_login(self):
        self.assertEqual(self.client.delete(self.url).status_code, 401)

    def test_member_cancels_only_their_own_registration_and_it_is_audited(self):
        self.client.force_authenticate(self.member)
        self.assertEqual(self.client.delete(self.url).status_code, 204)
        self.assertFalse(Response.objects.filter(pk=self.mine.pk).exists())
        self.assertTrue(Response.objects.filter(pk=self.theirs.pk).exists())
        entry = AuditLog.objects.get(action='Cancelled Event Registration')
        self.assertEqual((entry.actor_id, entry.target_id, entry.details['registrations_removed']), (self.member.id, 'talk', 1))

    def test_the_seat_is_freed_so_the_person_can_register_again(self):
        self.client.force_authenticate(self.member)
        before = Response.objects.filter(form=self.form).count()
        self.client.delete(self.url)
        self.assertEqual(Response.objects.filter(form=self.form).count(), before - 1)
        self.assertEqual(Response.objects.filter(form=self.form, user=self.member).count(), 0)

    def test_someone_who_is_not_registered_gets_not_found(self):
        self.client.force_authenticate(_user('stranger'))
        response = self.client.delete(self.url)
        self.assertEqual((response.status_code, response.json()['code']), (404, registration.NOT_REGISTERED))

    def test_cannot_cancel_once_the_event_has_started(self):
        past = timezone.now() - timezone.timedelta(minutes=5)
        Event.objects.filter(pk=self.event.pk).update(start_time=past, end_time=past + timezone.timedelta(hours=2))
        self.client.force_authenticate(self.member)
        response = self.client.delete(self.url)
        self.assertEqual((response.status_code, response.json()['code']), (409, registration.EVENT_STARTED))
        self.assertTrue(Response.objects.filter(pk=self.mine.pk).exists())

    def test_cannot_cancel_after_being_checked_in_even_without_a_date(self):
        Event.objects.filter(pk=self.event.pk).update(start_time=None, end_time=None)
        self._scan(self.mine)
        self.client.force_authenticate(self.member)
        response = self.client.delete(self.url)
        self.assertEqual((response.status_code, response.json()['code']), (409, registration.ALREADY_ATTENDED))

    def test_an_event_without_a_date_can_still_be_cancelled_before_check_in(self):
        Event.objects.filter(pk=self.event.pk).update(start_time=None, end_time=None)
        self.client.force_authenticate(self.member)
        self.assertEqual(self.client.delete(self.url).status_code, 204)

    def test_preview_submissions_are_not_touched(self):
        preview = Response.objects.create(form=self.form, user=self.member, is_test_submission=True)
        self.client.force_authenticate(self.member)
        self.client.delete(self.url)
        self.assertTrue(Response.objects.filter(pk=preview.pk).exists())

    def test_an_event_without_a_form_has_nothing_to_cancel(self):
        bare = Event.objects.create(title='Bare', slug='bare', description='d')
        self.client.force_authenticate(self.member)
        self.assertEqual(self.client.delete(f'/api/events/{bare.slug}/my-registration/').status_code, 404)

    def test_profile_says_whether_the_event_has_started(self):
        from apps.accounts.serializers import UserProfileDetailSerializer
        item = UserProfileDetailSerializer(self.member).data['registered_events'][0]
        self.assertEqual((item['event_slug'], item['event_started']), ('talk', False))
        self.assertEqual((item['start_time'], item['end_time']), (self.event.start_time, self.event.end_time))
        Event.objects.filter(pk=self.event.pk).update(start_time=timezone.now() - timezone.timedelta(hours=1))
        item = UserProfileDetailSerializer(self.member).data['registered_events'][0]
        self.assertTrue(item['event_started'])


class SeatReopeningTests(TestCase):
    """A form that closed because it filled up must open again when a seat comes back."""

    def setUp(self):
        self.client = APIClient()
        self.first, self.second = _user('first'), _user('second')
        self.form = Form.objects.create(title='Talk', slug='talk-reg', status=FormStatus.PUBLISHED, max_total_responses=2)
        soon = timezone.now() + timezone.timedelta(days=3)
        self.event = Event.objects.create(title='Talk', slug='talk', description='d', registration_form=self.form, start_time=soon, capacity=2)
        Response.objects.create(form=self.form, user=self.first)
        Response.objects.create(form=self.form, user=self.second)
        Form.objects.filter(pk=self.form.pk).update(status=FormStatus.CLOSED)  # what the last registration does

    def _cancel(self, user):
        self.client.force_authenticate(user)
        return self.client.delete(f'/api/events/{self.event.slug}/my-registration/')

    def test_cancelling_from_a_full_event_publishes_the_form_again(self):
        self.assertEqual(self._cancel(self.first).status_code, 204)
        self.form.refresh_from_db()
        self.assertEqual(self.form.status, FormStatus.PUBLISHED)

    def test_a_form_the_organizer_closed_before_it_filled_stays_closed(self):
        Response.objects.filter(form=self.form, user=self.second).delete()  # one seat free, yet closed on purpose
        self.assertEqual(self._cancel(self.first).status_code, 204)
        self.form.refresh_from_db()
        self.assertEqual(self.form.status, FormStatus.CLOSED)

    def test_a_form_past_its_own_closing_time_stays_closed(self):
        Form.objects.filter(pk=self.form.pk).update(close_at=timezone.now() - timezone.timedelta(hours=1))
        self._cancel(self.first)
        self.form.refresh_from_db()
        self.assertEqual(self.form.status, FormStatus.CLOSED)

    def test_raising_the_capacity_of_a_one_click_event_publishes_its_form_again(self):
        from apps.events.quick_registration import apply_quick_registration, sync_quick_form_details
        event = Event.objects.create(title='Quick', slug='quick', description='d', capacity=1)
        form = apply_quick_registration(event, ['full_name', 'email'])
        event.registration_form = form
        event.save()
        Response.objects.create(form=form, user=self.first)
        Form.objects.filter(pk=form.pk).update(status=FormStatus.CLOSED)
        event.capacity = 3
        event.save()
        sync_quick_form_details(Event.objects.get(pk=event.pk))
        form.refresh_from_db()
        self.assertEqual((form.max_total_responses, form.status), (3, FormStatus.PUBLISHED))
