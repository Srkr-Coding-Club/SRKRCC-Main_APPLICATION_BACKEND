"""Round enhancements (kinds, shortlist target, rule-based shortlisting, ordering, effective status) and idea scoring."""
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.hackathons.models import (
    CheckIn, EntryStatus, HackathonStaff, IdeaStatus, IdeaSubmission, Round, RoundEntry, Submission, TeamMember,
)
from .helpers import make_hackathon, make_team, make_user

HOUR = timezone.timedelta(hours=1)


def _api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


class RoundEnhancementTests(APITestCase):
    def setUp(self):
        self.hack = make_hackathon(min_team_size=1, features={'ideas': True, 'submissions': True, 'judging': True, 'check_in': True})
        self.admin = make_user(role='ADMIN')
        self.base = f'/api/hackathons/{self.hack.slug}'
        self.teams = [make_team(self.hack, make_user(), name=f'Team {letter}') for letter in 'ABCD']
        # project scores: A 90, B 80, C 80, D unscored
        for team, score in zip(self.teams[:3], (90, 80, 80)):
            Submission.objects.create(team=team, project_title=team.name, description='d', repo_url='https://x.io/r', score=score, review_count=2)
        self.round = _api(self.admin).post(f'{self.base}/rounds/', {'name': 'Screening', 'kind': 'SCREENING'}, format='json').data

    def _auto(self, **body):
        return _api(self.admin).post(f"{self.base}/rounds/{self.round['id']}/auto-shortlist/", body, format='json')

    def test_round_kind_and_limit_are_stored_and_validated(self):
        resp = _api(self.admin).patch(f"{self.base}/rounds/{self.round['id']}/", {'kind': 'FINAL', 'shortlist_limit': 3}, format='json')
        self.assertEqual((resp.data['kind'], resp.data['shortlist_limit']), ('FINAL', 3))
        self.assertEqual(_api(self.admin).patch(f"{self.base}/rounds/{self.round['id']}/", {'shortlist_limit': 0}, format='json').status_code, 400)
        self.assertEqual(_api(self.admin).patch(f"{self.base}/rounds/{self.round['id']}/", {'kind': 'NOPE'}, format='json').status_code, 400)

    def test_preview_changes_nothing_and_top_n_keeps_ties(self):
        resp = self._auto(rule='PROJECT_SCORE_TOP', value=2)
        self.assertEqual(resp.status_code, 200, resp.data)
        picked = sorted(row['team_name'] for row in resp.data['selected'])
        self.assertEqual(picked, ['Team A', 'Team B', 'Team C'])  # B and C tie for second place
        self.assertFalse(resp.data['applied'])
        self.assertFalse(RoundEntry.objects.filter(status=EntryStatus.SHORTLISTED).exists())

    def test_apply_shortlists_and_can_reject_the_rest(self):
        resp = self._auto(rule='PROJECT_SCORE_MIN', value=85, apply=True, reject_rest=True)
        self.assertTrue(resp.data['applied'])
        statuses = {e.team.name: e.status for e in RoundEntry.objects.select_related('team')}
        self.assertEqual(statuses, {'Team A': 'SHORTLISTED', 'Team B': 'REJECTED', 'Team C': 'REJECTED', 'Team D': 'REJECTED'})

    def test_flag_rules_use_idea_project_and_check_in_facts(self):
        IdeaSubmission.objects.create(team=self.teams[3], title='I', problem='p', solution='s', status=IdeaStatus.APPROVED)
        names = lambda resp: sorted(row['team_name'] for row in resp.data['selected'])
        self.assertEqual(names(self._auto(rule='IDEA_APPROVED')), ['Team D'])
        self.assertEqual(names(self._auto(rule='HAS_PROJECT')), ['Team A', 'Team B', 'Team C'])
        CheckIn.objects.create(hackathon=self.hack, user=self.teams[1].leader, team=self.teams[1])
        self.assertEqual(names(self._auto(rule='CHECKED_IN')), ['Team B'])
        extra = make_user()
        TeamMember.objects.create(team=self.teams[1], user=extra, hackathon=self.hack)
        self.assertEqual(names(self._auto(rule='ALL_CHECKED_IN')), [])
        CheckIn.objects.create(hackathon=self.hack, user=extra, team=self.teams[1])
        self.assertEqual(names(self._auto(rule='ALL_CHECKED_IN')), ['Team B'])

    def test_bad_rules_and_empty_selection_are_refused(self):
        self.assertEqual(self._auto(rule='MAGIC').status_code, 400)
        self.assertEqual(self._auto(rule='PROJECT_SCORE_TOP', value='x').status_code, 400)
        self.assertEqual(self._auto(rule='PROJECT_SCORE_MIN', value=150).status_code, 400)
        self.assertEqual(self._auto(rule='IDEA_SCORE_MIN', value=50, apply=True).data['code'], 'NOTHING_SELECTED')

    def test_shortlist_limit_needs_confirmation_to_exceed(self):
        _api(self.admin).patch(f"{self.base}/rounds/{self.round['id']}/", {'shortlist_limit': 2}, format='json')
        ids = [t.id for t in self.teams[:3]]
        decide = f"{self.base}/rounds/{self.round['id']}/decide/"
        resp = _api(self.admin).post(decide, {'team_ids': ids, 'status': 'SHORTLISTED'}, format='json')
        self.assertEqual((resp.status_code, resp.data['code']), (409, 'SHORTLIST_LIMIT'))
        self.assertEqual(resp.data['would_be'], 3)
        self.assertEqual(_api(self.admin).post(decide, {'team_ids': ids[:2], 'status': 'SHORTLISTED'}, format='json').status_code, 200)
        self.assertEqual(_api(self.admin).post(decide, {'team_ids': ids, 'status': 'SHORTLISTED', 'confirm_over_limit': True}, format='json').status_code, 200)

    def test_auto_shortlist_respects_the_limit_too(self):
        _api(self.admin).patch(f"{self.base}/rounds/{self.round['id']}/", {'shortlist_limit': 1}, format='json')
        preview = self._auto(rule='PROJECT_SCORE_TOP', value=3)
        self.assertTrue(preview.data['over_limit'])
        self.assertEqual(self._auto(rule='PROJECT_SCORE_TOP', value=3, apply=True).data['code'], 'SHORTLIST_LIMIT')

    def test_rounds_reorder_only_before_any_decision(self):
        second = _api(self.admin).post(f'{self.base}/rounds/', {'name': 'Final', 'kind': 'FINAL'}, format='json').data
        resp = _api(self.admin).post(f'{self.base}/rounds/reorder/', {'ids': [second['id'], self.round['id']]}, format='json')
        self.assertEqual([r['name'] for r in resp.data], ['Final', 'Screening'])
        self.assertEqual(_api(self.admin).post(f'{self.base}/rounds/reorder/', {'ids': [self.round['id']]}, format='json').status_code, 400)
        self._auto(rule='HAS_PROJECT', apply=True)
        blocked = _api(self.admin).post(f'{self.base}/rounds/reorder/', {'ids': [self.round['id'], second['id']]}, format='json')
        self.assertEqual(blocked.data['code'], 'ROUNDS_IN_USE')

    def test_status_follows_the_dates_when_they_are_set(self):
        base = f"{self.base}/rounds/{self.round['id']}/"
        past = {'starts_at': (timezone.now() - 3 * HOUR).isoformat(), 'ends_at': (timezone.now() - HOUR).isoformat()}
        self.assertEqual(_api(self.admin).patch(base, past, format='json').data['effective_status'], 'COMPLETED')
        live = {'starts_at': (timezone.now() - HOUR).isoformat(), 'ends_at': (timezone.now() + HOUR).isoformat()}
        self.assertEqual(_api(self.admin).patch(base, live, format='json').data['effective_status'], 'ACTIVE')
        soon = {'starts_at': (timezone.now() + HOUR).isoformat(), 'ends_at': (timezone.now() + 3 * HOUR).isoformat()}
        self.assertEqual(_api(self.admin).patch(base, soon, format='json').data['effective_status'], 'UPCOMING')

    def test_entries_carry_the_facts_needed_to_decide(self):
        IdeaSubmission.objects.create(team=self.teams[0], title='I', problem='p', solution='s', status=IdeaStatus.APPROVED, score=77, review_count=1)
        rows = _api(self.admin).get(f"{self.base}/rounds/{self.round['id']}/entries/").data
        team_a = next(row for row in rows if row['team_name'] == 'Team A')
        self.assertEqual((team_a['idea_status'], team_a['idea_score'], team_a['has_project'], team_a['project_score']), ('APPROVED', 77.0, True, 90.0))
        team_d = next(row for row in rows if row['team_name'] == 'Team D')
        self.assertEqual((team_d['idea_status'], team_d['has_project'], team_d['project_score']), (None, False, None))

    def test_only_round_managers_can_use_the_new_actions(self):
        outsider = make_user()
        self.assertEqual(_api(outsider).post(f"{self.base}/rounds/{self.round['id']}/auto-shortlist/", {'rule': 'HAS_PROJECT'}, format='json').status_code, 403)
        manager = make_user()
        HackathonStaff.objects.create(hackathon=self.hack, user=manager, role='ORGANIZER', permissions=['rounds'])
        self.assertEqual(_api(manager).post(f"{self.base}/rounds/{self.round['id']}/auto-shortlist/", {'rule': 'HAS_PROJECT'}, format='json').status_code, 200)


