import tempfile
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from apps.colleges.models import DEFAULT_COLLEGE_CODE, College, get_default_college
from apps.colleges.text import normalize_search_text

User = get_user_model()

RAW_EXPORT = (
    'S. No.,University Name,College Name,College Type,State Name,District Name\n'
    '1,"Andhra University, Visakhapatnam (Id: U-0006)","Sri N.S.N.Raju Degree College (Id: \nC-23892)",'
    'Affiliated College,Andhra Pradesh,East Godavari\n'
    '2,"Andhra University, Visakhapatnam (Id: U-0006)",Caf’e College (Id: C-1),Affiliated College,Kerala,Idukki\n'
    '3,"Andhra University, Visakhapatnam (Id: U-0006)",Duplicate (Id: C-1),Affiliated College,Kerala,Idukki\n'
    '4,"Andhra University, Visakhapatnam (Id: U-0006)",No Id College,Affiliated College,Kerala,Idukki\n'
)


class NormalizeSearchTextTests(TestCase):
    def test_dotted_initials_match_plain_acronym(self):
        self.assertEqual(normalize_search_text('S.R.K.R. Engineering College'), 'srkr engineering college')

    def test_punctuation_between_words_becomes_space(self):
        self.assertEqual(normalize_search_text('Arts & Science-College'), 'arts science college')


class ImportCollegesTests(TestCase):
    def _write(self, text, encoding='cp1252'):
        handle = tempfile.NamedTemporaryFile('wb', suffix='.csv', delete=False)
        handle.write(text.encode(encoding))
        handle.close()
        self.addCleanup(Path(handle.name).unlink)
        return Path(handle.name)

    def _import(self, **options):
        call_command('import_colleges', stdout=StringIO(), **options)

    def test_raw_export_is_parsed_and_deduplicated(self):
        self._import(file=self._write(RAW_EXPORT))
        self.assertEqual(College.objects.get(code='C-23892').name, 'Sri N.S.N.Raju Degree College')
        cafe = College.objects.get(code='C-1')
        self.assertEqual(cafe.name, 'Caf’e College')
        self.assertEqual(cafe.university, 'Andhra University, Visakhapatnam')
        self.assertFalse(College.objects.filter(name='No Id College').exists())

    def test_import_is_idempotent(self):
        path = self._write(RAW_EXPORT)
        self._import(file=path)
        count = College.objects.count()
        self._import(file=path)
        self.assertEqual(College.objects.count(), count)

    def test_dry_run_writes_nothing(self):
        before = College.objects.count()
        self._import(file=self._write(RAW_EXPORT), dry_run=True)
        self.assertEqual(College.objects.count(), before)

    def test_bundled_catalogue_loads_without_duplicating_default_college(self):
        self._import()
        self.assertGreater(College.objects.count(), 30000)
        self.assertEqual(College.objects.filter(code=DEFAULT_COLLEGE_CODE).count(), 1)
        self.assertEqual(get_default_college().name, 'SRKR Engineering College')


class CollegeSearchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        for index in range(75):
            College.objects.create(code=f'T-{index}', name=f'Zeta Institute {index:02d}', state='Goa')
        College.objects.create(code='T-a', name='Alpha Kavya Institute', state='Goa')
        College.objects.create(code='T-b', name='Kavya Memorial College', state='Goa')
        College.objects.create(code='T-c', name='S.R.K.R. Engineering College (Old)', state='Goa')

    def setUp(self):
        self.client = APIClient()

    def _names(self, **params):
        response = self.client.get('/api/colleges/', params)
        self.assertEqual(response.status_code, 200)
        return [row['name'] for row in response.data['results']], response

    def test_is_public(self):
        _, response = self._names()
        self.assertIn('count', response.data)

    def test_empty_query_pins_default_college_first(self):
        names, _ = self._names()
        self.assertEqual(names[0], 'SRKR Engineering College')

    def test_prefix_matches_rank_before_substring_matches(self):
        names, _ = self._names(q='kavya')
        self.assertEqual(names, ['Kavya Memorial College', 'Alpha Kavya Institute'])

    def test_every_word_must_match_in_any_order(self):
        names, _ = self._names(q='college kavya')
        self.assertEqual(names, ['Kavya Memorial College'])

    def test_dotted_and_plain_acronyms_find_the_same_college(self):
        dotted, _ = self._names(q='s.r.k.r')
        plain, _ = self._names(q='srkr')
        self.assertEqual(sorted(dotted), sorted(plain))
        self.assertIn('S.R.K.R. Engineering College (Old)', plain)

    def test_default_college_is_found_by_its_founders_full_name(self):
        names, _ = self._names(q='sagi ramakrishnam raju')
        self.assertEqual(names, [])
        names, _ = self._names(q='sagi rama krishnam raju')
        self.assertEqual(names, ['SRKR Engineering College'])

    def test_no_match_returns_empty_page(self):
        names, response = self._names(q='qqqqzzzz')
        self.assertEqual(names, [])
        self.assertEqual(response.data['count'], 0)

    def test_pagination_exposes_next_page_without_overlap(self):
        first, response = self._names(page_size=50)
        self.assertEqual(len(first), 50)
        self.assertIsNotNone(response.data['next'])
        second, _ = self._names(page_size=50, page=2)
        self.assertFalse(set(first) & set(second))

    def test_detail_returns_college(self):
        college = get_default_college()
        response = self.client.get(f'/api/colleges/{college.id}/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['code'], DEFAULT_COLLEGE_CODE)


class CollegeOnUsersTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.other = College.objects.create(code='T-x', name='Other Institute', state='Goa')

    def _payload(self, **overrides):
        payload = {
            'email': 'new@gmail.com', 'password': 'StrongPass123!', 'first_name': 'New',
            'last_name': 'Member', 'roll_number': '22B91A0599', 'branch': 'CSE', 'year': 2,
        }
        payload.update(overrides)
        return payload

    def test_signup_without_college_defaults_to_srkr(self):
        response = self.client.post('/api/auth/register/', self._payload(), format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(User.objects.get(email='new@gmail.com').college, get_default_college())

    def test_signup_with_chosen_college(self):
        response = self.client.post('/api/auth/register/', self._payload(college_id=self.other.id), format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(User.objects.get(email='new@gmail.com').college, self.other)

    def test_signup_with_unknown_college_is_rejected_on_the_field(self):
        response = self.client.post('/api/auth/register/', self._payload(college_id=999999), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('college_id', response.data)

    def test_profile_returns_college_and_allows_changing_it(self):
        self.client.post('/api/auth/register/', self._payload(), format='json')
        self.client.force_authenticate(User.objects.get(email='new@gmail.com'))
        me = self.client.get('/api/auth/me/')
        self.assertEqual(me.data['college']['code'], DEFAULT_COLLEGE_CODE)
        patched = self.client.patch('/api/auth/me/', {'college_id': self.other.id}, format='json')
        self.assertEqual(patched.status_code, 200)
        self.assertEqual(patched.data['college']['code'], 'T-x')

    def test_signup_with_unlisted_college_stores_typed_name_and_no_catalogue_link(self):
        response = self.client.post('/api/auth/register/', self._payload(other_college_name='  Tiny   Town Institute '), format='json')
        self.assertEqual(response.status_code, 201)
        user = User.objects.get(email='new@gmail.com')
        self.assertIsNone(user.college)
        self.assertEqual(user.other_college_name, 'Tiny Town Institute')

    def test_signup_rejects_unlisted_college_with_bad_characters(self):
        response = self.client.post('/api/auth/register/', self._payload(other_college_name='<script>'), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('other_college_name', response.data)

    def test_catalogue_choice_wins_over_typed_name(self):
        response = self.client.post(
            '/api/auth/register/',
            self._payload(college_id=self.other.id, other_college_name='Ignored Institute'),
            format='json',
        )
        self.assertEqual(response.status_code, 201)
        user = User.objects.get(email='new@gmail.com')
        self.assertEqual(user.college, self.other)
        self.assertEqual(user.other_college_name, '')

    def test_profile_switches_between_catalogue_and_typed_college(self):
        self.client.post('/api/auth/register/', self._payload(), format='json')
        self.client.force_authenticate(User.objects.get(email='new@gmail.com'))

        typed = self.client.patch('/api/auth/me/', {'college_id': None, 'other_college_name': 'Tiny Town Institute'}, format='json')
        self.assertEqual(typed.status_code, 200)
        self.assertIsNone(typed.data['college'])
        self.assertEqual(typed.data['other_college_name'], 'Tiny Town Institute')

        listed = self.client.patch('/api/auth/me/', {'college_id': self.other.id}, format='json')
        self.assertEqual(listed.data['college']['code'], 'T-x')
        self.assertEqual(listed.data['other_college_name'], '')

    def test_profile_cannot_clear_college_without_a_typed_name(self):
        self.client.post('/api/auth/register/', self._payload(), format='json')
        self.client.force_authenticate(User.objects.get(email='new@gmail.com'))
        response = self.client.patch('/api/auth/me/', {'college_id': None}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('college_id', response.data)
