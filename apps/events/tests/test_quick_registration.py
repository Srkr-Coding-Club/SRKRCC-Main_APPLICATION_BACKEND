from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.events.models import Event
from apps.forms.models import FieldType, Form, FormField, FormStatus, Response
from apps.forms.tests.factories import add_field, make_form

User = get_user_model()


def _times():
    start = timezone.now() + timezone.timedelta(days=7)
    return start, start + timezone.timedelta(hours=3)


class QuickRegistrationTestBase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            username='admin1', email='admin1@srkr.ac.in', password='pw12345!', role='ADMIN',
        )
        self.member = User.objects.create_user(
            username='member1', email='member1@srkr.ac.in', password='pw12345!', role='NON_AFFILIATE',
            first_name='Aditi', last_name='Sharma', phone_number='9876543210', branch='CSE',
            roll_number='22B91A0501', year=3,
        )

    def _payload(self, **overrides):
        start, end = _times()
        payload = dict(
            title='One Click Workshop', slug='one-click-workshop', description='About the workshop',
            category='Workshop', venue='Seminar Hall', capacity=100,
            start_time=start.isoformat(), end_time=end.isoformat(),
        )
        payload.update(overrides)
        return payload

    def _create_event(self, fields, **overrides):
        self.client.force_authenticate(self.admin)
        resp = self.client.post(
            '/api/events/', self._payload(quick_registration_fields=fields, **overrides), format='json',
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        return resp.data


class AdminSetupTests(QuickRegistrationTestBase):
    def test_creating_an_event_builds_a_published_profile_bound_form(self):
        data = self._create_event(['phone_number', 'branch'])

        self.assertTrue(data['is_quick_registration'])
        self.assertEqual(data['registration_profile_fields'], ['full_name', 'email', 'phone_number', 'branch'])
        self.assertTrue(data['form_slug'])

        form = Form.objects.get(pk=data['registration_form'])
        self.assertEqual(form.status, FormStatus.PUBLISHED)
        self.assertEqual(form.title, 'One Click Workshop')
        self.assertEqual(form.max_total_responses, 100)
        self.assertFalse(form.allow_multiple_responses)
        fields = list(form.fields.all())
        self.assertEqual([f.profile_field for f in fields], ['full_name', 'email', 'phone_number', 'branch'])
        self.assertTrue(all(f.is_required for f in fields))

    def test_name_and_email_are_always_collected(self):
        data = self._create_event(['year'])
        self.assertEqual(data['registration_profile_fields'], ['full_name', 'email', 'year'])

    def test_the_order_does_not_depend_on_the_order_sent(self):
        data = self._create_event(['branch', 'phone_number', 'email'])
        self.assertEqual(data['registration_profile_fields'], ['full_name', 'email', 'phone_number', 'branch'])

    def test_updating_the_details_reuses_the_same_form(self):
        created = self._create_event(['phone_number'])
        form_id = created['registration_form']
        version_before = Form.objects.get(pk=form_id).version

        resp = self.client.patch(
            f"/api/events/{created['slug']}/", {'quick_registration_fields': ['branch']}, format='json',
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['registration_form'], form_id)
        self.assertEqual(resp.data['registration_profile_fields'], ['full_name', 'email', 'branch'])

        form = Form.objects.get(pk=form_id)
        self.assertEqual(form.version, version_before + 1)
        removed = FormField.all_objects.get(form=form, profile_field='phone_number')
        self.assertTrue(removed.is_deleted)  # soft-deleted, so old responses still export it

    def test_changing_the_capacity_updates_the_response_cap(self):
        created = self._create_event(['branch'])
        self.client.patch(f"/api/events/{created['slug']}/", {'capacity': 25}, format='json')
        self.assertEqual(Form.objects.get(pk=created['registration_form']).max_total_responses, 25)

    def test_renaming_the_event_renames_its_form(self):
        created = self._create_event(['branch'])
        self.client.patch(f"/api/events/{created['slug']}/", {'title': 'Renamed Workshop'}, format='json')
        self.assertEqual(Form.objects.get(pk=created['registration_form']).title, 'Renamed Workshop')

    def test_switching_from_a_custom_form_leaves_that_form_untouched(self):
        custom = make_form(title='Custom Form', slug='custom-form')
        add_field(custom, FieldType.TEXT, label='Why join?', required=True)
        self.client.force_authenticate(self.admin)
        created = self.client.post(
            '/api/events/', self._payload(registration_form=custom.id), format='json',
        ).data
        self.assertFalse(created['is_quick_registration'])

        resp = self.client.patch(
            f"/api/events/{created['slug']}/", {'quick_registration_fields': ['branch']}, format='json',
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertNotEqual(resp.data['registration_form'], custom.id)
        self.assertTrue(resp.data['is_quick_registration'])

        custom.refresh_from_db()
        self.assertEqual(custom.status, FormStatus.PUBLISHED)
        self.assertEqual([f.label for f in custom.fields.all()], ['Why join?'])

    def test_choosing_both_a_form_and_one_click_is_rejected(self):
        form = make_form()
        self.client.force_authenticate(self.admin)
        resp = self.client.post(
            '/api/events/',
            self._payload(registration_form=form.id, quick_registration_fields=['branch']), format='json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('quick_registration_fields', resp.data)

    def test_an_unknown_detail_is_rejected(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.post(
            '/api/events/', self._payload(quick_registration_fields=['password']), format='json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(Event.objects.filter(slug='one-click-workshop').exists())

    def test_a_member_cannot_set_up_one_click_registration(self):
        self.client.force_authenticate(self.member)
        resp = self.client.post(
            '/api/events/', self._payload(quick_registration_fields=['branch']), format='json',
        )
        self.assertEqual(resp.status_code, 403)

    def test_an_event_with_a_custom_form_is_not_one_click(self):
        form = make_form()
        add_field(form, FieldType.TEXT, label='Question')
        self.client.force_authenticate(self.admin)
        data = self.client.post('/api/events/', self._payload(registration_form=form.id), format='json').data
        self.assertFalse(data['is_quick_registration'])
        self.assertEqual(data['registration_profile_fields'], [])

    def test_a_hand_built_form_of_only_profile_fields_counts_as_one_click(self):
        form = make_form()
        add_field(form, FieldType.TEXT, label='Name', profile_field='full_name')
        add_field(form, FieldType.EMAIL, label='Email', profile_field='email')
        self.client.force_authenticate(self.admin)
        data = self.client.post('/api/events/', self._payload(registration_form=form.id), format='json').data
        self.assertTrue(data['is_quick_registration'])
        self.assertEqual(data['registration_profile_fields'], ['full_name', 'email'])

    def test_adding_an_ordinary_question_turns_it_into_a_custom_form(self):
        created = self._create_event(['branch'])
        add_field(Form.objects.get(pk=created['registration_form']), FieldType.TEXT, label='Extra question')
        resp = self.client.get(f"/api/events/{created['slug']}/")
        self.assertFalse(resp.data['is_quick_registration'])
        self.assertEqual(resp.data['registration_profile_fields'], [])

    def test_the_public_event_list_reports_one_click_events(self):
        self._create_event(['branch'])
        public = APIClient().get('/api/events/')
        self.assertEqual(public.status_code, 200)
        payload = public.data['results'] if isinstance(public.data, dict) else public.data
        self.assertTrue(payload[0]['is_quick_registration'])


class MemberRegistrationTests(QuickRegistrationTestBase):
    def _register(self, user, form_id, key='k1'):
        client = APIClient()
        if user is not None:
            client.force_authenticate(user)
        return client.post(
            '/api/forms/submissions/', {'form': form_id, 'answers': [], 'idempotency_key': key}, format='json',
        )

    def test_one_click_registers_with_the_members_profile_details(self):
        data = self._create_event(['phone_number', 'branch', 'year'])
        resp = self._register(self.member, data['registration_form'])
        self.assertEqual(resp.status_code, 201, resp.data)

        response = Response.objects.get(form_id=data['registration_form'], user=self.member)
        stored = {a.field.profile_field: a.value for a in response.answers.select_related('field')}
        self.assertEqual(stored['full_name'], 'Aditi Sharma')
        self.assertEqual(stored['email'], 'member1@srkr.ac.in')
        self.assertEqual(stored['phone_number'], '9876543210')
        self.assertEqual(stored['branch'], 'CSE')

    def test_a_missing_profile_detail_blocks_registration_and_names_it(self):
        incomplete = User.objects.create_user(
            username='nophone', email='nophone@srkr.ac.in', password='pw12345!', first_name='No', last_name='Phone',
        )
        data = self._create_event(['phone_number'])
        resp = self._register(incomplete, data['registration_form'])
        self.assertEqual(resp.status_code, 400)
        self.assertIn('Phone Number', str(resp.data))
        self.assertFalse(Response.objects.filter(form_id=data['registration_form']).exists())

    def test_signing_in_is_required(self):
        data = self._create_event(['branch'])
        resp = self._register(None, data['registration_form'])
        self.assertEqual(resp.status_code, 400)
        self.assertIn('log in', str(resp.data).lower())

    def test_a_member_cannot_register_twice(self):
        data = self._create_event(['branch'])
        self.assertEqual(self._register(self.member, data['registration_form'], 'a').status_code, 201)
        self.assertEqual(self._register(self.member, data['registration_form'], 'b').status_code, 400)
        self.assertEqual(Response.objects.filter(form_id=data['registration_form']).count(), 1)

    def test_registration_closes_when_the_event_is_full(self):
        data = self._create_event(['branch'], capacity=1)
        other = User.objects.create_user(
            username='other', email='other@srkr.ac.in', password='pw12345!', first_name='Other', branch='ECE',
        )
        self.assertEqual(self._register(self.member, data['registration_form'], 'a').status_code, 201)
        self.assertEqual(Form.objects.get(pk=data['registration_form']).status, FormStatus.CLOSED)
        self.assertEqual(self._register(other, data['registration_form'], 'b').status_code, 400)

    def test_the_registration_count_is_reported_on_the_event(self):
        data = self._create_event(['branch'])
        self._register(self.member, data['registration_form'])
        resp = self.client.get(f"/api/events/{data['slug']}/")
        self.assertEqual(resp.data['registration_count'], 1)
