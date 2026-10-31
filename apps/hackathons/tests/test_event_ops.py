"""Admin-controlled event operations: registration rules, capacity, submissions, judging, results,
check-in, certificates, team finder, cloning and analytics."""
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.hackathons.checkin import make_token
from apps.hackathons.models import Hackathon, HackathonStaff, Submission, Team, TeamMember, TeamStatus
from .helpers import make_hackathon, make_ps, make_team, make_user

HOUR = timezone.timedelta(hours=1)


def _api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


class RegistrationControlsTests(APITestCase):
    def setUp(self):
        self.hack = make_hackathon(min_team_size=1)
        self.url = f'/api/hackathons/{self.hack.slug}/teams/'

    def _create(self, user, name='Alpha', **body):
        return _api(user).post(self.url, {'name': name, **body}, format='json')

    def test_eligibility_limits_by_branch(self):
        self.hack.eligibility = {'branches': ['ECE']}
        self.hack.save()
        resp = self._create(make_user(branch='CSE'))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'NOT_ELIGIBLE')
        self.assertEqual(self._create(make_user(branch='ece'), name='Beta').status_code, 201)

    def test_rules_must_be_accepted_and_are_recorded(self):
        self.hack.rules, self.hack.require_rules_acceptance = 'Be kind.', True
        self.hack.save()
        user = make_user()
        self.assertEqual(self._create(user).data['code'], 'RULES_NOT_ACCEPTED')
        self.assertEqual(self._create(user, accept_rules=True).status_code, 201)
        self.assertIsNotNone(TeamMember.objects.get(user=user).rules_accepted_at)

    def test_required_questions_are_enforced_and_saved(self):
        self.hack.registration_questions = [
            {'key': 'tshirt', 'label': 'T-shirt size', 'type': 'select', 'required': True, 'options': ['S', 'M', 'L']},
            {'key': 'diet', 'label': 'Diet', 'type': 'text', 'required': False},
        ]
        self.hack.save()
        user = make_user()
        self.assertEqual(self._create(user).data['code'], 'ANSWER_REQUIRED')
        self.assertEqual(self._create(user, answers={'tshirt': 'XXL'}).data['code'], 'ANSWER_INVALID')
        self.assertEqual(self._create(user, answers={'tshirt': 'M'}).status_code, 201)
        self.assertEqual(TeamMember.objects.get(user=user).answers, {'tshirt': 'M'})

    def test_full_hackathon_refuses_without_a_waitlist(self):
        self.hack.max_teams = 1
        self.hack.save()
        self.assertEqual(self._create(make_user()).status_code, 201)
        resp = self._create(make_user(), name='Late')
        self.assertEqual(resp.data['code'], 'HACKATHON_FULL')

    def test_waitlisted_team_is_promoted_when_a_spot_opens(self):
        self.hack.max_teams, self.hack.waitlist_enabled = 1, True
        self.hack.save()
        first = self._create(make_user())
        queued = self._create(make_user(), name='Queued')
        self.assertTrue(queued.data['is_waitlisted'])
        admin = make_user(role='ADMIN')
        with self.captureOnCommitCallbacks(execute=True):
            resp = _api(admin).post(f"/api/hackathons/teams/{first.data['id']}/admin-set-status/",
                                    {'status': 'WITHDRAWN', 'reason': 'Dropped out'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(Team.objects.get(pk=first.data['id']).status_reason, 'Dropped out')
        self.assertFalse(Team.objects.get(pk=queued.data['id']).is_waitlisted)

    def test_raising_the_cap_promotes_the_queue(self):
        self.hack.max_teams, self.hack.waitlist_enabled = 1, True
        self.hack.save()
        self._create(make_user())
        queued = self._create(make_user(), name='Queued')
        admin = make_user(role='ADMIN')
        resp = _api(admin).patch(f'/api/hackathons/{self.hack.slug}/', {'max_teams': 2}, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertFalse(Team.objects.get(pk=queued.data['id']).is_waitlisted)

    def test_cap_cannot_drop_below_placed_teams(self):
        self._create(make_user())
        self._create(make_user(), name='Two')
        resp = _api(make_user(role='ADMIN')).patch(f'/api/hackathons/{self.hack.slug}/', {'max_teams': 1}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_problem_statements_stay_hidden_until_reveal_time(self):
        make_ps(self.hack)
        self.hack.problem_statements_reveal_at = timezone.now() + HOUR
        self.hack.save()
        url = f'/api/hackathons/{self.hack.slug}/problem-statements/'
        self.assertEqual(_api().get(url).data, [])
        self.assertEqual(len(_api(make_user(role='ADMIN')).get(url).data), 1)
        self.hack.problem_statements_reveal_at = timezone.now() - HOUR
        self.hack.save()
        self.assertEqual(len(_api().get(url).data), 1)

    def test_invalid_settings_are_rejected(self):
        admin = _api(make_user(role='ADMIN'))
        url = f'/api/hackathons/{self.hack.slug}/'
        for bad in ({'features': {'teleport': True}}, {'eligibility': {'colleges': 'x'}},
                    {'registration_questions': [{'label': 'Pick', 'type': 'select', 'options': ['only']}]},
                    {'community_links': [{'label': 'Chat', 'url': 'javascript:alert(1)'}]},
                    {'certificate_config': {'eligibility': 'EVERYONE'}}, {'contacts': [{'name': 'No way to reach'}]}):
            self.assertEqual(admin.patch(url, bad, format='json').status_code, 400, bad)


class SubmissionTests(APITestCase):
    def setUp(self):
        self.hack = make_hackathon(min_team_size=1, features={'submissions': True})
        self.leader, self.member = make_user(), make_user()
        self.team = make_team(self.hack, self.leader, self.member)
        self.url = f'/api/hackathons/{self.hack.slug}/my-submission/'
        self.body = {'project_title': 'Robo', 'description': 'A small robot that moves along a line and carries parts around the lab.', 'repo_url': 'https://github.com/x/robo'}

    def test_leader_submits_and_can_update(self):
        resp = _api(self.leader).put(self.url, self.body, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['submission']['project_title'], 'Robo')
        self.body['project_title'] = 'Robo 2'
        self.assertEqual(_api(self.leader).put(self.url, self.body, format='json').data['submission']['project_title'], 'Robo 2')
        self.assertEqual(Submission.objects.filter(team=self.team).count(), 1)

    def test_members_cannot_submit_and_feature_can_be_off(self):
        self.assertEqual(_api(self.member).put(self.url, self.body, format='json').status_code, 403)
        self.hack.features = {}
        self.hack.save()
        self.assertEqual(_api(self.leader).put(self.url, self.body, format='json').data['code'], 'FEATURE_OFF')

    def test_window_is_enforced(self):
        self.hack.submission_deadline = timezone.now() - HOUR
        self.hack.save()
        self.assertEqual(_api(self.leader).put(self.url, self.body, format='json').data['code'], 'SUBMISSIONS_CLOSED')
        self.hack.submission_deadline, self.hack.submission_opens_at = None, timezone.now() + HOUR
        self.hack.save()
        self.assertEqual(_api(self.leader).put(self.url, self.body, format='json').data['code'], 'SUBMISSIONS_NOT_OPEN')

    def test_bad_links_are_rejected_with_the_field(self):
        resp = _api(self.leader).put(self.url, {**self.body, 'repo_url': 'not a url'}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['field'], 'repo_url')

    def test_waitlisted_or_forming_teams_cannot_submit(self):
        self.team.is_waitlisted = True
        self.team.save()
        self.assertEqual(_api(self.leader).put(self.url, self.body, format='json').data['code'], 'TEAM_NOT_REGISTERED')

    def test_admin_list_lists_missing_teams_and_reminds_them(self):
        admin = _api(make_user(role='ADMIN'))
        resp = admin.get(f'/api/hackathons/{self.hack.slug}/submissions/')
        self.assertEqual([t['id'] for t in resp.data['missing']], [self.team.id])
        self.assertEqual(admin.post(f'/api/hackathons/{self.hack.slug}/submissions/remind/').data['reminded'], 1)
        self.assertEqual(_api(self.leader).get(f'/api/hackathons/{self.hack.slug}/submissions/').status_code, 403)


class JudgingAndResultsTests(APITestCase):
    def setUp(self):
        self.hack = make_hackathon(min_team_size=1, features={'submissions': True, 'judging': True, 'showcase': True})
        self.admin = make_user(role='ADMIN')
        self.judge = make_user()
        HackathonStaff.objects.create(hackathon=self.hack, user=self.judge, role='JUDGE')
        self.team_a = make_team(self.hack, make_user(), name='Alpha')
        self.team_b = make_team(self.hack, make_user(), name='Beta')
        self.sub_a = Submission.objects.create(team=self.team_a, project_title='A', description='d', repo_url='https://x.io/a')
        self.sub_b = Submission.objects.create(team=self.team_b, project_title='B', description='d', repo_url='https://x.io/b')
        base = f'/api/hackathons/{self.hack.slug}'
        self.criteria_url, self.judging_url = f'{base}/criteria/', f'{base}/judging/'
        resp = _api(self.admin).put(self.criteria_url, {'criteria': [
            {'name': 'Idea', 'max_score': 10, 'weight': 3}, {'name': 'Demo', 'max_score': 5, 'weight': 1},
        ]}, format='json')
        self.idea, self.demo = [str(c['id']) for c in resp.data]

    def _review(self, judge, sub, idea, demo, comment=''):
        return _api(judge).post(f'{self.judging_url}{sub.id}/', {'scores': {self.idea: idea, self.demo: demo}, 'comment': comment}, format='json')

    def test_weighted_total_and_average_across_judges(self):
        self.assertEqual(self._review(self.judge, self.sub_a, 10, 5).data['total'], 100.0)
        second = make_user()
        HackathonStaff.objects.create(hackathon=self.hack, user=second, role='JUDGE')
        self._review(second, self.sub_a, 5, 0)  # (0.5*3 + 0*1) / 4 = 37.5
        self.sub_a.refresh_from_db()
        self.assertEqual((self.sub_a.score, self.sub_a.review_count), (68.75, 2))

    def test_scores_are_range_checked_and_required(self):
        self.assertEqual(self._review(self.judge, self.sub_a, 11, 1).data['code'], 'SCORE_RANGE')
        resp = _api(self.judge).post(f'{self.judging_url}{self.sub_a.id}/', {'scores': {self.idea: 5}}, format='json')
        self.assertEqual(resp.data['code'], 'SCORE_REQUIRED')

    def test_only_assigned_judges_score_and_never_their_own_team(self):
        self.assertEqual(self._review(make_user(), self.sub_a, 1, 1).status_code, 403)
        TeamMember.objects.create(team=self.team_a, user=self.judge)
        self.assertEqual(self._review(self.judge, self.sub_a, 1, 1).data['code'], 'CONFLICT_OF_INTEREST')

    def test_participants_cannot_be_made_judges(self):
        resp = _api(self.admin).post(f'/api/hackathons/{self.hack.slug}/staff/',
                                     {'email': self.team_a.leader.email, 'role': 'JUDGE'}, format='json')
        self.assertEqual(resp.data['code'], 'CONFLICT')

    def test_leaderboard_ranks_with_ties(self):
        self._review(self.judge, self.sub_a, 10, 5)
        self._review(self.judge, self.sub_b, 10, 5)
        rows = _api(self.admin).get(f'/api/hackathons/{self.hack.slug}/leaderboard/').data['rows']
        self.assertEqual([r['rank'] for r in rows], [1, 1])

    def test_changing_the_rubric_rescoring_existing_reviews(self):
        self._review(self.judge, self.sub_a, 10, 5)
        resp = _api(self.admin).put(self.criteria_url, {'criteria': [{'id': int(self.idea), 'name': 'Idea', 'max_score': 20, 'weight': 1}]}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.sub_a.refresh_from_db()
        self.assertEqual(self.sub_a.score, 50.0)  # 10 of the new 20

    def test_results_are_hidden_until_published(self):
        admin = _api(self.admin)
        base = f'/api/hackathons/{self.hack.slug}'
        resp = admin.put(f'{base}/winners/', {'winners': [{'team_id': self.team_a.id, 'title': 'Winner', 'rank': 1, 'prize': 'Rs 10,000'}]}, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertFalse(_api().get(f'{base}/results/').data['published'])
        self.assertEqual(_api().get(f'{base}/showcase/').data['projects'], [])
        self.assertEqual(admin.post(f'{base}/results/publish/').status_code, 200)
        public = _api().get(f'{base}/results/').data
        self.assertEqual(public['winners'][0]['team_name'], 'Alpha')
        self.assertEqual(public['leaderboard'], [])  # scores stay private unless show_leaderboard
        self.assertEqual(len(_api().get(f'{base}/showcase/').data['projects']), 2)
        admin.post(f'{base}/results/unpublish/')
        self.assertFalse(_api().get(f'{base}/results/').data['published'])

    def test_scores_are_locked_once_results_are_published(self):
        _api(self.admin).put(f'/api/hackathons/{self.hack.slug}/winners/', {'winners': [{'team_id': self.team_a.id, 'title': 'Winner'}]}, format='json')
        _api(self.admin).post(f'/api/hackathons/{self.hack.slug}/results/publish/')
        self.assertEqual(self._review(self.judge, self.sub_a, 5, 5).data['code'], 'RESULTS_PUBLISHED')

    def test_cannot_publish_with_nothing_to_show_or_pick_foreign_teams(self):
        admin = _api(self.admin)
        base = f'/api/hackathons/{self.hack.slug}'
        self.assertEqual(admin.post(f'{base}/results/publish/').data['code'], 'NOTHING_TO_PUBLISH')
        other = make_team(make_hackathon(), make_user())
        resp = admin.put(f'{base}/winners/', {'winners': [{'team_id': other.id, 'title': 'Winner'}]}, format='json')
        self.assertEqual(resp.status_code, 400)


class CheckInAndCertificateTests(APITestCase):
    def setUp(self):
        self.hack = make_hackathon(min_team_size=1, features={'check_in': True, 'certificates': True})
        self.admin = make_user(role='ADMIN')
        self.volunteer = make_user()
        HackathonStaff.objects.create(hackathon=self.hack, user=self.volunteer, role='VOLUNTEER')
        self.person = make_user()
        self.team = make_team(self.hack, self.person, name='Gamma')
        self.base = f'/api/hackathons/{self.hack.slug}'

    def test_qr_scan_checks_in_once(self):
        token = _api(self.person).get(f'{self.base}/my-check-in/').data['token']
        first = _api(self.volunteer).post(f'{self.base}/check-in/', {'token': token}, format='json')
        self.assertTrue(first.data['created'])
        self.assertFalse(_api(self.volunteer).post(f'{self.base}/check-in/', {'token': token}, format='json').data['created'])
        roster = _api(self.volunteer).get(f'{self.base}/check-in/').data
        self.assertEqual((roster['total'], roster['checked_in']), (1, 1))
        _api(self.volunteer).delete(f'{self.base}/check-in/{self.person.id}/')
        self.assertEqual(_api(self.volunteer).get(f'{self.base}/check-in/').data['checked_in'], 0)

    def test_tokens_are_signed_per_hackathon_and_only_staff_scan(self):
        other = make_hackathon(min_team_size=1, features={'check_in': True})
        make_team(other, self.person, name='Elsewhere')
        foreign = _api(self.person).get(f'/api/hackathons/{other.slug}/my-check-in/').data['token']
        self.assertEqual(_api(self.volunteer).post(f'{self.base}/check-in/', {'token': foreign}, format='json').data['code'], 'WRONG_HACKATHON')
        self.assertEqual(_api(self.volunteer).post(f'{self.base}/check-in/', {'token': 'garbage'}, format='json').data['code'], 'INVALID_QR')
        self.assertEqual(_api(self.person).post(f'{self.base}/check-in/', {'user_id': self.person.id}, format='json').status_code, 403)

    def test_window_gates_qr_scans_but_not_manual_check_in(self):
        self.hack.check_in_opens_at = timezone.now() + HOUR
        self.hack.save()
        mine = _api(self.person).get(f'{self.base}/my-check-in/').data
        self.assertEqual((mine['window'], mine['token']), ('NOT_OPEN', ''))
        self.hack.check_in_opens_at = None
        self.hack.save()
        token = _api(self.person).get(f'{self.base}/my-check-in/').data['token']
        self.hack.check_in_closes_at = timezone.now() - HOUR
        self.hack.save()
        self.assertEqual(_api(self.volunteer).post(f'{self.base}/check-in/', {'token': token}, format='json').data['code'], 'CHECK_IN_CLOSED')
        # an offline scan made while the window was open is still accepted when replayed late
        early_at = timezone.now() - 2 * HOUR
        early_token = make_token(self.hack, self.person, at=early_at)
        replay = _api(self.volunteer).post(f'{self.base}/check-in/', {'token': early_token, 'scanned_at': early_at.isoformat()}, format='json')
        self.assertTrue(replay.data['created'])
        self.assertEqual(replay.data['checked_in_by'], (self.volunteer.first_name + ' ' + self.volunteer.last_name).strip() or self.volunteer.email)
        self.assertTrue(_api(self.volunteer).post(f'{self.base}/check-in/', {'user_id': self.person.id}, format='json').status_code == 200)

    def test_qr_codes_rotate_and_old_ones_expire(self):
        stale = make_token(self.hack, self.person, at=timezone.now() - 10 * timezone.timedelta(minutes=1))
        self.assertEqual(_api(self.volunteer).post(f'{self.base}/check-in/', {'token': stale}, format='json').data['code'], 'QR_EXPIRED')
        recent = make_token(self.hack, self.person, at=timezone.now() - timezone.timedelta(minutes=1))
        self.assertTrue(_api(self.volunteer).post(f'{self.base}/check-in/', {'token': recent}, format='json').data['created'])

    def test_only_organizers_override_an_unregistered_team_and_need_a_reason(self):
        forming = make_user()
        make_team(self.hack, forming, name='Forming', status=TeamStatus.FORMING)
        body = {'user_id': forming.id, 'override': True}
        self.assertEqual(_api(self.volunteer).post(f'{self.base}/check-in/', {**body, 'reason': 'VIP'}, format='json').status_code, 403)
        self.assertEqual(_api(self.admin).post(f'{self.base}/check-in/', body, format='json').data['code'], 'REASON_REQUIRED')
        done = _api(self.admin).post(f'{self.base}/check-in/', {**body, 'reason': 'Teammate arrived late'}, format='json')
        self.assertTrue(done.data['created'])
        from apps.hackathons.models import CheckIn
        self.assertEqual(CheckIn.objects.get(user=forming).override_reason, 'Teammate arrived late')
        removed = make_user()
        make_team(self.hack, removed, name='Gone', status=TeamStatus.DISQUALIFIED)
        self.assertEqual(_api(self.admin).post(f'{self.base}/check-in/', {'user_id': removed.id, 'override': True, 'reason': 'x'}, format='json').data['code'], 'TEAM_REMOVED')

    def test_roster_is_searchable_paged_and_lists_held_back_people_for_organizers_only(self):
        for i in range(3):
            make_team(self.hack, make_user(first_name=f'Zed{i}'), name=f'Zed team {i}')
        waiting = make_user(first_name='Waiter')
        make_team(self.hack, waiting, name='Queue', status=TeamStatus.FORMING)
        page = _api(self.volunteer).get(f'{self.base}/check-in/?limit=2&offset=2').data
        self.assertEqual((page['total'], page['count'], len(page['participants'])), (4, 4, 2))
        found = _api(self.volunteer).get(f'{self.base}/check-in/?q=zed').data
        self.assertEqual((found['count'], found['total']), (3, 4))
        self.assertEqual(found['held_back'], [])
        self.assertEqual(_api(self.volunteer).get(f'{self.base}/check-in/?q=Waiter').data['held_back'], [])
        held = _api(self.admin).get(f'{self.base}/check-in/?q=Waiter').data['held_back']
        self.assertEqual([h['name'] for h in held], ['Waiter Last'])
        _api(self.volunteer).post(f'{self.base}/check-in/', {'user_id': self.person.id}, format='json')
        self.assertEqual(_api(self.volunteer).get(f'{self.base}/check-in/?missing=1').data['count'], 3)
        self.assertEqual(_api(self.volunteer).get(f'{self.base}/check-in/').data['participants'][0]['checked_in_by'] is not None, True)

    def test_no_show_follow_up_reminds_and_withdraws_so_the_waitlist_moves(self):
        self.hack.max_teams, self.hack.waitlist_enabled = 2, True
        self.hack.save()
        absent = make_team(self.hack, make_user(), name='Absent')
        queued = make_team(self.hack, make_user(), name='Queued')
        queued.is_waitlisted = True
        queued.save()
        _api(self.volunteer).post(f'{self.base}/check-in/', {'user_id': self.person.id}, format='json')
        self.assertEqual(_api(self.volunteer).get(f'{self.base}/check-in/no-shows/').status_code, 403)
        report = _api(self.admin).get(f'{self.base}/check-in/no-shows/').data
        self.assertEqual([t['name'] for t in report['teams']], ['Absent'])
        self.assertEqual(_api(self.admin).post(f'{self.base}/check-in/no-shows/', {'action': 'remind'}, format='json').data['reminded'], 1)
        with self.captureOnCommitCallbacks(execute=True):
            out = _api(self.admin).post(f'{self.base}/check-in/no-shows/', {'action': 'withdraw', 'team_ids': [absent.id, self.team.id]}, format='json')
        self.assertEqual(out.data['withdrawn'], 1)  # Gamma has someone checked in, so it is untouched
        absent.refresh_from_db(); queued.refresh_from_db(); self.team.refresh_from_db()
        self.assertEqual((absent.status, self.team.status, queued.is_waitlisted), ('WITHDRAWN', 'REGISTERED', False))

    def test_window_must_close_after_it_opens(self):
        now = timezone.now()
        resp = _api(self.admin).patch(self.base + '/', {'check_in_opens_at': now.isoformat(), 'check_in_closes_at': (now - HOUR).isoformat()}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_unregistered_teams_cannot_check_in(self):
        self.team.status = TeamStatus.FORMING
        self.team.save()
        self.assertEqual(_api(self.volunteer).post(f'{self.base}/check-in/', {'user_id': self.person.id}, format='json').data['code'], 'TEAM_NOT_REGISTERED')

    def test_certificate_needs_release_eligibility_and_verifies(self):
        url = f'{self.base}/my-certificate/'
        self.assertEqual(_api(self.person).get(url).data['code'], 'NOT_RELEASED')
        self.hack.certificate_config = {'released': True, 'eligibility': 'CHECKED_IN', 'body': '{name} built things with {team} at {hackathon}.'}
        self.hack.save()
        self.assertEqual(_api(self.person).get(url).status_code, 403)
        _api(self.volunteer).post(f'{self.base}/check-in/', {'user_id': self.person.id}, format='json')
        cert = _api(self.person).get(url).data
        self.assertIn('with Gamma at', cert['body'])
        verified = _api().get(f"/api/hackathons/certificates/verify/{cert['code']}/").data
        self.assertTrue(verified['valid'])
        self.assertEqual(verified['team'], 'Gamma')
        self.assertEqual(_api().get('/api/hackathons/certificates/verify/forged/').status_code, 404)


class FinderCloneAnalyticsTests(APITestCase):
    def setUp(self):
        self.hack = make_hackathon(min_team_size=1, features={'team_finder': True})
        self.base = f'/api/hackathons/{self.hack.slug}'

    def test_seeker_listing_hides_emails_and_leader_can_invite(self):
        seeker, leader = make_user(), make_user()
        team = make_team(self.hack, leader, name='Recruiters')
        self.assertEqual(_api(seeker).put(f'{self.base}/finder/me/', {'skills': 'React'}, format='json').status_code, 200)
        listing = _api(leader).get(f'{self.base}/finder/').data
        self.assertEqual(len(listing['seekers']), 1)
        self.assertNotIn(seeker.email, str(listing))
        self.assertEqual(_api(leader).post(f"{self.base}/finder/seekers/{listing['seekers'][0]['id']}/invite/").status_code, 200)
        self.assertTrue(team.invites.filter(invited_user=seeker).exists())

    def test_recruiting_teams_are_listed_and_requests_notify_the_leader(self):
        leader, seeker = make_user(), make_user()
        team = make_team(self.hack, leader, name='Open Door')
        resp = _api(leader).patch(f'/api/hackathons/teams/{team.id}/', {'looking_for_members': True, 'looking_for': 'a designer'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(_api(seeker).get(f'{self.base}/finder/').data['teams'][0]['looking_for'], 'a designer')
        self.assertEqual(_api(seeker).post(f'{self.base}/finder/teams/{team.id}/request/', {'message': 'hi'}, format='json').status_code, 200)

    def test_finder_is_off_unless_enabled(self):
        self.hack.features = {}
        self.hack.save()
        self.assertEqual(_api(make_user()).get(f'{self.base}/finder/').data['code'], 'FEATURE_OFF')

    def test_clone_shifts_dates_and_starts_as_a_draft(self):
        self.hack.rules = 'Rules'
        self.hack.submission_deadline = self.hack.start_date + HOUR
        self.hack.save()
        make_ps(self.hack)
        new_start = self.hack.start_date + timezone.timedelta(days=365)
        resp = _api(make_user(role='ADMIN')).post(f'{self.base}/clone/', {
            'title': 'Next Year', 'start_date': new_start.isoformat(), 'copy_problem_statements': True}, format='json')
        self.assertEqual(resp.status_code, 201, resp.data)
        clone = Hackathon.objects.get(slug=resp.data['slug'])
        self.assertEqual((clone.status, clone.rules, clone.problem_statements.count()), ('DRAFT', 'Rules', 1))
        self.assertEqual(clone.submission_deadline, self.hack.submission_deadline + timezone.timedelta(days=365))
        self.assertEqual(clone.teams.count(), 0)
        self.assertEqual(clone.cloned_from_id, self.hack.id)

    def test_analytics_compare_with_the_edition_it_was_cloned_from(self):
        now = timezone.now()
        last = make_hackathon(min_team_size=1, registration_opens_at=now - 100 * 24 * HOUR, registration_closes_at=now - 80 * 24 * HOUR)
        early, late = make_team(last, make_user(), name='Early'), make_team(last, make_user(), name='Late')
        make_team(last, make_user(), name='Dropped', status=TeamStatus.WITHDRAWN)
        Team.objects.filter(pk=early.pk).update(created_at=last.registration_opens_at + 2 * 24 * HOUR)
        TeamMember.objects.filter(team=early).update(joined_at=last.registration_opens_at + 2 * 24 * HOUR)
        Team.objects.filter(pk=late.pk).update(created_at=last.registration_opens_at + 15 * 24 * HOUR)
        TeamMember.objects.filter(team=late).update(joined_at=last.registration_opens_at + 15 * 24 * HOUR)
        self.hack.cloned_from, self.hack.registration_opens_at = last, now - 5 * 24 * HOUR
        self.hack.save()
        admin = _api(make_user(role='ADMIN'))
        prev = admin.get(f'{self.base}/analytics/').data['previous_edition']
        self.assertEqual((prev['slug'], prev['teams_then'], prev['teams_final'], prev['participants_final']), (last.slug, 1, 2, 2))
        self.hack.cloned_from = None
        self.hack.save()
        self.assertIsNone(admin.get(f'{self.base}/analytics/').data['previous_edition'])

    def test_analytics_and_exports_are_organizer_only(self):
        make_team(self.hack, make_user(), name='Counted')
        admin = _api(make_user(role='ADMIN'))
        data = admin.get(f'{self.base}/analytics/').data
        self.assertEqual({row['key']: row['count'] for row in data['funnel']}['registered'], 1)
        steps = [row['key'] for row in data['funnel']]
        self.assertEqual(steps, ['participants', 'teams', 'registered', 'ideas', 'ideas_approved', 'submitted', 'scored', 'shortlisted', 'winners'])
        self.assertTrue(all(row['count'] == 0 for row in data['funnel'] if row['key'] in ('ideas_approved', 'scored')))
        self.assertEqual(admin.get(f'{self.base}/export/participants/').status_code, 200)
        self.assertEqual(_api(make_user()).get(f'{self.base}/analytics/').status_code, 403)
        self.assertEqual(_api(make_user()).get(f'{self.base}/export/participants/').status_code, 403)


class SubmissionDetailRulesTests(SubmissionTests):
    """The details of a project: how long a title and description must be, and what the links may point at."""

    def put(self, **change):
        return _api(self.leader).put(self.url, {**self.body, **change}, format='json')

    def test_a_good_submission_with_every_link_is_saved_as_sent(self):
        resp = self.put(
            repo_url='https://github.com/code-owls/robo', demo_url='https://robo.vercel.app', video_url='https://youtu.be/abc',
            slides_url='https://docs.google.com/presentation/d/abc/edit', tech_stack=['React', 'react', ' Django ', ''],
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        sub = Submission.objects.get(team=self.team)
        self.assertEqual((sub.repo_url, sub.video_url), ('https://github.com/code-owls/robo', 'https://youtu.be/abc'))
        self.assertEqual(sub.tech_stack, ['React', 'Django'])   # trimmed, empty dropped, repeat dropped

    def test_each_bad_link_names_its_field(self):
        cases = [('repo_url', 'https://example.com/owner/repo'), ('repo_url', 'https://github.com/owner'), ('repo_url', 'http://github.com/o/r'),
                 ('demo_url', 'https://localhost:3000'), ('demo_url', 'https://192.168.0.4/app'), ('video_url', 'https://example.com/v.mp4'),
                 ('slides_url', 'ftp://files.example.com/a.pdf'), ('demo_url', 'https://example.org/' + 'a' * 300)]
        for field, link in cases:
            resp = self.put(**{field: link})
            self.assertEqual((resp.status_code, resp.data['field']), (400, field), link)

    def test_an_over_long_link_is_a_400_not_a_server_error(self):
        self.assertEqual(self.put(repo_url='https://github.com/owner/' + 'r' * 300).status_code, 400)

    def test_title_and_description_need_substance(self):
        for change, field in (({'project_title': 'A'}, 'project_title'), ({'project_title': '   '}, 'project_title'),
                              ({'description': 'good project'}, 'description'), ({'description': ' ' * 40}, 'description')):
            resp = self.put(**change)
            self.assertEqual((resp.status_code, resp.data['field']), (400, field), change)
        self.assertEqual(self.put(project_title='Bot').status_code, 200)

    def test_a_refused_save_changes_nothing(self):
        self.assertEqual(self.put().status_code, 200)
        self.assertEqual(self.put(project_title='Changed', repo_url='https://example.com/x/y').status_code, 400)
        self.assertEqual(Submission.objects.get(team=self.team).project_title, self.body['project_title'])
