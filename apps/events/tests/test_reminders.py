from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.events import reminders
from apps.events.models import Event, EventReminder
from apps.forms.models import Form, FormStatus, Response
from apps.notifications import preferences
from apps.notifications.models import Notification, NotificationCategory

User = get_user_model()
HOURS = timezone.timedelta


def _user(username, **extra):
    return User.objects.create_user(username=username, email=f'{username}@srkr.ac.in', password='pw12345!', role='NON_AFFILIATE', **extra)


class EventReminderTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.form = Form.objects.create(title='Talk', slug='talk-reg', status=FormStatus.PUBLISHED)
        self.event = Event.objects.create(
            title='Intro Talk', slug='intro-talk', description='d', venue='Hall A', registration_form=self.form,
            start_time=self.now + HOURS(hours=20), end_time=self.now + HOURS(hours=22),
        )
        self.asha = _user('asha', first_name='Asha')

    def _register(self, user, hours_ago=100):
        response = Response.objects.create(form=self.form, user=user)
        Response.objects.filter(pk=response.pk).update(submitted_at=self.now - HOURS(hours=hours_ago))
        return response

    def test_a_registered_member_gets_the_day_before_reminder_in_the_bell_and_by_email(self):
        self._register(self.asha)
        sent = reminders.send_due(self.now)
        self.assertEqual(sent, {'DAY': 1, 'HOUR': 0})
        note = Notification.objects.get(recipient=self.asha)
        self.assertEqual((note.title, note.category, note.link_url), ('Intro Talk starts tomorrow', NotificationCategory.EVENT, '/events/intro-talk'))
        self.assertIn('Hall A', note.message)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['asha@srkr.ac.in'])
        self.assertIn('intro-talk', mail.outbox[0].body)

    def test_running_again_does_not_send_the_same_reminder_twice(self):
        self._register(self.asha)
        reminders.send_due(self.now)
        self.assertEqual(reminders.send_due(self.now + HOURS(minutes=30)), {'DAY': 0, 'HOUR': 0})
        self.assertEqual(Notification.objects.filter(recipient=self.asha).count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_an_event_later_the_same_day_says_today_not_tomorrow(self):
        local_now = timezone.localtime(self.now)
        morning = local_now.replace(hour=2, minute=0, second=0, microsecond=0)
        Event.objects.filter(pk=self.event.pk).update(start_time=morning + HOURS(hours=19))
        self._register(self.asha)
        reminders.send_due(morning)
        self.assertEqual(Notification.objects.get(recipient=self.asha).title, 'Intro Talk starts today')

    def test_the_hour_before_reminder_is_a_second_message(self):
        self._register(self.asha)
        reminders.send_due(self.now)
        self.assertEqual(reminders.send_due(self.now + HOURS(hours=19, minutes=30)), {'DAY': 0, 'HOUR': 1})
        self.assertEqual(
            list(Notification.objects.filter(recipient=self.asha).order_by('id').values_list('title', flat=True)),
            ['Intro Talk starts tomorrow', 'Intro Talk starts in about an hour'],
        )

    def test_nothing_is_sent_before_the_window_opens_or_after_the_event_started(self):
        self._register(self.asha)
        Event.objects.filter(pk=self.event.pk).update(start_time=self.now + HOURS(hours=30))
        self.assertEqual(reminders.send_due(self.now), {'DAY': 0, 'HOUR': 0})
        Event.objects.filter(pk=self.event.pk).update(start_time=self.now - HOURS(minutes=5))
        self.assertEqual(reminders.send_due(self.now), {'DAY': 0, 'HOUR': 0})
        self.assertEqual(Notification.objects.count(), 0)

    def test_someone_who_registered_inside_the_window_is_not_reminded_of_it(self):
        self._register(self.asha, hours_ago=2)
        self.assertEqual(reminders.send_due(self.now), {'DAY': 0, 'HOUR': 0})

    def test_turning_email_off_keeps_the_bell_reminder(self):
        self._register(self.asha)
        preferences.set_event_reminder_emails(self.asha, False)
        reminders.send_due(self.now)
        self.assertEqual(Notification.objects.filter(recipient=self.asha).count(), 1)
        self.assertEqual(len(mail.outbox), 0)

    def test_preview_submissions_anonymous_and_inactive_users_are_skipped(self):
        preview = Response.objects.create(form=self.form, user=self.asha, is_test_submission=True)
        Response.objects.filter(pk=preview.pk).update(submitted_at=self.now - HOURS(hours=100))
        anonymous = Response.objects.create(form=self.form, user=None)
        Response.objects.filter(pk=anonymous.pk).update(submitted_at=self.now - HOURS(hours=100))
        gone = _user('gone', is_active=False)
        self._register(gone)
        self.assertEqual(reminders.send_due(self.now), {'DAY': 0, 'HOUR': 0})

    def test_a_cancelled_registration_gets_no_reminder(self):
        response = self._register(self.asha)
        response.delete()
        self.assertEqual(reminders.send_due(self.now), {'DAY': 0, 'HOUR': 0})

    def test_events_without_a_date_or_form_are_ignored(self):
        Event.objects.create(title='No date', slug='no-date', description='d', registration_form=self.form)
        Event.objects.create(title='No form', slug='no-form', description='d', start_time=self.now + HOURS(hours=2))
        self.assertEqual(reminders.send_due(self.now), {'DAY': 0, 'HOUR': 0})

    def test_the_command_reports_what_it_sent(self):
        self._register(self.asha)
        from io import StringIO
        out = StringIO()
        call_command('send_event_reminders', stdout=out)
        self.assertIn('Sent 1 day-before and 0 hour-before', out.getvalue())
        self.assertEqual(EventReminder.objects.count(), 1)


class NotificationPreferencesApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = _user('pref')
        self.url = '/api/notifications/preferences/'

    def test_requires_login(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_defaults_to_on_and_can_be_switched_off_and_on(self):
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get(self.url).json(), {'event_reminder_emails': True})
        self.assertEqual(self.client.patch(self.url, {'event_reminder_emails': False}, format='json').json(), {'event_reminder_emails': False})
        self.assertEqual(self.client.get(self.url).json(), {'event_reminder_emails': False})
        self.assertEqual(self.client.patch(self.url, {'event_reminder_emails': True}, format='json').json(), {'event_reminder_emails': True})

    def test_rejects_anything_that_is_not_true_or_false(self):
        self.client.force_authenticate(self.user)
        for value in ('yes', 1, None):
            self.assertEqual(self.client.patch(self.url, {'event_reminder_emails': value}, format='json').status_code, 400)

    def test_one_person_cannot_change_another_persons_choice(self):
        other = _user('other')
        self.client.force_authenticate(other)
        self.client.patch(self.url, {'event_reminder_emails': False}, format='json')
        self.assertTrue(preferences.event_reminder_emails_enabled(self.user))
