"""Every organizer download follows one file contract (see apps/core/dmc/export_service.py) and the right permissions."""
import csv
import io
import json
import re

from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.audit.models import AuditLog
from apps.hackathons.exports import EXPORT_KINDS
from apps.hackathons.models import (
    CheckIn, HackathonStaff, IdeaSubmission, Round, RoundEntry, Submission, TeamMember,
)
from .helpers import make_hackathon, make_ps, make_team, make_user

BOM = '﻿'
DATE_TIME = re.compile(r'^\d{2} [A-Z][a-z]{2} \d{4}, \d{2}:\d{2}$')


def _api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


def _rows(response):
    return list(csv.reader(io.StringIO(response.content.decode('utf-8-sig'))))


class ExportBase(APITestCase):
    def setUp(self):
        self.hack = make_hackathon(
            min_team_size=1, features={'ideas': True, 'submissions': True, 'judging': True, 'check_in': True},
            registration_questions=[{'key': 'tshirt', 'label': 'T-shirt size', 'type': 'text', 'required': False, 'options': []}],
        )
        self.admin = make_user(role='ADMIN')
        self.leader = make_user(first_name='Asha', last_name='Rao')
        self.ps = make_ps(self.hack, 'PS-1', title='Offline telemedicine', domain='HealthTech')
        self.team = make_team(self.hack, self.leader, name='Alpha', ps=self.ps)
        TeamMember.objects.filter(user=self.leader).update(answers={'tshirt': 'M'}, rules_accepted_at=timezone.now())
        self.base = f'/api/hackathons/{self.hack.slug}/export'

    def export(self, kind, who=None, **params):
        return _api(who or self.admin).get(f'{self.base}/{kind}/', params)

    def add_everything(self):
        """One row for every export kind."""
        Submission.objects.create(team=self.team, project_title='Tele', description='d', repo_url='https://x.io/r', score=88, review_count=2)
        IdeaSubmission.objects.create(team=self.team, title='Idea', problem='p', solution='s', tech_stack=['React', 'Django'], status='APPROVED')
        round_obj = Round.objects.create(hackathon=self.hack, name='Screening', order=1)
        RoundEntry.objects.get_or_create(round=round_obj, team=self.team)
        CheckIn.objects.create(hackathon=self.hack, user=self.leader, team=self.team, checked_in_by=self.admin)
        return round_obj

    def params_for(self, kind, round_obj):
        return {'round': round_obj.id} if kind == 'rounds' else {}


