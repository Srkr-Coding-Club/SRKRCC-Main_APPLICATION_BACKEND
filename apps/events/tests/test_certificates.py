from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.attendance.models import AttendanceBadge, AttendanceRecord, AttendanceSession, SessionLabel
from apps.events import certificates
from apps.events.models import Event
from apps.forms.models import Form, FormStatus, Response

User = get_user_model()


def _user(username, role='NON_AFFILIATE', **extra):
    return User.objects.create_user(username=username, email=f'{username}@srkr.ac.in', password='pw12345!', role=role, **extra)


class EventCertificateTests(TestCase):
    """GET /api/events/{slug}/my-certificate/, the admin settings and the public verify endpoint."""

    def setUp(self):
        self.client = APIClient()
        self.admin = _user('admin1', 'ADMIN')
        self.attendee = _user('attendee', first_name='Asha', last_name='Rao')
        self.no_show = _user('noshow')
        self.form = Form.objects.create(title='Workshop', slug='workshop-reg', status=FormStatus.PUBLISHED)
        past = timezone.now() - timezone.timedelta(days=1)
        self.event = Event.objects.create(
            title='Web Workshop', slug='web-workshop', description='d', registration_form=self.form,
            start_time=past, end_time=past + timezone.timedelta(hours=2),
        )
        self.session = AttendanceSession.objects.create(form=self.form, day_index=0, session_label=SessionLabel.MORNING, date=past.date())
        for user, scanned in ((self.attendee, True), (self.no_show, False)):
            badge = AttendanceBadge.objects.create(response=Response.objects.create(form=self.form, user=user))
            if scanned:
                AttendanceRecord.objects.create(badge=badge, session=self.session)
        self.mine = f'/api/events/{self.event.slug}/my-certificate/'
        self.settings_url = f'/api/events/{self.event.slug}/certificates/'

    def _release(self, **extra):
        Event.objects.filter(pk=self.event.pk).update(certificates_released=True, **extra)

    def test_nothing_opens_until_the_organizers_release_certificates(self):
        self.client.force_authenticate(self.attendee)
        response = self.client.get(self.mine)
        self.assertEqual((response.status_code, response.json()['code']), (403, certificates.NOT_RELEASED))

    def test_requires_login(self):
        self.assertEqual(self.client.get(self.mine).status_code, 401)

    def test_attendee_gets_a_certificate_with_their_name_and_the_default_wording(self):
        self._release()
        self.client.force_authenticate(self.attendee)
        data = self.client.get(self.mine).json()
        self.assertEqual((data['name'], data['event'], data['kind']), ('Asha Rao', 'Web Workshop', 'PARTICIPATION'))
        self.assertEqual(data['body'], 'Asha Rao attended Web Workshop.')
        self.assertTrue(data['code'].startswith('ev.'))

    def test_registered_but_not_scanned_is_refused(self):
        self._release()
        self.client.force_authenticate(self.no_show)
        response = self.client.get(self.mine)
        self.assertEqual((response.status_code, response.json()['code']), (403, certificates.NOT_ELIGIBLE))

    def test_custom_wording_and_signatory_are_used(self):
        self._release(certificate_body='{name} completed {event} with distinction.', certificate_signatory_name='Dr. Rao', certificate_signatory_title='Faculty Advisor')
        self.client.force_authenticate(self.attendee)
        data = self.client.get(self.mine).json()
        self.assertEqual(data['body'], 'Asha Rao completed Web Workshop with distinction.')
        self.assertEqual((data['signatory_name'], data['signatory_title']), ('Dr. Rao', 'Faculty Advisor'))

    def test_a_genuine_code_verifies_publicly_and_a_forged_one_does_not(self):
        self._release()
        self.client.force_authenticate(self.attendee)
        code = self.client.get(self.mine).json()['code']
        anonymous = APIClient()
        ok = anonymous.get(f'/api/events/certificates/verify/{code}/')
        self.assertEqual(ok.status_code, 200)
        self.assertEqual((ok.json()['valid'], ok.json()['name'], ok.json()['event']), (True, 'Asha Rao', 'Web Workshop'))
        for bad in (code[:-3] + 'abc', 'ev.garbage', 'not-an-event-code', ''):
            self.assertEqual(anonymous.get(f'/api/events/certificates/verify/{bad or "x"}/').status_code, 404, bad)

    def test_a_hackathon_style_code_is_not_accepted_as_an_event_code(self):
        self.assertEqual(APIClient().get('/api/events/certificates/verify/abc:def:ghi/').status_code, 404)

    def test_admin_reads_and_changes_settings_and_sees_who_is_eligible(self):
        self.client.force_authenticate(self.admin)
        before = self.client.get(self.settings_url).json()
        self.assertEqual((before['certificates_released'], before['eligible_count']), (False, 1))
        saved = self.client.put(self.settings_url, {'certificates_released': True, 'certificate_signatory_name': 'Dr. Rao'}, format='json')
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(saved.json()['certificates_released'])
        self.assertEqual(saved.json()['preview']['signatory_name'], 'Dr. Rao')
        self.event.refresh_from_db()
        self.assertTrue(self.event.certificates_released)

    def test_unknown_placeholders_in_the_wording_are_rejected(self):
        self.client.force_authenticate(self.admin)
        response = self.client.put(self.settings_url, {'certificate_body': '{name} met {team}.'}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_members_cannot_use_the_admin_settings(self):
        self.client.force_authenticate(self.attendee)
        self.assertEqual(self.client.get(self.settings_url).status_code, 403)
        self.assertEqual(self.client.put(self.settings_url, {'certificates_released': True}, format='json').status_code, 403)
