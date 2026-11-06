"""Phase 3 of round judging: the API, who may call what, and what a judge can and cannot see."""
import json
from datetime import timedelta
from unittest import mock

from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.accounts.models import PasswordSetupToken
from apps.hackathons import judge_invites
from apps.hackathons.models import HackathonStaff, IdeaSubmission, Submission
from apps.hackathons.services import RoundService
from .helpers import make_hackathon, make_ps, make_team, make_user

RUBRIC = [{'name': 'Idea', 'max_score': 10, 'weight': 3}, {'name': 'Demo', 'max_score': 10, 'weight': 1}]


def api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


class Base(APITestCase):
    def setUp(self):
        self.hack = make_hackathon(min_team_size=1, features={'round_judging': True, 'judging': True, 'ideas': True})
        self.admin = make_user(role='ADMIN')
        self.judges = []
        for number in range(3):
            judge = make_user(first_name=f'Judge{number}')
            HackathonStaff.objects.create(hackathon=self.hack, user=judge, role='JUDGE', permissions=['judge'])
            self.judges.append(judge)
        ps = make_ps(self.hack, title='Smart bins', description='Bins that report fill levels', domain='IoT')
        self.teams = [make_team(self.hack, make_user(), name=f'Secret Team {n}', ps=ps) for n in range(3)]
        self.round = RoundService.create_round(self.hack, self.admin, name='Semi-final')
        self.base = f'/api/hackathons/{self.hack.slug}'
        self.url = f'{self.base}/rounds/{self.round.id}/evaluation/'
        self.entries = {e.team.name: e for e in self.round.entries.select_related('team')}

    def organizer(self, permissions):
        user = make_user()
        HackathonStaff.objects.create(hackathon=self.hack, user=user, role='ORGANIZER', permissions=permissions)
        return user

    def set_up(self, **settings):
        client = api(self.admin)
        body = {'evaluation_mode': 'JUDGED', 'assignment_mode': 'ALL', **settings}
        self.assertEqual(client.patch(self.url, body, format='json').status_code, 200)
        self.assertEqual(client.put(f'{self.url}criteria/', {'criteria': RUBRIC}, format='json').status_code, 200)
        reply = client.put(f'{self.url}panel/', {'judge_ids': [j.id for j in self.judges]}, format='json')
        self.assertEqual(reply.status_code, 200, reply.data)
        return client

    def start(self, **settings):
        client = self.set_up(**settings)
        reply = client.post(f'{self.url}start/')
        self.assertEqual(reply.status_code, 200, reply.data)
        return client

    def my(self, round_id=None):
        return f'{self.base}/my-judging/{round_id or self.round.id}/'

    def scores(self, idea=8, demo=6):
        a, b = [c['id'] for c in api(self.admin).get(self.url).data['criteria']]
        return {str(a): idea, str(b): demo}


class PermissionMatrix(Base):
    ORGANIZER_CALLS = (
        ('get', '', None), ('patch', '', {'blind': True}), ('put', 'criteria/', {'criteria': []}), ('put', 'panel/', {'judge_ids': []}),
        ('post', 'assign/', {}), ('post', 'start/', {}), ('get', 'progress/', None), ('get', 'results/', None),
    )

    def call(self, user, method, suffix, body):
        client = api(user)
        return getattr(client, method)(f'{self.url}{suffix}', body, format='json') if body is not None else getattr(client, method)(f'{self.url}{suffix}')

    def test_site_admins_and_organizers_with_judging_may_set_a_round_up(self):
        for user in (self.admin, self.organizer(['judging'])):
            self.assertEqual(self.call(user, 'get', '', None).status_code, 200)

    def test_everyone_else_is_turned_away_from_every_organizer_route(self):
        outsiders = {
            'organizer without judging': self.organizer(['rounds', 'teams']), 'judge': self.judges[0], 'volunteer': make_user(),
            'participant': self.teams[0].leader, 'stranger': make_user(),
        }
        HackathonStaff.objects.create(hackathon=self.hack, user=outsiders['volunteer'], role='VOLUNTEER', permissions=['check_in'])
        for who, user in outsiders.items():
            for method, suffix, body in self.ORGANIZER_CALLS:
                self.assertEqual(self.call(user, method, suffix, body).status_code, 403, f'{who} {method} {suffix}')
        for method, suffix, body in self.ORGANIZER_CALLS:
            self.assertEqual(self.call(None, method, suffix, body).status_code, 401, f'anonymous {method} {suffix}')

    def test_an_unknown_round_or_action_is_a_404(self):
        client = api(self.admin)
        self.assertEqual(client.get(f'{self.base}/rounds/99999/evaluation/').status_code, 404)
        self.assertEqual(client.post(f'{self.url}explode/').status_code, 404)

    def test_the_older_round_actions_still_work(self):
        reply = api(self.admin).post(f'{self.base}/rounds/{self.round.id}/populate/')
        self.assertEqual(reply.status_code, 200)

    def test_the_round_list_says_which_rounds_are_judged(self):
        api(self.admin).patch(self.url, {'evaluation_mode': 'JUDGED'}, format='json')
        rows = api(self.admin).get(f'{self.base}/rounds/').data
        self.assertEqual((rows[0]['evaluation_mode'], rows[0]['evaluation_status']), ('JUDGED', 'NOT_STARTED'))


