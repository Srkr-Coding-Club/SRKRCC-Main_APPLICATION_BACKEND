"""Scanning a team's code: the roster it opens, the batch check-in of the people ticked, undoing a batch, and the codes themselves."""
import csv
import io
from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.audit.models import AuditLog
from apps.hackathons.checkin import TOKEN_BUCKET_SECONDS, TOKEN_BUCKET_TOLERANCE, make_team_token, make_token
from apps.hackathons.models import (
    CheckIn, CheckInScanMode, CheckInSession, CheckInSlot, CheckInVia, HackathonStaff, TeamStatus,
)
from apps.hackathons import check_in_sessions
from .helpers import make_hackathon, make_team, make_user

HOUR = timedelta(hours=1)


def _api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


class TeamScanBase(APITestCase):
    def setUp(self):
        self.hack = make_hackathon(min_team_size=1, features={'check_in': True})
        self.admin = make_user(role='ADMIN')
        self.volunteer = make_user(first_name='Vik', last_name='Vol')
        HackathonStaff.objects.create(hackathon=self.hack, user=self.volunteer, role='VOLUNTEER')
        self.leader = make_user(first_name='Asha', last_name='Rao')
        self.second = make_user(first_name='Bo', last_name='Li')
        self.third = make_user(first_name='Cy', last_name='Doe')
        self.team = make_team(self.hack, self.leader, self.second, self.third, name='Alpha')
        self.base = f'/api/hackathons/{self.hack.slug}'
        self.main = self.hack.main_check_in_session()
        self.hack.check_in_days, self.hack.check_in_slots_per_day = 1, 2
        self.hack.save()
        check_in_sessions.sync(self.hack)
        self.lunch = self.hack.check_in_sessions.get(slot=CheckInSlot.AFTERNOON)

    def code(self, team=None, at=None):
        return make_team_token(self.hack, team or self.team, at=at)

    def scan(self, token, who=None, **body):
        return _api(who or self.volunteer).post(f'{self.base}/check-in/scan/', {'token': token, **body}, format='json')

    def batch(self, user_ids, who=None, token=None, **body):
        return _api(who or self.volunteer).post(
            f'{self.base}/check-in/team-batch/', {'token': token or self.code(), 'user_ids': user_ids, **body}, format='json',
        )

    def arrive(self, *users):
        for user in users:
            CheckIn.objects.create(hackathon=self.hack, session=self.main, user=user, team=self.team)


