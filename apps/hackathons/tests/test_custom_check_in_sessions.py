"""Sessions an organizer adds by hand, the check-in grouped by team, and the exports built on it."""
import csv
import io
from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.audit.models import AuditLog
from apps.hackathons import check_in_sessions
from apps.hackathons.checkin import make_team_token, make_token
from apps.hackathons.models import CheckIn, CheckInSession, HackathonStaff
from .helpers import make_hackathon, make_team, make_user

HOUR = timedelta(hours=1)


def _api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


class Base(APITestCase):
    def setUp(self):
        self.hack = make_hackathon(min_team_size=1, features={'check_in': True})
        self.admin = make_user(role='ADMIN')
        self.volunteer = make_user()
        HackathonStaff.objects.create(hackathon=self.hack, user=self.volunteer, role='VOLUNTEER')
        self.asha, self.ben, self.cara, self.dev = (make_user(first_name=n, last_name='X') for n in ('Asha', 'Ben', 'Cara', 'Dev'))
        self.alpha = make_team(self.hack, self.asha, self.ben, name='Alpha')
        self.beta = make_team(self.hack, self.cara, self.dev, name='Beta')
        self.base = f'/api/hackathons/{self.hack.slug}'
        self.sessions_url = f'{self.base}/check-in/sessions/'
        self.main = self.hack.main_check_in_session()
        self.day = timezone.localtime(self.hack.start_date).date().isoformat()

    def add(self, user=None, **body):
        body.setdefault('name', 'Lunch')
        body.setdefault('date', self.day)
        return _api(user or self.admin).post(self.sessions_url, body, format='json')

    def scan(self, user, session=None):
        body = {'user_id': user.id, **({'session': session.id} if session else {})}
        return _api(self.admin).post(f'{self.base}/check-in/', body, format='json')