class OrganizerFlow(Base):
    def test_the_start_checklist_fills_in_as_the_round_is_set_up(self):
        client = api(self.admin)
        client.patch(self.url, {'evaluation_mode': 'JUDGED', 'assignment_mode': 'ALL'}, format='json')
        data = client.get(self.url).data
        self.assertEqual((data['can_start'], data['criteria_source'], data['enabled']), (False, 'hackathon', True))
        self.assertEqual({c['key']: c['ok'] for c in data['readiness']}, {'teams': True, 'judges': False, 'rubric': False, 'coverage': False, 'deadline': True})
        client.put(f'{self.url}criteria/', {'criteria': RUBRIC}, format='json')
        data = client.put(f'{self.url}panel/', {'judge_ids': [j.id for j in self.judges]}, format='json').data
        self.assertEqual((data['can_start'], data['criteria_source'], len(data['panel'])), (True, 'round', 3))

    def test_the_whole_run_from_setup_to_a_ranked_close(self):
        client = self.start()
        evaluation = client.get(self.url).data
        self.assertEqual((evaluation['status'], evaluation['coverage']['assignments']), ('IN_PROGRESS', 9))
        names = list(self.entries)
        for number, judge in enumerate(self.judges):
            for rank, name in enumerate(names):
                reply = api(judge).put(f'{self.my()}teams/{self.entries[name].id}/', {'scores': self.scores(10 - rank * 3, 10 - rank * 3), 'comment': f'j{number}'}, format='json')
                self.assertEqual(reply.status_code, 200, reply.data)
        progress = client.get(f'{self.url}progress/').data
        self.assertEqual(progress['totals'], {'assigned': 9, 'scored': 9, 'pending': 0})
        closed = client.post(f'{self.url}close/').data
        self.assertEqual((closed['ranked'], closed['evaluation']['status']), (3, 'CLOSED'))
        rows = client.get(f'{self.url}results/').data['rows']
        self.assertEqual([(r['team_name'], r['rank'], r['review_count']) for r in rows], [(names[0], 1, 3), (names[1], 2, 3), (names[2], 3, 3)])
        self.assertEqual(len(rows[0]['per_judge']), 3)
        reopened = client.post(f'{self.url}reopen/', {'reason': 'Recheck'}, format='json').data
        self.assertEqual(reopened['evaluation']['status'], 'IN_PROGRESS')

    def test_errors_come_back_as_a_code_and_a_field(self):
        client = api(self.admin)
        client.patch(self.url, {'evaluation_mode': 'JUDGED'}, format='json')
        bad = client.patch(self.url, {'judges_per_team': 0}, format='json')
        self.assertEqual((bad.status_code, bad.data['code'], bad.data['field']), (400, 'INVALID', 'judges_per_team'))
        early = client.post(f'{self.url}start/')
        self.assertEqual((early.status_code, early.data['code']), (400, 'NO_JUDGES'))
        self.assertEqual(client.post(f'{self.url}close/').data['code'], 'NOT_SCORING')

    def test_manual_assignment_through_the_api(self):
        client = self.set_up(assignment_mode='MANUAL')
        manual = [{'entry_id': e.id, 'judge_ids': [self.judges[i % 3].id]} for i, e in enumerate(self.entries.values())]
        reply = client.post(f'{self.url}assign/', {'manual': manual}, format='json')
        self.assertEqual((reply.status_code, reply.data['result']['assignments'], reply.data['result']['short']), (200, 3, []))
        self.assertEqual(len(reply.data['progress']['teams']), 3)

    def test_a_deadline_can_be_moved_while_judging_runs(self):
        client = self.start()
        later = (timezone.now() + timedelta(days=1)).isoformat()
        self.assertEqual(client.patch(self.url, {'scoring_deadline': later}, format='json').status_code, 200)
        self.assertEqual(client.patch(self.url, {'blind': True}, format='json').data['code'], 'EVALUATION_STARTED')

    def test_the_feature_switch_gates_the_setup_calls(self):
        self.hack.features = {}
        self.hack.save()
        client = api(self.admin)
        self.assertFalse(client.get(self.url).data['enabled'])
        self.assertEqual(client.patch(self.url, {'blind': True}, format='json').data['code'], 'FEATURE_OFF')