class CodeTests(TeamScanBase):
    def test_the_scanner_tells_a_team_code_from_a_member_code(self):
        team = self.scan(self.code())
        self.assertEqual((team.status_code, team.data['kind']), (200, 'team'))
        member = self.scan(make_token(self.hack, self.leader))
        self.assertEqual((member.data['kind'], member.data['created']), ('member', True))
        self.assertEqual(CheckIn.objects.count(), 1)

    def test_a_team_code_is_not_accepted_where_a_person_is_expected_and_the_other_way_round(self):
        as_member = _api(self.volunteer).post(f'{self.base}/check-in/', {'token': self.code()}, format='json')
        self.assertEqual(as_member.data['code'], 'TEAM_CODE')
        as_team = self.batch([self.leader.id], token=make_token(self.hack, self.leader))
        self.assertEqual(as_team.data['code'], 'NOT_A_TEAM_CODE')
        self.assertFalse(CheckIn.objects.exists())

    def test_tampered_foreign_and_expired_codes_are_refused_like_member_codes(self):
        good = self.code()
        self.assertEqual(self.scan(good[:-3] + 'xyz').data['code'], 'INVALID_QR')
        for junk in ('', '   ', 'a.b.c', 12345):
            self.assertEqual(self.scan(junk).data['code'], 'INVALID_QR', junk)
        other = make_hackathon(min_team_size=1, features={'check_in': True})
        foreign = make_team(other, make_user(), name='Foreign')
        self.assertEqual(self.scan(make_team_token(other, foreign)).data['code'], 'WRONG_HACKATHON')
        edge = timezone.now() - timedelta(seconds=TOKEN_BUCKET_SECONDS * TOKEN_BUCKET_TOLERANCE)
        self.assertEqual(self.scan(self.code(at=edge)).status_code, 200)
        beyond = timezone.now() - timedelta(seconds=TOKEN_BUCKET_SECONDS * (TOKEN_BUCKET_TOLERANCE + 2))
        self.assertEqual(self.scan(self.code(at=beyond)).data['code'], 'QR_EXPIRED')
        self.assertEqual(self.scan(self.code(at=timezone.now() + 10 * timedelta(minutes=1))).data['code'], 'QR_EXPIRED')

    def test_a_signed_code_for_a_team_that_no_longer_exists_is_not_found(self):
        token = self.code()
        self.team.delete()
        refused = self.scan(token)
        self.assertEqual((refused.status_code, refused.data['code']), (404, 'TEAM_NOT_FOUND'))

    def test_participants_cannot_scan_and_the_module_must_be_on(self):
        self.assertEqual(self.scan(self.code(), who=self.leader).status_code, 403)
        self.assertEqual(self.batch([self.leader.id], who=self.leader).status_code, 403)
        self.assertEqual(_api().post(f'{self.base}/check-in/scan/', {'token': self.code()}, format='json').status_code, 401)
        self.hack.features = {}
        self.hack.save()
        self.assertEqual(self.scan(self.code()).data['code'], 'FEATURE_OFF')

    def test_participants_get_both_codes_while_any_session_is_open_and_none_otherwise(self):
        mine = _api(self.leader).get(f'{self.base}/my-check-in/').data
        self.assertTrue(mine['token'] and mine['team_token'])
        self.assertEqual(mine['team'], {'id': self.team.id, 'name': 'Alpha'})
        self.assertEqual(_api(self.second).get(f'{self.base}/my-check-in/').data['team_token'] != '', True)
        self.hack.check_in_closes_at = timezone.now() - HOUR
        self.hack.save()
        CheckInSession.objects.filter(pk=self.lunch.pk).update(opens_at=None, closes_at=timezone.now() - HOUR)
        closed = _api(self.leader).get(f'{self.base}/my-check-in/').data
        self.assertEqual((closed['token'], closed['team_token']), ('', ''))
        CheckInSession.objects.filter(pk=self.lunch.pk).update(closes_at=None)
        later = _api(self.leader).get(f'{self.base}/my-check-in/').data
        self.assertEqual((later['window'], bool(later['token']), bool(later['team_token'])), ('CLOSED', True, True))

    def test_the_team_code_is_the_same_for_every_member_and_changes_with_the_team(self):
        a = _api(self.leader).get(f'{self.base}/my-check-in/').data['team_token']
        b = _api(self.third).get(f'{self.base}/my-check-in/').data['team_token']
        other = make_team(self.hack, make_user(), name='Beta')
        self.assertEqual(self.scan(a).data['team']['id'], self.scan(b).data['team']['id'])
        self.assertNotEqual(self.scan(self.code(other)).data['team']['id'], self.team.id)


