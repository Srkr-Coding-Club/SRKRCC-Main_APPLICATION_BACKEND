"""Organizer control over withdrawing a team: the policy (instant, needs approval, locked), a deadline, and the end of the event."""
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.hackathons import withdrawals
from apps.hackathons.models import CheckIn, Round, RoundEntry, Team, TeamStatus, WithdrawalPolicy, WithdrawalRequest
from .helpers import make_hackathon, make_team, make_user

HOUR = timezone.timedelta(hours=1)


def _api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


class WithdrawalControlBase(APITestCase):
    def setUp(self):
        self.hack = make_hackathon(min_team_size=1)
        self.admin = make_user(role='ADMIN')
        self.leader = make_user(first_name='Asha')
        self.team = make_team(self.hack, self.leader, name='Alpha')                    # registered
        self.leave_url = f'/api/hackathons/teams/{self.team.id}/leave/'

    def leave(self, reason='Changed my plans'):
        return _api(self.leader).post(self.leave_url, {'reason': reason}, format='json')

    def set_rules(self, **fields):
        type(self.hack).objects.filter(pk=self.hack.pk).update(**fields)
        self.hack.refresh_from_db()
        self.team.refresh_from_db()


class PolicyTests(WithdrawalControlBase):
    def test_by_default_a_registered_team_still_needs_approval(self):
        reply = self.leave()
        self.assertEqual(reply.status_code, 200)
        self.assertTrue(reply.data['withdrawal_requested'])
        self.team.refresh_from_db()
        self.assertEqual(self.team.status, TeamStatus.REGISTERED)

    def test_instant_lets_a_registered_team_go_at_once(self):
        self.set_rules(withdrawal_policy=WithdrawalPolicy.INSTANT)
        reply = self.leave()
        self.assertEqual((reply.status_code, reply.data['left']), (200, True))
        self.team.refresh_from_db()
        self.assertEqual(self.team.status, TeamStatus.WITHDRAWN)
        self.assertFalse(WithdrawalRequest.objects.filter(team=self.team).exists())

    def test_instant_still_asks_for_approval_once_the_team_is_in_a_round(self):
        self.set_rules(withdrawal_policy=WithdrawalPolicy.INSTANT)
        round_ = Round.objects.create(hackathon=self.hack, name='Screening', order=1)
        RoundEntry.objects.create(round=round_, team=self.team)
        self.assertTrue(self.leave().data['withdrawal_requested'])

    def test_instant_still_asks_for_approval_once_someone_has_checked_in(self):
        self.set_rules(withdrawal_policy=WithdrawalPolicy.INSTANT)
        session = self.hack.main_check_in_session()
        CheckIn.objects.create(hackathon=self.hack, session=session, user=self.leader, team=self.team)
        self.assertTrue(self.leave().data['withdrawal_requested'])

    def test_locked_refuses_a_registered_team_with_a_clear_reason(self):
        self.set_rules(withdrawal_policy=WithdrawalPolicy.LOCKED)
        reply = self.leave()
        self.assertEqual((reply.status_code, reply.data['code'], reply.data['reason']), (400, withdrawals.WITHDRAWAL_LOCKED, 'LOCKED'))
        self.assertIn('closed withdrawals', reply.data['detail'])
        self.assertFalse(WithdrawalRequest.objects.filter(team=self.team).exists())

    def test_a_team_that_holds_no_place_can_always_leave_even_when_locked(self):
        self.set_rules(withdrawal_policy=WithdrawalPolicy.LOCKED)
        forming = make_team(self.hack, make_user(), name='Beta', status=TeamStatus.FORMING)
        solo = forming.leader
        reply = _api(solo).post(f'/api/hackathons/teams/{forming.id}/leave/', {'reason': 'Not ready'}, format='json')
        self.assertEqual(reply.status_code, 200)
        forming.refresh_from_db()
        self.assertEqual(forming.status, TeamStatus.WITHDRAWN)


