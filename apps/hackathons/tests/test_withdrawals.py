"""Creating and withdrawing teams over and over: the per-person limit, the cooldown, reasons, and organizer approval."""
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.audit.models import AuditLog
from apps.hackathons.models import (
    HackathonStaff, Round, RoundEntry, Team, TeamAllowance, TeamMember, TeamStatus, WithdrawalRequest, WithdrawalStatus,
)
from apps.hackathons.withdrawals import MAX_SELF_CREATED_TEAMS, WITHDRAW_COOLDOWN
from .helpers import make_hackathon, make_team, make_user

MINUTE = timezone.timedelta(minutes=1)


def _api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


class WithdrawalBase(APITestCase):
    min_team_size = 2

    def setUp(self):
        self.hack = make_hackathon(min_team_size=self.min_team_size)
        self.admin = make_user(role='ADMIN')
        self.user = make_user(first_name='Asha', last_name='Rao')
        self.base = f'/api/hackathons/{self.hack.slug}'

    def create(self, who=None, name='Alpha', hack=None):
        slug = (hack or self.hack).slug
        return _api(who or self.user).post(f'/api/hackathons/{slug}/teams/', {'name': name}, format='json')

    def leave(self, team_id, who=None, reason='Changed my plans'):
        body = {'reason': reason} if reason is not None else {}
        return _api(who or self.user).post(f'/api/hackathons/teams/{team_id}/leave/', body, format='json')

    def cool_down(self):
        """Pretend the cooldown has passed."""
        Team.objects.filter(withdrawn_at__isnull=False).update(withdrawn_at=timezone.now() - WITHDRAW_COOLDOWN - MINUTE)


class CreationLimitTests(WithdrawalBase):
    def test_the_third_team_is_refused_even_after_withdrawing_the_others(self):
        for index in range(MAX_SELF_CREATED_TEAMS):
            made = self.create(name=f'Team {index}')
            self.assertEqual(made.status_code, 201, made.data)
            self.assertEqual(self.leave(made.data['id']).status_code, 200)
            self.cool_down()
        refused = self.create(name='One more')
        self.assertEqual(refused.data['code'], 'TEAM_LIMIT_REACHED')
        self.assertEqual(refused.data['limit'], MAX_SELF_CREATED_TEAMS)
        self.assertIn('Ask the organizers', refused.data['detail'])

    def test_a_just_withdrawn_team_blocks_the_next_one_until_the_cooldown_is_over(self):
        first = self.create(name='First')
        self.leave(first.data['id'])
        blocked = self.create(name='Second')
        self.assertEqual(blocked.data['code'], 'WITHDRAW_COOLDOWN')
        self.assertIn('retry_at', blocked.data)
        self.assertRegex(blocked.data['detail'], r'in \d+ minutes?\.')
        self.cool_down()
        self.assertEqual(self.create(name='Second').status_code, 201)

    def test_somebody_elses_withdrawal_does_not_cool_you_down_and_teams_are_counted_per_person(self):
        other = make_user()
        first = self.create(who=other, name='Theirs')
        self.leave(first.data['id'], who=other)
        self.assertEqual(self.create(name='Mine').status_code, 201)

    def test_the_limit_is_per_hackathon(self):
        elsewhere = make_hackathon(min_team_size=2)
        for index in range(MAX_SELF_CREATED_TEAMS):
            made = self.create(name=f'Team {index}')
            self.leave(made.data['id'])
            self.cool_down()
        self.assertEqual(self.create(name='Here').data['code'], 'TEAM_LIMIT_REACHED')
        self.assertEqual(self.create(name='There', hack=elsewhere).status_code, 201)

    def test_teams_an_organizer_creates_for_someone_do_not_count_against_them(self):
        person = make_user()
        made = _api(self.admin).post(f'{self.base}/teams/', {'name': 'Admin made', 'leader_email': person.email}, format='json')
        self.assertEqual(made.status_code, 201, made.data)
        self.assertIsNone(Team.objects.get(name='Admin made').created_by)
        self.leave(made.data['id'], who=person)
        self.cool_down()
        for index in range(MAX_SELF_CREATED_TEAMS):
            self.assertEqual(self.create(who=person, name=f'Own {index}').status_code, 201)
            self.leave(Team.objects.get(name=f'Own {index}').id, who=person)
            self.cool_down()

    def test_an_organizer_can_allow_more_teams_for_one_person_only(self):
        for index in range(MAX_SELF_CREATED_TEAMS):
            made = self.create(name=f'Team {index}')
            self.leave(made.data['id'])
            self.cool_down()
        self.assertEqual(self.create(name='Extra').data['code'], 'TEAM_LIMIT_REACHED')
        granted = _api(self.admin).post(f'{self.base}/team-allowances/', {'user_id': self.user.id, 'extra': 1, 'reason': 'Team broke up'}, format='json')
        self.assertEqual(granted.data, {'user_id': self.user.id, 'extra': 1})
        self.assertEqual(self.create(name='Extra').status_code, 201)
        self.leave(Team.objects.get(name='Extra').id)
        self.cool_down()
        self.assertEqual(self.create(name='Extra two').data['code'], 'TEAM_LIMIT_REACHED')
        self.assertEqual(self.create(who=make_user(), name='Someone else').status_code, 201)
        self.assertTrue(AuditLog.objects.filter(action='Allowed Extra Hackathon Teams').exists())

    def test_a_team_the_person_is_only_a_member_of_does_not_count(self):
        other = make_user()
        theirs = self.create(who=other, name='Theirs')
        TeamMember.objects.create(team=Team.objects.get(pk=theirs.data['id']), user=self.user)
        TeamMember.objects.filter(user=self.user).delete()
        for index in range(MAX_SELF_CREATED_TEAMS):
            self.assertEqual(self.create(name=f'Mine {index}').status_code, 201)
            self.leave(Team.objects.get(name=f'Mine {index}').id)
            self.cool_down()