class RosterTests(TeamScanBase):
    def test_a_team_scan_lists_the_members_and_checks_nobody_in(self):
        self.arrive(self.leader)
        data = self.scan(self.code()).data
        self.assertEqual((data['team']['name'], data['team']['placed'], data['all_checked_in'], data['can_override']), ('Alpha', True, False, False))
        by_name = {m['name']: m for m in data['members']}
        self.assertEqual(set(by_name), {'Asha Rao', 'Bo Li', 'Cy Doe'})
        self.assertEqual((by_name['Asha Rao']['checked_in'], by_name['Bo Li']['checked_in']), (True, False))
        self.assertEqual(by_name['Asha Rao']['role'], 'LEADER')
        self.assertEqual(data['session']['id'], self.main.id)
        self.assertEqual(CheckIn.objects.count(), 1)

    def test_everyone_already_checked_in_is_flagged(self):
        self.arrive(self.leader, self.second, self.third)
        self.assertTrue(self.scan(self.code()).data['all_checked_in'])

    def test_a_later_session_marks_the_people_who_have_not_arrived_as_blocked(self):
        self.arrive(self.leader)
        members = {m['name']: m for m in self.scan(self.code(), session=self.lunch.id).data['members']}
        self.assertEqual((members['Asha Rao']['blocked'], members['Bo Li']['blocked']), (None, 'NOT_ARRIVED'))

    def test_organizers_are_told_they_may_override(self):
        self.assertTrue(self.scan(self.code(), who=self.admin).data['can_override'])

    def test_each_session_decides_whether_a_team_code_is_welcome(self):
        CheckInSession.objects.filter(pk=self.lunch.pk).update(scan_mode=CheckInScanMode.MEMBERS)
        self.assertEqual(self.scan(self.code(), session=self.lunch.id).data['code'], 'MEMBER_SCAN_ONLY')
        for mode in (CheckInScanMode.TEAMS, CheckInScanMode.BOTH):
            CheckInSession.objects.filter(pk=self.lunch.pk).update(scan_mode=mode)
            self.assertEqual(self.scan(self.code(), session=self.lunch.id).status_code, 200, mode)

    def test_a_team_code_obeys_the_sessions_window_and_switch(self):
        CheckInSession.objects.filter(pk=self.lunch.pk).update(opens_at=timezone.now() + HOUR)
        self.assertEqual(self.scan(self.code(), session=self.lunch.id).data['code'], 'CHECK_IN_NOT_OPEN')
        CheckInSession.objects.filter(pk=self.lunch.pk).update(opens_at=None, closes_at=timezone.now() - HOUR)
        self.assertEqual(self.scan(self.code(), session=self.lunch.id).data['code'], 'CHECK_IN_CLOSED')
        CheckInSession.objects.filter(pk=self.lunch.pk).update(closes_at=None, is_active=False)
        self.assertEqual(self.scan(self.code(), session=self.lunch.id).data['code'], 'SESSION_OFF')

    def test_a_team_that_is_not_registered_is_for_organizers_only_and_a_removed_team_is_for_nobody(self):
        self.team.status = TeamStatus.FORMING
        self.team.save()
        self.assertEqual(self.scan(self.code()).data['code'], 'TEAM_NOT_REGISTERED')
        shown = self.scan(self.code(), who=self.admin).data
        self.assertFalse(shown['team']['placed'])
        self.team.status, self.team.is_waitlisted = TeamStatus.REGISTERED, True
        self.team.save()
        self.assertEqual(self.scan(self.code()).data['code'], 'TEAM_NOT_REGISTERED')
        for status in (TeamStatus.DISQUALIFIED, TeamStatus.WITHDRAWN):
            self.team.status = status
            self.team.save()
            self.assertEqual(self.scan(self.code(), who=self.admin).data['code'], 'TEAM_REMOVED', status)

    def test_unknown_and_foreign_sessions_are_refused(self):
        other = make_hackathon(min_team_size=1, features={'check_in': True}).main_check_in_session()
        for bad in (999999, other.id, 'abc'):
            self.assertEqual(self.scan(self.code(), session=bad).status_code, 404, bad)


