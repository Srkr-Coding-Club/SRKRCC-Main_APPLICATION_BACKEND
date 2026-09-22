from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.test import APIClient

from apps.events.models import Event
from apps.forms.models import Form, FormStatus, Response

User = get_user_model()


def _times():
    start = timezone.now() + timezone.timedelta(days=7)
    return start, start + timezone.timedelta(hours=3)


class EventsApiTests(TestCase):
    """POST/GET /api/events/ — admin panel event creation + form linking + metrics."""

    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            username='admin1', email='admin1@srkr.ac.in', password='pw12345!', role='ADMIN',
        )
        self.club_lead = User.objects.create_user(
            username='lead1', email='lead1@srkr.ac.in', password='pw12345!', role='CLUB_LEAD',
        )
        self.member = User.objects.create_user(
            username='member1', email='member1@srkr.ac.in', password='pw12345!', role='NON_AFFILIATE',
        )
        self.form = Form.objects.create(
            title='Workshop Registration', slug='workshop-registration', status=FormStatus.PUBLISHED,
        )

    def _payload(self, **overrides):
        start, end = _times()
        payload = dict(
            title='Hands-on Web Dev & Next.js Workshop',
            slug='hands-on-web-dev-nextjs-workshop',
            description='## About\nJoin us for a **hands-on** session.',
            category='Workshop',
            venue='CSE Seminar Hall',
            capacity=100,
            start_time=start.isoformat(),
            end_time=end.isoformat(),
            registration_form=self.form.id,
        )
        payload.update(overrides)
        return payload

    # --- Read access -----------------------------------------------------

    def test_anonymous_can_list_events(self):
        Event.objects.create(
            title='X', slug='x', description='d',
            start_time=_times()[0], end_time=_times()[1],
        )
        resp = self.client.get('/api/events/')
        self.assertEqual(resp.status_code, 200)

    # --- Write authorization ----------------------------------------------

    def test_anonymous_cannot_create_event(self):
        resp = self.client.post('/api/events/', self._payload(), format='json')
        self.assertIn(resp.status_code, (401, 403))
        self.assertEqual(Event.objects.count(), 0)

    def test_regular_member_cannot_create_event(self):
        self.client.force_authenticate(self.member)
        resp = self.client.post('/api/events/', self._payload(), format='json')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(Event.objects.count(), 0)

    def test_club_lead_can_create_event(self):
        self.client.force_authenticate(self.club_lead)
        resp = self.client.post('/api/events/', self._payload(), format='json')
        self.assertEqual(resp.status_code, 201, resp.data)

    # --- Admin create + form link + metrics --------------------------------

    def test_admin_can_create_event_with_linked_form(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.post('/api/events/', self._payload(), format='json')
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data['form_slug'], self.form.slug)
        self.assertEqual(resp.data['form_title'], self.form.title)
        self.assertEqual(resp.data['registration_count'], 0)

    def test_event_without_form_link_is_allowed(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.post('/api/events/', self._payload(registration_form=None), format='json')
        self.assertEqual(resp.status_code, 201, resp.data)
        # source='registration_form.slug' on a read-only field is skipped by DRF
        # entirely (not serialized as null) when registration_form is None.
        self.assertIsNone(resp.data.get('form_slug'))
        self.assertEqual(resp.data['registration_count'], 0)

    def test_registration_count_reflects_real_non_test_responses(self):
        event = Event.objects.create(
            title='Y', slug='y', description='d', registration_form=self.form,
            start_time=_times()[0], end_time=_times()[1],
        )
        Response.objects.create(form=self.form, is_test_submission=False)
        Response.objects.create(form=self.form, is_test_submission=False)
        Response.objects.create(form=self.form, is_test_submission=True)  # excluded

        self.client.force_authenticate(self.admin)
        resp = self.client.get(f'/api/events/{event.slug}/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['registration_count'], 2)

    def test_description_markdown_source_is_stored_verbatim(self):
        """Markdown (and any raw text, incl. angle brackets) is stored as opaque
        text server-side — rendering/escaping is the frontend MarkdownRenderer's
        job, so the API must not mutate or interpret it."""
        self.client.force_authenticate(self.admin)
        payload = self._payload(description='<script>alert(1)</script>\n## Heading')
        resp = self.client.post('/api/events/', payload, format='json')
        self.assertEqual(resp.status_code, 201, resp.data)
        event = Event.objects.get(slug=payload['slug'])
        self.assertEqual(event.description, payload['description'])

    # --- Lifecycle: close / reopen ------------------------------------------

    def test_new_event_defaults_to_live(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.post('/api/events/', self._payload(), format='json')
        self.assertEqual(resp.data['status'], 'LIVE')

    def test_admin_can_close_and_reopen_event(self):
        event = Event.objects.create(
            title='Z', slug='z', description='d',
            start_time=_times()[0], end_time=_times()[1],
        )
        self.client.force_authenticate(self.admin)

        resp = self.client.post(f'/api/events/{event.slug}/close/')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['status'], 'CLOSED')
        event.refresh_from_db()
        self.assertEqual(event.status, 'CLOSED')

        resp = self.client.post(f'/api/events/{event.slug}/reopen/')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['status'], 'LIVE')
        event.refresh_from_db()
        self.assertEqual(event.status, 'LIVE')

    def test_regular_member_cannot_close_event(self):
        event = Event.objects.create(
            title='Z2', slug='z2', description='d',
            start_time=_times()[0], end_time=_times()[1],
        )
        self.client.force_authenticate(self.member)
        resp = self.client.post(f'/api/events/{event.slug}/close/')
        self.assertEqual(resp.status_code, 403)
        event.refresh_from_db()
        self.assertEqual(event.status, 'LIVE')

    def test_status_is_not_client_settable_on_create(self):
        """status is read_only — the close/reopen actions are the only path to change it."""
        self.client.force_authenticate(self.admin)
        resp = self.client.post('/api/events/', self._payload(status='CLOSED'), format='json')
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data['status'], 'LIVE')

    # --- Visibility: hide / show ---------------------------------------------

    def test_hidden_event_is_excluded_from_public_list_and_detail(self):
        event = Event.objects.create(
            title='Hidden', slug='hidden-event', description='d',
            start_time=_times()[0], end_time=_times()[1],
        )
        self.client.force_authenticate(self.admin)
        resp = self.client.post(f'/api/events/{event.slug}/hide/')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(resp.data['is_hidden'])

        anon = APIClient()
        list_resp = anon.get('/api/events/')
        self.assertNotIn(event.slug, [e['slug'] for e in list_resp.data])
        detail_resp = anon.get(f'/api/events/{event.slug}/')
        self.assertEqual(detail_resp.status_code, 404)

    def test_admin_still_sees_hidden_event(self):
        event = Event.objects.create(
            title='Hidden', slug='hidden-event-2', description='d',
            start_time=_times()[0], end_time=_times()[1],
            visible_until=timezone.now() - timezone.timedelta(hours=1),
        )
        self.client.force_authenticate(self.admin)
        resp = self.client.get('/api/events/')
        self.assertIn(event.slug, [e['slug'] for e in resp.data])

    def test_show_undoes_hide(self):
        event = Event.objects.create(
            title='E', slug='show-me-again', description='d',
            start_time=_times()[0], end_time=_times()[1],
        )
        self.client.force_authenticate(self.admin)
        self.client.post(f'/api/events/{event.slug}/hide/')
        resp = self.client.post(f'/api/events/{event.slug}/show/')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertFalse(resp.data['is_hidden'])

        anon = APIClient()
        list_resp = anon.get('/api/events/')
        self.assertIn(event.slug, [e['slug'] for e in list_resp.data])

    def test_regular_member_cannot_hide_event(self):
        event = Event.objects.create(
            title='E', slug='cant-touch-this', description='d',
            start_time=_times()[0], end_time=_times()[1],
        )
        self.client.force_authenticate(self.member)
        resp = self.client.post(f'/api/events/{event.slug}/hide/')
        self.assertEqual(resp.status_code, 403)
        event.refresh_from_db()
        self.assertIsNone(event.visible_until)

    # --- Optional venue/schedule -------------------------------------------

    def test_event_can_be_created_without_venue_or_schedule(self):
        self.client.force_authenticate(self.admin)
        payload = self._payload()
        del payload['start_time']
        del payload['end_time']
        del payload['venue']
        resp = self.client.post('/api/events/', payload, format='json')
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertIsNone(resp.data['start_time'])
        self.assertIsNone(resp.data['end_time'])
        self.assertEqual(resp.data['venue'], 'Campus Auditorium')  # model default, since venue wasn't sent

    def test_public_can_still_list_an_undated_event(self):
        """Regression: ordering by -start_time must not crash or silently
        drop rows once start_time can be null."""
        Event.objects.create(title='Undated', slug='undated-event', description='d')
        resp = self.client.get('/api/events/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('undated-event', [e['slug'] for e in resp.data])

    # --- Registration window (from the linked Form) -------------------------

    def test_registration_opens_and_closes_at_reflect_linked_form(self):
        opens = timezone.now() + timezone.timedelta(days=1)
        closes = timezone.now() + timezone.timedelta(days=5)
        self.form.open_at = opens
        self.form.close_at = closes
        self.form.save(update_fields=['open_at', 'close_at'])

        event = Event.objects.create(
            title='E', slug='reg-window-event', description='d', registration_form=self.form,
        )
        self.client.force_authenticate(self.admin)
        resp = self.client.get(f'/api/events/{event.slug}/')
        self.assertEqual(resp.status_code, 200)
        # DateTimeField.to_representation() always renders as an ISO string
        # (regardless of resp.data being otherwise "unrendered" native types),
        # so compare parsed instants rather than the string against a datetime.
        self.assertEqual(parse_datetime(resp.data['registration_opens_at']), opens)
        self.assertEqual(parse_datetime(resp.data['registration_closes_at']), closes)

    def test_registration_window_absent_without_linked_form(self):
        event = Event.objects.create(title='E', slug='no-form-event', description='d')
        self.client.force_authenticate(self.admin)
        resp = self.client.get(f'/api/events/{event.slug}/')
        self.assertIsNone(resp.data.get('registration_opens_at'))
        self.assertIsNone(resp.data.get('registration_closes_at'))

    def test_is_hidden_reflects_future_visible_from_without_explicit_hide(self):
        """A scheduled-but-not-yet-open event (visible_from in the future) is
        just as hidden as one explicitly hidden — same public-facing effect,
        even though no one clicked "Hide"."""
        event = Event.objects.create(
            title='E', slug='not-yet', description='d',
            start_time=_times()[0], end_time=_times()[1],
            visible_from=timezone.now() + timezone.timedelta(days=1),
        )
        self.client.force_authenticate(self.admin)
        resp = self.client.get(f'/api/events/{event.slug}/')
        self.assertTrue(resp.data['is_hidden'])

        anon = APIClient()
        self.assertEqual(anon.get(f'/api/events/{event.slug}/').status_code, 404)
