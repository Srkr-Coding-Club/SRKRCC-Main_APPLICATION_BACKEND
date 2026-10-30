"""Check-in sessions (days and slots): generation, scanning into a session, the arrival rule, windows, undo, and what 'checked in' still means."""
from datetime import timedelta

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.audit.models import AuditLog
from apps.hackathons import check_in_sessions
from apps.hackathons.checkin import make_token
from apps.hackathons.models import (
    CheckIn, CheckInScanMode, CheckInSession, CheckInSlot, CheckInVia, HackathonStaff, TeamMember,
)
from .helpers import make_hackathon, make_team, make_user

HOUR = timedelta(hours=1)


def _api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


class SessionBase(APITestCase):
    def setUp(self):
        self.hack = make_hackathon(min_team_size=1, features={'check_in': True, 'certificates': True})
        self.admin = make_user(role='ADMIN')
        self.volunteer = make_user(first_name='Vik', last_name='Vol')
        HackathonStaff.objects.create(hackathon=self.hack, user=self.volunteer, role='VOLUNTEER')
        self.person = make_user(first_name='Asha', last_name='Rao')
        self.team = make_team(self.hack, self.person, name='Alpha')
        self.base = f'/api/hackathons/{self.hack.slug}'
        self.main = self.hack.main_check_in_session()

    def schedule(self, days=2, slots=2):
        self.hack.check_in_days, self.hack.check_in_slots_per_day = days, slots
        self.hack.save()
        check_in_sessions.sync(self.hack)
        return {(s.day_index, s.slot): s for s in self.hack.check_in_sessions.all()}

    def scan(self, who=None, **body):
        return _api(who or self.volunteer).post(f'{self.base}/check-in/', body, format='json')

    def arrive(self, user=None):
        return self.scan(user_id=(user or self.person).id)


