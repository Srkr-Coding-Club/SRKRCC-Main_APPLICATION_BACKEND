"""Edge cases for day-of check-in: rotating codes, offline replays, the window, walk-in overrides, paging, stats and no-shows."""
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.hackathons.checkin import (
    MAX_ROSTER_LIMIT, TOKEN_BUCKET_SECONDS, TOKEN_BUCKET_TOLERANCE, UNKNOWN_VOLUNTEER, make_token,
)
from apps.hackathons.models import CheckIn, HackathonStaff, TeamMember, TeamStatus
from .helpers import make_hackathon, make_team, make_user

MINUTE = timezone.timedelta(minutes=1)
HOUR = timezone.timedelta(hours=1)


def _api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


class CheckInEdgeCaseBase(APITestCase):
    def setUp(self):
        self.hack = make_hackathon(min_team_size=1, features={'check_in': True})
        self.admin = make_user(role='ADMIN')
        self.volunteer = make_user(first_name='Vik', last_name='Vol')
        HackathonStaff.objects.create(hackathon=self.hack, user=self.volunteer, role='VOLUNTEER')
        self.person = make_user(first_name='Asha', last_name='Rao')
        self.team = make_team(self.hack, self.person, name='Alpha')
        self.base = f'/api/hackathons/{self.hack.slug}'

    def scan(self, who=None, **body):
        return _api(who or self.volunteer).post(f'{self.base}/check-in/', body, format='json')

    def get(self, path='', who=None):
        return _api(who or self.volunteer).get(f'{self.base}/check-in/{path}')


class RotatingCodeTests(CheckInEdgeCaseBase):
    def test_codes_are_valid_up_to_the_tolerance_and_expire_just_after(self):
        edge = timezone.now() - timezone.timedelta(seconds=TOKEN_BUCKET_SECONDS * TOKEN_BUCKET_TOLERANCE)
        self.assertTrue(self.scan(token=make_token(self.hack, self.person, at=edge)).data['created'])
        CheckIn.objects.all().delete()
        beyond = timezone.now() - timezone.timedelta(seconds=TOKEN_BUCKET_SECONDS * (TOKEN_BUCKET_TOLERANCE + 2))
        self.assertEqual(self.scan(token=make_token(self.hack, self.person, at=beyond)).data['code'], 'QR_EXPIRED')

    def test_a_code_from_the_future_is_also_rejected(self):
        ahead = timezone.now() + 10 * MINUTE
        self.assertEqual(self.scan(token=make_token(self.hack, self.person, at=ahead)).data['code'], 'QR_EXPIRED')

    def test_old_codes_without_a_bucket_are_expired(self):
        from django.core import signing
        legacy = signing.dumps({'h': self.hack.id, 'u': self.person.id}, salt='hackathon-check-in', compress=True)
        self.assertEqual(self.scan(token=legacy).data['code'], 'QR_EXPIRED')

    def test_tampered_blank_and_non_string_tokens_are_invalid(self):
        good = make_token(self.hack, self.person)
        for bad in (good[:-3] + 'xyz', '   ', 'a.b.c', 12345):
            self.assertEqual(self.scan(token=bad).data.get('code'), 'INVALID_QR', bad)

    def test_a_valid_code_for_someone_who_left_the_team_is_refused(self):
        token = make_token(self.hack, self.person)
        TeamMember.objects.filter(user=self.person).delete()
        self.assertEqual(self.scan(token=token).data['code'], 'NOT_A_PARTICIPANT')

    def test_my_check_in_withholds_the_code_when_closed_and_for_unplaced_teams(self):
        self.hack.check_in_closes_at = timezone.now() - HOUR
        self.hack.save()
        mine = _api(self.person).get(f'{self.base}/my-check-in/').data
        self.assertEqual((mine['window'], mine['token']), ('CLOSED', ''))
        outsider = make_user()
        self.assertEqual(_api(outsider).get(f'{self.base}/my-check-in/').status_code, 400)