class ImmediateWithdrawalTests(WithdrawalBase):
    def test_a_reason_is_required_and_kept(self):
        team = self.create().data
        for blank in (None, '', '   '):
            self.assertEqual(self.leave(team['id'], reason=blank).data['code'], 'REASON_REQUIRED')
        self.assertEqual(Team.objects.get(pk=team['id']).status, TeamStatus.FORMING)
        done = self.leave(team['id'], reason='  We  could not find\n teammates  ')
        self.assertEqual((done.data['left'], done.data['withdrawal_requested']), (True, False))
        stored = Team.objects.get(pk=team['id'])
        self.assertEqual((stored.status, stored.status_reason, stored.withdrawn_by), (TeamStatus.WITHDRAWN, 'We could not find teammates', self.user))
        self.assertIsNotNone(stored.withdrawn_at)
        self.assertIsNone(stored.leader)
        self.assertEqual(AuditLog.objects.filter(action='Withdrew Hackathon Team').latest('id').details['reason'], 'We could not find teammates')

    def test_a_very_long_reason_is_trimmed(self):
        team = self.create().data
        self.leave(team['id'], reason='x' * 1000)
        self.assertEqual(len(Team.objects.get(pk=team['id']).status_reason), 300)

    def test_a_forming_team_is_withdrawn_at_once_and_a_waitlisted_one_too(self):
        team = self.create().data
        self.assertEqual(self.leave(team['id']).data['left'], True)
        queued = make_team(self.hack, make_user(), name='Queued', status=TeamStatus.FORMING)
        queued.is_waitlisted = True
        queued.save()
        self.assertEqual(self.leave(queued.id, who=queued.leader).data['left'], True)
        self.assertFalse(WithdrawalRequest.objects.exists())

    def test_members_leave_freely_and_a_leader_with_a_team_must_transfer_first(self):
        team = self.create().data
        member = make_user()
        TeamMember.objects.create(team=Team.objects.get(pk=team['id']), user=member)
        self.assertEqual(self.leave(team['id'], who=member, reason=None).data['left'], True)
        TeamMember.objects.create(team=Team.objects.get(pk=team['id']), user=make_user())
        self.assertEqual(self.leave(team['id']).data['code'], 'LEADER_MUST_TRANSFER')

    def test_a_stranger_cannot_withdraw_someone_elses_team(self):
        team = self.create().data
        self.assertEqual(self.leave(team['id'], who=make_user()).status_code, 404)


