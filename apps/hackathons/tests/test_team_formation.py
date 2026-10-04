"""Team creation, invites, leader-only edits and membership rules."""
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.hackathons.models import (
    HackathonStatus, InviteStatus, Team, TeamInvite, TeamMember, TeamStatus,
)
from .helpers import make_hackathon, make_ps, make_team, make_user


class TeamCreationTests(APITestCase):
    def setUp(self):
        self.hack = make_hackathon()
        self.ps = make_ps(self.hack)
        self.user = make_user()
        self.client.force_authenticate(self.user)

    def _create(self, **body):
        payload = {'name': 'Bug Slayers', 'problem_statement': self.ps.id}
        payload.update(body)
        return self.client.post(f'/api/hackathons/{self.hack.slug}/teams/', payload, format='json')

    def test_create_team_makes_caller_leader_and_member(self):
        resp = self._create()
        self.assertEqual(resp.status_code, 201, resp.data)
        team = Team.objects.get(pk=resp.data['id'])
        self.assertEqual(team.leader, self.user)
        self.assertEqual(team.memberships.get().role, 'LEADER')
        self.assertEqual(team.status, TeamStatus.FORMING)  # min_team_size=2

    def test_single_member_team_is_registered_immediately(self):
        self.hack.min_team_size = 1
        self.hack.save()
        resp = self._create()
        self.assertEqual(resp.data['status'], TeamStatus.REGISTERED)

    def test_anonymous_cannot_create(self):
        self.client.force_authenticate(None)
        self.assertIn(self._create().status_code, (401, 403))

    def test_closed_hackathon_rejects(self):
        self.hack.status = HackathonStatus.CLOSED
        self.hack.save()
        resp = self._create()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'REGISTRATION_CLOSED')

    def test_registration_window_enforced(self):
        self.hack.registration_closes_at = timezone.now() - timezone.timedelta(minutes=1)
        self.hack.save()
        self.assertEqual(self._create().data['code'], 'REGISTRATION_CLOSED')
        self.hack.registration_closes_at = None
        self.hack.registration_opens_at = timezone.now() + timezone.timedelta(days=1)
        self.hack.save()
        self.assertEqual(self._create().data['code'], 'REGISTRATION_CLOSED')

    def test_cannot_create_second_team(self):
        self.assertEqual(self._create().status_code, 201)
        resp = self._create(name='Another')
        self.assertEqual(resp.data['code'], 'ALREADY_IN_TEAM')

    def test_duplicate_name_case_insensitive(self):
        make_team(self.hack, make_user(), name='bug slayers')
        self.assertEqual(self._create().data['code'], 'NAME_TAKEN')

    def test_problem_statement_required_when_hackathon_has_them(self):
        resp = self._create(problem_statement=None)
        self.assertEqual(resp.data['code'], 'PROBLEM_STATEMENT_REQUIRED')

    def test_problem_statement_capacity(self):
        self.ps.max_teams = 1
        self.ps.save()
        make_team(self.hack, make_user(), ps=self.ps)
        self.assertEqual(self._create().data['code'], 'PROBLEM_STATEMENT_FULL')

    def test_inactive_or_foreign_problem_statement_rejected(self):
        other = make_ps(make_hackathon(), code='X')
        self.assertEqual(self._create(problem_statement=other.id).data['code'], 'PROBLEM_STATEMENT_INVALID')
        self.ps.is_active = False
        self.ps.save()
        self.assertEqual(self._create().data['code'], 'PROBLEM_STATEMENT_INVALID')

    def test_incomplete_profile_blocks(self):
        self.hack.required_profile_fields = ['phone_number', 'roll_number']
        self.hack.save()
        resp = self._create()
        self.assertEqual(resp.data['code'], 'PROFILE_INCOMPLETE')
        self.assertIn('Roll Number', resp.data['detail'])


