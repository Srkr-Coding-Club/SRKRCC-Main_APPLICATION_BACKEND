from django.test import TestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from .helpers import make_hackathon, make_ps, make_user


class ProblemDomainConfigTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(make_user(role='ADMIN'))
        self.hack = make_hackathon(min_team_size=1)
        self.url = f'/api/hackathons/{self.hack.slug}/'
        self.ps_url = f'{self.url}problem-statements/'

    def _domains(self, domains):
        return self.client.patch(self.url, {'problem_domains': domains}, format='json')

    def _create(self, **overrides):
        body = {'title': 'T', 'description': 'D', 'domain': 'AI', **overrides}
        return self.client.post(self.ps_url, body, format='json')

    def test_admin_configures_domains_and_everyone_can_read_them(self):
        response = self._domains(['  AI ', 'EdTech', 'ai', ''])
        self.assertEqual(response.data['problem_domains'], ['AI', 'EdTech'])
        self.assertEqual(APIClient().get(self.url).data['problem_domains'], ['AI', 'EdTech'])

    def test_invalid_domain_lists_are_rejected(self):
        for bad in ('AI', ['x' * 101], [str(i) for i in range(31)]):
            self.assertEqual(self._domains(bad).status_code, 400, bad)

    def test_statements_must_use_a_configured_domain(self):
        self._domains(['AI', 'EdTech'])
        self.assertEqual(self._create(domain='Robotics').status_code, 400)
        created = self._create(domain='edtech')
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.data['domain'], 'EdTech')

    def test_without_a_configured_list_any_domain_is_accepted(self):
        self.assertEqual(self._create(domain='Whatever').status_code, 201)

    def test_domains_in_use_cannot_be_removed(self):
        self._domains(['AI', 'EdTech'])
        self._create(domain='AI')
        response = self._domains(['EdTech'])
        self.assertEqual(response.status_code, 400)
        self.assertIn('AI', str(response.data['problem_domains']))
        self.assertEqual(self._domains(['AI', 'EdTech', 'IoT']).status_code, 200)

    def test_first_domain_list_must_cover_existing_statements(self):
        make_ps(self.hack, domain='Legacy')
        self.assertEqual(self._domains(['AI']).status_code, 400)
        self.assertEqual(self._domains(['AI', 'legacy']).status_code, 200)

    def test_unused_domains_can_be_removed(self):
        self._domains(['AI', 'EdTech'])
        self._create(domain='AI')
        self.assertEqual(self._domains(['AI']).data['problem_domains'], ['AI'])

    def test_csv_import_adds_new_domains_and_normalises_case(self):
        self._domains(['AI'])
        csv = 'title,description,domain\nOne,d,ai\nTwo,d,Health Tech\n'.encode()
        response = self.client.post(
            f'{self.ps_url}upload/', {'file': SimpleUploadedFile('p.csv', csv, content_type='text/csv')}, format='multipart',
        )
        self.assertEqual(response.data['created'], 2)
        self.hack.refresh_from_db()
        self.assertEqual(self.hack.problem_domains, ['AI', 'Health Tech'])
        listed = {p['title']: p['domain'] for p in self.client.get(self.ps_url).data}
        self.assertEqual(listed, {'One': 'AI', 'Two': 'Health Tech'})


class ProblemStatementConfigTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(make_user(role='ADMIN'))
        self.hack = make_hackathon(min_team_size=1)
        self.ps_url = f'/api/hackathons/{self.hack.slug}/problem-statements/'

    def _create(self, **overrides):
        return self.client.post(self.ps_url, {'title': 'T', 'description': 'D', 'domain': 'AI', **overrides}, format='json')

    def test_ids_and_order_are_generated(self):
        first, second, third = (self._create(title=name).data for name in ('A', 'B', 'C'))
        self.assertEqual([first['code'], second['code'], third['code']], ['PS-001', 'PS-002', 'PS-003'])
        self.assertEqual([first['order'], second['order'], third['order']], [1, 2, 3])

    def test_a_typed_code_is_ignored(self):
        self.assertEqual(self._create(code='HACK-9').data['code'], 'PS-001')

    def test_tags_are_cleaned_and_limited(self):
        self.assertEqual(self._create(tags=[' iot ', 'IoT', 'ml', '']).data['tags'], ['iot', 'ml'])
        for bad in ('iot', ['x' * 31], [str(i) for i in range(11)]):
            self.assertEqual(self._create(tags=bad).status_code, 400, bad)

    def test_admin_can_edit_every_field_and_deactivate(self):
        ps = self._create(max_teams=2).data
        response = self.client.patch(
            f"{self.ps_url}{ps['id']}/",
            {'title': 'New', 'description': 'New D', 'domain': 'Web', 'tags': ['a'], 'max_teams': None, 'is_active': False},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        data = response.data
        self.assertEqual((data['title'], data['domain'], data['tags'], data['max_teams'], data['is_active']), ('New', 'Web', ['a'], None, False))
        self.assertEqual(data['code'], ps['code'])