class ApprovalTests(WithdrawalBase):
    min_team_size = 1

    def setUp(self):
        super().setUp()
        self.team = make_team(self.hack, self.user, name='Solo')

    def request(self, reason='Our laptop broke'):
        return self.leave(self.team.id, reason=reason)

    def test_a_registered_team_becomes_a_request_and_stays_until_decided(self):
        response = self.request()
        self.assertEqual((response.data['left'], response.data['withdrawal_requested']), (False, True))
        self.team.refresh_from_db()
        self.assertEqual(self.team.status, TeamStatus.REGISTERED)
        self.assertEqual(self.team.leader, self.user)
        self.assertTrue(TeamMember.objects.filter(team=self.team, user=self.user).exists())
        pending = WithdrawalRequest.objects.get()
        self.assertEqual((pending.status, pending.reason), (WithdrawalStatus.PENDING, 'Our laptop broke'))
        self.assertEqual(self.request().data['code'], 'WITHDRAWAL_PENDING')
        self.assertEqual(WithdrawalRequest.objects.count(), 1)

    def test_a_team_in_a_round_needs_approval_even_while_forming(self):
        forming = make_team(self.hack, make_user(), name='Forming', status=TeamStatus.FORMING)
        round_obj = Round.objects.create(hackathon=self.hack, name='Screening', order=1)
        RoundEntry.objects.create(round=round_obj, team=forming)
        self.assertTrue(self.leave(forming.id, who=forming.leader).data['withdrawal_requested'])

    def test_the_leader_can_take_the_request_back_and_ask_again(self):
        self.request()
        cancelled = _api(self.user).post(f'/api/hackathons/teams/{self.team.id}/cancel-withdrawal/', {}, format='json')
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(WithdrawalRequest.objects.get().status, WithdrawalStatus.CANCELLED)
        self.assertEqual(_api(self.user).post(f'/api/hackathons/teams/{self.team.id}/cancel-withdrawal/', {}, format='json').data['code'], 'NO_WITHDRAWAL_PENDING')
        self.assertTrue(self.request().data['withdrawal_requested'])
        self.assertEqual(WithdrawalRequest.objects.count(), 2)

    def test_approving_withdraws_the_team_notifies_and_promotes_the_waitlist(self):
        self.hack.max_teams, self.hack.waitlist_enabled = 1, True
        self.hack.save()
        queued = make_team(self.hack, make_user(), name='Queued')
        queued.is_waitlisted = True
        queued.save()
        self.request()
        item = WithdrawalRequest.objects.get()
        with self.captureOnCommitCallbacks(execute=True):
            done = _api(self.admin).post(f'{self.base}/withdrawals/{item.id}/decide/', {'approve': True, 'note': 'Thanks for telling us'}, format='json')
        self.assertEqual(done.data['status'], 'APPROVED')
        self.team.refresh_from_db()
        queued.refresh_from_db()
        self.assertEqual((self.team.status, self.team.status_reason, self.team.withdrawn_by), (TeamStatus.WITHDRAWN, 'Our laptop broke', self.user))
        self.assertFalse(queued.is_waitlisted)
        self.assertTrue(AuditLog.objects.filter(action='Approved Hackathon Team Withdrawal').exists())
        from apps.notifications.models import Notification
        self.assertTrue(Notification.objects.filter(recipient=self.user, title__contains='withdrawal approved').exists())

    def test_denying_keeps_the_team_and_tells_the_leader_why(self):
        self.request()
        item = WithdrawalRequest.objects.get()
        with self.captureOnCommitCallbacks(execute=True):
            _api(self.admin).post(f'{self.base}/withdrawals/{item.id}/decide/', {'approve': False, 'note': 'Please stay, we have a spot'}, format='json')
        self.team.refresh_from_db()
        self.assertEqual(self.team.status, TeamStatus.REGISTERED)
        item.refresh_from_db()
        self.assertEqual((item.status, item.decision_note, item.decided_by), (WithdrawalStatus.DENIED, 'Please stay, we have a spot', self.admin))
        mine = _api(self.user).get(f'{self.base}/my-team/').data['withdrawal_request']
        self.assertEqual((mine['status'], mine['decision_note']), ('DENIED', 'Please stay, we have a spot'))

    def test_a_request_can_only_be_decided_once(self):
        self.request()
        item = WithdrawalRequest.objects.get()
        url = f'{self.base}/withdrawals/{item.id}/decide/'
        _api(self.admin).post(url, {'approve': False}, format='json')
        self.assertEqual(_api(self.admin).post(url, {'approve': True}, format='json').data['code'], 'ALREADY_DECIDED')

    def test_approving_is_refused_when_other_people_joined_meanwhile(self):
        self.request()
        TeamMember.objects.create(team=self.team, user=make_user())
        item = WithdrawalRequest.objects.get()
        refused = _api(self.admin).post(f'{self.base}/withdrawals/{item.id}/decide/', {'approve': True}, format='json')
        self.assertEqual(refused.data['code'], 'TEAM_NOT_EMPTY')
        self.team.refresh_from_db()
        self.assertEqual(self.team.status, TeamStatus.REGISTERED)

    def test_an_organizer_changing_the_status_closes_the_waiting_request(self):
        self.request()
        _api(self.admin).post(f'/api/hackathons/teams/{self.team.id}/admin-set-status/', {'status': 'DISQUALIFIED', 'reason': 'rules'}, format='json')
        item = WithdrawalRequest.objects.get()
        self.assertEqual(item.status, WithdrawalStatus.CANCELLED)
        self.assertEqual(_api(self.admin).post(f'{self.base}/withdrawals/{item.id}/decide/', {'approve': True}, format='json').data['code'], 'ALREADY_DECIDED')

    def test_the_decision_must_say_approve_or_deny(self):
        self.request()
        item = WithdrawalRequest.objects.get()
        for body in ({}, {'approve': 'yes'}, {'approve': None}):
            self.assertEqual(_api(self.admin).post(f'{self.base}/withdrawals/{item.id}/decide/', body, format='json').data['code'], 'INVALID')

    def test_organizers_with_the_teams_permission_are_told_when_a_request_arrives(self):
        organizer = make_user()
        HackathonStaff.objects.create(hackathon=self.hack, user=organizer, role='ORGANIZER')
        volunteer = make_user()
        HackathonStaff.objects.create(hackathon=self.hack, user=volunteer, role='VOLUNTEER')
        from apps.notifications.models import Notification
        with self.captureOnCommitCallbacks(execute=True):
            self.request()
        self.assertTrue(Notification.objects.filter(recipient=organizer, title__contains='wants to withdraw').exists())
        self.assertFalse(Notification.objects.filter(recipient=volunteer, title__contains='wants to withdraw').exists())