class IdeaScoringTests(APITestCase):
    def setUp(self):
        self.hack = make_hackathon(min_team_size=1, features={'ideas': True, 'judging': True})
        self.admin = make_user(role='ADMIN')
        self.judge = make_user()
        HackathonStaff.objects.create(hackathon=self.hack, user=self.judge, role='JUDGE')
        self.team = make_team(self.hack, make_user(), name='Idea Team')
        self.idea = IdeaSubmission.objects.create(team=self.team, title='Bins', problem='p', solution='s')
        self.base = f'/api/hackathons/{self.hack.slug}'
        self.rubric = _api(self.admin).put(f'{self.base}/criteria/', {'target': 'IDEA', 'criteria': [
            {'name': 'Clarity', 'max_score': 10, 'weight': 3}, {'name': 'Feasibility', 'max_score': 5, 'weight': 1}]}, format='json').data

    def _score(self, judge, clarity, feasibility):
        scores = {str(self.rubric[0]['id']): clarity, str(self.rubric[1]['id']): feasibility}
        return _api(judge).post(f'{self.base}/judging/ideas/{self.idea.id}/', {'scores': scores, 'comment': 'ok'}, format='json')

    def test_rubrics_are_kept_apart_per_target(self):
        _api(self.admin).put(f'{self.base}/criteria/', {'criteria': [{'name': 'Demo', 'max_score': 10, 'weight': 1}]}, format='json')
        self.assertEqual([c['name'] for c in _api(self.admin).get(f'{self.base}/criteria/').data], ['Demo'])
        self.assertEqual([c['name'] for c in _api(self.admin).get(f'{self.base}/criteria/?target=IDEA').data], ['Clarity', 'Feasibility'])
        self.assertEqual(_api(self.admin).get(f'{self.base}/criteria/?target=OTHER').status_code, 400)

    def test_suggested_idea_rubric_can_be_seeded_once(self):
        other = make_hackathon(min_team_size=1, features={'ideas': True, 'judging': True})
        url = f'/api/hackathons/{other.slug}/criteria/?target=IDEA'
        seeded = _api(self.admin).post(url)
        self.assertEqual((seeded.status_code, seeded.data[0]['name']), (201, 'Problem clarity'))
        self.assertEqual(_api(self.admin).post(url).data['code'], 'ALREADY_CONFIGURED')

    def test_judge_scores_an_idea_and_the_average_is_kept(self):
        self.assertEqual(self._score(self.judge, 10, 5).data['total'], 100.0)
        second = make_user()
        HackathonStaff.objects.create(hackathon=self.hack, user=second, role='JUDGE')
        self._score(second, 5, 0)
        self.idea.refresh_from_db()
        self.assertEqual((self.idea.score, self.idea.review_count), (68.75, 2))
        board = _api(self.admin).get(f'{self.base}/leaderboard/?target=IDEA').data['rows']
        self.assertEqual((board[0]['team_name'], board[0]['score']), ('Idea Team', 68.75))
        reviews = _api(self.admin).get(f'{self.base}/ideas/{self.idea.id}/reviews/').data
        self.assertEqual(len(reviews), 2)

    def test_the_idea_rubric_is_enforced(self):
        self.assertEqual(self._score(self.judge, 11, 1).data['code'], 'SCORE_RANGE')
        self.assertEqual(self._score(make_user(), 1, 1).status_code, 403)
        listing = _api(self.judge).get(f'{self.base}/judging/ideas/').data
        self.assertEqual((len(listing['ideas']), len(listing['criteria'])), (1, 2))

    def test_no_scoring_without_a_rubric_or_for_your_own_team(self):
        other = make_hackathon(min_team_size=1, features={'ideas': True, 'judging': True})
        other_team = make_team(other, make_user(), name='Other')
        other_idea = IdeaSubmission.objects.create(team=other_team, title='T', problem='p', solution='s')
        judge = make_user()
        HackathonStaff.objects.create(hackathon=other, user=judge, role='JUDGE')
        resp = _api(judge).post(f'/api/hackathons/{other.slug}/judging/ideas/{other_idea.id}/', {'scores': {}}, format='json')
        self.assertEqual(resp.data['code'], 'NO_CRITERIA')
        from apps.hackathons.models import TeamMember
        TeamMember.objects.create(team=self.team, hackathon=self.hack, user=self.judge)
        self.assertEqual(self._score(self.judge, 5, 2).data['code'], 'CONFLICT_OF_INTEREST')

    def test_changing_the_idea_rubric_rescores_existing_reviews(self):
        self._score(self.judge, 10, 5)
        _api(self.admin).put(f'{self.base}/criteria/', {'target': 'IDEA', 'criteria': [
            {'id': self.rubric[0]['id'], 'name': 'Clarity', 'max_score': 20, 'weight': 1}]}, format='json')
        self.idea.refresh_from_db()
        self.assertEqual(self.idea.score, 50.0)

    def test_idea_scores_drive_round_shortlisting(self):
        self._score(self.judge, 10, 5)
        rnd = _api(self.admin).post(f'{self.base}/rounds/', {'name': 'Idea round', 'kind': 'IDEA_REVIEW'}, format='json').data
        resp = _api(self.admin).post(f"{self.base}/rounds/{rnd['id']}/auto-shortlist/", {'rule': 'IDEA_SCORE_MIN', 'value': 90}, format='json')
        self.assertEqual([row['team_name'] for row in resp.data['selected']], ['Idea Team'])