class SharedContractTests(ExportBase):
    def test_every_kind_follows_the_same_file_contract(self):
        round_obj = self.add_everything()
        for key, spec in EXPORT_KINDS.items():
            with self.subTest(kind=key):
                response = self.export(key, **self.params_for(key, round_obj))
                self.assertEqual(response.status_code, 200, key)
                self.assertTrue(response['Content-Type'].startswith('text/csv'))
                self.assertTrue(response.content.decode('utf-8').startswith(BOM), 'UTF-8 BOM so Excel reads non-English text')
                disposition = response['Content-Disposition']
                self.assertRegex(disposition, rf'filename="{self.hack.slug}-{re.escape(spec.name)}-\d{{8}}_\d{{6}}\.csv"')
                rows = _rows(response)
                headers = [col.label for col in spec.columns(self.hack)]
                self.assertEqual(rows[0], headers)
                self.assertGreaterEqual(len(rows), 2, f'{key} should have a data row')
                self.assertTrue(all(len(row) == len(headers) for row in rows), 'every row has every column')

    def test_headers_are_readable_labels_not_field_names(self):
        self.add_everything()
        for key, spec in EXPORT_KINDS.items():
            labels = [col.label for col in spec.columns(self.hack)]
            self.assertTrue(all(label[0].isupper() and '_' not in label for label in labels), (key, labels))

    def test_booleans_read_yes_or_no_and_lists_are_joined(self):
        self.add_everything()
        teams = _rows(self.export('teams'))
        self.assertEqual(teams[1][teams[0].index('Open Innovation')], 'No')
        ideas = _rows(self.export('ideas'))
        self.assertEqual(ideas[1][ideas[0].index('Tech Stack')], 'React, Django')
        self.assertEqual(ideas[1][ideas[0].index('Slides File Uploaded')], 'No')
        participants = _rows(self.export('participants'))
        self.assertEqual(participants[1][participants[0].index('Rules Accepted')], 'Yes')

    def test_dates_are_ist_and_readable(self):
        self.add_everything()
        ideas = _rows(self.export('ideas'))
        self.assertRegex(ideas[1][ideas[0].index('Submitted At')], DATE_TIME)
        submissions = _rows(self.export('submissions'))
        self.assertRegex(submissions[1][submissions[0].index('Submitted At')], DATE_TIME)
        checkin = _rows(self.export('check-in'))
        stamp = CheckIn.objects.get().checked_in_at
        self.assertEqual(checkin[1][checkin[0].index('Checked In At')], timezone.localtime(stamp).strftime('%d %b %Y, %H:%M'))

    def test_text_that_looks_like_a_formula_is_neutralised(self):
        self.team.name = '=HYPERLINK("http://evil","click")'
        self.team.save()
        self.leader.first_name = '+cmd'
        self.leader.save()
        teams = _rows(self.export('teams'))
        self.assertTrue(teams[1][teams[0].index('Team')].startswith("'="))
        self.assertTrue(teams[1][teams[0].index('Name')].startswith("'+cmd"))

    def test_non_english_text_survives_the_round_trip(self):
        self.leader.first_name = 'తెలుగు'
        self.leader.last_name = 'ಕನ್ನಡ ₹'
        self.leader.save()
        teams = _rows(self.export('teams'))
        self.assertEqual(teams[1][teams[0].index('Name')], 'తెలుగు ಕನ್ನಡ ₹')

    def test_values_with_commas_quotes_and_newlines_stay_in_one_cell(self):
        IdeaSubmission.objects.create(team=self.team, title='Say "hi", ok', problem='line one\nline two', solution='s')
        ideas = _rows(self.export('ideas'))
        self.assertEqual(ideas[1][ideas[0].index('Title')], 'Say "hi", ok')
        self.assertEqual(ideas[1][ideas[0].index('Problem')], 'line one\nline two')

    def test_excel_and_json_carry_the_same_columns_and_rows_as_the_csv(self):
        self.add_everything()
        csv_rows = _rows(self.export('ideas'))
        xlsx = self.export('ideas', export_format='xlsx')
        self.assertEqual(xlsx['Content-Type'], 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        self.assertRegex(xlsx['Content-Disposition'], r'\.xlsx"')
        import openpyxl
        sheet = openpyxl.load_workbook(io.BytesIO(xlsx.content)).active
        sheet_rows = [[cell if cell is not None else '' for cell in row] for row in sheet.iter_rows(values_only=True)]
        self.assertEqual(sheet_rows[0], csv_rows[0])
        self.assertEqual(len(sheet_rows), len(csv_rows))

        as_json = self.export('ideas', export_format='json')
        self.assertEqual(as_json['Content-Type'], 'application/json')
        records = json.loads(as_json.content)
        self.assertEqual(list(records[0].keys()), csv_rows[0])
        self.assertEqual(records[0]['Title'], 'Idea')
        self.assertRegex(records[0]['Submitted At'], r'^\d{4}-\d{2}-\d{2}T')

    def test_an_empty_hackathon_still_gives_a_file_with_just_the_headers(self):
        empty = make_hackathon(min_team_size=1, features={'check_in': True})
        for key in ('teams', 'participants', 'ideas', 'submissions', 'check-in'):
            response = _api(self.admin).get(f'/api/hackathons/{empty.slug}/export/{key}/')
            self.assertEqual(response.status_code, 200, key)
            self.assertEqual(len(_rows(response)), 1, key)

    def test_the_form_responses_and_data_center_exports_use_the_same_name_and_date_rules(self):
        from apps.core.dmc.export_service import export_filename, format_export_datetime
        from datetime import datetime, timezone as dt_timezone

        utc = datetime(2026, 10, 27, 6, 22, tzinfo=dt_timezone.utc)
        self.assertEqual(export_filename('demo', 'responses', 'csv', utc), 'demo-responses-20261027_115200.csv')
        self.assertEqual(format_export_datetime(utc), '27 Oct 2026, 11:52')
        self.assertEqual(format_export_datetime(utc.isoformat()), '27 Oct 2026, 11:52')
        self.assertEqual(format_export_datetime('not a date'), None)


class ExportContentTests(ExportBase):
    def test_teams_has_one_row_per_member_with_the_problem(self):
        buddy = make_user(first_name='Bo', last_name='Li')
        TeamMember.objects.create(team=self.team, user=buddy)
        rows = _rows(self.export('teams'))
        header = rows[0]
        self.assertEqual(len(rows) - 1, 2)
        self.assertEqual({r[header.index('Name')] for r in rows[1:]}, {'Asha Rao', 'Bo Li'})
        self.assertEqual(rows[1][header.index('Problem ID')], 'PS-1')
        self.assertEqual(rows[1][header.index('Problem Domain')], 'HealthTech')

    def test_teams_can_be_limited_to_the_rows_on_screen(self):
        other = make_team(self.hack, make_user(), name='Beta')
        rows = _rows(self.export('teams', ids=str(other.id)))
        self.assertEqual([r[0] for r in rows[1:]], ['Beta'])
        self.assertEqual(len(_rows(self.export('teams', ids=''))), 1)
        self.assertEqual(len(_rows(self.export('teams', ids='abc,,999999'))), 1)

    def test_open_innovation_teams_show_their_own_problem(self):
        oi = make_team(self.hack, make_user(), name='Gamma')
        oi.problem_statement, oi.is_open_innovation = None, True
        oi.custom_problem_title, oi.custom_problem_domain = 'My idea', 'IoT'
        oi.save()
        rows = _rows(self.export('teams', ids=str(oi.id)))
        self.assertEqual(rows[1][rows[0].index('Open Innovation')], 'Yes')
        self.assertEqual(rows[1][rows[0].index('Problem Title')], 'My idea')

    def test_participants_include_the_registration_questions_as_columns(self):
        rows = _rows(self.export('participants'))
        self.assertIn('T-shirt size', rows[0])
        self.assertEqual(rows[1][rows[0].index('T-shirt size')], 'M')

    def test_a_question_label_that_matches_a_fixed_column_does_not_overwrite_it(self):
        self.hack.registration_questions = [{'key': 'email', 'label': 'Email', 'type': 'text', 'required': False, 'options': []}]
        self.hack.save()
        TeamMember.objects.filter(user=self.leader).update(answers={'email': 'other@x.io'})
        rows = _rows(self.export('participants'))
        self.assertEqual(rows[0].count('Email'), 2)
        self.assertEqual(rows[1][rows[0].index('Email')], self.leader.email)

    def test_rounds_needs_a_round_from_this_hackathon(self):
        round_obj = self.add_everything()
        self.assertEqual(self.export('rounds').data['code'], 'ROUND_REQUIRED')
        self.assertEqual(self.export('rounds', round='abc').data['code'], 'ROUND_REQUIRED')
        other = Round.objects.create(hackathon=make_hackathon(min_team_size=1), name='Elsewhere', order=1)
        self.assertEqual(self.export('rounds', round=other.id).status_code, 404)
        rows = _rows(self.export('rounds', round=round_obj.id))
        self.assertEqual(rows[1][rows[0].index('Team')], 'Alpha')
        self.assertEqual(rows[1][rows[0].index('Project Submitted')], 'Yes')
        self.assertEqual(rows[1][rows[0].index('Members Checked In')], '1')

    def test_round_results_can_be_narrowed_by_status(self):
        round_obj = self.add_everything()
        make_team(self.hack, make_user(), name='Beta')
        RoundEntry.objects.get_or_create(round=round_obj, team=self.hack.teams.get(name='Beta'))
        RoundEntry.objects.filter(team__name='Beta').update(status='REJECTED')
        names = lambda **p: [r[0] for r in _rows(self.export('rounds', round=round_obj.id, **p))[1:]]
        self.assertEqual(sorted(names()), ['Alpha', 'Beta'])
        self.assertEqual(names(status='REJECTED'), ['Beta'])

    def test_check_in_lists_everyone_placed_and_who_scanned_them(self):
        TeamMember.objects.create(team=self.team, user=make_user(first_name='Late'))
        self.add_everything()
        rows = _rows(self.export('check-in'))
        header = rows[0]
        by_name = {r[header.index('Name')].split()[0]: r for r in rows[1:]}
        self.assertEqual(by_name['Asha'][header.index('Checked In')], 'Yes')
        self.assertEqual(by_name['Asha'][header.index('Checked In By')], f'{self.admin.first_name} {self.admin.last_name}'.strip())
        self.assertEqual(by_name['Late'][header.index('Checked In')], 'No')
        self.assertEqual(by_name['Late'][header.index('Checked In At')], '')


class ExportAccessTests(ExportBase):
    def test_each_kind_needs_its_own_permission(self):
        round_obj = self.add_everything()
        cases = {
            'teams': 'teams', 'participants': 'teams', 'ideas': 'ideas', 'submissions': 'submissions',
            'rounds': 'rounds', 'check-in': 'check_in',
        }
        for kind, permission in cases.items():
            for other in set(cases.values()) - {permission}:
                if kind == 'teams' and other in ('rounds', 'results'):
                    continue  # the team list is also open to people who run rounds
                staff = make_user()
                HackathonStaff.objects.create(hackathon=self.hack, user=staff, role='ORGANIZER', permissions=[other])
                self.assertEqual(self.export(kind, who=staff, **self.params_for(kind, round_obj)).status_code, 403, (kind, other))
            holder = make_user()
            HackathonStaff.objects.create(hackathon=self.hack, user=holder, role='ORGANIZER', permissions=[permission])
            self.assertEqual(self.export(kind, who=holder, **self.params_for(kind, round_obj)).status_code, 200, (kind, permission))

    def test_a_check_in_volunteer_can_download_only_the_check_in_list(self):
        volunteer = make_user()
        HackathonStaff.objects.create(hackathon=self.hack, user=volunteer, role='VOLUNTEER')
        self.assertEqual(self.export('check-in', who=volunteer).status_code, 200)
        for kind in ('teams', 'participants', 'ideas', 'submissions'):
            self.assertEqual(self.export(kind, who=volunteer).status_code, 403, kind)

    def test_participants_and_strangers_cannot_download_and_anonymous_is_rejected(self):
        for who in (self.leader, make_user()):
            for kind in EXPORT_KINDS:
                self.assertEqual(self.export(kind, who=who).status_code, 403, kind)
        self.assertEqual(_api().get(f'{self.base}/teams/').status_code, 401)

    def test_staff_of_another_hackathon_cannot_download_this_one(self):
        other = make_hackathon(min_team_size=1)
        staff = make_user()
        HackathonStaff.objects.create(hackathon=other, user=staff, role='ORGANIZER')
        self.assertEqual(self.export('teams', who=staff).status_code, 403)

    def test_unknown_kind_and_format_are_rejected(self):
        self.assertEqual(self.export('passwords').status_code, 404)
        self.assertEqual(self.export('teams', export_format='pdf').data['code'], 'INVALID')
        self.assertEqual(self.export('teams', export_format='CSV').status_code, 200)

    def test_downloads_are_audited_with_who_what_and_how_many_rows(self):
        self.add_everything()
        self.export('ideas', export_format='xlsx')
        entry = AuditLog.objects.filter(action='Hackathon Export (XLSX)').latest('id')
        self.assertEqual(entry.target_id, self.hack.slug)
        self.assertEqual(entry.details, {'kind': 'ideas', 'format': 'xlsx', 'rows': 1})