class OrganizerViewTests(WithdrawalBase):
    min_team_size = 1

    def test_overview_lists_waiting_requests_recent_decisions_and_repeat_creators(self):
        team = make_team(self.hack, self.user, name='Solo')
        self.leave(team.id)
        repeat = make_user(first_name='Rep', last_name='Eater')
        for index in range(MAX_SELF_CREATED_TEAMS):
            Team.objects.create(
                hackathon=self.hack, name=f'Rep {index}', status=TeamStatus.WITHDRAWN, created_by=repeat, withdrawn_by=repeat,
                withdrawn_at=timezone.now() - timezone.timedelta(days=1),
            )
        data = _api(self.admin).get(f'{self.base}/withdrawals/').data
        self.assertEqual(data['limits'], {'max_teams': MAX_SELF_CREATED_TEAMS, 'cooldown_minutes': 10})
        self.assertEqual(len(data['pending']), 1)
        self.assertEqual((data['pending'][0]['team_name'], data['pending'][0]['reason']), ('Solo', 'Changed my plans'))
        rows = {r['email']: r for r in data['repeaters']}
        self.assertEqual(list(rows), [repeat.email])
        self.assertEqual((rows[repeat.email]['teams_created'], rows[repeat.email]['teams_withdrawn'], rows[repeat.email]['at_limit']), (2, 2, True))
        item = WithdrawalRequest.objects.get()
        _api(self.admin).post(f'{self.base}/withdrawals/{item.id}/decide/', {'approve': False, 'note': 'No'}, format='json')
        decided = _api(self.admin).get(f'{self.base}/withdrawals/').data
        self.assertEqual((len(decided['pending']), decided['decided'][0]['status'], decided['decided'][0]['decided_by']), (0, 'DENIED', f'{self.admin.first_name} {self.admin.last_name}'.strip()))
        TeamAllowance.objects.create(hackathon=self.hack, user=repeat, extra=1)
        again = {r['email']: r for r in _api(self.admin).get(f'{self.base}/withdrawals/').data['repeaters']}
        self.assertEqual((again[repeat.email]['extra_allowed'], again[repeat.email]['at_limit']), (1, False))

    def test_only_organizers_with_the_teams_permission_see_or_decide_anything(self):
        team = make_team(self.hack, self.user, name='Solo')
        self.leave(team.id)
        item = WithdrawalRequest.objects.get()
        outsider = make_user()
        volunteer = make_user()
        HackathonStaff.objects.create(hackathon=self.hack, user=volunteer, role='VOLUNTEER')
        judge = make_user()
        HackathonStaff.objects.create(hackathon=self.hack, user=judge, role='JUDGE')
        for who in (self.user, outsider, volunteer, judge):
            self.assertEqual(_api(who).get(f'{self.base}/withdrawals/').status_code, 403)
            self.assertEqual(_api(who).post(f'{self.base}/withdrawals/{item.id}/decide/', {'approve': True}, format='json').status_code, 403)
            self.assertEqual(_api(who).post(f'{self.base}/team-allowances/', {'user_id': who.id}, format='json').status_code, 403)
        self.assertEqual(_api().get(f'{self.base}/withdrawals/').status_code, 401)
        organizer = make_user()
        HackathonStaff.objects.create(hackathon=self.hack, user=organizer, role='ORGANIZER')
        self.assertEqual(_api(organizer).get(f'{self.base}/withdrawals/').status_code, 200)

    def test_a_request_of_another_hackathon_cannot_be_decided_here(self):
        other = make_hackathon(min_team_size=1)
        team = make_team(other, self.user, name='Elsewhere')
        self.leave(team.id)
        item = WithdrawalRequest.objects.get()
        self.assertEqual(_api(self.admin).post(f'{self.base}/withdrawals/{item.id}/decide/', {'approve': True}, format='json').status_code, 404)

    def test_allowance_input_is_validated_and_clamped(self):
        url = f'{self.base}/team-allowances/'
        self.assertEqual(_api(self.admin).post(url, {}, format='json').data['code'], 'INVALID')
        self.assertEqual(_api(self.admin).post(url, {'user_id': 'abc'}, format='json').data['code'], 'INVALID')
        self.assertEqual(_api(self.admin).post(url, {'user_id': 999999}, format='json').status_code, 404)
        self.assertEqual(_api(self.admin).post(url, {'user_id': self.user.id, 'extra': 'many'}, format='json').data['code'], 'INVALID')
        self.assertEqual(_api(self.admin).post(url, {'user_id': self.user.id, 'extra': 500}, format='json').data['extra'], 10)
        self.assertEqual(_api(self.admin).post(url, {'user_id': self.user.id, 'extra': 0}, format='json').data['extra'], 1)
        self.assertEqual(TeamAllowance.objects.filter(user=self.user).count(), 1)

    def test_my_team_has_no_withdrawal_request_until_one_exists_and_hides_cancelled_ones(self):
        team = make_team(self.hack, self.user, name='Solo')
        self.assertIsNone(_api(self.user).get(f'{self.base}/my-team/').data['withdrawal_request'])
        self.leave(team.id)
        self.assertEqual(_api(self.user).get(f'{self.base}/my-team/').data['withdrawal_request']['status'], 'PENDING')
        _api(self.user).post(f'/api/hackathons/teams/{team.id}/cancel-withdrawal/', {}, format='json')
        self.assertIsNone(_api(self.user).get(f'{self.base}/my-team/').data['withdrawal_request'])