class GenerationTests(SessionBase):
    def test_the_main_session_is_created_once_with_the_arrival_defaults(self):
        self.assertEqual(self.hack.check_in_sessions.count(), 1)
        again = self.hack.main_check_in_session()
        self.assertEqual(again.id, self.main.id)
        self.assertEqual((self.main.label, self.main.is_main, self.main.needs_arrival, self.main.counts_for_certificate),
                         ('Check-in', True, False, True))
        self.assertEqual(self.main.date, timezone.localtime(self.hack.start_date).date())

    def test_days_and_slots_generate_every_session_in_order_with_consecutive_dates(self):
        sessions = self.schedule(days=2, slots=3)
        self.assertEqual(len(sessions), 6)
        order = [(s.day_index, s.slot) for s in self.hack.check_in_sessions.all()]
        self.assertEqual(order, [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2)])
        self.assertEqual([s.label for s in self.hack.check_in_sessions.all()][:3], ['Check-in', 'Day 1 Afternoon', 'Day 1 Evening'])
        later = sessions[(1, CheckInSlot.AFTERNOON)]
        self.assertEqual(later.label, 'Day 2 Afternoon')
        self.assertEqual(later.date, self.main.date + timedelta(days=1))
        self.assertTrue(later.needs_arrival)
        self.assertFalse(later.counts_for_certificate)
        self.assertEqual(sessions[(0, CheckInSlot.MORNING)].id, self.main.id)

    def test_syncing_again_changes_nothing(self):
        self.schedule(days=2, slots=2)
        before = list(self.hack.check_in_sessions.values_list('id', flat=True))
        check_in_sessions.sync(self.hack)
        self.assertEqual(list(self.hack.check_in_sessions.values_list('id', flat=True)), before)

    def test_shrinking_deletes_empty_sessions_and_retires_those_with_check_ins(self):
        sessions = self.schedule(days=2, slots=2)
        busy = sessions[(1, CheckInSlot.AFTERNOON)]
        self.arrive()
        self.assertEqual(self.scan(user_id=self.person.id, session=busy.id).status_code, 200)
        self.schedule(days=1, slots=1)
        remaining = {(s.day_index, s.slot): s for s in self.hack.check_in_sessions.all()}
        self.assertEqual(set(remaining), {(0, CheckInSlot.MORNING), (1, CheckInSlot.AFTERNOON)})
        busy.refresh_from_db()
        self.assertTrue(busy.outside_schedule)
        self.assertFalse(busy.is_scannable)
        self.assertEqual(self.scan(user_id=self.person.id, session=busy.id).data['code'], 'SESSION_OFF')
        self.assertEqual(CheckIn.objects.filter(session=busy).count(), 1)

    def test_growing_back_clears_the_retired_mark(self):
        sessions = self.schedule(days=2, slots=1)
        later = sessions[(1, CheckInSlot.MORNING)]
        self.arrive()
        self.scan(user_id=self.person.id, session=later.id)
        self.schedule(days=1, slots=1)
        later.refresh_from_db()
        self.assertTrue(later.outside_schedule)
        self.schedule(days=2, slots=1)
        later.refresh_from_db()
        self.assertFalse(later.outside_schedule)

    def test_the_main_session_survives_any_shrink_even_when_moved(self):
        sessions = self.schedule(days=2, slots=2)
        moved = sessions[(1, CheckInSlot.AFTERNOON)]
        CheckInSession.objects.filter(pk=self.main.pk).update(is_main=False)
        CheckInSession.objects.filter(pk=moved.pk).update(is_main=True)
        self.schedule(days=1, slots=1)
        self.assertTrue(CheckInSession.objects.filter(pk=moved.pk, is_main=True).exists())

    def test_session_dates_follow_the_event_dates(self):
        self.schedule(days=2, slots=1)
        self.hack.start_date += timedelta(days=10)
        self.hack.end_date += timedelta(days=10)
        self.hack.save()
        check_in_sessions.sync(self.hack)
        for session in self.hack.check_in_sessions.all():
            self.assertEqual(session.date, timezone.localtime(self.hack.start_date).date() + timedelta(days=session.day_index))

    def test_garbage_schedules_are_clamped(self):
        self.assertEqual(check_in_sessions.clamp_schedule(0, 0), (1, 1))
        self.assertEqual(check_in_sessions.clamp_schedule(99, 99), (check_in_sessions.MAX_DAYS, 3))
        self.assertEqual(check_in_sessions.clamp_schedule('x', None), (1, 1))

    def test_an_organizer_sets_days_and_slots_and_the_sessions_follow(self):
        resp = _api(self.admin).patch(f'{self.base}/', {'check_in_days': 3, 'check_in_slots_per_day': 2}, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual((resp.data['check_in_days'], resp.data['check_in_slots_per_day']), (3, 2))
        self.assertEqual(self.hack.check_in_sessions.count(), 6)

    def test_out_of_range_days_and_slots_are_rejected(self):
        for body in ({'check_in_days': 0}, {'check_in_days': 15}, {'check_in_slots_per_day': 0}, {'check_in_slots_per_day': 4}):
            self.assertEqual(_api(self.admin).patch(f'{self.base}/', body, format='json').status_code, 400, body)

    def test_nothing_is_generated_while_check_in_is_switched_off(self):
        other = make_hackathon(min_team_size=1, features={})
        resp = _api(self.admin).patch(f'/api/hackathons/{other.slug}/', {'check_in_days': 3}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(other.check_in_sessions.count(), 0)

    def test_check_ins_made_without_naming_a_session_go_to_the_main_one(self):
        record = CheckIn.objects.create(hackathon=self.hack, user=self.person, team=self.team)
        self.assertEqual(record.session_id, self.main.id)

    def test_cloning_keeps_the_schedule_settings(self):
        self.hack.check_in_days, self.hack.check_in_slots_per_day = 3, 2
        self.hack.save()
        new_start = self.hack.start_date + timedelta(days=365)
        resp = _api(self.admin).post(f'{self.base}/clone/', {'title': 'Next Year', 'start_date': new_start.isoformat()}, format='json')
        self.assertEqual(resp.status_code, 201, resp.data)
        clone = type(self.hack).objects.get(slug=resp.data['slug'])
        self.assertEqual((clone.check_in_days, clone.check_in_slots_per_day), (3, 2))

    def test_deleting_a_hackathon_with_check_ins_still_works(self):
        sessions = self.schedule(days=2, slots=1)
        self.arrive()
        self.scan(user_id=self.person.id, session=sessions[(1, CheckInSlot.MORNING)].id)
        self.hack.delete()
        self.assertEqual(CheckIn.objects.count(), 0)
        self.assertEqual(CheckInSession.objects.count(), 0)


class ScanningTests(SessionBase):
    def setUp(self):
        super().setUp()
        self.sessions = self.schedule(days=2, slots=1)
        self.day2 = self.sessions[(1, CheckInSlot.MORNING)]

    def token(self):
        return _api(self.person).get(f'{self.base}/my-check-in/').data['token']

    def test_the_one_member_code_works_for_every_session_and_records_how(self):
        self.assertEqual(self.scan(token=self.token()).data['via'], CheckInVia.MEMBER_QR)
        later = self.scan(token=self.token(), session=self.day2.id)
        self.assertTrue(later.data['created'])
        self.assertEqual((later.data['session_id'], later.data['via']), (self.day2.id, CheckInVia.MEMBER_QR))
        self.assertEqual(CheckIn.objects.filter(user=self.person).count(), 2)

    def test_a_person_is_checked_in_once_per_session(self):
        self.arrive()
        self.assertFalse(self.arrive().data['created'])
        self.assertTrue(self.scan(user_id=self.person.id, session=self.day2.id).data['created'])
        self.assertFalse(self.scan(user_id=self.person.id, session=self.day2.id).data['created'])
        self.assertEqual(CheckIn.objects.count(), 2)

    def test_a_later_session_needs_arrival_first_for_codes_and_ticks_alike(self):
        self.assertEqual(self.scan(token=self.token(), session=self.day2.id).data['code'], 'NOT_ARRIVED')
        self.assertEqual(self.scan(user_id=self.person.id, session=self.day2.id).data['code'], 'NOT_ARRIVED')
        self.assertFalse(CheckIn.objects.exists())

    def test_an_organizer_can_waive_the_arrival_rule_with_a_reason_and_a_volunteer_cannot(self):
        body = {'user_id': self.person.id, 'session': self.day2.id, 'override': True}
        self.assertEqual(self.scan(who=self.volunteer, reason='VIP', **body).status_code, 403)
        self.assertEqual(self.scan(who=self.admin, **body).data['code'], 'REASON_REQUIRED')
        done = self.scan(who=self.admin, reason='Arrived before the desk opened', **body)
        self.assertEqual((done.data['created'], done.data['via']), (True, CheckInVia.OVERRIDE))
        self.assertEqual(CheckIn.objects.get().override_reason, 'Arrived before the desk opened')

    def test_a_session_that_does_not_need_arrival_can_be_scanned_alone(self):
        CheckInSession.objects.filter(pk=self.day2.pk).update(needs_arrival=False)
        self.assertTrue(self.scan(user_id=self.person.id, session=self.day2.id).data['created'])

    def test_each_session_has_its_own_window_and_the_main_one_still_uses_the_hackathon_times(self):
        CheckInSession.objects.filter(pk=self.day2.pk).update(opens_at=timezone.now() + HOUR)
        self.arrive()
        token = self.token()
        self.assertEqual(self.scan(token=token, session=self.day2.id).data['code'], 'CHECK_IN_NOT_OPEN')
        self.assertTrue(self.scan(user_id=self.person.id, session=self.day2.id).data['created'])  # a manual tick ignores the window
        self.hack.check_in_closes_at = timezone.now() - HOUR
        self.hack.save()
        self.assertEqual(check_in_sessions.window_state(self.hack.main_check_in_session()), 'CLOSED')
        self.assertEqual(check_in_sessions.window_state(CheckInSession.objects.get(pk=self.day2.pk)), 'NOT_OPEN')

    def test_a_replayed_offline_scan_is_judged_against_that_sessions_window_at_scan_time(self):
        self.arrive()
        at = timezone.now() - 2 * HOUR
        CheckInSession.objects.filter(pk=self.day2.pk).update(opens_at=at - HOUR, closes_at=at + HOUR - timedelta(minutes=30))
        CheckInSession.objects.filter(pk=self.day2.pk).update(closes_at=timezone.now() - timedelta(minutes=10))
        replay = self.scan(token=make_token(self.hack, self.person, at=at), scanned_at=at.isoformat(), session=self.day2.id)
        self.assertTrue(replay.data['created'])

    def test_a_switched_off_or_retired_session_cannot_be_scanned_at_all(self):
        self.arrive()
        CheckInSession.objects.filter(pk=self.day2.pk).update(is_active=False)
        self.assertEqual(self.scan(user_id=self.person.id, session=self.day2.id).data['code'], 'SESSION_OFF')
        self.assertEqual(self.scan(token=self.token(), session=self.day2.id).data['code'], 'SESSION_OFF')

    def test_a_teams_only_session_refuses_a_member_code_but_accepts_a_ticked_name(self):
        CheckInSession.objects.filter(pk=self.day2.pk).update(scan_mode=CheckInScanMode.TEAMS)
        self.arrive()
        self.assertEqual(self.scan(token=self.token(), session=self.day2.id).data['code'], 'TEAM_SCAN_ONLY')
        self.assertTrue(self.scan(user_id=self.person.id, session=self.day2.id).data['created'])
        for mode in (CheckInScanMode.MEMBERS, CheckInScanMode.BOTH):
            CheckIn.objects.filter(session=self.day2).delete()
            CheckInSession.objects.filter(pk=self.day2.pk).update(scan_mode=mode)
            self.assertTrue(self.scan(token=self.token(), session=self.day2.id).data['created'], mode)

    def test_unknown_foreign_and_garbage_sessions_are_refused_and_blank_means_the_main_session(self):
        other = make_hackathon(min_team_size=1, features={'check_in': True}).main_check_in_session()
        for bad in (999999, other.id, 'abc', -1):
            refused = self.scan(user_id=self.person.id, session=bad)
            self.assertEqual((refused.status_code, refused.data['code']), (404, 'SESSION_NOT_FOUND'), bad)
        self.assertEqual(self.scan(user_id=self.person.id, session='').data['session_id'], self.main.id)
        self.assertEqual(self.scan(user_id=self.person.id).data['session_id'], self.main.id)


class UndoTests(SessionBase):
    def setUp(self):
        super().setUp()
        self.sessions = self.schedule(days=1, slots=3)
        self.lunch = self.sessions[(0, CheckInSlot.AFTERNOON)]
        self.dinner = self.sessions[(0, CheckInSlot.EVENING)]
        self.arrive()
        self.scan(user_id=self.person.id, session=self.lunch.id)
        self.scan(user_id=self.person.id, session=self.dinner.id)

    def undo(self, session=None, **params):
        query = {**({'session': session.id} if session else {}), **params}
        suffix = ('?' + '&'.join(f'{key}={value}' for key, value in query.items())) if query else ''
        return _api(self.volunteer).delete(f'{self.base}/check-in/{self.person.id}/{suffix}')

    def test_undoing_a_later_session_leaves_the_others(self):
        self.assertEqual(self.undo(self.lunch).status_code, 200)
        self.assertEqual(sorted(CheckIn.objects.values_list('session_id', flat=True)), sorted([self.main.id, self.dinner.id]))

    def test_undoing_arrival_is_refused_while_later_sessions_exist_and_says_how_many(self):
        refused = self.undo()
        self.assertEqual((refused.status_code, refused.data['code'], refused.data['later']), (400, 'HAS_LATER_CHECKINS', 2))
        self.assertEqual(CheckIn.objects.count(), 3)

    def test_removing_everything_together_needs_the_cascade_flag(self):
        self.assertEqual(self.undo(cascade='1').status_code, 200)
        self.assertEqual(CheckIn.objects.count(), 0)

    def test_undoing_arrival_is_fine_when_there_are_no_later_sessions(self):
        CheckIn.objects.exclude(session=self.main).delete()
        self.assertEqual(self.undo().status_code, 200)
        self.assertEqual(CheckIn.objects.count(), 0)

    def test_undoing_someone_who_was_never_checked_in_is_harmless(self):
        CheckIn.objects.all().delete()
        self.assertEqual(self.undo(self.lunch).status_code, 200)


class RosterAndSessionListTests(SessionBase):
    def setUp(self):
        super().setUp()
        self.sessions = self.schedule(days=2, slots=1)
        self.day2 = self.sessions[(1, CheckInSlot.MORNING)]
        self.other = make_user(first_name='Bo')
        make_team(self.hack, self.other, name='Beta')
        self.arrive()
        self.arrive(self.other)
        self.scan(user_id=self.person.id, session=self.day2.id)

    def roster(self, **params):
        return _api(self.volunteer).get(f'{self.base}/check-in/', params).data

    def test_the_roster_counts_who_is_checked_in_to_the_chosen_session(self):
        main, day2 = self.roster(), self.roster(session=self.day2.id)
        self.assertEqual((main['total'], main['checked_in'], day2['checked_in']), (2, 2, 1))
        self.assertEqual((main['session']['id'], day2['session']['id']), (self.main.id, self.day2.id))
        self.assertEqual([p['name'] for p in self.roster(session=self.day2.id, missing=1)['participants']], ['Bo ' + self.other.last_name])

    def test_stats_are_per_session_and_default_to_arrival(self):
        stats = lambda **p: _api(self.admin).get(f'{self.base}/check-in/stats/', p).data
        self.assertEqual((stats()['total'], stats(session=self.day2.id)['total']), (2, 1))
        self.assertEqual(stats(session=self.day2.id)['session']['name'], 'Day 2 Morning')

    def test_desk_staff_see_every_session_with_its_count_and_participants_do_not(self):
        rows = _api(self.volunteer).get(f'{self.base}/check-in/sessions/').data
        self.assertEqual([(r['name'], r['is_main'], r['checked_in']) for r in rows], [('Check-in', True, 2), ('Day 2 Morning', False, 1)])
        for field in ('opens_at', 'closes_at', 'window', 'scan_mode', 'needs_arrival', 'is_active', 'outside_schedule'):
            self.assertIn(field, rows[0])
        self.assertEqual(_api(self.person).get(f'{self.base}/check-in/sessions/').status_code, 403)
        self.hack.features = {}
        self.hack.save()
        self.assertEqual(_api(self.volunteer).get(f'{self.base}/check-in/sessions/').data['code'], 'FEATURE_OFF')

    def test_a_participant_sees_their_own_standing_in_each_session(self):
        mine = _api(self.person).get(f'{self.base}/my-check-in/').data['sessions']
        self.assertEqual([(s['name'], s['checked_in']) for s in mine], [('Check-in', True), ('Day 2 Morning', True)])
        theirs = _api(self.other).get(f'{self.base}/my-check-in/').data['sessions']
        self.assertEqual([(s['name'], s['checked_in']) for s in theirs], [('Check-in', True), ('Day 2 Morning', False)])

    def test_a_switched_off_session_is_hidden_from_people_who_were_not_in_it(self):
        CheckInSession.objects.filter(pk=self.day2.pk).update(is_active=False)
        self.assertEqual([s['name'] for s in _api(self.other).get(f'{self.base}/my-check-in/').data['sessions']], ['Check-in'])
        self.assertEqual([s['name'] for s in _api(self.person).get(f'{self.base}/my-check-in/').data['sessions']], ['Check-in', 'Day 2 Morning'])


class SessionEditTests(SessionBase):
    def setUp(self):
        super().setUp()
        self.sessions = self.schedule(days=1, slots=2)
        self.lunch = self.sessions[(0, CheckInSlot.AFTERNOON)]

    def patch(self, session, who=None, **body):
        return _api(who or self.admin).patch(f'{self.base}/check-in/sessions/{session.id}/', body, format='json')

    def test_an_organizer_renames_a_session_and_sets_its_window_and_switches(self):
        opens, closes = timezone.now() + HOUR, timezone.now() + 3 * HOUR
        done = self.patch(self.lunch, name='  Lunch   desk ', opens_at=opens.isoformat(), closes_at=closes.isoformat(),
                          needs_arrival=False, counts_for_certificate=True, scan_mode='TEAMS', is_active=False)
        self.assertEqual(done.status_code, 200, done.data)
        self.lunch.refresh_from_db()
        self.assertEqual((self.lunch.name, self.lunch.needs_arrival, self.lunch.counts_for_certificate, self.lunch.scan_mode, self.lunch.is_active),
                         ('Lunch desk', False, True, 'TEAMS', False))
        self.assertEqual((self.lunch.opens_at, self.lunch.closes_at), (opens, closes))
        self.assertEqual((done.data['name'], done.data['window']), ('Lunch desk', 'NOT_OPEN'))

    def test_a_blank_name_goes_back_to_the_generated_label_and_times_can_be_cleared(self):
        self.patch(self.lunch, name='Lunch desk', opens_at=(timezone.now() + HOUR).isoformat())
        done = self.patch(self.lunch, name='', opens_at=None, closes_at='')
        self.lunch.refresh_from_db()
        self.assertEqual((done.data['name'], self.lunch.opens_at, self.lunch.closes_at), ('Day 1 Afternoon', None, None))

    def test_the_main_sessions_times_are_the_hackathons_and_it_cannot_be_switched_off(self):
        opens, closes = timezone.now() - HOUR, timezone.now() + HOUR
        done = self.patch(self.main, opens_at=opens.isoformat(), closes_at=closes.isoformat(), needs_arrival=True, counts_for_certificate=False)
        self.assertEqual(done.status_code, 200, done.data)
        self.hack.refresh_from_db()
        self.main.refresh_from_db()
        self.assertEqual((self.hack.check_in_opens_at, self.hack.check_in_closes_at), (opens, closes))
        self.assertEqual((self.main.needs_arrival, self.main.counts_for_certificate), (False, True))
        self.assertEqual(self.patch(self.main, is_active=False).data['code'], 'MAIN_SESSION_ALWAYS_ON')
        self.assertEqual(self.patch(self.main, name='Arrival desk').data['name'], 'Arrival desk')
        self.hack.refresh_from_db()
        self.assertEqual(self.hack.check_in_opens_at, opens)

    def test_bad_input_is_named_per_field(self):
        cases = [
            ({'name': 'x' * 81}, 'name'), ({'needs_arrival': 'yes'}, 'needs_arrival'), ({'is_active': 1}, 'is_active'),
            ({'scan_mode': 'EVERYONE'}, 'scan_mode'), ({'opens_at': 'tomorrow'}, 'opens_at'), ({'opens_at': '2026-01-01T10:00:00'}, 'opens_at'),
        ]
        for body, field in cases:
            refused = self.patch(self.lunch, **body)
            self.assertEqual((refused.status_code, refused.data['field']), (400, field), body)
        now = timezone.now()
        self.assertEqual(self.patch(self.lunch, opens_at=now.isoformat(), closes_at=now.isoformat()).data['field'], 'closes_at')
        self.patch(self.lunch, opens_at=(now + 2 * HOUR).isoformat())
        self.assertEqual(self.patch(self.lunch, closes_at=(now + HOUR).isoformat()).data['field'], 'closes_at')

    def test_only_organizers_with_the_teams_permission_may_edit_and_other_hackathons_sessions_are_not_found(self):
        for who in (self.volunteer, self.person, make_user()):
            self.assertEqual(self.patch(self.lunch, who=who, name='x').status_code, 403)
        self.assertEqual(_api().patch(f'{self.base}/check-in/sessions/{self.lunch.id}/', {'name': 'x'}, format='json').status_code, 401)
        other = make_hackathon(min_team_size=1, features={'check_in': True}).main_check_in_session()
        self.assertEqual(self.patch(other, name='x').status_code, 404)

    def test_edits_are_audited_and_a_switched_off_session_stops_scanning_at_once(self):
        self.arrive()
        self.patch(self.lunch, is_active=False)
        self.assertEqual(self.scan(user_id=self.person.id, session=self.lunch.id).data['code'], 'SESSION_OFF')
        self.patch(self.lunch, is_active=True)
        self.assertTrue(self.scan(user_id=self.person.id, session=self.lunch.id).data['created'])
        self.assertEqual(AuditLog.objects.filter(action='Edited Check-in Session').count(), 2)


class MainSessionMeaningTests(SessionBase):
    """Certificates, round rules, analytics, no-shows and exports keep meaning "arrived at the main session"."""

    def setUp(self):
        super().setUp()
        self.sessions = self.schedule(days=1, slots=2)
        self.lunch = self.sessions[(0, CheckInSlot.AFTERNOON)]

    def only_lunch(self):
        """A check-in to a later session without arriving (as an old import or a waived rule could leave it)."""
        CheckIn.objects.create(hackathon=self.hack, session=self.lunch, user=self.person, team=self.team)

    def test_a_certificate_needs_the_arrival_check_in_not_just_lunch(self):
        from apps.hackathons.certificates import _earned

        self.hack.certificate_config = {'released': True, 'eligibility': 'CHECKED_IN'}
        self.hack.save()
        membership = TeamMember.objects.get(user=self.person)
        self.only_lunch()
        self.assertFalse(_earned(self.hack, membership))
        self.arrive()
        self.assertTrue(_earned(self.hack, membership))

    def test_the_check_in_round_rules_count_arrival_only(self):
        from apps.hackathons import round_rules
        from apps.hackathons.models import Round, RoundEntry

        round_obj = Round.objects.create(hackathon=self.hack, name='Screening', order=1)
        RoundEntry.objects.get_or_create(round=round_obj, team=self.team)
        picked = lambda rule: [row['team_name'] for row in round_rules.evaluate(round_obj, rule, None) if row['selected']]
        self.only_lunch()
        self.assertEqual((picked('CHECKED_IN'), picked('ALL_CHECKED_IN')), ([], []))
        self.arrive()
        self.assertEqual((picked('CHECKED_IN'), picked('ALL_CHECKED_IN')), (['Alpha'], ['Alpha']))

    def test_analytics_no_shows_and_the_exports_count_arrival_only(self):
        from apps.hackathons import analytics

        self.only_lunch()
        self.assertEqual(analytics.overview(self.hack)['checked_in'], 0)
        report = _api(self.admin).get(f'{self.base}/check-in/no-shows/').data
        self.assertEqual([t['name'] for t in report['teams']], ['Alpha'])
        self.assertEqual(report['absent_participants'], 1)
        import csv
        import io
        rows = list(csv.reader(io.StringIO(_api(self.admin).get(f'{self.base}/export/check-in/').content.decode('utf-8-sig'))))
        self.assertEqual(rows[1][rows[0].index('Checked In')], 'No')
        self.arrive()
        self.assertEqual(analytics.overview(self.hack)['checked_in'], 1)
        self.assertEqual(_api(self.admin).get(f'{self.base}/check-in/no-shows/').data['teams'], [])


class SessionFollowUpTests(SessionBase):
    """Exports and no-show follow-up for a chosen session; with no session they stay about the arrival check-in."""

    def setUp(self):
        super().setUp()
        self.hack.check_in_days = 2
        self.hack.save()
        check_in_sessions.sync(self.hack)
        self.day2 = self.hack.check_in_sessions.get(day_index=1)
        self.late = make_user(first_name='Late', last_name='Joiner')
        TeamMember.objects.create(team=self.team, user=self.late)
        self.absent_team_leader = make_user(first_name='Zed', last_name='Gone')
        self.absent = make_team(self.hack, self.absent_team_leader, name='Gone')
        self.arrive()  # Asha: arrival only

    def export(self, kind, **params):
        return _api(self.admin).get(f'{self.base}/export/{kind}/', {'export_format': 'csv', **params})

    @staticmethod
    def rows(response):
        import csv
        import io

        return list(csv.reader(io.StringIO(response.content.decode('utf-8-sig'))))

    def check_in_day2(self, user):
        return self.scan(user_id=user.id, session=self.day2.id)

    # -- exports
    def test_check_in_export_lists_the_chosen_session(self):
        self.check_in_day2(self.person)
        by_name = lambda response: {r[0]: r[4] for r in self.rows(response)[1:]}
        self.assertEqual(self.rows(self.export('check-in'))[0][4], 'Checked In')
        self.assertEqual(by_name(self.export('check-in')), {'Asha Rao': 'Yes', 'Late Joiner': 'No', 'Zed Gone': 'No'})
        CheckIn.objects.filter(session=self.main).delete()
        self.assertEqual(by_name(self.export('check-in')), {'Asha Rao': 'No', 'Late Joiner': 'No', 'Zed Gone': 'No'})
        self.assertEqual(by_name(self.export('check-in', session=self.day2.id)), {'Asha Rao': 'Yes', 'Late Joiner': 'No', 'Zed Gone': 'No'})

    def test_check_in_export_file_name_says_which_session(self):
        main_name = self.export('check-in')['Content-Disposition']
        day2_name = self.export('check-in', session=self.day2.id)['Content-Disposition']
        self.assertRegex(main_name, rf'{self.hack.slug}-check-in-\d{{8}}_\d{{6}}\.csv')
        self.assertRegex(day2_name, rf'{self.hack.slug}-check-in-day-2-morning-\d{{8}}_\d{{6}}\.csv')

    def test_an_unknown_session_is_not_found(self):
        other = make_hackathon(min_team_size=1, features={'check_in': True}).main_check_in_session()
        self.assertEqual(self.export('check-in', session=other.id).status_code, 404)
        self.assertEqual(self.export('check-in', session='nope').status_code, 404)

    def test_participants_export_has_a_column_per_later_session(self):
        self.check_in_day2(self.person)
        rows = self.rows(self.export('participants'))
        header = rows[0]
        column = header.index('Attended: Day 2 Morning')
        self.assertEqual({r[header.index('Name')]: r[column] for r in rows[1:]}, {'Asha Rao': 'Yes', 'Late Joiner': 'No', 'Zed Gone': 'No'})
        self.assertEqual(header.count('Checked In'), 1)

    def test_participants_export_is_unchanged_for_a_single_session_event(self):
        single = make_hackathon(min_team_size=1, features={'check_in': True})
        single.main_check_in_session()
        header = self.rows(_api(self.admin).get(f'/api/hackathons/{single.slug}/export/participants/', {'export_format': 'csv'}))[0]
        self.assertFalse([h for h in header if h.startswith('Attended')])

    def test_a_switched_off_session_with_nobody_has_no_column_but_one_with_records_keeps_it(self):
        self.check_in_day2(self.person)
        self.day2.is_active = False
        self.day2.save()
        self.assertIn('Attended: Day 2 Morning', self.rows(self.export('participants'))[0])
        CheckIn.objects.filter(session=self.day2).delete()
        self.assertNotIn('Attended: Day 2 Morning', self.rows(self.export('participants'))[0])

    # -- no-shows
    def no_shows(self, **params):
        return _api(self.admin).get(f'{self.base}/check-in/no-shows/', params)

    def test_no_shows_default_to_arrival(self):
        report = self.no_shows().data
        self.assertEqual([t['name'] for t in report['teams']], ['Gone'])
        self.assertEqual(report['session']['is_main'], True)
        self.assertEqual(report['absent_participants'], 2)

    def test_no_shows_for_another_session_use_that_sessions_check_ins(self):
        self.check_in_day2(self.person)
        report = self.no_shows(session=self.day2.id).data
        self.assertEqual([t['name'] for t in report['teams']], ['Gone'])
        self.assertEqual(report['absent_participants'], 2)  # Late and Zed
        self.assertEqual(report['session']['id'], self.day2.id)
        CheckIn.objects.filter(session=self.day2).delete()
        again = self.no_shows(session=self.day2.id).data
        self.assertEqual([t['name'] for t in again['teams']], ['Alpha', 'Gone'])
        self.assertEqual(again['absent_participants'], 3)

    def test_no_shows_reject_an_unknown_session_and_still_need_organizer_rights(self):
        self.assertEqual(self.no_shows(session=999999).status_code, 404)
        self.assertEqual(_api(self.volunteer).get(f'{self.base}/check-in/no-shows/', {'session': self.day2.id}).status_code, 403)

    def test_reminder_names_the_session_and_reaches_only_people_missing_from_it(self):
        from unittest import mock

        self.check_in_day2(self.person)
        with mock.patch('apps.hackathons.services._notify_users') as notify:
            out = _api(self.admin).post(f'{self.base}/check-in/no-shows/', {'action': 'remind', 'session': self.day2.id}, format='json')
        self.assertEqual(out.data['reminded'], 2)
        people, title, message = notify.call_args.args[:3]
        self.assertEqual({u.id for u in people}, {self.late.id, self.absent_team_leader.id})
        self.assertIn('Day 2 Morning', title)
        self.assertIn('Day 2 Morning', message)

    def test_reminder_without_a_session_is_the_arrival_reminder(self):
        from unittest import mock

        with mock.patch('apps.hackathons.services._notify_users') as notify:
            out = _api(self.admin).post(f'{self.base}/check-in/no-shows/', {'action': 'remind'}, format='json')
        self.assertEqual(out.data['reminded'], 2)
        self.assertIn('please check in', notify.call_args.args[1])

    def test_no_reminder_for_a_switched_off_session(self):
        self.day2.is_active = False
        self.day2.save()
        out = _api(self.admin).post(f'{self.base}/check-in/no-shows/', {'action': 'remind', 'session': self.day2.id}, format='json')
        self.assertEqual((out.status_code, out.data['code']), (400, 'SESSION_OFF'))

    def test_withdrawing_is_for_arrival_only(self):
        out = _api(self.admin).post(f'{self.base}/check-in/no-shows/', {'action': 'withdraw', 'session': self.day2.id, 'team_ids': [self.absent.id]}, format='json')
        self.assertEqual((out.status_code, out.data['code']), (400, 'ARRIVAL_ONLY'))
        self.absent.refresh_from_db()
        self.assertEqual(self.absent.status, 'REGISTERED')


class MainSessionMigrationTests(TransactionTestCase):
    """The data migration: every hackathon that uses check-in gets a main session and its check-ins join it."""

    before = [('hackathons', '0030_check_in_sessions_schema')]
    after = [('hackathons', '0032_check_in_session_required')]

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_existing_check_ins_join_a_main_session_that_carries_the_window(self):
        executor = MigrationExecutor(connection)
        # The accounts app stays at its latest state: the users table has columns the hackathons migrations know nothing about.
        accounts = executor.loader.graph.leaf_nodes('accounts')
        self.before, self.after = self.before + accounts, self.after + accounts
        executor.migrate(self.before)
        old = executor.loader.project_state(self.before).apps
        User, Hackathon, Team, TeamMember, CheckIn = (old.get_model('accounts', 'User'), *(old.get_model('hackathons', n) for n in ('Hackathon', 'Team', 'TeamMember', 'CheckIn')))
        start = timezone.now() + timedelta(days=5)

        def hackathon(slug, **kw):
            return Hackathon.objects.create(title=slug, slug=slug, theme='t', description='d', start_date=start, end_date=start + HOUR, **kw)

        busy = hackathon('busy', features={'check_in': True}, check_in_opens_at=start)
        idle = hackathon('idle', features={'check_in': True})
        off = hackathon('off', features={})
        legacy = hackathon('legacy', features={})  # check-in was switched off afterwards but people had been checked in
        users = [User.objects.create(username=f'u{i}', email=f'u{i}@x.io') for i in range(3)]
        for hack, user in ((busy, users[0]), (busy, users[1]), (legacy, users[2])):
            team = Team.objects.create(hackathon=hack, name=f'T{user.id}', leader=user, status='REGISTERED')
            TeamMember.objects.create(team=team, hackathon=hack, user=user, role='LEADER')
            CheckIn.objects.create(hackathon=hack, user=user, team=team, override_reason='VIP' if user is users[1] else '')

        executor = MigrationExecutor(connection)
        executor.migrate(self.after)
        new = executor.loader.project_state(self.after).apps
        Session, NewCheckIn = new.get_model('hackathons', 'CheckInSession'), new.get_model('hackathons', 'CheckIn')

        self.assertEqual(sorted(Session.objects.values_list('hackathon__slug', flat=True)), ['busy', 'idle', 'legacy'])
        self.assertFalse(Session.objects.filter(hackathon__slug='off').exists())
        for session in Session.objects.all():
            self.assertEqual((session.is_main, session.name, session.day_index, session.slot, session.needs_arrival, session.counts_for_certificate),
                             (True, 'Check-in', 0, 0, False, True))
        self.assertFalse(NewCheckIn.objects.filter(session__isnull=True).exists())
        self.assertEqual(NewCheckIn.objects.filter(session__hackathon__slug='busy').count(), 2)
        self.assertEqual(NewCheckIn.objects.filter(session__hackathon__slug='legacy').count(), 1)
        self.assertEqual(sorted(NewCheckIn.objects.values_list('via', flat=True)), ['LIST', 'LIST', 'OVERRIDE'])
        self.assertEqual(Session.objects.get(hackathon__slug='busy').date, timezone.localtime(start).date())