class OfflineReplayTests(CheckInEdgeCaseBase):
    def test_garbage_naive_and_future_scan_times_fall_back_to_now(self):
        token = lambda: make_token(self.hack, self.person)
        for bad in ('not a date', '2026-01-01T09:00:00', '', None, 123):
            CheckIn.objects.all().delete()
            record = self.scan(token=token(), scanned_at=bad)
            self.assertTrue(record.data['created'], bad)
            self.assertLess(abs((timezone.now() - CheckIn.objects.get().checked_in_at).total_seconds()), 30, bad)
        CheckIn.objects.all().delete()
        future = (timezone.now() + 5 * HOUR).isoformat()
        self.scan(token=token(), scanned_at=future)
        self.assertLessEqual(CheckIn.objects.get().checked_in_at, timezone.now())

    def test_a_scan_older_than_a_day_is_clamped_not_trusted(self):
        long_ago = timezone.now() - timezone.timedelta(days=10)
        self.scan(token=make_token(self.hack, self.person, at=timezone.now() - 24 * HOUR), scanned_at=long_ago.isoformat())
        age = timezone.now() - CheckIn.objects.get().checked_in_at
        self.assertLess(age, 25 * HOUR)

    def test_window_is_judged_at_scan_time_for_both_ends(self):
        self.hack.check_in_opens_at = timezone.now() - HOUR
        self.hack.check_in_closes_at = timezone.now() - 30 * MINUTE
        self.hack.save()
        inside_at = timezone.now() - 45 * MINUTE
        before_at = timezone.now() - 90 * MINUTE
        self.assertEqual(self.scan(token=make_token(self.hack, self.person, at=before_at), scanned_at=before_at.isoformat()).data['code'], 'CHECK_IN_NOT_OPEN')
        self.assertTrue(self.scan(token=make_token(self.hack, self.person, at=inside_at), scanned_at=inside_at.isoformat()).data['created'])

    def test_replaying_the_same_scan_twice_is_harmless(self):
        at = timezone.now() - 5 * MINUTE
        body = {'token': make_token(self.hack, self.person, at=at), 'scanned_at': at.isoformat()}
        first, second = self.scan(**body), self.scan(**body)
        self.assertEqual((first.data['created'], second.data['created']), (True, False))
        self.assertEqual(CheckIn.objects.count(), 1)

    def test_scans_are_refused_when_the_module_is_off_or_the_scanner_is_not_staff(self):
        token = make_token(self.hack, self.person)
        self.assertEqual(self.scan(who=self.person, token=token).status_code, 403)
        self.assertEqual(_api().post(f'{self.base}/check-in/', {'token': token}, format='json').status_code, 401)
        self.hack.features = {'check_in': False}
        self.hack.save()
        self.assertEqual(self.scan(token=token).data['code'], 'FEATURE_OFF')


class WindowSettingTests(CheckInEdgeCaseBase):
    def patch(self, **body):
        return _api(self.admin).patch(self.base + '/', body, format='json')

    def test_only_one_end_set_and_clearing_both_are_allowed(self):
        now = timezone.now()
        self.assertEqual(self.patch(check_in_opens_at=(now + HOUR).isoformat()).status_code, 200)
        self.assertEqual(self.patch(check_in_closes_at=(now + 3 * HOUR).isoformat()).status_code, 200)
        self.assertEqual(self.patch(check_in_opens_at=None, check_in_closes_at=None).status_code, 200)
        self.hack.refresh_from_db()
        self.assertIsNone(self.hack.check_in_opens_at)

    def test_equal_or_reversed_times_are_rejected_and_a_lone_close_before_a_stored_open_too(self):
        now = timezone.now()
        self.assertEqual(self.patch(check_in_opens_at=now.isoformat(), check_in_closes_at=now.isoformat()).status_code, 400)
        self.patch(check_in_opens_at=(now + 2 * HOUR).isoformat())
        self.assertEqual(self.patch(check_in_closes_at=(now + HOUR).isoformat()).status_code, 400)

    def test_participant_sees_the_opening_time_before_it_opens_and_a_code_once_open(self):
        self.hack.check_in_opens_at = timezone.now() + HOUR
        self.hack.save()
        mine = _api(self.person).get(f'{self.base}/my-check-in/').data
        self.assertEqual((mine['window'], mine['token']), ('NOT_OPEN', ''))
        self.assertIsNotNone(mine['opens_at'])
        self.hack.check_in_opens_at = timezone.now() - MINUTE
        self.hack.save()
        self.assertTrue(_api(self.person).get(f'{self.base}/my-check-in/').data['token'])


