"""The team groups every screen counts by (cohorts.py), and that the numbers on different tabs add up."""
from django.core.cache import cache
from rest_framework.test import APIClient, APITestCase

from apps.hackathons import cohorts
from apps.hackathons.models import EntryStatus, HackathonWinner, IdeaSubmission, Round, RoundEntry, Submission, TeamStatus
from .helpers import make_hackathon, make_team, make_user


def api(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def idea(team, **extra):
    return IdeaSubmission.objects.create(team=team, title=f'{team.name} idea', problem='p', solution='s', plan='w', **extra)


def project(team):
    return Submission.objects.create(team=team, project_title=f'{team.name} app', tagline='t', description='d', repo_url='https://github.com/x/y')


class Cohorts(APITestCase):
    """One hackathon with a team in every state, some with ideas and projects."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.hack = make_hackathon(min_team_size=1, features={'ideas': True, 'submissions': True})
        self.admin = make_user(role='ADMIN')
        team = lambda name, status=TeamStatus.REGISTERED, **kw: self._team(name, status, **kw)  # noqa: E731
        self.competing = [team('Comp A'), team('Comp B'), team('Comp C')]
        self.forming = team('Forming', TeamStatus.FORMING)
        self.waitlisted = team('Queued', waitlisted=True)
        self.paying = team('Paying', TeamStatus.PAYMENT_PENDING)
        self.rejected = team('Rejected pay', TeamStatus.PAYMENT_REJECTED)
        self.withdrawn = team('Withdrawn', TeamStatus.WITHDRAWN)
        self.disqualified = team('Disqualified', TeamStatus.DISQUALIFIED)
        for t in (self.competing[0], self.competing[1], self.withdrawn, self.disqualified):
            idea(t)
        for t in (self.competing[0], self.withdrawn):
            project(t)

    def _team(self, name, status, waitlisted=False):
        t = make_team(self.hack, make_user(), name=name, status=status)
        if waitlisted:
            type(t).objects.filter(pk=t.pk).update(is_waitlisted=True)
        return t

    def get(self, path, **params):
        reply = api(self.admin).get(f'/api/hackathons/{self.hack.slug}/{path}', params)
        self.assertEqual(reply.status_code, 200, reply.data)
        return reply.data

    def test_every_team_is_in_exactly_one_bucket(self):
        counts = cohorts.breakdown(self.hack)
        self.assertEqual(counts, {'competing': 3, 'forming': 1, 'waitlisted': 1, 'awaiting_payment': 2, 'removed': 2, 'total': 9})
        self.assertEqual(counts['total'], self.hack.teams.count())

    def test_ideas_tab_adds_up_to_the_competing_teams(self):
        data = self.get('ideas/')
        listed = {row['team_name'] if 'team_name' in row else row['team']['name'] for row in data['rows']}
        self.assertEqual(listed, {'Comp A', 'Comp B'})
        self.assertEqual(data['removed_rows'], 2)
        self.assertEqual({m['name'] for m in data['missing']}, {'Comp C'})
        competing_with_idea = IdeaSubmission.objects.filter(cohorts.competing_q('team__'), team__hackathon=self.hack).count()
        self.assertEqual(competing_with_idea + len(data['missing']), data['teams']['competing'])
        self.assertEqual(sum(data['counts'].values()), len(data['rows']))

    def test_removed_teams_ideas_are_listed_only_when_asked_for(self):
        data = self.get('ideas/', include_removed='1')
        self.assertEqual(len(data['rows']), 4)
        self.assertEqual(sum(data['counts'].values()), 2)  # the counts never include them

    def test_projects_tab_works_the_same_way(self):
        data = self.get('submissions/')
        self.assertEqual([row['team'] for row in data['rows']], ['Comp A'])
        self.assertEqual((data['removed_rows'], {m['name'] for m in data['missing']}), (1, {'Comp B', 'Comp C'}))
        self.assertEqual(len(self.get('submissions/', include_removed='1')['rows']), 2)

    def test_the_admin_and_the_judges_see_the_same_ideas(self):
        from apps.hackathons.models import HackathonStaff
        judge = make_user()
        HackathonStaff.objects.create(hackathon=self.hack, user=judge, role='JUDGE', permissions=['judge'])
        type(self.hack).objects.filter(pk=self.hack.pk).update(features={'ideas': True, 'submissions': True, 'judging': True})
        judged = api(judge).get(f'/api/hackathons/{self.hack.slug}/judging/ideas/').data['ideas']
        self.assertEqual(len(judged), len(self.get('ideas/')['rows']))

    def test_each_funnel_step_is_part_of_the_one_before_it(self):
        round_ = Round.objects.create(hackathon=self.hack, order=1, name='R1')
        for t in (self.competing[0], self.withdrawn):
            RoundEntry.objects.create(round=round_, team=t, status=EntryStatus.SHORTLISTED)
        HackathonWinner.objects.create(hackathon=self.hack, team=self.withdrawn, title='Winner')
        funnel = {step['key']: step['count'] for step in self.get('overview/')['analytics']['funnel']}
        self.assertEqual(
            [funnel[k] for k in ('teams', 'registered', 'ideas', 'submitted', 'shortlisted', 'winners')],
            [7, 3, 2, 1, 1, 0],
        )
        for later in ('ideas', 'ideas_approved', 'submitted', 'scored', 'shortlisted', 'winners'):
            self.assertGreaterEqual(funnel['registered'], funnel[later], later)

    def test_people_are_counted_the_same_way_on_the_overview_and_the_stats(self):
        data = self.get('overview/')
        participants = next(step['count'] for step in data['analytics']['funnel'] if step['key'] == 'participants')
        self.assertEqual(participants, 5)  # 3 competing, 1 forming, 1 waitlisted: one person each
        self.assertEqual(participants, data['stats']['participants'])
        self.assertEqual(data['analytics']['teams']['total'], 9)

    def test_a_round_counts_only_teams_still_in_the_hackathon(self):
        round_ = Round.objects.create(hackathon=self.hack, order=1, name='R1')
        for t in (*self.competing, self.withdrawn):
            RoundEntry.objects.create(round=round_, team=t)
        from apps.hackathons.serializers import RoundSerializer
        self.assertEqual(RoundSerializer().get_entry_counts(round_)['total'], 3)