class DeadlineTests(WithdrawalControlBase):
    def test_before_the_deadline_withdrawing_works(self):
        self.set_rules(withdrawal_deadline=timezone.now() + HOUR)
        self.assertEqual(self.leave().status_code, 200)

    def test_after_the_deadline_it_is_refused_and_says_when_it_closed(self):
        self.set_rules(withdrawal_deadline=timezone.now() - HOUR)
        reply = self.leave()
        self.assertEqual((reply.status_code, reply.data['code'], reply.data['reason']), (400, withdrawals.WITHDRAWAL_CLOSED, 'DEADLINE'))
        self.assertRegex(reply.data['detail'], r'Withdrawals closed on \d{2} \w{3} \d{4}')

    def test_an_organizer_can_still_decide_a_request_made_before_the_deadline(self):
        self.set_rules(withdrawal_deadline=timezone.now() + HOUR)
        self.leave()
        request = WithdrawalRequest.objects.get(team=self.team)
        self.set_rules(withdrawal_deadline=timezone.now() - HOUR)
        reply = _api(self.admin).post(f'/api/hackathons/{self.hack.slug}/withdrawals/{request.id}/decide/', {'approve': True}, format='json')
        self.assertEqual(reply.status_code, 200, reply.data)
        self.team.refresh_from_db()
        self.assertEqual(self.team.status, TeamStatus.WITHDRAWN)


class AfterTheEventTests(WithdrawalControlBase):
    def test_nobody_can_withdraw_once_the_hackathon_has_ended(self):
        self.set_rules(start_date=timezone.now() - 3 * HOUR, end_date=timezone.now() - HOUR)
        reply = self.leave()
        self.assertEqual((reply.status_code, reply.data['code'], reply.data['reason']), (400, withdrawals.WITHDRAWAL_CLOSED, 'ENDED'))

    def test_that_includes_a_team_that_was_only_forming(self):
        self.set_rules(start_date=timezone.now() - 3 * HOUR, end_date=timezone.now() - HOUR)
        forming = make_team(self.hack, make_user(), name='Beta', status=TeamStatus.FORMING)
        reply = _api(forming.leader).post(f'/api/hackathons/teams/{forming.id}/leave/', {'reason': 'x'}, format='json')
        self.assertEqual(reply.data['reason'], 'ENDED')


class DashboardPayloadTests(WithdrawalControlBase):
    def window(self):
        return _api(self.leader).get(f'/api/hackathons/{self.hack.slug}/my-team/').data['withdrawal_window']

    def test_the_dashboard_is_told_what_is_possible(self):
        state = self.window()
        self.assertEqual((state['open'], state['policy'], state['needs_approval'], state['reason']), (True, 'APPROVAL', True, None))
        self.set_rules(withdrawal_policy=WithdrawalPolicy.INSTANT)
        self.assertFalse(self.window()['needs_approval'])

    def test_a_deadline_is_shown_while_open_and_a_reason_once_closed(self):
        deadline = timezone.now() + HOUR
        self.set_rules(withdrawal_deadline=deadline)
        self.assertIsNotNone(self.window()['closes_at'])
        self.set_rules(withdrawal_policy=WithdrawalPolicy.LOCKED)
        closed = self.window()
        self.assertEqual((closed['open'], closed['reason']), (False, 'LOCKED'))
        self.assertIn('closed withdrawals', closed['message'])

    def test_a_person_without_a_team_gets_no_window(self):
        outsider = make_user()
        reply = _api(outsider).get(f'/api/hackathons/{self.hack.slug}/my-team/')
        self.assertIsNone(reply.data['withdrawal_window'])


class OrganizerSettingsTests(WithdrawalControlBase):
    def test_an_admin_sets_the_policy_and_deadline_and_members_cannot(self):
        url = f'/api/hackathons/{self.hack.slug}/'
        deadline = (timezone.now() + 5 * HOUR).isoformat()
        done = _api(self.admin).patch(url, {'withdrawal_policy': 'LOCKED', 'withdrawal_deadline': deadline}, format='json')
        self.assertEqual(done.status_code, 200, done.data)
        self.hack.refresh_from_db()
        self.assertEqual(self.hack.withdrawal_policy, 'LOCKED')
        self.assertIsNotNone(self.hack.withdrawal_deadline)
        refused = _api(self.leader).patch(url, {'withdrawal_policy': 'INSTANT'}, format='json')
        self.assertIn(refused.status_code, (401, 403, 404))
        self.hack.refresh_from_db()
        self.assertEqual(self.hack.withdrawal_policy, 'LOCKED')

    def test_an_unknown_policy_is_rejected(self):
        reply = _api(self.admin).patch(f'/api/hackathons/{self.hack.slug}/', {'withdrawal_policy': 'WHENEVER'}, format='json')
        self.assertEqual(reply.status_code, 400)