class OverrideTests(CheckInEdgeCaseBase):
    def setUp(self):
        super().setUp()
        self.forming_user = make_user(first_name='Fay')
        self.forming = make_team(self.hack, self.forming_user, name='Forming', status=TeamStatus.FORMING)

    def override(self, who=None, user=None, **extra):
        return self.scan(who=who or self.admin, user_id=(user or self.forming_user).id, override=True, **extra)

    def test_whitespace_only_reason_is_rejected_and_long_reasons_are_trimmed(self):
        self.assertEqual(self.override(reason='   ').data['code'], 'REASON_REQUIRED')
        self.assertTrue(self.override(reason='  late   arrival ' + 'x' * 400).data['created'])
        reason = CheckIn.objects.get(user=self.forming_user).override_reason
        self.assertEqual(len(reason), 200)
        self.assertTrue(reason.startswith('late arrival'))

    def test_waitlisted_members_can_be_let_in_too(self):
        self.forming.status, self.forming.is_waitlisted = TeamStatus.REGISTERED, True
        self.forming.save()
        self.assertTrue(self.override(reason='VIP').data['created'])

    def test_a_staff_member_without_the_teams_permission_cannot_override(self):
        self.assertEqual(self.override(who=self.volunteer, reason='VIP').status_code, 403)
        organizer = make_user()
        HackathonStaff.objects.create(hackathon=self.hack, user=organizer, role='ORGANIZER')
        self.assertTrue(self.override(who=organizer, reason='VIP').data['created'])

    def test_override_on_an_already_placed_person_stores_no_reason(self):
        self.assertTrue(self.override(user=self.person, reason='not needed').data['created'])
        self.assertEqual(CheckIn.objects.get(user=self.person).override_reason, '')

    def test_override_never_applies_to_qr_scans(self):
        token = make_token(self.hack, self.forming_user)
        self.assertEqual(self.scan(who=self.admin, token=token, override=True, reason='VIP').data['code'], 'TEAM_NOT_REGISTERED')

    def test_withdrawn_people_and_strangers_are_refused_even_with_override(self):
        self.forming.status = TeamStatus.WITHDRAWN
        self.forming.save()
        self.assertEqual(self.override(reason='VIP').data['code'], 'TEAM_REMOVED')
        stranger = make_user()
        self.assertEqual(self.override(user=stranger, reason='VIP').status_code, 404)

    def test_an_overridden_check_in_can_be_undone_and_repeated(self):
        self.override(reason='VIP')
        self.assertEqual(_api(self.volunteer).delete(f'{self.base}/check-in/{self.forming_user.id}/').status_code, 200)
        self.assertFalse(CheckIn.objects.filter(user=self.forming_user).exists())
        self.assertTrue(self.override(reason='VIP again').data['created'])