class WhatAJudgeSees(Base):
    def setUp(self):
        super().setUp()
        self.idea = IdeaSubmission.objects.create(team=self.teams[0], title='Bin Buddy', problem='Overflowing bins', solution='Sensors', plan='Week 1', tech_stack=['IoT'])
        Submission.objects.create(team=self.teams[0], project_title='Bin Buddy v1', tagline='Smarter bins', description='Built it', repo_url='https://github.com/x/y')

    def test_a_judge_lists_only_the_rounds_they_sit_on_with_their_own_counts(self):
        self.start(assignment_mode='BALANCED', judges_per_team=1)
        mine = api(self.judges[0]).get(f'{self.base}/my-judging/').data
        self.assertEqual(len(mine['rounds']), 1)
        row = mine['rounds'][0]
        self.assertEqual((row['status'], row['assigned'], row['scored'], row['can_score']), ('IN_PROGRESS', 1, 0, True))
        self.assertEqual(api(make_user()).get(f'{self.base}/my-judging/').data['rounds'], [])
        self.assertEqual(api(self.teams[0].leader).get(f'{self.base}/my-judging/').data['rounds'], [])

    def test_only_the_teams_they_were_given_and_never_others(self):
        self.start(assignment_mode='BALANCED', judges_per_team=1)
        mine = api(self.judges[0]).get(self.my()).data
        given = {t['entry_id'] for t in mine['teams']}
        self.assertEqual(len(given), 1)
        others = [e.id for e in self.entries.values() if e.id not in given]
        for entry_id in others:
            self.assertEqual(api(self.judges[0]).get(f'{self.my()}teams/{entry_id}/').status_code, 404)
            self.assertEqual(api(self.judges[0]).put(f'{self.my()}teams/{entry_id}/', {'scores': self.scores()}, format='json').status_code, 403)

    def test_a_judge_off_the_panel_or_a_participant_is_turned_away(self):
        self.start()
        stranger = make_user()
        HackathonStaff.objects.create(hackathon=self.hack, user=stranger, role='JUDGE', permissions=['judge'])
        for user in (stranger, self.teams[0].leader, make_user()):
            self.assertEqual(api(user).get(self.my()).status_code, 403)
        self.assertEqual(api().get(self.my()).status_code, 401)

    def test_nothing_about_teams_is_shown_before_judging_starts(self):
        self.set_up()
        data = api(self.judges[0]).get(self.my()).data
        self.assertEqual((data['status'], data['teams']), ('NOT_STARTED', []))
        self.assertEqual(api(self.judges[0]).get(f'{self.my()}teams/{self.entries["Secret Team 0"].id}/').data['code'], 'NOT_SCORING')

    def test_a_team_shows_what_to_read_and_the_judges_own_scores(self):
        self.start()
        entry = self.entries['Secret Team 0']
        read = api(self.judges[0]).get(f'{self.my()}teams/{entry.id}/').data
        self.assertEqual((read['label'], read['can_score'], read['my_review']), ('Secret Team 0', True, None))
        self.assertEqual((read['material']['idea']['title'], read['material']['project']['title'], read['material']['problem']['domain']), ('Bin Buddy', 'Bin Buddy v1', 'IoT'))
        api(self.judges[0]).put(f'{self.my()}teams/{entry.id}/', {'scores': self.scores(9, 7), 'comment': 'Solid'}, format='json')
        again = api(self.judges[0]).get(f'{self.my()}teams/{entry.id}/').data
        self.assertEqual((again['my_review']['comment'], again['my_review']['total'] > 0), ('Solid', True))

    def test_a_judge_never_sees_other_judges_scores_or_who_they_are(self):
        self.start()
        entry = self.entries['Secret Team 0']
        api(self.judges[1]).put(f'{self.my()}teams/{entry.id}/', {'scores': self.scores(1, 1), 'comment': 'SECRET-COMMENT'}, format='json')
        text = json.dumps([api(self.judges[0]).get(self.my()).data, api(self.judges[0]).get(f'{self.my()}teams/{entry.id}/').data, api(self.judges[0]).get(f'{self.base}/my-judging/').data], default=str)
        self.assertNotIn('SECRET-COMMENT', text)
        self.assertNotIn(self.judges[1].email, text)
        self.assertEqual(api(self.judges[0]).get(f'{self.url}results/').status_code, 403)

    def test_a_blind_round_names_no_team_and_no_person(self):
        self.start(blind=True)
        entry = self.entries['Secret Team 0']
        leader = self.teams[0].leader
        data = [api(self.judges[0]).get(self.my()).data, api(self.judges[0]).get(f'{self.my()}teams/{entry.id}/').data]
        text = json.dumps(data, default=str)
        self.assertNotIn('Secret Team', text)
        self.assertNotIn(leader.email, text)
        self.assertNotIn(leader.first_name, text)
        self.assertEqual(data[1]['label'], f'Team {entry.id:03d}')
        self.assertIsNone(data[1]['material']['idea']['linkedin_url'])

    def test_stepping_aside_over_the_api(self):
        self.start(assignment_mode='BALANCED', judges_per_team=2)
        entry_id = api(self.judges[0]).get(self.my()).data['teams'][0]['entry_id']
        refused = api(self.judges[0]).post(f'{self.my()}teams/{entry_id}/recuse/', {'reason': ''}, format='json')
        self.assertEqual((refused.status_code, refused.data['code']), (400, 'REASON_REQUIRED'))
        done = api(self.judges[0]).post(f'{self.my()}teams/{entry_id}/recuse/', {'reason': 'I mentored them'}, format='json')
        self.assertEqual((done.status_code, done.data['reassigned']), (200, True))
        after = api(self.judges[0]).get(f'{self.my()}teams/{entry_id}/').data
        self.assertEqual((after['recused'], after['can_score']), (True, False))

    def test_scores_over_the_api_are_checked_and_locked_when_closed(self):
        client = self.start()
        entry_id = self.entries['Secret Team 1'].id
        bad = api(self.judges[0]).put(f'{self.my()}teams/{entry_id}/', {'scores': self.scores(11, 5)}, format='json')
        self.assertEqual((bad.status_code, bad.data['code']), (400, 'SCORE_RANGE'))
        api(self.judges[0]).put(f'{self.my()}teams/{entry_id}/', {'scores': self.scores()}, format='json')
        client.post(f'{self.url}close/', {'force': True, 'reason': 'testing'}, format='json')
        late = api(self.judges[0]).put(f'{self.my()}teams/{entry_id}/', {'scores': self.scores()}, format='json')
        self.assertEqual((late.status_code, late.data['code']), (409, 'NOT_SCORING'))
        self.assertFalse(api(self.judges[0]).get(f'{self.my()}teams/{entry_id}/').data['can_score'])