class CustomSessions(Base):
    def test_an_organizer_adds_a_session_with_a_title_time_and_who_checks_in(self):
        opens = self.hack.start_date + HOUR
        reply = self.add(name='  Prize   ceremony ', opens_at=opens.isoformat(), closes_at=(opens + HOUR).isoformat(), scan_mode='TEAMS', needs_arrival=False)
        self.assertEqual(reply.status_code, 201, reply.data)
        self.assertEqual((reply.data['name'], reply.data['scan_mode'], reply.data['is_custom'], reply.data['slot']), ('Prize ceremony', 'TEAMS', True, None))
        session = CheckInSession.objects.get(pk=reply.data['id'])
        self.assertEqual((session.needs_arrival, session.counts_for_certificate, session.day_index), (False, False, 0))
        self.assertTrue(AuditLog.objects.filter(action='Added Check-in Session', actor=self.admin).exists())

    def test_there_can_be_several_in_one_day_and_they_appear_after_the_generated_ones_in_time_order(self):
        self.hack.check_in_days, self.hack.check_in_slots_per_day = 1, 2
        self.hack.save()
        check_in_sessions.sync(self.hack)
        late = (self.hack.start_date + 5 * HOUR).isoformat()
        early = (self.hack.start_date + HOUR).isoformat()
        self.add(name='Dinner', opens_at=late)
        self.add(name='Lunch', opens_at=early)
        names = [s['name'] for s in _api(self.admin).get(self.sessions_url).data]
        self.assertEqual(names, ['Check-in', 'Day 1 Afternoon', 'Lunch', 'Dinner'])

    def test_the_title_and_day_are_required_and_must_be_sensible(self):
        for body, field in (({'name': '   '}, 'name'), ({'name': 'x' * 81}, 'name'), ({'date': '2001-01-01'}, 'date'), ({'date': 'soon'}, 'date'),
                            ({'scan_mode': 'EVERYONE'}, 'scan_mode'), ({'needs_arrival': 'yes'}, 'needs_arrival')):
            reply = self.add(**body)
            self.assertEqual((reply.status_code, reply.data['field']), (400, field), body)
        opens = self.hack.start_date + HOUR
        reply = self.add(opens_at=opens.isoformat(), closes_at=(opens - HOUR).isoformat())
        self.assertEqual(reply.data['field'], 'closes_at')

    def test_a_title_cannot_repeat_on_the_same_day(self):
        self.assertEqual(self.add(name='Lunch').status_code, 201)
        self.assertEqual(self.add(name='lunch').data['code'], 'DUPLICATE')

    def test_only_organizers_with_the_teams_permission_may_add_or_remove(self):
        self.assertEqual(self.add(self.volunteer).status_code, 403)
        self.assertEqual(self.add(make_user()).status_code, 403)
        self.assertEqual(_api().post(self.sessions_url, {}, format='json').status_code, 401)
        created = self.add().data['id']
        self.assertEqual(_api(self.volunteer).delete(f'{self.sessions_url}{created}/').status_code, 403)

    def test_it_needs_check_in_to_be_switched_on(self):
        self.hack.features = {}
        self.hack.save()
        self.assertEqual(self.add().data['code'], 'FEATURE_OFF')

    def test_saving_the_schedule_never_removes_or_changes_a_custom_session(self):
        created = self.add(name='Lunch').data['id']
        self.hack.check_in_days, self.hack.check_in_slots_per_day = 3, 3
        self.hack.save()
        check_in_sessions.sync(self.hack)
        self.hack.check_in_days, self.hack.check_in_slots_per_day = 1, 1
        self.hack.save()
        check_in_sessions.sync(self.hack)
        session = CheckInSession.objects.get(pk=created)
        self.assertEqual((session.is_custom, session.outside_schedule), (True, False))

    def test_people_can_be_scanned_into_it_with_either_code(self):
        session = CheckInSession.objects.get(pk=self.add(name='Lunch', needs_arrival=False).data['id'])
        client = _api(self.volunteer)
        member = client.post(f'{self.base}/check-in/', {'token': make_token(self.hack, self.asha), 'session': session.id}, format='json')
        self.assertEqual(member.status_code, 200, member.data)
        team = client.post(f'{self.base}/check-in/team-batch/', {'token': make_team_token(self.hack, self.beta), 'user_ids': [self.cara.id, self.dev.id], 'session': session.id}, format='json')
        self.assertEqual(team.data['created'], 2)
        self.assertEqual(session.check_ins.count(), 3)

    def test_the_arrival_rule_applies_to_it_when_asked_for(self):
        session = self.add(name='Lunch', needs_arrival=True).data['id']
        self.assertEqual(self.scan(self.asha, CheckInSession.objects.get(pk=session)).data['code'], 'NOT_ARRIVED')

    def test_it_can_be_renamed_and_moved_but_not_onto_an_existing_title(self):
        a = self.add(name='Lunch').data['id']
        self.add(name='Dinner')
        url = f'{self.sessions_url}{a}/'
        self.assertEqual(_api(self.admin).patch(url, {'name': 'Brunch'}, format='json').data['name'], 'Brunch')
        self.assertEqual(_api(self.admin).patch(url, {'name': 'dinner'}, format='json').status_code, 400)

    def test_deleting_one_with_nobody_in_removes_it_but_one_with_check_ins_is_retired(self):
        empty = self.add(name='Lunch').data['id']
        busy = self.add(name='Dinner', needs_arrival=False).data['id']
        self.scan(self.asha, CheckInSession.objects.get(pk=busy))
        self.assertEqual(_api(self.admin).delete(f'{self.sessions_url}{empty}/').data, {'outcome': 'deleted'})
        self.assertFalse(CheckInSession.objects.filter(pk=empty).exists())
        self.assertEqual(_api(self.admin).delete(f'{self.sessions_url}{busy}/').data, {'outcome': 'retired'})
        retired = CheckInSession.objects.get(pk=busy)
        self.assertEqual((retired.outside_schedule, retired.check_ins.count()), (True, 1))

    def test_a_retired_session_can_be_brought_back_but_not_retired_by_hand(self):
        busy = self.add(name='Dinner', needs_arrival=False).data['id']
        self.scan(self.asha, CheckInSession.objects.get(pk=busy))
        _api(self.admin).delete(f'{self.sessions_url}{busy}/')
        url = f'{self.sessions_url}{busy}/'
        self.assertEqual(_api(self.admin).patch(url, {'outside_schedule': True}, format='json').status_code, 400)
        reply = _api(self.admin).patch(url, {'outside_schedule': False}, format='json')
        self.assertEqual((reply.status_code, reply.data['outside_schedule']), (200, False))
        self.assertTrue(CheckInSession.objects.get(pk=busy).is_scannable)

    def test_a_session_you_added_can_move_to_another_day_of_the_event(self):
        self.hack.end_date = self.hack.start_date + timedelta(days=2)
        self.hack.save()
        created = self.add(name='Workshop').data['id']
        day_two = (timezone.localtime(self.hack.start_date).date() + timedelta(days=1)).isoformat()
        reply = _api(self.admin).patch(f'{self.sessions_url}{created}/', {'date': day_two}, format='json')
        self.assertEqual((reply.status_code, str(reply.data['date']), reply.data['day_index']), (200, day_two, 1))
        self.assertEqual(_api(self.admin).patch(f'{self.sessions_url}{created}/', {'date': '2001-01-01'}, format='json').status_code, 400)

    def test_generated_sessions_and_the_main_one_cannot_be_deleted(self):
        self.hack.check_in_days = 2
        self.hack.save()
        generated = check_in_sessions.sync(self.hack)[-1]
        for session in (self.main, generated):
            reply = _api(self.admin).delete(f'{self.sessions_url}{session.id}/')
            self.assertEqual((reply.status_code, reply.data['code']), (400, 'NOT_CUSTOM'))