class RosterEdgeCaseTests(CheckInEdgeCaseBase):
    def test_limit_and_offset_are_clamped_and_garbage_falls_back(self):
        for i in range(3):
            make_team(self.hack, make_user(), name=f'T{i}')
        data = self.get('?limit=0').data
        self.assertEqual((data['limit'], len(data['participants'])), (1, 1))
        self.assertEqual(self.get('?limit=99999').data['limit'], MAX_ROSTER_LIMIT)
        self.assertEqual(self.get('?limit=abc&offset=xyz').data['limit'], 100)
        self.assertEqual(self.get('?offset=-5').data['offset'], 0)
        self.assertEqual(self.get('?offset=1000').data['participants'], [])

    def test_search_treats_wildcards_and_blank_input_literally(self):
        self.assertEqual(self.get('?q=%25').data['count'], 0)
        self.assertEqual(self.get('?q=_').data['count'], 0)
        self.assertEqual(self.get('?q=%20%20').data['count'], 1)
        self.assertEqual(self.get('?q=asha%20rao').data['count'], 1)  # every word may match a different field
        self.assertEqual(self.get('?q=asha%20nobody').data['count'], 0)
        self.assertEqual(self.get('?q=ALPHA').data['count'], 1)

    def test_missing_combines_with_search_and_totals_ignore_filters(self):
        other = make_user(first_name='Bo')
        make_team(self.hack, other, name='Beta')
        self.scan(user_id=self.person.id)
        data = self.get('?missing=1&q=a').data
        self.assertEqual([p['team_name'] for p in data['participants']], ['Beta'])
        self.assertEqual((data['total'], data['checked_in']), (2, 1))
        self.assertTrue(all(not p['checked_in'] for p in data['participants']))

    def test_held_back_hides_checked_in_removed_and_non_organizer_results(self):
        waiting = make_user(first_name='Wendy')
        make_team(self.hack, waiting, name='Queue', status=TeamStatus.FORMING)
        gone = make_user(first_name='Wendell')
        make_team(self.hack, gone, name='Gone', status=TeamStatus.WITHDRAWN)
        names = lambda who: [h['name'] for h in self.get('?q=Wend', who=who).data['held_back']]
        self.assertEqual(names(self.volunteer), [])
        self.assertEqual(len(names(self.admin)), 1)
        self.scan(who=self.admin, user_id=waiting.id, override=True, reason='VIP')
        self.assertEqual(names(self.admin), [])
        self.assertEqual(self.get('', who=self.admin).data['held_back'], [])  # no search, nothing listed

    def test_a_person_in_a_waitlisted_team_is_not_counted_in_the_total(self):
        queued = make_team(self.hack, make_user(), name='Queued')
        queued.is_waitlisted = True
        queued.save()
        self.assertEqual(self.get().data['total'], 1)

    def test_an_empty_hackathon_roster_is_well_formed(self):
        empty = make_hackathon(min_team_size=1, features={'check_in': True})
        data = _api(self.admin).get(f'/api/hackathons/{empty.slug}/check-in/').data
        self.assertEqual((data['total'], data['checked_in'], data['count'], data['participants']), (0, 0, 0, []))


class StatsTests(CheckInEdgeCaseBase):
    def stats(self, who=None):
        return _api(who or self.admin).get(f'{self.base}/check-in/stats/')

    def test_empty_event_has_empty_stats(self):
        data = self.stats().data
        self.assertEqual((data['total'], data['volunteers'], data['timeline']), (0, [], []))

    def test_volunteers_are_counted_and_a_removed_account_is_labelled(self):
        second = make_user(first_name='Bo')
        make_team(self.hack, second, name='Beta')
        third = make_user()
        make_team(self.hack, third, name='Gamma')
        self.scan(user_id=self.person.id)
        self.scan(user_id=second.id)
        self.scan(who=self.admin, user_id=third.id)
        CheckIn.objects.filter(user=third).update(checked_in_by=None)
        by_name = {v['name']: v['count'] for v in self.stats().data['volunteers']}
        self.assertEqual(by_name, {'Vik Vol': 2, UNKNOWN_VOLUNTEER: 1})
        self.assertEqual(self.stats().data['volunteers'][0]['name'], 'Vik Vol')

    def test_timeline_fills_gaps_and_goes_hourly_for_long_spans(self):
        other = make_user()
        make_team(self.hack, other, name='Beta')
        base = timezone.now().replace(minute=0, second=0, microsecond=0) - HOUR
        self.scan(user_id=self.person.id)
        self.scan(user_id=other.id)
        CheckIn.objects.filter(user=self.person).update(checked_in_at=base)
        CheckIn.objects.filter(user=other).update(checked_in_at=base + 50 * MINUTE)
        data = self.stats().data
        self.assertEqual(data['bucket_minutes'], 15)
        self.assertEqual([b['count'] for b in data['timeline']], [1, 0, 0, 1])
        CheckIn.objects.filter(user=other).update(checked_in_at=base + 3 * timezone.timedelta(days=1))
        data = self.stats().data
        self.assertEqual(data['bucket_minutes'], 60)
        self.assertEqual(sum(b['count'] for b in data['timeline']), 2)

    def test_overrides_are_counted_and_only_organizers_may_read_stats(self):
        forming = make_user()
        make_team(self.hack, forming, name='Forming', status=TeamStatus.FORMING)
        self.scan(who=self.admin, user_id=forming.id, override=True, reason='VIP')
        self.assertEqual(self.stats().data['overrides'], 1)
        self.assertEqual(self.stats(self.volunteer).status_code, 403)
        self.assertEqual(self.stats(self.person).status_code, 403)