class Invitations(Base):
    def setUp(self):
        super().setUp()
        self.url = f'{self.base}/judge-invites/'
        patcher = mock.patch.object(judge_invites.EmailNotificationService, 'send_email', return_value=True)
        self.send = patcher.start()
        self.addCleanup(patcher.stop)

    def test_one_invitation_over_the_api(self):
        reply = api(self.admin).post(self.url, {'name': 'Asha Rao', 'email': 'asha@example.org'}, format='json')
        self.assertEqual((reply.status_code, reply.data['state'], reply.data['email_sent']), (201, 'INVITED', True))
        self.assertEqual(PasswordSetupToken.objects.filter(user__email='asha@example.org').count(), 1)
        listed = api(self.admin).get(self.url).data
        self.assertEqual([(r['email'], r['state']) for r in listed], [('asha@example.org', 'INVITED')])
        self.assertNotIn('token', json.dumps(listed, default=str))

    def test_a_list_reports_each_row_on_its_own(self):
        reply = api(self.admin).post(self.url, {'judges': [{'name': 'A', 'email': 'a@example.org'}, {'name': 'B', 'email': 'nope'}]}, format='json')
        self.assertEqual(reply.status_code, 200)
        self.assertEqual([r['ok'] for r in reply.data['results']], [True, False])

    def test_resend_and_revoke(self):
        invite_id = api(self.admin).post(self.url, {'name': 'Asha', 'email': 'asha@example.org'}, format='json').data['id']
        too_soon = api(self.admin).post(f'{self.url}{invite_id}/resend/')
        self.assertEqual((too_soon.status_code, too_soon.data['code']), (400, 'TOO_SOON'))
        from apps.hackathons.models import JudgeInvite
        JudgeInvite.objects.update(last_sent_at=timezone.now() - timedelta(minutes=5))
        self.assertEqual(api(self.admin).post(f'{self.url}{invite_id}/resend/').data['resend_count'], 1)
        revoked = api(self.admin).delete(f'{self.url}{invite_id}/')
        self.assertEqual((revoked.status_code, revoked.data['state']), (200, 'REVOKED'))
        self.assertFalse(HackathonStaff.objects.filter(hackathon=self.hack, user__email='asha@example.org').exists())

    def test_only_people_who_manage_the_team_may_invite(self):
        body = {'name': 'Asha', 'email': 'asha@example.org'}
        for who in (self.judges[0], self.organizer(['judging', 'rounds']), self.teams[0].leader, make_user()):
            self.assertEqual(api(who).post(self.url, body, format='json').status_code, 403)
            self.assertEqual(api(who).get(self.url).status_code, 403)
        self.assertEqual(api().post(self.url, body, format='json').status_code, 401)
        self.assertEqual(api(self.organizer(['staff', 'judge'])).post(self.url, body, format='json').status_code, 201)

    def test_a_delegate_cannot_give_a_judge_more_than_they_hold(self):
        delegate = self.organizer(['staff'])
        reply = api(delegate).post(self.url, {'name': 'Asha', 'email': 'asha@example.org'}, format='json')
        self.assertEqual(reply.status_code, 403)

    def test_an_invited_judge_can_then_be_put_on_a_panel(self):
        invited = api(self.admin).post(self.url, {'name': 'Asha Rao', 'email': 'asha@example.org'}, format='json').data
        round_url = f'{self.base}/rounds/{self.round.id}/evaluation/'
        api(self.admin).patch(round_url, {'evaluation_mode': 'JUDGED'}, format='json')
        reply = api(self.admin).put(f'{round_url}panel/', {'judge_ids': [invited['user_id']]}, format='json')
        self.assertEqual((reply.status_code, [p['email'] for p in reply.data['panel']]), (200, ['asha@example.org']))


class TheSwitch(Base):
    def test_an_organizer_can_turn_round_judging_on_and_off(self):
        client = api(self.admin)
        url = f'{self.base}/'
        off = client.patch(url, {'features': {'round_judging': False}}, format='json')
        self.assertEqual((off.status_code, off.data['features']['round_judging']), (200, False))
        on = client.patch(url, {'features': {'round_judging': True, 'judging': True}}, format='json')
        self.assertEqual((on.status_code, on.data['features']['round_judging']), (200, True))
        self.assertEqual(client.patch(url, {'features': {'made_up': True}}, format='json').status_code, 400)
