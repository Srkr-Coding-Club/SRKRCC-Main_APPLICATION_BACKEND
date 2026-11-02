import json

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.attendance.models import AttendanceBadge, AttendanceRecord, AttendanceSession, SessionLabel
from apps.events import feedback as feedback_service
from apps.events.models import Event, EventFeedback
from apps.forms.models import Form, FormStatus, Response

User = get_user_model()


def _user(username, role='NON_AFFILIATE'):
    return User.objects.create_user(username=username, email=f'{username}@srkr.ac.in', password='pw12345!', role=role)


class EventFeedbackTests(TestCase):
    """GET/PUT /api/events/{slug}/feedback/ and the admin summary and export."""

    def setUp(self):
        self.client = APIClient()
        self.admin = _user('admin1', 'ADMIN')
        self.attendee = _user('attendee')
        self.no_show = _user('noshow')
        self.stranger = _user('stranger')
        self.form = Form.objects.create(title='Workshop', slug='workshop-reg', status=FormStatus.PUBLISHED)
        past = timezone.now() - timezone.timedelta(days=1)
        self.event = Event.objects.create(
            title='Workshop', slug='workshop', description='d', registration_form=self.form,
            start_time=past, end_time=past + timezone.timedelta(hours=2),
        )
        self.session = AttendanceSession.objects.create(form=self.form, day_index=0, session_label=SessionLabel.MORNING, date=past.date())
        self._register(self.attendee, scanned=True)
        self._register(self.no_show, scanned=False)
        self.url = f'/api/events/{self.event.slug}/feedback/'

    def _register(self, user, scanned):
        response = Response.objects.create(form=self.form, user=user)
        badge = AttendanceBadge.objects.create(response=response)
        if scanned:
            AttendanceRecord.objects.create(badge=badge, session=self.session)

    def _body(self, **overrides):
        return {'rating': 5, 'would_recommend': True, 'went_well': 'Clear examples', 'to_improve': '', **overrides}

    def test_requires_login(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_attendee_can_create_then_change_feedback(self):
        self.client.force_authenticate(self.attendee)
        self.assertTrue(self.client.get(self.url).json()['eligible'])
        created = self.client.put(self.url, self._body(), format='json')
        self.assertEqual(created.status_code, 201)
        changed = self.client.put(self.url, self._body(rating=3), format='json')
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(EventFeedback.objects.filter(event=self.event).count(), 1)
        self.assertEqual(self.client.get(self.url).json()['feedback']['rating'], 3)

    def test_registered_but_not_scanned_is_refused(self):
        self.client.force_authenticate(self.no_show)
        body = self.client.get(self.url).json()
        self.assertEqual((body['eligible'], body['reason']), (False, feedback_service.NOT_ATTENDED))
        response = self.client.put(self.url, self._body(), format='json')
        self.assertEqual((response.status_code, response.json()['code']), (403, feedback_service.NOT_ATTENDED))

    def test_someone_who_never_registered_is_refused(self):
        self.client.force_authenticate(self.stranger)
        self.assertEqual(self.client.put(self.url, self._body(), format='json').status_code, 403)

    def test_not_open_until_the_event_has_ended(self):
        future = timezone.now() + timezone.timedelta(days=2)
        Event.objects.filter(pk=self.event.pk).update(start_time=future, end_time=future + timezone.timedelta(hours=1))
        self.client.force_authenticate(self.attendee)
        response = self.client.put(self.url, self._body(), format='json')
        self.assertEqual((response.status_code, response.json()['code']), (403, feedback_service.EVENT_NOT_ENDED))

    def test_event_without_a_date_never_opens(self):
        Event.objects.filter(pk=self.event.pk).update(start_time=None, end_time=None)
        self.client.force_authenticate(self.attendee)
        self.assertEqual(self.client.get(self.url).json()['reason'], feedback_service.EVENT_NOT_ENDED)

    def test_event_with_only_a_start_ends_an_hour_later(self):
        Event.objects.filter(pk=self.event.pk).update(start_time=timezone.now() - timezone.timedelta(minutes=30), end_time=None)
        self.event.refresh_from_db()
        self.assertFalse(feedback_service.has_ended(self.event))
        Event.objects.filter(pk=self.event.pk).update(start_time=timezone.now() - timezone.timedelta(hours=2))
        self.event.refresh_from_db()
        self.assertTrue(feedback_service.has_ended(self.event))

    def test_hidden_event_still_accepts_feedback_from_attendees(self):
        Event.objects.filter(pk=self.event.pk).update(visible_until=timezone.now() - timezone.timedelta(hours=1))
        self.client.force_authenticate(self.attendee)
        self.assertEqual(self.client.put(self.url, self._body(), format='json').status_code, 201)

    def test_rejects_ratings_outside_one_to_five(self):
        self.client.force_authenticate(self.attendee)
        for rating in (0, 6):
            self.assertEqual(self.client.put(self.url, self._body(rating=rating), format='json').status_code, 400)

    def test_rejects_overlong_comments(self):
        self.client.force_authenticate(self.attendee)
        self.assertEqual(self.client.put(self.url, self._body(went_well='x' * 501), format='json').status_code, 400)

    def test_one_feedback_per_person_in_the_database(self):
        EventFeedback.objects.create(event=self.event, user=self.attendee, rating=4, would_recommend=True)
        with self.assertRaises(IntegrityError), transaction.atomic():
            EventFeedback.objects.create(event=self.event, user=self.attendee, rating=2, would_recommend=False)

    def test_summary_adds_up_and_is_admin_only(self):
        other = _user('second')
        self._register(other, scanned=True)
        EventFeedback.objects.create(event=self.event, user=self.attendee, rating=5, would_recommend=True, went_well='Great')
        EventFeedback.objects.create(event=self.event, user=other, rating=3, would_recommend=False)
        summary_url = f'{self.url}summary/'

        self.client.force_authenticate(self.attendee)
        self.assertEqual(self.client.get(summary_url).status_code, 403)

        self.client.force_authenticate(self.admin)
        data = self.client.get(summary_url).json()
        self.assertEqual(data['count'], 2)
        self.assertEqual(data['average_rating'], 4.0)
        self.assertEqual(data['recommend_rate'], 0.5)
        self.assertEqual(data['distribution'], {'1': 0, '2': 0, '3': 1, '4': 0, '5': 1})
        self.assertEqual(data['attendees'], 2)
        self.assertEqual(data['response_rate'], 1.0)
        self.assertEqual([c['went_well'] for c in data['comments']], ['Great'])

    def test_summary_of_an_event_with_no_feedback_is_empty_not_an_error(self):
        self.client.force_authenticate(self.admin)
        data = self.client.get(f'{self.url}summary/').json()
        self.assertEqual((data['count'], data['average_rating'], data['recommend_rate']), (0, None, None))

    def test_export_is_admin_only_formula_safe_and_audited(self):
        EventFeedback.objects.create(event=self.event, user=self.attendee, rating=4, would_recommend=True, went_well='=HYPERLINK("x")')
        export_url = f'{self.url}export/'

        self.client.force_authenticate(self.attendee)
        self.assertEqual(self.client.get(export_url).status_code, 403)

        self.client.force_authenticate(self.admin)
        response = self.client.get(export_url)
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment', response['Content-Disposition'])
        self.assertNotIn(',=HYPERLINK', response.content.decode('utf-8-sig'))
        self.assertEqual(self.client.get(export_url, {'export_format': 'pdf'}).status_code, 400)
        as_json = json.loads(self.client.get(export_url, {'export_format': 'json'}).content)
        self.assertEqual(len(as_json), 1)


class ProfileRegisteredEventsTests(TestCase):
    """The profile's registered-events list says which Event a registration belongs to."""

    def test_event_registrations_carry_the_event_slug_and_plain_forms_do_not(self):
        from apps.accounts.serializers import UserProfileDetailSerializer
        user = _user('member')
        event_form = Form.objects.create(title='Talk', slug='talk-reg', status=FormStatus.PUBLISHED)
        plain_form = Form.objects.create(title='Survey', slug='survey', status=FormStatus.PUBLISHED)
        Event.objects.create(title='Talk', slug='talk', description='d', registration_form=event_form)
        Response.objects.create(form=event_form, user=user)
        Response.objects.create(form=plain_form, user=user)

        items = UserProfileDetailSerializer(user).data['registered_events']

        self.assertEqual({item['form_slug']: item['event_slug'] for item in items}, {'talk-reg': 'talk', 'survey': None})
        self.assertEqual({item['form_slug']: item['event_ended'] for item in items}, {'talk-reg': False, 'survey': False})
        self.assertEqual({item['form_slug']: item['certificates_released'] for item in items}, {'talk-reg': False, 'survey': False})