class BatchTests(TeamScanBase):
    def test_only_the_ticked_people_are_checked_in_together_and_recorded_as_a_team_scan(self):
        done = self.batch([self.leader.id, self.third.id])
        self.assertEqual((done.status_code, done.data['created'], done.data['already'], done.data['refused']), (200, 2, 0, 0))
        self.assertEqual({r['name'] for r in done.data['results']}, {'Asha Rao', 'Cy Doe'})
        rows = CheckIn.objects.all()
        self.assertEqual({r.user_id for r in rows}, {self.leader.id, self.third.id})
        self.assertEqual({r.via for r in rows}, {CheckInVia.TEAM_QR})
        self.assertEqual({str(r.batch_id) for r in rows}, {done.data['batch_id']})
        self.assertEqual({r.checked_in_by_id for r in rows}, {self.volunteer.id})
        audits = AuditLog.objects.filter(action='Checked In Team Members')
        self.assertEqual(audits.count(), 1)
        self.assertEqual(sorted(audits.get().details['people']), sorted([self.leader.email, self.third.email]))

    def test_the_same_people_again_are_reported_as_already_checked_in_and_make_no_new_batch(self):
        self.batch([self.leader.id])
        again = self.batch([self.leader.id])
        self.assertEqual((again.data['created'], again.data['already'], again.data['batch_id']), (0, 1, None))
        self.assertEqual(CheckIn.objects.count(), 1)

    def test_one_refusal_does_not_stop_the_others_in_a_later_session(self):
        self.arrive(self.leader)
        done = self.batch([self.leader.id, self.second.id], session=self.lunch.id)
        statuses = {r['name']: (r['status'], r.get('code')) for r in done.data['results']}
        self.assertEqual(statuses, {'Asha Rao': ('created', None), 'Bo Li': ('refused', 'NOT_ARRIVED')})
        self.assertEqual((done.data['created'], done.data['refused']), (1, 1))
        self.assertEqual(CheckIn.objects.filter(session=self.lunch).count(), 1)

    def test_people_from_another_team_and_garbage_ids_are_refused_or_ignored(self):
        stranger = make_user()
        make_team(self.hack, stranger, name='Beta')
        done = self.batch([stranger.id, self.leader.id, 'abc', None, self.leader.id])
        codes = {r['user_id']: r.get('code') for r in done.data['results']}
        self.assertEqual(codes, {stranger.id: 'NOT_IN_TEAM', self.leader.id: None})
        self.assertEqual(CheckIn.objects.filter(user=stranger).count(), 0)
        self.assertEqual(CheckIn.objects.filter(user=self.leader).count(), 1)

    def test_picking_nobody_is_an_error(self):
        for ids in ([], None, ['abc']):
            self.assertEqual(self.batch(ids).data['code'], 'NOBODY_SELECTED', ids)

    def test_an_organizer_can_let_in_people_who_have_not_arrived_with_a_reason_and_a_volunteer_cannot(self):
        body = {'session': self.lunch.id, 'override': True}
        self.assertEqual(self.batch([self.second.id], who=self.volunteer, reason='VIP', **body).status_code, 403)
        self.assertEqual(self.batch([self.second.id], who=self.admin, **body).data['code'], 'REASON_REQUIRED')
        done = self.batch([self.second.id], who=self.admin, reason='Came in by the side gate', **body)
        self.assertEqual(done.data['created'], 1)
        record = CheckIn.objects.get(user=self.second)
        self.assertEqual((record.via, record.override_reason), (CheckInVia.OVERRIDE, 'Came in by the side gate'))

    def test_an_unregistered_team_needs_an_organizer_and_a_reason(self):
        self.team.status = TeamStatus.FORMING
        self.team.save()
        self.assertEqual(self.batch([self.leader.id]).data['code'], 'TEAM_NOT_REGISTERED')
        self.assertEqual(self.batch([self.leader.id], who=self.admin).data['code'], 'REASON_REQUIRED')
        done = self.batch([self.leader.id], who=self.admin, override=True, reason='Registration still being fixed')
        self.assertEqual((done.data['created'], CheckIn.objects.get().via), (1, CheckInVia.OVERRIDE))

    def test_a_batch_needs_a_current_team_code_and_a_session_that_takes_team_scans(self):
        self.assertEqual(self.batch([self.leader.id], token='junk').data['code'], 'INVALID_QR')
        old = timezone.now() - timedelta(seconds=TOKEN_BUCKET_SECONDS * (TOKEN_BUCKET_TOLERANCE + 2))
        self.assertEqual(self.batch([self.leader.id], token=self.code(at=old)).data['code'], 'QR_EXPIRED')
        CheckInSession.objects.filter(pk=self.main.pk).update(scan_mode=CheckInScanMode.MEMBERS)
        self.assertEqual(self.batch([self.leader.id]).data['code'], 'MEMBER_SCAN_ONLY')
        self.assertFalse(CheckIn.objects.exists())

    def test_a_replayed_team_scan_is_judged_at_the_time_it_was_scanned(self):
        at = timezone.now() - 2 * HOUR
        CheckInSession.objects.filter(pk=self.lunch.pk).update(opens_at=at - HOUR, closes_at=timezone.now() - timedelta(minutes=10))
        self.arrive(self.leader)
        done = self.batch([self.leader.id], token=self.code(at=at), session=self.lunch.id, scanned_at=at.isoformat())
        self.assertEqual(done.data['created'], 1)

    def test_a_large_team_is_checked_in_in_one_call(self):
        big = make_team(self.hack, make_user(), *[make_user() for _ in range(7)], name='Big')
        ids = list(big.memberships.values_list('user_id', flat=True))
        done = self.batch(ids, token=self.code(big))
        self.assertEqual((done.data['created'], len(done.data['results'])), (8, 8))


