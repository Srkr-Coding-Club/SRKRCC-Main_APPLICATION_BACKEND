"""Generated problem-statement IDs, CSV upload, public visibility and open innovation."""
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.core.dmc.contracts import QueryRequest
from apps.core.dmc.registry import get_dataset
from apps.hackathons.models import EntryStatus, ProblemStatement, Round, RoundEntry, Team, TeamStatus
from .helpers import make_hackathon, make_ps, make_team, make_user

OI = {'title': 'Smart bin', 'description': 'IoT bins that report fill level.', 'domain': 'IoT'}


class GeneratedIdTests(APITestCase):
    def setUp(self):
        self.hack = make_hackathon()
        self.admin = make_user(role='ADMIN')
        self.url = f'/api/hackathons/{self.hack.slug}/problem-statements/'
        self.client.force_authenticate(self.admin)

    def _create(self, **overrides):
        body = {'title': 'Smart Campus', 'description': 'Automate attendance.', 'domain': 'EdTech'}
        body.update(overrides)
        return self.client.post(self.url, body, format='json')

    def test_ids_are_generated_sequentially_and_a_typed_code_is_ignored(self):
        first = self._create(code='MY-CODE')
        second = self._create(title='Second')
        self.assertEqual((first.status_code, second.status_code), (201, 201))
        self.assertEqual((first.data['code'], second.data['code']), ('PS-001', 'PS-002'))

    def test_next_id_follows_the_highest_existing_one(self):
        make_ps(self.hack, 'PS-001')
        make_ps(self.hack, 'PS-007')
        make_ps(self.hack, 'LEGACY')
        self.assertEqual(self._create().data['code'], 'PS-008')

    def test_each_hackathon_numbers_its_own_statements(self):
        make_ps(make_hackathon(), 'PS-005')
        self.assertEqual(self._create().data['code'], 'PS-001')

    def test_title_description_and_domain_are_all_required(self):
        for missing in ('title', 'description', 'domain'):
            resp = self._create(**{missing: '   '})
            self.assertEqual(resp.status_code, 400, missing)
            self.assertIn(missing, resp.data)
        self.assertFalse(ProblemStatement.objects.filter(hackathon=self.hack).exists())

    def test_the_id_cannot_be_edited(self):
        created = self._create().data
        resp = self.client.patch(f"{self.url}{created['id']}/", {'code': 'HACKED', 'title': 'Renamed'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual((resp.data['code'], resp.data['title']), ('PS-001', 'Renamed'))

    def test_max_teams_must_be_positive(self):
        self.assertEqual(self._create(max_teams=0).status_code, 400)


class PublicVisibilityTests(APITestCase):
    def setUp(self):
        self.hack = make_hackathon()
        make_ps(self.hack, 'PS-001', domain='EdTech')
        make_ps(self.hack, 'PS-002', is_active=False)
        self.url = f'/api/hackathons/{self.hack.slug}/problem-statements/'

    def test_anyone_can_read_the_active_statements_with_their_domain(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([(p['code'], p['domain']) for p in resp.data], [('PS-001', 'EdTech')])

    def test_logged_in_participants_see_the_same_list(self):
        self.client.force_authenticate(make_user())
        self.assertEqual([p['code'] for p in self.client.get(self.url).data], ['PS-001'])

    def test_admins_also_see_inactive_ones(self):
        self.client.force_authenticate(make_user(role='ADMIN'))
        self.assertEqual({p['code'] for p in self.client.get(self.url).data}, {'PS-001', 'PS-002'})

    def test_a_hidden_hackathon_exposes_nothing(self):
        self.hack.visible_until = timezone.now() - timezone.timedelta(days=1)
        self.hack.save()
        self.assertEqual(self.client.get(self.url).status_code, 404)


class CsvUploadTests(APITestCase):
    def setUp(self):
        self.hack = make_hackathon()
        self.url = f'/api/hackathons/{self.hack.slug}/problem-statements/upload/'
        self.client.force_authenticate(make_user(role='ADMIN'))

    def _upload(self, text, name='problems.csv', encoding='utf-8'):
        data = text.encode(encoding) if isinstance(text, str) else text
        return self.client.post(self.url, {'file': SimpleUploadedFile(name, data, content_type='text/csv')}, format='multipart')

    def test_creates_statements_with_generated_ids(self):
        resp = self._upload('title,description,domain\nSmart Bin,Fill-level IoT,IoT\nCrop Advisor,ML for farmers,AgriTech\n')
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual((resp.data['created'], resp.data['codes']), (2, ['PS-001', 'PS-002']))
        rows = ProblemStatement.objects.filter(hackathon=self.hack).order_by('code')
        self.assertEqual([(r.code, r.title, r.domain) for r in rows],
                         [('PS-001', 'Smart Bin', 'IoT'), ('PS-002', 'Crop Advisor', 'AgriTech')])

    def test_numbering_continues_after_existing_statements(self):
        make_ps(self.hack, 'PS-004')
        self.assertEqual(self._upload('title,description,domain\nA,a,x\n').data['codes'], ['PS-005'])

    def test_header_aliases_and_a_utf8_bom_are_accepted(self):
        resp = self._upload('﻿title,Details,Track\nBin,Fill-level IoT,IoT\n')
        self.assertEqual(resp.status_code, 201, resp.data)

    def test_bad_rows_are_reported_and_good_rows_still_import(self):
        resp = self._upload('title,description,domain\nGood,fine,IoT\n,no title,IoT\nNo domain,fine,\nAlso good,ok,ML\n')
        self.assertEqual(resp.data['created'], 2)
        self.assertEqual([(e['row'], e['message']) for e in resp.data['errors']],
                         [(3, 'title is required'), (4, 'domain is required')])

    def test_duplicates_are_skipped(self):
        make_ps(self.hack, 'PS-001', title='Smart Bin', domain='IoT')
        resp = self._upload('title,description,domain\nsmart bin,again,iot\nNew,n,ML\nNew,n,ML\n')
        self.assertEqual((resp.data['created'], resp.data['skipped']), (1, 2))

    def test_missing_columns_are_named(self):
        resp = self._upload('title,description\nA,a\n')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'CSV_MISSING_COLUMNS')
        self.assertIn('domain', resp.data['detail'])

    def test_file_checks(self):
        self.assertEqual(self.client.post(self.url, {}, format='multipart').data['code'], 'CSV_REQUIRED')
        self.assertEqual(self._upload('a', name='x.txt').data['code'], 'CSV_TYPE')
        self.assertEqual(self._upload(b'\xff\xfe\x00bad').data['code'], 'CSV_ENCODING')
        self.assertEqual(self._upload('title,description,domain\n' + 'a,b,c\n' * 501).data['code'], 'CSV_TOO_MANY_ROWS')

    def test_only_admins_can_upload(self):
        self.client.force_authenticate(make_user())
        self.assertEqual(self._upload('title,description,domain\nA,a,x\n').status_code, 403)
        self.client.force_authenticate(None)
        self.assertIn(self._upload('title,description,domain\nA,a,x\n').status_code, (401, 403))
        self.assertFalse(ProblemStatement.objects.filter(hackathon=self.hack).exists())


class OpenInnovationTests(APITestCase):
    def setUp(self):
        self.hack = make_hackathon(min_team_size=1)
        self.ps = make_ps(self.hack, 'PS-001', domain='EdTech', max_teams=1)
        self.user = make_user()
        self.client.force_authenticate(self.user)
        self.url = f'/api/hackathons/{self.hack.slug}/teams/'

    def _create(self, name='Bug Slayers', **body):
        return self.client.post(self.url, {'name': name, **body}, format='json')

    def test_a_team_can_bring_its_own_problem(self):
        resp = self._create(open_innovation=OI)
        self.assertEqual(resp.status_code, 201, resp.data)
        team = Team.objects.get(pk=resp.data['id'])
        self.assertTrue(team.is_open_innovation)
        self.assertIsNone(team.problem_statement)
        self.assertEqual((team.custom_problem_title, team.custom_problem_domain), ('Smart bin', 'IoT'))
        self.assertIsNone(resp.data['problem_statement'])
        self.assertEqual(resp.data['open_innovation'], {
            'code': f'OI-{team.pk:03d}', 'title': 'Smart bin', 'description': OI['description'], 'domain': 'IoT',
        })

    def test_title_description_and_domain_are_all_mandatory(self):
        for missing, field in (('title', 'custom_problem_title'), ('description', 'custom_problem_description'),
                               ('domain', 'custom_problem_domain')):
            for blank in ('', '   '):
                resp = self._create(open_innovation={**OI, missing: blank})
                self.assertEqual(resp.status_code, 400, (missing, blank))
                self.assertEqual((resp.data['code'], resp.data['field']), ('OPEN_INNOVATION_INCOMPLETE', field))
        self.assertFalse(Team.objects.exists())

    def test_overlong_values_are_rejected(self):
        resp = self._create(open_innovation={**OI, 'domain': 'x' * 101})
        self.assertEqual(resp.data['code'], 'OPEN_INNOVATION_TOO_LONG')

    def test_open_innovation_must_be_an_object(self):
        self.assertEqual(self._create(open_innovation='Smart bin').data['code'], 'INVALID')

    def test_a_statement_and_open_innovation_cannot_be_combined(self):
        resp = self._create(problem_statement=self.ps.id, open_innovation=OI)
        self.assertEqual(resp.data['code'], 'PROBLEM_CHOICE_CONFLICT')

    def test_admins_can_switch_open_innovation_off(self):
        self.hack.allow_open_innovation = False
        self.hack.save()
        resp = self._create(open_innovation=OI)
        self.assertEqual(resp.data['code'], 'OPEN_INNOVATION_DISABLED')
        self.assertEqual(self._create(problem_statement=self.ps.id).status_code, 201)

    def test_picking_nothing_is_still_rejected_while_statements_exist(self):
        resp = self._create()
        self.assertEqual(resp.data['code'], 'PROBLEM_STATEMENT_REQUIRED')
        self.assertIn('open innovation', resp.data['detail'])

    def test_a_hackathon_without_statements_does_not_force_a_problem(self):
        bare = make_hackathon(min_team_size=1)
        resp = self.client.post(f'/api/hackathons/{bare.slug}/teams/', {'name': 'No Problem'}, format='json')
        self.assertEqual(resp.status_code, 201, resp.data)
        resp = self.client.post(f'/api/hackathons/{bare.slug}/teams/', {'name': 'Other'}, format='json')
        self.assertEqual(resp.data['code'], 'ALREADY_IN_TEAM')

    def test_open_innovation_teams_do_not_use_up_statement_slots(self):
        self._create(open_innovation=OI)
        other = make_user()
        self.client.force_authenticate(other)
        self.assertEqual(self._create(name='Second', problem_statement=self.ps.id).status_code, 201)


class SwitchingProblemTests(APITestCase):
    def setUp(self):
        self.hack = make_hackathon(min_team_size=1)
        self.ps = make_ps(self.hack, 'PS-001', max_teams=1)
        self.leader = make_user()
        self.member = make_user()
        self.team = make_team(self.hack, self.leader, self.member, ps=self.ps)

    def _patch(self, user, body):
        self.client.force_authenticate(user)
        return self.client.patch(f'/api/hackathons/teams/{self.team.id}/', body, format='json')

    def test_statement_to_open_innovation_frees_the_slot(self):
        resp = self._patch(self.leader, {'open_innovation': OI})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.team.refresh_from_db()
        self.assertTrue(self.team.is_open_innovation)
        self.assertIsNone(self.team.problem_statement)
        self.client.force_authenticate(make_user())
        resp = self.client.post(f'/api/hackathons/{self.hack.slug}/teams/',
                                {'name': 'Next', 'problem_statement': self.ps.id}, format='json')
        self.assertEqual(resp.status_code, 201, resp.data)

    def test_open_innovation_back_to_a_statement_clears_the_custom_problem(self):
        self._patch(self.leader, {'open_innovation': OI})
        resp = self._patch(self.leader, {'problem_statement': self.ps.id})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.team.refresh_from_db()
        self.assertEqual((self.team.is_open_innovation, self.team.custom_problem_title, self.team.custom_problem_domain),
                         (False, '', ''))
        self.assertEqual(self.team.problem_statement, self.ps)

    def test_a_failed_switch_leaves_the_team_unchanged(self):
        resp = self._patch(self.leader, {'open_innovation': {**OI, 'domain': ''}})
        self.assertEqual(resp.status_code, 400)
        self.team.refresh_from_db()
        self.assertEqual((self.team.problem_statement, self.team.is_open_innovation), (self.ps, False))

    def test_renaming_does_not_touch_the_problem(self):
        self._patch(self.leader, {'open_innovation': OI})
        self.assertEqual(self._patch(self.leader, {'name': 'Renamed'}).status_code, 200)
        self.team.refresh_from_db()
        self.assertEqual((self.team.name, self.team.is_open_innovation, self.team.custom_problem_title),
                         ('Renamed', True, 'Smart bin'))

    def test_only_the_leader_can_change_it(self):
        self.assertEqual(self._patch(self.member, {'open_innovation': OI}).status_code, 403)

    def test_locked_edits_block_the_switch(self):
        self.hack.team_edits_locked = True
        self.hack.save()
        self.assertEqual(self._patch(self.leader, {'open_innovation': OI}).data['code'], 'TEAM_EDITS_LOCKED')


class AdminVisibilityTests(APITestCase):
    def setUp(self):
        self.hack = make_hackathon(min_team_size=1)
        self.ps = make_ps(self.hack, 'PS-001', domain='EdTech')
        self.statement_team = make_team(self.hack, make_user(), name='Alpha', ps=self.ps)
        self.oi_team = make_team(self.hack, make_user(), name='Beta')
        self.oi_team.is_open_innovation = True
        self.oi_team.custom_problem_title = 'Smart bin'
        self.oi_team.custom_problem_description = 'IoT bins.'
        self.oi_team.custom_problem_domain = 'IoT'
        self.oi_team.save()
        self.admin = make_user(role='ADMIN')
        self.client.force_authenticate(self.admin)

    def test_admin_team_list_shows_the_open_innovation_problem(self):
        teams = {t['name']: t for t in self.client.get(f'/api/hackathons/{self.hack.slug}/teams/').data}
        self.assertEqual(teams['Beta']['open_innovation']['domain'], 'IoT')
        self.assertIsNone(teams['Alpha']['open_innovation'])
        self.assertEqual(teams['Alpha']['problem_statement']['domain'], 'EdTech')

    def test_admins_can_filter_open_innovation_teams(self):
        resp = self.client.get(f'/api/hackathons/{self.hack.slug}/teams/', {'problem_statement': 'open_innovation'})
        self.assertEqual([t['name'] for t in resp.data], ['Beta'])
        resp = self.client.get(f'/api/hackathons/{self.hack.slug}/teams/', {'problem_statement': self.ps.id})
        self.assertEqual([t['name'] for t in resp.data], ['Alpha'])

    def test_stats_count_open_innovation_teams(self):
        stats = self.client.get(f'/api/hackathons/{self.hack.slug}/stats/').data
        self.assertEqual(stats['open_innovation_teams'], 1)
        self.assertEqual(stats['problem_statements'][0]['teams'], 1)

    def test_members_see_their_own_open_innovation_problem(self):
        self.client.force_authenticate(self.oi_team.leader)
        resp = self.client.get(f'/api/hackathons/{self.hack.slug}/my-team/')
        self.assertEqual(resp.data['team']['open_innovation']['title'], 'Smart bin')

    def test_invites_say_when_the_team_is_open_innovation(self):
        invitee = make_user()
        self.client.force_authenticate(self.oi_team.leader)
        self.client.post(f'/api/hackathons/teams/{self.oi_team.id}/invite/', {'email': invitee.email}, format='json')
        self.client.force_authenticate(invitee)
        invites = self.client.get('/api/hackathons/my-invites/').data
        self.assertTrue(invites[0]['is_open_innovation'])


class ExportTests(TestCase):
    def setUp(self):
        self.hack = make_hackathon(title='Spring Hack')
        ps = make_ps(self.hack, 'PS-001', domain='EdTech', title='Smart Attendance')
        self.alpha = make_team(self.hack, make_user(), name='Alpha', ps=ps)
        self.beta = make_team(self.hack, make_user(), name='Beta')
        Team.objects.filter(pk=self.beta.pk).update(
            is_open_innovation=True, custom_problem_title='Smart bin', custom_problem_description='d', custom_problem_domain='IoT',
        )
        rnd = Round.objects.create(hackathon=self.hack, order=1, name='R1')
        for team in (self.alpha, self.beta):
            RoundEntry.objects.create(round=rnd, team=team, status=EntryStatus.PENDING)

    def test_teams_dataset_exposes_problem_id_title_and_domain(self):
        adapter = get_dataset('hackathon_teams').adapter_class()
        records = {r['team_name'].value: r for r in adapter.query(QueryRequest(page=1, page_size=50), None).records}
        alpha, beta = records['Alpha'], records['Beta']
        self.assertEqual((alpha['problem_id'].value, alpha['problem_domain'].value, alpha['open_innovation'].value),
                         ('PS-001', 'EdTech', False))
        self.assertEqual((beta['problem_id'].value, beta['problem_title'].value, beta['problem_domain'].value,
                          beta['open_innovation'].value), (f'OI-{self.beta.pk:03d}', 'Smart bin', 'IoT', True))

    def test_round_results_label_open_innovation_teams(self):
        adapter = get_dataset('hackathon_round_entries').adapter_class()
        records = {r['team_name'].value: r for r in adapter.query(QueryRequest(page=1, page_size=50), None).records}
        self.assertEqual(records['Alpha']['problem_statement'].value, 'PS-001 — Smart Attendance')
        self.assertEqual(records['Beta']['problem_statement'].value, f'OI-{self.beta.pk:03d} — Smart bin')