class InviteFlowTests(APITestCase):
    def setUp(self):
        self.hack = make_hackathon()
        self.leader = make_user()
        self.friend = make_user()
        self.team = make_team(self.hack, self.leader, status=TeamStatus.FORMING)

    def _invite(self, email, actor=None):
        self.client.force_authenticate(actor or self.leader)
        return self.client.post(f'/api/hackathons/teams/{self.team.id}/invite/', {'email': email}, format='json')

    def _respond(self, invite_id, decision, user):
        self.client.force_authenticate(user)
        return self.client.post(f'/api/hackathons/invites/{invite_id}/{decision}/', {}, format='json')

    def test_invite_accept_registers_team(self):
        resp = self._invite(self.friend.email.upper())
        self.assertEqual(resp.status_code, 201, resp.data)
        invite_id = resp.data['invite']['id']
        resp = self._respond(invite_id, 'accept', self.friend)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.team.refresh_from_db()
        self.assertEqual(self.team.status, TeamStatus.REGISTERED)
        self.assertTrue(TeamMember.objects.filter(team=self.team, user=self.friend, role='MEMBER').exists())

    def test_decline(self):
        invite_id = self._invite(self.friend.email).data['invite']['id']
        self._respond(invite_id, 'decline', self.friend)
        self.assertEqual(TeamInvite.objects.get(pk=invite_id).status, InviteStatus.DECLINED)
        self.assertFalse(TeamMember.objects.filter(user=self.friend).exists())

    def test_only_invitee_can_respond(self):
        invite_id = self._invite(self.friend.email).data['invite']['id']
        self.assertEqual(self._respond(invite_id, 'accept', make_user()).status_code, 403)

    def test_member_cannot_invite(self):
        member = make_user()
        TeamMember.objects.create(team=self.team, user=member)
        self.assertEqual(self._invite(self.friend.email, actor=member).status_code, 403)

    def test_outsider_gets_404_on_team(self):
        self.assertEqual(self._invite(self.friend.email, actor=make_user()).status_code, 404)

    def test_unknown_email(self):
        self.assertEqual(self._invite('nobody@x.com').data['code'], 'USER_NOT_FOUND')

    def test_duplicate_pending_invite(self):
        self._invite(self.friend.email)
        self.assertEqual(self._invite(self.friend.email).data['code'], 'CANNOT_INVITE')

    def test_size_limit_counts_pending_invites(self):
        # max 3: leader + 2 pending fills it.
        self._invite(self.friend.email)
        self._invite(make_user().email)
        resp = self._invite(make_user().email)
        self.assertEqual(resp.data['code'], 'CANNOT_INVITE')
        self.assertIn('full', resp.data['detail'])

    def test_cannot_invite_someone_in_another_team(self):
        make_team(self.hack, make_user(), self.friend)
        self.assertEqual(self._invite(self.friend.email).data['code'], 'CANNOT_INVITE')

    def test_accepting_cancels_other_pending_invites(self):
        other_team = make_team(self.hack, make_user(), status=TeamStatus.FORMING, name='Other')
        first = self._invite(self.friend.email).data['invite']['id']
        self.client.force_authenticate(other_team.leader)
        second = self.client.post(
            f'/api/hackathons/teams/{other_team.id}/invite/', {'email': self.friend.email}, format='json',
        ).data['invite']['id']
        self._respond(first, 'accept', self.friend)
        self.assertEqual(TeamInvite.objects.get(pk=second).status, InviteStatus.CANCELLED)
        self.assertEqual(self._respond(second, 'accept', self.friend).data['code'], 'INVITE_NOT_PENDING')

    def test_accept_rechecks_one_team_rule(self):
        invite_id = self._invite(self.friend.email).data['invite']['id']
        # Joined some other team in the meantime (e.g. admin override).
        make_team(self.hack, make_user(), self.friend, name='Elsewhere')
        self.assertEqual(self._respond(invite_id, 'accept', self.friend).data['code'], 'ALREADY_IN_TEAM')

    def test_accept_rechecks_profile(self):
        self.hack.required_profile_fields = ['roll_number']
        self.hack.save()
        self.leader.roll_number = '21B91A0501'
        self.leader.save()
        invite_id = self._invite(self.friend.email).data['invite']['id']
        self.assertEqual(self._respond(invite_id, 'accept', self.friend).data['code'], 'PROFILE_INCOMPLETE')

    def test_accept_after_registration_closed(self):
        invite_id = self._invite(self.friend.email).data['invite']['id']
        self.hack.status = HackathonStatus.CLOSED
        self.hack.save()
        self.assertEqual(self._respond(invite_id, 'accept', self.friend).data['code'], 'REGISTRATION_CLOSED')

    def test_leader_can_cancel_invite(self):
        invite_id = self._invite(self.friend.email).data['invite']['id']
        resp = self.client.post(f'/api/hackathons/teams/{self.team.id}/cancel-invite/', {'invite_id': invite_id}, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(TeamInvite.objects.get(pk=invite_id).status, InviteStatus.CANCELLED)

    def test_creating_own_team_cancels_pending_invites(self):
        invite_id = self._invite(self.friend.email).data['invite']['id']
        self.hack.min_team_size = 1
        self.hack.save()
        self.client.force_authenticate(self.friend)
        self.client.post(f'/api/hackathons/{self.hack.slug}/teams/', {'name': 'Solo'}, format='json')
        self.assertEqual(TeamInvite.objects.get(pk=invite_id).status, InviteStatus.CANCELLED)


class LeaderOnlyEditTests(APITestCase):
    def setUp(self):
        self.hack = make_hackathon()
        self.ps1 = make_ps(self.hack, 'A')
        self.ps2 = make_ps(self.hack, 'B')
        self.leader = make_user()
        self.member = make_user()
        self.team = make_team(self.hack, self.leader, self.member, ps=self.ps1)

    def _patch(self, user, body):
        self.client.force_authenticate(user)
        return self.client.patch(f'/api/hackathons/teams/{self.team.id}/', body, format='json')

    def test_leader_can_rename_and_change_ps(self):
        resp = self._patch(self.leader, {'name': 'Renamed', 'problem_statement': self.ps2.id})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.team.refresh_from_db()
        self.assertEqual((self.team.name, self.team.problem_statement), ('Renamed', self.ps2))

    def test_member_cannot_edit(self):
        self.assertEqual(self._patch(self.member, {'name': 'Hijack'}).status_code, 403)

    def test_member_can_view(self):
        self.client.force_authenticate(self.member)
        resp = self.client.get(f'/api/hackathons/teams/{self.team.id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('phone_number', resp.data['members'][0])

    def test_edits_locked(self):
        self.hack.team_edits_locked = True
        self.hack.save()
        self.assertEqual(self._patch(self.leader, {'name': 'X'}).data['code'], 'TEAM_EDITS_LOCKED')

    def test_admin_bypasses_lock_and_sees_contacts(self):
        self.hack.team_edits_locked = True
        self.hack.save()
        resp = self._patch(make_user(role='ADMIN'), {'name': 'Admin Renamed'})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertIn('phone_number', resp.data['members'][0])

    def test_remove_member_and_status_recomputed(self):
        self.client.force_authenticate(self.leader)
        resp = self.client.post(f'/api/hackathons/teams/{self.team.id}/remove-member/', {'user_id': self.member.id}, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.team.refresh_from_db()
        self.assertEqual(self.team.status, TeamStatus.FORMING)

    def test_cannot_remove_leader(self):
        self.client.force_authenticate(self.leader)
        resp = self.client.post(f'/api/hackathons/teams/{self.team.id}/remove-member/', {'user_id': self.leader.id}, format='json')
        self.assertEqual(resp.data['code'], 'CANNOT_REMOVE_LEADER')

    def test_member_can_leave(self):
        self.client.force_authenticate(self.member)
        resp = self.client.post(f'/api/hackathons/teams/{self.team.id}/leave/', {}, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertFalse(TeamMember.objects.filter(user=self.member).exists())

    def test_leader_must_transfer_before_leaving(self):
        self.client.force_authenticate(self.leader)
        resp = self.client.post(f'/api/hackathons/teams/{self.team.id}/leave/', {}, format='json')
        self.assertEqual(resp.data['code'], 'LEADER_MUST_TRANSFER')

    def test_transfer_leadership(self):
        self.client.force_authenticate(self.leader)
        resp = self.client.post(f'/api/hackathons/teams/{self.team.id}/transfer-leadership/', {'user_id': self.member.id}, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.team.refresh_from_db()
        self.assertEqual(self.team.leader, self.member)
        self.assertEqual(TeamMember.objects.get(team=self.team, user=self.leader).role, 'MEMBER')
        self.assertEqual(TeamMember.objects.get(team=self.team, user=self.member).role, 'LEADER')

    def test_sole_leader_leaving_withdraws_team(self):
        TeamMember.objects.filter(user=self.member).delete()
        self.client.force_authenticate(self.leader)
        self.client.post(f'/api/hackathons/teams/{self.team.id}/leave/', {}, format='json')
        self.team.refresh_from_db()
        self.assertEqual(self.team.status, TeamStatus.WITHDRAWN)
        self.assertIsNone(self.team.leader)


class VisibilityAndLookupTests(APITestCase):
    def setUp(self):
        self.hack = make_hackathon()
        self.leader = make_user()
        self.team = make_team(self.hack, self.leader, status=TeamStatus.FORMING)

    def test_non_admin_cannot_list_all_teams(self):
        self.client.force_authenticate(self.leader)
        self.assertEqual(self.client.get(f'/api/hackathons/{self.hack.slug}/teams/').status_code, 403)

    def test_admin_lists_teams_with_filters(self):
        make_team(self.hack, make_user(), name='Zeta', status=TeamStatus.REGISTERED)
        self.client.force_authenticate(make_user(role='ADMIN'))
        resp = self.client.get(f'/api/hackathons/{self.hack.slug}/teams/', {'status': 'REGISTERED'})
        self.assertEqual([t['name'] for t in resp.data], ['Zeta'])
        resp = self.client.get(f'/api/hackathons/{self.hack.slug}/teams/', {'search': self.leader.email})
        self.assertEqual([t['id'] for t in resp.data], [self.team.id])

    def test_lookup_returns_minimal_fields(self):
        target = make_user(roll_number='21B91A0599')
        self.client.force_authenticate(self.leader)
        resp = self.client.get(f'/api/hackathons/{self.hack.slug}/user-lookup/', {'email': target.email})
        self.assertTrue(resp.data['can_invite'])
        self.assertNotIn('phone_number', resp.data)
        self.assertNotIn('roll_number', resp.data)

    def test_lookup_requires_auth_and_full_email(self):
        resp = self.client.get(f'/api/hackathons/{self.hack.slug}/user-lookup/', {'email': 'a@b.c'})
        self.assertIn(resp.status_code, (401, 403))
        self.client.force_authenticate(self.leader)
        resp = self.client.get(f'/api/hackathons/{self.hack.slug}/user-lookup/', {'email': 'user'})
        self.assertEqual(resp.data['code'], 'EMAIL_REQUIRED')

    def test_my_team_payload(self):
        self.client.force_authenticate(self.leader)
        resp = self.client.get(f'/api/hackathons/{self.hack.slug}/my-team/')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['is_leader'])
        self.assertEqual(resp.data['team']['id'], self.team.id)

        outsider = make_user()
        TeamInvite.objects.create(team=self.team, hackathon=self.hack, invited_user=outsider, invited_by=self.leader)
        self.client.force_authenticate(outsider)
        resp = self.client.get(f'/api/hackathons/{self.hack.slug}/my-team/')
        self.assertIsNone(resp.data['team'])
        self.assertEqual(len(resp.data['invites']), 1)
        self.assertEqual(len(self.client.get('/api/hackathons/my-invites/').data), 1)

    def test_admin_override_add_member_and_status(self):
        admin = make_user(role='ADMIN')
        self.client.force_authenticate(admin)
        extra = make_user()
        resp = self.client.post(f'/api/hackathons/teams/{self.team.id}/admin-add-member/', {'email': extra.email}, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['status'], TeamStatus.REGISTERED)
        resp = self.client.post(f'/api/hackathons/teams/{self.team.id}/admin-set-status/', {'status': 'DISQUALIFIED'}, format='json')
        self.assertEqual(resp.data['status'], 'DISQUALIFIED')

    def test_non_admin_cannot_use_admin_actions(self):
        self.client.force_authenticate(self.leader)
        resp = self.client.post(f'/api/hackathons/teams/{self.team.id}/admin-set-status/', {'status': 'REGISTERED'}, format='json')
        self.assertEqual(resp.status_code, 403)


class ProblemStatementApiTests(APITestCase):
    def setUp(self):
        self.hack = make_hackathon()
        self.admin = make_user(role='ADMIN')

    def test_admin_crud_and_public_sees_active_only(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.post(f'/api/hackathons/{self.hack.slug}/problem-statements/',
                                {'title': 'Smart Campus', 'description': 'Automate attendance.', 'domain': 'EdTech',
                                 'max_teams': 2, 'tags': ['iot']}, format='json')
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data['code'], 'PS-001')
        self.assertEqual(resp.data['domain'], 'EdTech')
        self.assertEqual(resp.data['slots_left'], 2)
        hidden = make_ps(self.hack, 'OFF', is_active=False)

        self.client.force_authenticate(None)
        codes = [p['code'] for p in self.client.get(f'/api/hackathons/{self.hack.slug}/problem-statements/').data]
        self.assertEqual(codes, ['PS-001'])
        anonymous_post = self.client.post(f'/api/hackathons/{self.hack.slug}/problem-statements/',
                                          {'title': 'Z', 'description': 'z', 'domain': 'z'}, format='json')
        self.assertIn(anonymous_post.status_code, (401, 403))

        self.client.force_authenticate(self.admin)
        resp = self.client.delete(f'/api/hackathons/{self.hack.slug}/problem-statements/{hidden.id}/')
        self.assertEqual(resp.data, {'deleted': True, 'id': hidden.id})

    def test_cannot_delete_ps_in_use(self):
        ps = make_ps(self.hack)
        make_team(self.hack, make_user(), ps=ps)
        self.client.force_authenticate(self.admin)
        resp = self.client.delete(f'/api/hackathons/{self.hack.slug}/problem-statements/{ps.id}/')
        self.assertEqual(resp.data['code'], 'PROBLEM_STATEMENT_IN_USE')

    def test_deleting_hackathon_cascades_full_graph(self):
        """Regression: Team.problem_statement used to be PROTECT, which made
        deleting any hackathon whose teams had picked a statement fail."""
        from apps.hackathons.models import (
            Hackathon, HackathonAnnouncement, ProblemStatement, Round, RoundEntry,
        )
        ps = make_ps(self.hack)
        leader = make_user()
        team = make_team(self.hack, leader, make_user(), ps=ps)
        TeamInvite.objects.create(team=team, hackathon=self.hack, invited_user=make_user(), invited_by=leader)
        rnd = Round.objects.create(hackathon=self.hack, order=1, name='R1')
        RoundEntry.objects.create(round=rnd, team=team)
        HackathonAnnouncement.objects.create(hackathon=self.hack, title='t', message='m', audience='TEAMS').target_teams.add(team)

        self.client.force_authenticate(self.admin)
        resp = self.client.delete(f'/api/hackathons/{self.hack.slug}/')
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Hackathon.objects.filter(pk=self.hack.pk).exists())
        for model in (Team, TeamMember, TeamInvite, ProblemStatement, Round, RoundEntry, HackathonAnnouncement):
            self.assertFalse(model.objects.exists(), model.__name__)

    def test_settings_validation(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.patch(f'/api/hackathons/{self.hack.slug}/', {'min_team_size': 4, 'max_team_size': 2}, format='json')
        self.assertEqual(resp.status_code, 400)
        resp = self.client.patch(f'/api/hackathons/{self.hack.slug}/', {'required_profile_fields': ['nope']}, format='json')
        self.assertEqual(resp.status_code, 400)
        resp = self.client.patch(f'/api/hackathons/{self.hack.slug}/',
                                 {'required_profile_fields': ['phone_number', 'branch'], 'max_team_size': 5}, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['required_profile_fields'], ['phone_number', 'branch'])