class UndoAndReportingTests(TeamScanBase):
    def test_a_batch_can_be_taken_back_without_touching_other_check_ins(self):
        self.arrive(self.second)
        done = self.batch([self.leader.id, self.third.id]).data
        gone = _api(self.volunteer).delete(f"{self.base}/check-in/batches/{done['batch_id']}/")
        self.assertEqual((gone.status_code, gone.data['removed']), (200, 2))
        self.assertEqual(list(CheckIn.objects.values_list('user_id', flat=True)), [self.second.id])
        self.assertTrue(AuditLog.objects.filter(action='Undid Team Check-in').exists())

    def test_taking_back_arrival_is_refused_while_later_sessions_hold_those_people(self):
        done = self.batch([self.leader.id]).data
        CheckIn.objects.create(hackathon=self.hack, session=self.lunch, user=self.leader, team=self.team)
        url = f"{self.base}/check-in/batches/{done['batch_id']}/"
        refused = _api(self.volunteer).delete(url)
        self.assertEqual((refused.status_code, refused.data['code'], refused.data['later']), (400, 'HAS_LATER_CHECKINS', 1))
        self.assertEqual(CheckIn.objects.count(), 2)
        self.assertEqual(_api(self.volunteer).delete(url + '?cascade=1').data['removed'], 1)
        self.assertEqual(CheckIn.objects.count(), 0)

    def test_an_unknown_batch_or_one_from_another_hackathon_removes_nothing(self):
        other = make_hackathon(min_team_size=1, features={'check_in': True})
        foreign_team = make_team(other, make_user(), name='Foreign')
        foreign = _api(self.admin).post(
            f'/api/hackathons/{other.slug}/check-in/team-batch/',
            {'token': make_team_token(other, foreign_team), 'user_ids': [foreign_team.leader_id]}, format='json',
        ).data
        self.assertEqual(foreign['created'], 1)
        for batch in (foreign['batch_id'], '00000000-0000-4000-8000-000000000000'):
            self.assertEqual(_api(self.volunteer).delete(f'{self.base}/check-in/batches/{batch}/').data['removed'], 0)
        self.assertEqual(CheckIn.objects.filter(hackathon=other).count(), 1)

    def test_a_participant_cannot_take_a_batch_back(self):
        done = self.batch([self.leader.id]).data
        self.assertEqual(_api(self.leader).delete(f"{self.base}/check-in/batches/{done['batch_id']}/").status_code, 403)

    def test_the_roster_and_stats_say_how_people_were_checked_in(self):
        self.batch([self.leader.id])
        _api(self.volunteer).post(f'{self.base}/check-in/', {'user_id': self.second.id}, format='json')
        _api(self.volunteer).post(f'{self.base}/check-in/', {'token': make_token(self.hack, self.third)}, format='json')
        roster = {p['name']: p['via'] for p in _api(self.volunteer).get(f'{self.base}/check-in/').data['participants']}
        self.assertEqual(roster, {'Asha Rao': 'TEAM_QR', 'Bo Li': 'LIST', 'Cy Doe': 'MEMBER_QR'})
        stats = _api(self.admin).get(f'{self.base}/check-in/stats/').data
        self.assertEqual(stats['by_via'], {'TEAM_QR': 1, 'LIST': 1, 'MEMBER_QR': 1})
        self.assertEqual(stats['team_scans'], 1)

    def test_the_old_member_scan_endpoint_is_unchanged_and_the_export_still_counts_arrival(self):
        done = _api(self.volunteer).post(f'{self.base}/check-in/', {'token': make_token(self.hack, self.leader)}, format='json')
        self.assertEqual((done.data['created'], done.data['via']), (True, CheckInVia.MEMBER_QR))
        text = _api(self.admin).get(f'{self.base}/export/check-in/').content.decode('utf-8-sig')
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual({r[0]: r[rows[0].index('Checked In')] for r in rows[1:]}, {'Asha Rao': 'Yes', 'Bo Li': 'No', 'Cy Doe': 'No'})