class ByTeam(Base):
    def setUp(self):
        super().setUp()
        self.scan(self.asha)
        self.scan(self.ben)
        self.scan(self.cara)
        self.url = f'{self.base}/check-in/by-team/'

    def get(self, user=None, **params):
        return _api(user or self.admin).get(self.url, params)

    def test_teams_come_with_their_members_counts_and_state(self):
        data = self.get().data
        self.assertEqual(data['totals'], {'teams': 2, 'present': 1, 'partial': 1, 'absent': 0, 'people': 4, 'people_in': 3})
        alpha, beta = data['teams']
        self.assertEqual((alpha['name'], alpha['state'], alpha['present'], alpha['total']), ('Alpha', 'present', 2, 2))
        self.assertEqual((beta['name'], beta['state'], beta['present'], beta['total']), ('Beta', 'partial', 1, 2))
        dev = next(m for m in beta['members'] if m['name'] == 'Dev X')
        self.assertFalse(dev['checked_in'])
        self.assertEqual(alpha['leader_email'], self.asha.email)
        self.assertIsNotNone(alpha['first_at'])

    def test_the_state_and_search_filters_keep_the_totals_of_the_whole_event(self):
        self.assertEqual([t['name'] for t in self.get(state='partial').data['teams']], ['Beta'])
        self.assertEqual(self.get(state='partial').data['totals']['teams'], 2)
        self.assertEqual([t['name'] for t in self.get(q='cara').data['teams']], ['Beta'])
        self.assertEqual(len(self.get(q='cara').data['teams'][0]['members']), 2)
        self.assertEqual(self.get(state='absent').data['teams'], [])

    def test_a_team_with_nobody_in_is_absent(self):
        CheckIn.objects.filter(team=self.beta).delete()
        self.assertEqual([t['name'] for t in self.get(state='absent').data['teams']], ['Beta'])

    def test_it_follows_the_chosen_session(self):
        lunch = CheckInSession.objects.get(pk=self.add(name='Lunch', needs_arrival=False).data['id'])
        self.scan(self.dev, lunch)
        data = self.get(session=lunch.id).data
        self.assertEqual((data['session']['name'], data['totals']['people_in']), ('Lunch', 1))
        self.assertEqual(self.get(session=99999).status_code, 404)

    def test_waitlisted_and_forming_teams_are_left_out(self):
        make_team(self.hack, make_user(), name='Gamma', status='FORMING')
        self.assertEqual([t['name'] for t in self.get().data['teams']], ['Alpha', 'Beta'])

    def test_only_organizers_may_see_it(self):
        self.assertEqual(self.get(self.volunteer).status_code, 403)
        self.assertEqual(self.get(make_user()).status_code, 403)
        self.assertEqual(_api().get(self.url).status_code, 401)


