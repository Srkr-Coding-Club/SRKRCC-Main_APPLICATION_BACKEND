"""Comparing a past-member sheet with the accounts: reading, every outcome, the actions and who may use them."""

import csv
import io
import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import ClubIdEvent, ClubIdReservation, ReconciliationRow
from apps.accounts.services import club_id_registry as registry
from apps.accounts.services import member_reconciliation as service

User = get_user_model()
BASE = '/api/auth/reconciliation'
MAPPING = {'name': 'Name', 'email': 'Email', 'club_id': 'Affiliate ID'}


def person(email, **extra):
    return User.objects.create_user(username=email.split('@')[0] + str(User.objects.count()), email=email, password='Str0ng!Passw0rd', **extra)


def api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


def csv_file(rows, headers=('Name', 'Email', 'Affiliate ID'), name='past.csv'):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(headers)
    writer.writerows(rows)
    return SimpleUploadedFile(name, out.getvalue().encode('utf-8-sig'), content_type='text/csv')


def xlsx_file(sheets, name='past.xlsx'):
    import openpyxl
    book = openpyxl.Workbook()
    book.remove(book.active)
    for title, rows in sheets.items():
        sheet = book.create_sheet(title)
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return SimpleUploadedFile(name, buffer.getvalue(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


class Case(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = person('boss@example.org', role='ADMIN')
        self.lead = person('lead@example.org', role='CLUB_LEAD')
        self.sita = person('sita@example.org', first_name='Sita', last_name='Rao', club_id='25SCC001', role='AFFILIATE')
        self.meena = person('meena@example.org', first_name='Meena', last_name='Kumari')
        self.omar = person('omar@example.org', first_name='Omar', last_name='Zaid', club_id='25SCC003', role='AFFILIATE')
        self.holder = person('holder@example.org', first_name='Hana', club_id='25SCC050', role='AFFILIATE')

    def upload(self, rows, user=None, mapping=MAPPING, **extra):
        return api(user or self.admin).post(f'{BASE}/runs/', {'file': csv_file(rows), 'mapping': json.dumps(mapping), **extra}, format='multipart')

    def run_with(self, rows):
        reply = self.upload(rows)
        self.assertEqual(reply.status_code, 201, reply.data)
        return reply.data['id']

    def row(self, run_id, number):
        return ReconciliationRow.objects.get(run_id=run_id, row_number=number)

    def outcomes(self, run_id):
        return {row.row_number: row.outcome for row in ReconciliationRow.objects.filter(run_id=run_id)}

    def act(self, run_id, row, action, user=None, **body):
        row_id = self.row(run_id, row).pk if isinstance(row, int) else row
        return api(user or self.admin).post(f'{BASE}/runs/{run_id}/rows/{row_id}/{action}/', body, format='json')


class Reading(Case):
    def test_inspect_suggests_the_columns_from_common_headings_without_saving_anything(self):
        reply = api(self.admin).post(f'{BASE}/inspect/', {'file': csv_file([['Sita', 'sita@example.org', '25SCC001']], headers=('Student Name', 'E-mail', 'Affiliate ID'))}, format='multipart')
        self.assertEqual(reply.status_code, 200, reply.data)
        self.assertEqual(reply.data['suggested'], {'name': 'Student Name', 'email': 'E-mail', 'club_id': 'Affiliate ID'})
        self.assertEqual((reply.data['total_rows'], len(reply.data['sample'])), (1, 1))
        self.assertEqual(ReconciliationRow.objects.count(), 0)

    def test_an_excel_workbook_with_several_sheets_lets_you_pick_one(self):
        book = {'2023': [['Name', 'Email', 'Affiliate ID'], ['Sita Rao', 'sita@example.org', '25SCC001']], '2024': [['Name', 'Email', 'Affiliate ID'], ['Meena', 'meena@example.org', ''], ['Omar', 'omar@example.org', '25SCC003']]}
        listing = api(self.admin).post(f'{BASE}/inspect/', {'file': xlsx_file(book)}, format='multipart').data
        self.assertEqual([(s['name'], s['rows']) for s in listing['sheets']], [('2023', 1), ('2024', 2)])
        second = api(self.admin).post(f'{BASE}/inspect/', {'file': xlsx_file(book), 'sheet': '2024'}, format='multipart').data
        self.assertEqual((second['sheet'], second['total_rows']), ('2024', 2))
        reply = api(self.admin).post(f'{BASE}/runs/', {'file': xlsx_file(book), 'sheet': '2024', 'mapping': json.dumps(MAPPING)}, format='multipart')
        self.assertEqual((reply.status_code, reply.data['total']), (201, 2), reply.data)
        self.assertEqual(api(self.admin).post(f'{BASE}/inspect/', {'file': xlsx_file(book), 'sheet': 'nope'}, format='multipart').data['code'], 'NO_SUCH_SHEET')

    def test_numbers_blank_lines_and_odd_headers_are_handled(self):
        file = xlsx_file({'S': [[None, None, None], ['Name', None, 'Affiliate ID'], ['A B', 'a@example.org', 25], [], ['C D', 'c@example.org', None]]})
        data = api(self.admin).post(f'{BASE}/inspect/', {'file': file}, format='multipart').data
        self.assertEqual(data['headers'], ['Name', 'Column 2', 'Affiliate ID'])
        self.assertEqual(data['total_rows'], 2)

    def test_unsupported_empty_oversized_and_overlong_files_are_refused_clearly(self):
        post = lambda file: api(self.admin).post(f'{BASE}/inspect/', {'file': file}, format='multipart')  # noqa: E731
        self.assertEqual(post(SimpleUploadedFile('old.xls', b'x')).data['code'], 'UNSUPPORTED')
        self.assertEqual(post(SimpleUploadedFile('empty.csv', b'')).data['code'], 'EMPTY')
        self.assertEqual(post(SimpleUploadedFile('broken.xlsx', b'not a workbook')).data['code'], 'UNREADABLE')
        with mock.patch.object(service, 'MAX_ALLOWED_ROWS', 2):
            self.assertEqual(post(csv_file([['a', 'a@x.org', ''], ['b', 'b@x.org', ''], ['c', 'c@x.org', '']])).data['code'], 'TOO_MANY_ROWS')
        self.assertEqual(api(self.admin).post(f'{BASE}/inspect/', {}, format='multipart').data['code'], 'NO_FILE')

    def test_the_mapping_must_name_real_columns_and_an_email_or_an_id(self):
        self.assertEqual(self.upload([['a', 'a@x.org', '']], mapping={'email': 'Nope'}).data['code'], 'BAD_MAPPING')
        self.assertEqual(self.upload([['a', 'a@x.org', '']], mapping={'name': 'Name'}).data['code'], 'BAD_MAPPING')
        self.assertEqual(api(self.admin).post(f'{BASE}/runs/', {'file': csv_file([['a', 'a@x.org', '']]), 'mapping': '{bad'}, format='multipart').data['code'], 'BAD_MAPPING')

    def test_a_latin1_csv_is_read(self):
        raw = 'Name,Email,Affiliate ID\nJosé,jose@example.org,\n'.encode('latin-1')
        data = api(self.admin).post(f'{BASE}/inspect/', {'file': SimpleUploadedFile('latin.csv', raw)}, format='multipart').data
        self.assertEqual(data['sample'][0]['Name'], 'José')


class Outcomes(Case):
    def test_every_kind_of_row_is_told_apart(self):
        ClubIdEvent.objects.create(club_id='25SCC060', kind='CLEARED')
        registry.reserve(self.admin, email='someone@example.org', club_id='25SCC070', reason='x')
        run = self.run_with([
            ['Sita Rao', 'sita@example.org', '25SCC001'],            # 2  match
            ['Sita Roa', 'SITA@example.org', '25scc001'],            # 3  repeat of row 2 (spelled differently)
            ['Someone Else', 'omar@example.org', '25SCC003'],        # 4  match, but a different name
            ['Meena Kumari', 'meena@example.org', '25SCC080'],       # 5  account without an id, id free
            ['Hana', 'sita2@example.org', '25SCC050'],               # 6  id held by another email
            ['Omar Zaid', 'omar@example.org', '25SCC090'],           # 7  repeat of row 4 (same email)
            ['Hana H', 'holder@example.org', '25SCC999'],            # 8  account has another id
            ['New Person', 'new@example.org', '25SCC100'],           # 9  no account, id free
            ['No Id', 'noid@example.org', ''],                       # 10 no account, no id
            ['Sita Again', 'sita@example.org', ''],                  # 11 repeat of row 2 (same email), no id
            ['Retired', 'meena@example.org', '25SCC060'],            # 12 repeat of row 5 (same email)
            ['Taken', 'taken@example.org', '25SCC070'],              # 13 reserved for someone else
            ['Bad', 'not-an-email', '25SCC101'],                     # 14 invalid
            ['Bad id', 'okay@example.org', 'XYZ'],                   # 15 invalid
            ['Nothing', '', ''],                                     # 16 invalid
        ])
        outcomes = self.outcomes(run)
        self.assertEqual(outcomes[2], 'MATCH')
        self.assertEqual(outcomes[3], 'DUPLICATE')
        self.assertEqual(outcomes[4], 'NAME_DIFFERS')
        self.assertEqual(outcomes[5], 'ID_MISSING_IN_APP')
        self.assertEqual(outcomes[6], 'EMAIL_MISMATCH')
        self.assertEqual(outcomes[7], 'DUPLICATE')
        self.assertEqual(outcomes[8], 'ID_MISMATCH')
        self.assertEqual(outcomes[9], 'NO_ACCOUNT')
        self.assertEqual(outcomes[10], 'NO_ACCOUNT_NO_ID')
        self.assertEqual(outcomes[11], 'DUPLICATE')
        self.assertEqual(outcomes[12], 'DUPLICATE')
        self.assertEqual(outcomes[13], 'ID_RESERVED')
        self.assertEqual((outcomes[14], outcomes[15], outcomes[16]), ('INVALID', 'INVALID', 'INVALID'))
        self.assertIn('row 2', self.row(run, 3).detail)

    def test_an_account_without_an_id_whose_sheet_id_belongs_to_someone_else_is_told_so(self):
        run = self.run_with([['Meena', 'meena@example.org', '25SCC050']])
        row = self.row(run, 2)
        self.assertEqual((row.outcome, row.other_user_id), ('ID_HELD_BY_OTHER', self.holder.pk))
        self.assertIn('holder@example.org', row.detail)

    def test_a_retired_id_and_an_email_with_no_id_in_the_sheet(self):
        ClubIdEvent.objects.create(club_id='25SCC060', kind='CLEARED')
        run = self.run_with([['Meena', 'meena@example.org', '25SCC060'], ['Sita', 'sita@example.org', '']])
        self.assertEqual(self.outcomes(run), {2: 'ID_RETIRED', 3: 'NO_ID_IN_SHEET'})
        self.assertIn('25SCC001', self.row(run, 3).detail)

    def test_the_same_person_with_an_id_only_or_an_email_with_a_known_id_under_another_address(self):
        run = self.run_with([['Hana', '', '25SCC050'], ['Hana', 'hana.new@example.org', '25SCC050']])
        self.assertEqual(self.outcomes(run), {2: 'EMAIL_MISMATCH', 3: 'DUPLICATE'})
        self.assertIn('no email', self.row(run, 2).detail)

    def test_a_reservation_for_the_same_email_is_not_a_conflict_and_is_mentioned(self):
        registry.reserve(self.admin, email='new@example.org', club_id='25SCC100', reason='x')
        run = self.run_with([['New', 'new@example.org', '25SCC100']])
        row = self.row(run, 2)
        self.assertEqual(row.outcome, 'NO_ACCOUNT')
        self.assertIn('already reserved', row.detail)

    def test_the_summary_counts_outcomes_and_what_is_still_open(self):
        run = self.run_with([['Sita Rao', 'sita@example.org', '25SCC001'], ['Meena', 'meena@example.org', '25SCC080'], ['New', 'new@example.org', '25SCC100'], ['Bad', 'x', '']])
        data = api(self.lead).get(f'{BASE}/runs/{run}/').data
        self.assertEqual((data['total'], data['open']), (4, 2))
        self.assertEqual([data['by_outcome'][key] for key in ('MATCH', 'ID_MISSING_IN_APP', 'NO_ACCOUNT', 'INVALID')], [1, 1, 1, 1])
        self.assertEqual(api(self.lead).get(f'{BASE}/runs/').data['results'][0]['id'], run)


class Listing(Case):
    def setUp(self):
        super().setUp()
        self.run = self.run_with([['Sita Rao', 'sita@example.org', '25SCC001'], ['Meena', 'meena@example.org', '25SCC080'], ['New', 'new@example.org', '25SCC100']])

    def rows(self, **params):
        return [row['row_number'] for row in api(self.lead).get(f'{BASE}/runs/{self.run}/rows/', params).data['results']]

    def test_filters_by_group_outcome_search_and_open(self):
        self.assertEqual(self.rows(group='matched'), [2])
        self.assertEqual(self.rows(group='link'), [3])
        self.assertEqual(self.rows(group='no_account'), [4])
        self.assertEqual(self.rows(outcome='MATCH,NO_ACCOUNT'), [2, 4])
        self.assertEqual(self.rows(q='meena'), [3])
        self.assertEqual(self.rows(open='1'), [3, 4])

    def test_a_row_carries_what_the_page_needs(self):
        row = api(self.lead).get(f'{BASE}/runs/{self.run}/rows/', {'group': 'link'}).data['results'][0]
        self.assertEqual((row['outcome'], row['matched_user']['email'], row['actions']), ('ID_MISSING_IN_APP', 'meena@example.org', ['link', 'follow_up', 'dismiss']))
        invite = api(self.lead).get(f'{BASE}/runs/{self.run}/rows/', {'group': 'no_account'}).data['results'][0]
        self.assertEqual(invite['actions'], ['invite', 'follow_up', 'dismiss'])

    def test_members_who_are_not_in_the_sheet_are_listed(self):
        reply = api(self.lead).get(f'{BASE}/runs/{self.run}/missing/')
        self.assertEqual(sorted(row['email'] for row in reply.data['results']), ['holder@example.org', 'omar@example.org'])


class Linking(Case):
    def test_linking_gives_the_account_the_id_records_why_and_the_row_becomes_a_match(self):
        run = self.run_with([['Meena Kumari', 'meena@example.org', '25SCC080']])
        reply = self.act(run, 2, 'link')
        self.assertEqual(reply.status_code, 200, reply.data)
        self.meena.refresh_from_db()
        self.assertEqual(self.meena.club_id, '25SCC080')
        self.assertEqual((reply.data['outcome'], reply.data['resolution']), ('MATCH', 'LINKED'))
        event = ClubIdEvent.objects.get(club_id='25SCC080')
        self.assertEqual((event.kind, event.actor_email), ('ASSIGNED', 'boss@example.org'))
        self.assertIn('past.csv, row 2', event.reason)

    def test_an_id_taken_since_the_sheet_was_read_is_refused_and_the_row_updates(self):
        run = self.run_with([['Meena', 'meena@example.org', '25SCC080']])
        person('quick@example.org', club_id='25SCC080')
        reply = self.act(run, 2, 'link')
        self.assertEqual((reply.status_code, reply.data['code']), (409, 'NOT_APPLICABLE'))
        self.meena.refresh_from_db()
        self.assertIsNone(self.meena.club_id)
        self.assertEqual(self.row(run, 2).outcome, 'ID_HELD_BY_OTHER')

    def test_linking_twice_is_refused(self):
        run = self.run_with([['Meena', 'meena@example.org', '25SCC080']])
        self.act(run, 2, 'link')
        self.assertEqual(self.act(run, 2, 'link').data['code'], 'NOT_APPLICABLE')

    def test_a_retired_id_needs_the_explicit_choice(self):
        ClubIdEvent.objects.create(club_id='25SCC060', kind='CLEARED')
        run = self.run_with([['Meena', 'meena@example.org', '25SCC060']])
        self.assertEqual(self.act(run, 2, 'link').data['code'], 'NOT_APPLICABLE')
        self.assertEqual(self.act(run, 2, 'link', allow_retired=True).status_code, 200)
        self.meena.refresh_from_db()
        self.assertEqual(self.meena.club_id, '25SCC060')

    def test_a_row_of_another_run_or_a_made_up_one_is_not_found(self):
        run = self.run_with([['Meena', 'meena@example.org', '25SCC080']])
        other = self.run_with([['Meena', 'meena@example.org', '25SCC080']])
        foreign = self.row(other, 2).pk
        post = lambda row_id: api(self.admin).post(f'{BASE}/runs/{run}/rows/{row_id}/link/', {}, format='json')  # noqa: E731
        self.assertEqual(post(foreign).status_code, 404)
        self.assertEqual(post(99999).status_code, 404)
        self.assertEqual(self.act(run, 2, 'explode').status_code, 404)


class Mismatches(Case):
    def test_using_the_sheet_value_changes_the_id_frees_the_old_one_and_needs_a_reason(self):
        run = self.run_with([['Omar Zaid', 'omar@example.org', '25SCC090']])
        self.assertEqual(self.row(run, 2).outcome, 'ID_MISMATCH')
        self.assertEqual(self.act(run, 2, 'use-sheet', note='').data['code'], 'REASON_REQUIRED')
        reply = self.act(run, 2, 'use-sheet', note='The sheet is from the original register')
        self.assertEqual(reply.status_code, 200, reply.data)
        self.omar.refresh_from_db()
        self.assertEqual(self.omar.club_id, '25SCC090')
        self.assertIsNone(User.objects.filter(club_id='25SCC003').first())
        self.assertFalse(registry.is_retired('25SCC003'))
        self.assertEqual((reply.data['resolution'], reply.data['outcome']), ('USED_SHEET', 'MATCH'))

    def test_using_a_sheet_id_somebody_else_holds_is_refused(self):
        run = self.run_with([['Omar', 'omar@example.org', '25SCC050']])
        self.assertEqual(self.row(run, 2).outcome, 'ID_MISMATCH')
        reply = self.act(run, 2, 'use-sheet', note='try')
        self.assertEqual((reply.status_code, reply.data['code']), (409, 'CLUB_ID_UNAVAILABLE'))
        self.omar.refresh_from_db()
        self.assertEqual(self.omar.club_id, '25SCC003')

    def test_keeping_the_app_value_following_up_and_dismissing_change_no_account(self):
        run = self.run_with([['Omar', 'omar@example.org', '25SCC090'], ['Hana', '', '25SCC050'], ['New', 'new@example.org', '25SCC100']])
        self.assertEqual(self.act(run, 2, 'decide', choice='KEEP_APP', note='App is right').data['resolution'], 'KEPT_APP')
        self.assertEqual(self.act(run, 3, 'decide', choice='FOLLOW_UP').data['resolution'], 'FOLLOW_UP')
        self.assertEqual(self.act(run, 4, 'decide', choice='DISMISS').data['resolution'], 'DISMISSED')
        self.assertEqual(self.act(run, 4, 'decide', choice='EXPLODE').data['code'], 'INVALID')
        self.omar.refresh_from_db()
        self.assertEqual(self.omar.club_id, '25SCC003')
        self.assertEqual(api(self.lead).get(f'{BASE}/runs/{run}/').data['open'], 0)
        self.assertEqual(self.row(run, 2).note, 'App is right')


class Inviting(Case):
    def setUp(self):
        super().setUp()
        patcher = mock.patch('apps.core.tasks.run_in_background', side_effect=lambda fn: fn())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_inviting_reserves_the_id_for_the_email_and_sends_one_email(self):
        run = self.run_with([['New Person', 'new@example.org', '25SCC100']])
        with self.captureOnCommitCallbacks(execute=True):
            reply = self.act(run, 2, 'invite')
        self.assertEqual(reply.status_code, 200, reply.data)
        self.assertEqual(reply.data['resolution'], 'INVITED')
        reservation = ClubIdReservation.objects.get(club_id='25SCC100')
        self.assertEqual((reservation.reserved_for_email, reservation.source, reservation.status), ('new@example.org', 'RECONCILIATION', 'RESERVED'))
        sent = [m for m in mail.outbox if 'new@example.org' in m.to]
        self.assertEqual(len(sent), 1)
        self.assertIn('25SCC100', sent[0].body)

    def test_inviting_twice_or_a_row_that_is_not_an_invitation_is_refused(self):
        run = self.run_with([['New', 'new@example.org', '25SCC100'], ['Meena', 'meena@example.org', '25SCC080']])
        self.act(run, 2, 'invite')
        self.assertEqual(self.act(run, 2, 'invite').data['code'], 'ALREADY_INVITED')
        self.assertEqual(self.act(run, 3, 'invite').data['code'], 'NOT_APPLICABLE')

    def test_the_person_gets_the_reserved_id_when_they_register_with_that_email(self):
        run = self.run_with([['New Person', 'new@example.org', '25SCC100']])
        self.act(run, 2, 'invite')
        reply = api().post('/api/auth/register/', {
            'username': 'newperson', 'email': 'new@example.org', 'password': 'StrongPass123!', 'first_name': 'New', 'last_name': 'Person',
            'roll_number': '22B91A0555', 'branch': 'CSE', 'year': 2,
        }, format='json')
        self.assertEqual(reply.status_code, 201, reply.data)
        self.assertEqual(User.objects.get(email='new@example.org').club_id, '25SCC100')
        shown = api(self.admin).get(f'{BASE}/runs/{run}/rows/').data['results'][0]
        self.assertEqual(shown['outcome'], 'MATCH')           # the list shows what the accounts say now

    def test_an_id_reserved_for_another_email_cannot_be_invited_for(self):
        registry.reserve(self.admin, email='other@example.org', club_id='25SCC100', reason='x')
        run = self.run_with([['New', 'new@example.org', '25SCC100']])
        self.assertEqual(self.row(run, 2).outcome, 'ID_RESERVED')
        self.assertEqual(self.act(run, 2, 'invite').data['code'], 'NOT_APPLICABLE')


class Rechecking(Case):
    def test_a_recheck_picks_up_changes_made_since_the_sheet_was_read(self):
        run = self.run_with([['Meena', 'meena@example.org', '25SCC080'], ['New', 'new@example.org', '25SCC100']])
        registry.assign(self.admin, self.meena.pk, '25SCC080', reason='done by hand')
        person('new@example.org', club_id='25SCC100')
        reply = api(self.lead).post(f'{BASE}/runs/{run}/recheck/')
        self.assertEqual((reply.status_code, reply.data['changed']), (200, 2))
        self.assertEqual(self.outcomes(run), {2: 'MATCH', 3: 'MATCH'})
        self.assertEqual(reply.data['summary']['open'], 0)
        self.assertEqual(api(self.meena).post(f'{BASE}/runs/{run}/recheck/').status_code, 403)


class Bulk(Case):
    def setUp(self):
        super().setUp()
        patcher = mock.patch('apps.core.tasks.run_in_background', side_effect=lambda fn: fn())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.second = person('second@example.org', first_name='Second')
        self.run = self.run_with([['Meena', 'meena@example.org', '25SCC080'], ['Second', 'second@example.org', '25SCC081'], ['New', 'new@example.org', '25SCC100'], ['Newer', 'newer@example.org', '25SCC101']])

    def bulk(self, **body):
        return api(self.admin).post(f'{BASE}/runs/{self.run}/bulk/', body, format='json')

    def test_links_every_open_account_without_an_id_after_the_count_is_confirmed(self):
        reply = self.bulk(action='link', outcome='ID_MISSING_IN_APP', expected=2)
        self.assertEqual((reply.status_code, reply.data), (200, {'done': 2, 'failed': []}))
        self.assertEqual(sorted(User.objects.filter(club_id__in=['25SCC080', '25SCC081']).values_list('email', flat=True)), ['meena@example.org', 'second@example.org'])

    def test_a_changed_list_is_not_acted_on(self):
        self.act(self.run, 2, 'link')
        reply = self.bulk(action='link', outcome='ID_MISSING_IN_APP', expected=2)
        self.assertEqual((reply.status_code, reply.data['code'], reply.data['open']), (409, 'STALE', 1))

    def test_one_failure_does_not_stop_the_rest_and_is_reported_with_its_row(self):
        person('quick@example.org', club_id='25SCC081')
        reply = self.bulk(action='link', outcome='ID_MISSING_IN_APP', expected=2)
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.data['done'], 1)
        self.assertEqual([item['row_number'] for item in reply.data['failed']], [3])

    def test_invites_everyone_with_no_account_and_rejects_nonsense(self):
        with self.captureOnCommitCallbacks(execute=True):
            reply = self.bulk(action='invite', outcome='NO_ACCOUNT', expected=2)
        self.assertEqual(reply.data['done'], 2)
        self.assertEqual(ClubIdReservation.objects.filter(source='RECONCILIATION').count(), 2)
        self.assertEqual(self.bulk(action='delete', outcome='MATCH', expected=1).data['code'], 'INVALID')
        self.assertEqual(self.bulk(action='link', outcome='ID_MISSING_IN_APP', expected='many').data['code'], 'INVALID')


class Exporting(Case):
    def setUp(self):
        super().setUp()
        self.run = self.run_with([['Sita Rao', 'sita@example.org', '25SCC001'], ['Meena', 'meena@example.org', '25SCC080'], ['=cmd()', 'new@example.org', '25SCC100']])

    def table(self, **params):
        reply = api(self.lead).get(f'{BASE}/runs/{self.run}/export/', params)
        self.assertEqual(reply.status_code, 200)
        return list(csv.reader(io.StringIO(reply.content.decode('utf-8-sig')))), reply

    def test_the_report_has_every_row_with_its_result_and_decision(self):
        rows, reply = self.table()
        self.assertEqual(rows[0][:5], ['Sheet Row', 'Name In Sheet', 'Email In Sheet', 'Club ID In Sheet', 'Result'])
        self.assertEqual([row[4] for row in rows[1:]], ['Matches', 'Account has no Club ID', 'No account'])
        self.assertIn('attachment', reply['Content-Disposition'])

    def test_a_spreadsheet_formula_in_the_sheet_is_not_executable_in_the_report(self):
        rows, _ = self.table()
        self.assertFalse(rows[3][1].startswith('='))

    def test_the_report_can_be_narrowed_and_the_missing_members_exported(self):
        rows, _ = self.table(group='link')
        self.assertEqual(len(rows), 2)
        missing, _ = self.table(kind='missing')
        self.assertEqual(sorted(row[2] for row in missing[1:]), ['holder@example.org', 'omar@example.org'])

    def test_excel_and_json_work_and_a_bad_format_is_refused(self):
        self.assertEqual(api(self.lead).get(f'{BASE}/runs/{self.run}/export/', {'export_format': 'xlsx'}).status_code, 200)
        self.assertEqual(api(self.lead).get(f'{BASE}/runs/{self.run}/export/', {'export_format': 'json'}).status_code, 200)
        self.assertEqual(api(self.lead).get(f'{BASE}/runs/{self.run}/export/', {'export_format': 'pdf'}).status_code, 400)


class Permissions(Case):
    def test_a_club_lead_reads_and_exports_but_cannot_upload_or_act(self):
        run = self.run_with([['Meena', 'meena@example.org', '25SCC080']])
        self.assertEqual(api(self.lead).get(f'{BASE}/runs/{run}/rows/').status_code, 200)
        self.assertEqual(self.upload([['a', 'a@x.org', '']], user=self.lead).status_code, 403)
        self.assertEqual(api(self.lead).post(f'{BASE}/inspect/', {'file': csv_file([['a', 'a@x.org', '']])}, format='multipart').status_code, 403)
        self.assertEqual(self.act(run, 2, 'link', user=self.lead).status_code, 403)
        self.assertEqual(api(self.lead).post(f'{BASE}/runs/{run}/bulk/', {'action': 'link', 'outcome': 'ID_MISSING_IN_APP', 'expected': 1}, format='json').status_code, 403)
        self.assertEqual(api(self.lead).delete(f'{BASE}/runs/{run}/').status_code, 403)

    def test_participants_and_visitors_get_nothing(self):
        run = self.run_with([['Meena', 'meena@example.org', '25SCC080']])
        for path in (f'runs/', f'runs/{run}/', f'runs/{run}/rows/', f'runs/{run}/missing/', f'runs/{run}/export/'):
            self.assertEqual(api(self.meena).get(f'{BASE}/{path}').status_code, 403, path)
            self.assertEqual(api().get(f'{BASE}/{path}').status_code, 401, path)

    def test_an_admin_can_delete_a_run_and_everything_it_stored(self):
        run = self.run_with([['Meena', 'meena@example.org', '25SCC080']])
        self.assertEqual(api(self.admin).delete(f'{BASE}/runs/{run}/').status_code, 204)
        self.assertEqual(ReconciliationRow.objects.count(), 0)
        self.assertEqual(api(self.admin).get(f'{BASE}/runs/{run}/').status_code, 404)

    def test_upload_and_actions_are_audited(self):
        from apps.audit.models import AuditLog
        run = self.run_with([['Meena', 'meena@example.org', '25SCC080']])
        self.act(run, 2, 'link')
        self.assertTrue(AuditLog.objects.filter(action='Past-Member Sheet Compared').exists())
        self.assertTrue(AuditLog.objects.filter(action='Past-Member Row Link').exists())