class NoShowEdgeCaseTests(CheckInEdgeCaseBase):
    def post(self, **body):
        return _api(self.admin).post(f'{self.base}/check-in/no-shows/', body, format='json')

    def test_unknown_action_and_missing_team_ids_are_handled(self):
        self.assertEqual(self.post(action='explode').status_code, 400)
        self.assertEqual(self.post(action='withdraw').data['withdrawn'], 0)
        self.assertEqual(self.post(action='withdraw', team_ids=[]).data['withdrawn'], 0)
        self.assertEqual(self.post(action='withdraw', team_ids=['abc', None, -1, 10**9]).data['withdrawn'], 0)

    def test_teams_of_other_hackathons_and_already_withdrawn_teams_are_untouched(self):
        other = make_hackathon(min_team_size=1, features={'check_in': True})
        foreign = make_team(other, make_user(), name='Foreign')
        done = make_team(self.hack, make_user(), name='Done', status=TeamStatus.WITHDRAWN)
        self.assertEqual(self.post(action='withdraw', team_ids=[foreign.id, done.id]).data['withdrawn'], 0)
        foreign.refresh_from_db()
        self.assertEqual(foreign.status, TeamStatus.REGISTERED)

    def test_withdrawing_twice_is_safe_and_arrived_teams_are_never_listed(self):
        absent = make_team(self.hack, make_user(), name='Absent')
        self.scan(user_id=self.person.id)
        listed = [t['name'] for t in _api(self.admin).get(f'{self.base}/check-in/no-shows/').data['teams']]
        self.assertEqual(listed, ['Absent'])
        self.assertEqual(self.post(action='withdraw', team_ids=[absent.id, self.team.id]).data['withdrawn'], 1)
        self.assertEqual(self.post(action='withdraw', team_ids=[absent.id]).data['withdrawn'], 0)

    def test_withdrawing_is_refused_while_a_future_closing_time_is_pending(self):
        absent = make_team(self.hack, make_user(), name='Absent')
        self.hack.check_in_closes_at = timezone.now() + HOUR
        self.hack.save()
        self.assertEqual(self.post(action='withdraw', team_ids=[absent.id]).data['code'], 'CHECK_IN_STILL_OPEN')
        self.hack.check_in_closes_at = timezone.now() - MINUTE
        self.hack.save()
        self.assertEqual(self.post(action='withdraw', team_ids=[absent.id]).data['withdrawn'], 1)

    def test_remind_with_everyone_present_reminds_nobody_and_follow_up_needs_the_module(self):
        self.scan(user_id=self.person.id)
        self.assertEqual(self.post(action='remind').data['reminded'], 0)
        self.hack.features = {}
        self.hack.save()
        self.assertEqual(self.post(action='remind').data['code'], 'FEATURE_OFF')

    def test_only_organizers_can_follow_up(self):
        for who in (self.volunteer, self.person):
            self.assertEqual(_api(who).post(f'{self.base}/check-in/no-shows/', {'action': 'remind'}, format='json').status_code, 403)
            self.assertEqual(_api(who).get(f'{self.base}/check-in/no-shows/').status_code, 403)

    def test_a_team_with_only_some_members_present_is_not_a_no_show(self):
        buddy = make_user()
        TeamMember.objects.create(team=self.team, user=buddy)
        self.scan(user_id=self.person.id)
        report = _api(self.admin).get(f'{self.base}/check-in/no-shows/').data
        self.assertEqual(report['teams'], [])
        self.assertEqual(report['absent_participants'], 1)