class Exports(Base):
    def setUp(self):
        super().setUp()
        for person in (self.asha, self.ben, self.cara):
            self.scan(person)

    def rows(self, kind, user=None, **params):
        reply = _api(user or self.admin).get(f'{self.base}/export/{kind}/', {'export_format': 'csv', **params})
        self.assertEqual(reply.status_code, 200, getattr(reply, 'data', ''))
        return reply, list(csv.DictReader(io.StringIO(reply.content.decode('utf-8-sig'))))

    def test_one_row_per_team_with_how_many_are_in(self):
        _, rows = self.rows('check-in-teams')
        self.assertEqual([(r['Team'], r['Status'], r['Checked In'], r['Members'], r['Not Checked In']) for r in rows],
                         [('Alpha', 'Everyone in', '2', '2', '0'), ('Beta', 'Some in', '1', '2', '1')])
        self.assertEqual(rows[0]['Leader Email'], self.asha.email)

    def test_one_row_per_person_grouped_under_their_team(self):
        _, rows = self.rows('check-in-by-team')
        self.assertEqual([(r['Team'], r['Name'], r['Checked In']) for r in rows],
                         [('Alpha', 'Asha X', 'Yes'), ('Alpha', 'Ben X', 'Yes'), ('Beta', 'Cara X', 'Yes'), ('Beta', 'Dev X', 'No')])
        self.assertEqual(rows[0]['Team Checked In'], '2')

    def test_the_state_filter_applies_and_the_file_names_the_session(self):
        lunch = CheckInSession.objects.get(pk=self.add(name='Lunch', needs_arrival=False).data['id'])
        reply, rows = self.rows('check-in-teams', session=lunch.id)
        self.assertEqual({r['Status'] for r in rows}, {'Nobody in'})
        self.assertIn('lunch', reply['Content-Disposition'])
        _, only = self.rows('check-in-teams', state='partial')
        self.assertEqual([r['Team'] for r in only], ['Beta'])

    def test_the_matrix_has_a_column_for_every_session_and_a_count(self):
        lunch = CheckInSession.objects.get(pk=self.add(name='Lunch', needs_arrival=False).data['id'])
        self.scan(self.asha, lunch)
        _, rows = self.rows('check-in-matrix')
        asha = next(r for r in rows if r['Name'] == 'Asha X')
        self.assertEqual((asha['Check-in'], asha['Lunch'], asha['Sessions Attended']), ('Yes', 'Yes', '2'))
        dev = next(r for r in rows if r['Name'] == 'Dev X')
        self.assertEqual((dev['Check-in'], dev['Lunch'], dev['Sessions Attended']), ('No', 'No', '0'))

    def test_volunteers_may_download_them_but_outsiders_may_not(self):
        for kind in ('check-in-teams', 'check-in-by-team', 'check-in-matrix'):
            self.rows(kind, self.volunteer)
            reply = _api(make_user()).get(f'{self.base}/export/{kind}/')
            self.assertEqual(reply.status_code, 403, kind)
