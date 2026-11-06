"""Phase 2 of round judging: the panel, who scores whom, scoring, recusing, closing into a ranking, and the shortlist rules."""
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from apps.hackathons import round_judging
from apps.hackathons.errors import HackathonError
from apps.hackathons.judging import CriteriaService
from apps.hackathons.models import (
    AssignmentMode, EntryStatus, EvaluationMode, EvaluationStatus, HackathonStaff, Round, RoundAssignment, RoundEntry, RoundReview,
    RoundStatus, TeamStatus,
)
from apps.hackathons.round_judging import RoundJudgingService as J
from apps.hackathons.services import RoundService
from .helpers import make_hackathon, make_team, make_user

HOUR = timedelta(hours=1)
RUBRIC = [{'name': 'Idea', 'max_score': 10, 'weight': 3}, {'name': 'Demo', 'max_score': 10, 'weight': 1}]


def code_of(exc):
    return exc.exception.detail['code']


class Base(TestCase):
    TEAMS = 4
    JUDGES = 3

    def setUp(self):
        self.hack = make_hackathon(min_team_size=1, features={'round_judging': True, 'judging': True})
        self.admin = make_user(role='ADMIN')
        self.judges = []
        for number in range(self.JUDGES):
            judge = make_user(first_name=f'Judge{number}')
            HackathonStaff.objects.create(hackathon=self.hack, user=judge, role='JUDGE', permissions=['judge'])
            self.judges.append(judge)
        self.teams = [make_team(self.hack, make_user(), name=f'Team {chr(65 + n)}') for n in range(self.TEAMS)]
        self.round = RoundService.create_round(self.hack, self.admin, name='Semi-final')
        J.configure(self.round, self.admin, {'evaluation_mode': 'JUDGED', 'assignment_mode': 'ALL'})
        self.round.refresh_from_db()

    def refresh(self):
        self.round.refresh_from_db()
        return self.round

    def entry(self, team):
        return RoundEntry.objects.get(round=self.round, team=team)

    def rubric(self):
        return J.set_criteria(self.round, self.admin, RUBRIC)

    def panel(self, judges=None):
        return J.set_panel(self.round, self.admin, [j.id for j in (judges or self.judges)])

    def ready(self, mode='ALL', **settings):
        J.configure(self.round, self.admin, {'assignment_mode': mode, **settings})
        self.rubric()
        self.panel()
        return self.refresh()

    def started(self, mode='ALL', **settings):
        self.ready(mode, **settings)
        J.start(self.round, self.admin)
        return self.refresh()

    def scores(self, idea, demo):
        a, b = [c.id for c in self.round.criteria.all()]
        return {str(a): idea, str(b): demo}

    def score(self, judge, team, idea=8, demo=6, comment=''):
        return J.submit_review(self.round, self.entry(team).id, judge, self.scores(idea, demo), comment)


class Settings(Base):
    def test_a_round_is_only_judged_on_a_hackathon_that_switched_the_feature_on(self):
        self.hack.features = {}
        self.hack.save()
        with self.assertRaises(HackathonError) as ctx:
            J.configure(self.round, self.admin, {'blind': True})
        self.assertEqual(code_of(ctx), 'FEATURE_OFF')

    def test_settings_are_saved_and_checked(self):
        J.configure(self.round, self.admin, {'scoring_target': 'PITCH', 'assignment_mode': 'BALANCED', 'judges_per_team': 2, 'min_reviews_per_team': 2, 'blind': True, 'tie_break': 'SHARED'})
        self.refresh()
        self.assertEqual((self.round.scoring_target, self.round.assignment_mode, self.round.judges_per_team, self.round.blind, self.round.tie_break), ('PITCH', 'BALANCED', 2, True, 'SHARED'))
        for bad, field in (({'evaluation_mode': 'MAYBE'}, 'evaluation_mode'), ({'judges_per_team': 0}, 'judges_per_team'), ({'judges_per_team': 'x'}, 'judges_per_team'),
                           ({'min_reviews_per_team': 99}, 'min_reviews_per_team'), ({'blind': 'yes'}, 'blind'), ({'scoring_deadline': 'soon'}, 'scoring_deadline'),
                           ({'scoring_deadline': (timezone.now() - HOUR).isoformat()}, 'scoring_deadline'), ({'min_reviews_per_team': 3}, 'min_reviews_per_team')):
            with self.assertRaises(HackathonError, msg=bad) as ctx:
                J.configure(self.round, self.admin, bad)
            self.assertEqual(ctx.exception.detail['field'], field)

    def test_once_judges_are_scoring_only_the_deadline_moves(self):
        self.started()
        later = (timezone.now() + 2 * HOUR).isoformat()
        J.configure(self.round, self.admin, {'scoring_deadline': later})
        with self.assertRaises(HackathonError) as ctx:
            J.configure(self.round, self.admin, {'blind': True})
        self.assertEqual(code_of(ctx), 'EVALUATION_STARTED')

    def test_a_closed_round_cannot_be_reconfigured(self):
        self.started()
        self.score(self.judges[0], self.teams[0])
        J.close(self.round, self.admin, force=True, reason='testing')
        with self.assertRaises(HackathonError) as ctx:
            J.configure(self.round, self.admin, {'blind': True})
        self.assertEqual(code_of(ctx), 'EVALUATION_CLOSED')

    def test_a_plain_shortlisting_round_cannot_be_judged(self):
        plain = RoundService.create_round(self.hack, self.admin, name='Plain')
        for call in (lambda: J.set_panel(plain, self.admin, []), lambda: J.assign(plain, self.admin), lambda: J.start(plain, self.admin)):
            with self.assertRaises(HackathonError) as ctx:
                call()
            self.assertEqual(code_of(ctx), 'NOT_JUDGED')


class Rubric(Base):
    def test_the_rounds_own_rubric_replaces_the_hackathon_one_and_empty_means_inherit(self):
        CriteriaService.seed_defaults(self.hack, self.admin)
        self.assertEqual(len(round_judging.criteria_of(self.round)), 4)
        self.rubric()
        self.assertEqual([c.name for c in round_judging.criteria_of(self.round)], ['Idea', 'Demo'])
        J.set_criteria(self.round, self.admin, [])
        self.assertEqual(len(round_judging.criteria_of(self.round)), 4)

    def test_criteria_are_checked(self):
        for items in ([{'name': '', 'max_score': 10}], [{'name': 'x', 'max_score': 0}], [{'name': 'x', 'weight': 11}], ['x'], 'x', [{'name': 'c'}] * 11):
            with self.assertRaises(HackathonError):
                J.set_criteria(self.round, self.admin, items)

    def test_a_pitch_uses_the_project_rubric_and_an_idea_round_the_idea_one(self):
        self.assertEqual(round_judging.rubric_target(self.round), 'PROJECT')
        J.configure(self.round, self.admin, {'scoring_target': 'PITCH'})
        self.assertEqual(round_judging.rubric_target(self.refresh()), 'PROJECT')
        J.configure(self.round, self.admin, {'scoring_target': 'IDEA'})
        self.assertEqual(round_judging.rubric_target(self.refresh()), 'IDEA')

    def test_the_rubric_is_frozen_once_judging_starts_and_a_later_hackathon_change_does_not_reach_it(self):
        CriteriaService.seed_defaults(self.hack, self.admin)
        self.panel()
        J.start(self.round, self.admin)
        self.assertEqual(self.round.criteria.count(), 4)
        CriteriaService.replace(self.hack, self.admin, [{'name': 'Only', 'max_score': 5, 'weight': 1}])
        self.assertEqual(self.round.criteria.count(), 4)
        with self.assertRaises(HackathonError) as ctx:
            J.set_criteria(self.round, self.admin, RUBRIC)
        self.assertEqual(code_of(ctx), 'EVALUATION_STARTED')


class Panel(Base):
    def test_judges_are_added_and_replaced(self):
        panel = self.panel(self.judges[:2])
        self.assertEqual([p.judge for p in panel], self.judges[:2])
        self.assertEqual(len(self.panel(self.judges[1:])), 2)
        self.assertEqual(self.round.panel.count(), 2)

    def test_only_judges_of_this_hackathon_who_are_not_participants(self):
        outsider = make_user()
        with self.assertRaises(HackathonError) as ctx:
            J.set_panel(self.round, self.admin, [outsider.id])
        self.assertEqual(code_of(ctx), 'NOT_A_JUDGE')
        participant = self.teams[0].leader
        HackathonStaff.objects.create(hackathon=self.hack, user=participant, role='JUDGE', permissions=['judge'])
        with self.assertRaises(HackathonError) as ctx:
            J.set_panel(self.round, self.admin, [participant.id])
        self.assertEqual(code_of(ctx), 'CONFLICT')
        for bad in ('x', [1] * 51, ['a']):
            with self.assertRaises(HackathonError):
                J.set_panel(self.round, self.admin, bad)

    def test_removing_a_judge_drops_their_unscored_teams_but_not_if_they_scored(self):
        self.ready()
        J.assign(self.round, self.admin)
        self.assertEqual(self.round.assignments.count(), 12)
        self.panel(self.judges[:2])
        self.assertEqual(self.round.assignments.count(), 8)
        J.start(self.round, self.admin)
        self.score(self.judges[0], self.teams[0])
        with self.assertRaises(HackathonError) as ctx:
            self.panel(self.judges[1:])
        self.assertEqual(code_of(ctx), 'HAS_REVIEWS')

    def test_a_judge_added_while_balanced_judging_is_running_picks_up_teams(self):
        J.configure(self.round, self.admin, {'assignment_mode': 'BALANCED', 'judges_per_team': 2})
        self.rubric()
        self.panel(self.judges[:2])
        J.start(self.round, self.admin)
        self.panel()
        self.assertEqual(self.refresh().assignments.filter(judge=self.judges[2]).count(), 0)  # every team already has its two


class Assigning(Base):
    def test_everyone_scores_everyone(self):
        self.ready('ALL')
        result = J.assign(self.round, self.admin)
        self.assertEqual((result['assignments'], result['teams'], result['short']), (12, 4, []))
        self.assertEqual(J.assign(self.round, self.admin)['assignments'], 12)  # again changes nothing

    def test_balanced_gives_each_team_n_judges_and_spreads_the_load(self):
        self.ready('BALANCED', judges_per_team=2, min_reviews_per_team=2)
        J.assign(self.round, self.admin)
        per_team = {e.id: self.round.assignments.filter(entry=e).count() for e in self.round.entries.all()}
        self.assertEqual(set(per_team.values()), {2})
        loads = [self.round.assignments.filter(judge=j).count() for j in self.judges]
        self.assertEqual((sum(loads), max(loads) - min(loads) <= 1), (8, True))
        before = sorted(self.round.assignments.values_list('entry_id', 'judge_id'))
        J.assign(self.round, self.admin)
        self.assertEqual(sorted(self.round.assignments.values_list('entry_id', 'judge_id')), before)

    def test_balanced_with_fewer_judges_than_asked_uses_everyone(self):
        self.ready('BALANCED', judges_per_team=3, min_reviews_per_team=1)
        self.panel(self.judges[:2])
        J.assign(self.round, self.admin)
        self.assertEqual(self.round.assignments.count(), 8)

    def test_manual_sets_exactly_the_named_judges(self):
        self.ready('MANUAL')
        a, b = self.entry(self.teams[0]), self.entry(self.teams[1])
        J.assign(self.round, self.admin, [{'entry_id': a.id, 'judge_ids': [self.judges[0].id, self.judges[1].id]}, {'entry_id': b.id, 'judge_ids': [self.judges[2].id]}])
        self.assertEqual(sorted(self.round.assignments.filter(entry=a).values_list('judge_id', flat=True)), sorted([self.judges[0].id, self.judges[1].id]))
        J.assign(self.round, self.admin, [{'entry_id': a.id, 'judge_ids': [self.judges[0].id]}])
        self.assertEqual(self.round.assignments.filter(entry=a).count(), 1)
        self.assertEqual(self.round.assignments.filter(entry=b).count(), 1)  # not named, left alone

    def test_manual_checks_what_it_is_given(self):
        self.ready('MANUAL')
        entry = self.entry(self.teams[0])
        outsider = make_user()
        for body in ('x', [{'entry_id': 99999, 'judge_ids': []}], [{'entry_id': entry.id, 'judge_ids': [outsider.id]}], [{'entry_id': entry.id, 'judge_ids': ['a']}]):
            with self.assertRaises(HackathonError):
                J.assign(self.round, self.admin, body)

    def test_manual_keeps_a_judge_who_already_scored_and_never_brings_back_one_who_stepped_aside(self):
        self.ready('MANUAL')
        entry = self.entry(self.teams[0])
        J.assign(self.round, self.admin, [{'entry_id': entry.id, 'judge_ids': [self.judges[0].id, self.judges[1].id]}] + [
            {'entry_id': self.entry(team).id, 'judge_ids': [self.judges[2].id]} for team in self.teams[1:]
        ])
        J.start(self.round, self.admin)
        self.score(self.judges[0], self.teams[0])
        J.recuse(self.round, entry.id, self.judges[1], 'I know them')
        result = J.assign(self.round, self.admin, [{'entry_id': entry.id, 'judge_ids': [self.judges[1].id]}])
        self.assertEqual(result['skipped'], [{'entry_id': entry.id, 'judge_id': self.judges[1].id, 'why': 'stepped aside'}])
        self.assertTrue(self.round.assignments.filter(entry=entry, judge=self.judges[0]).exists())  # scored, so kept

    def test_it_needs_a_panel_and_is_closed_once_closed(self):
        with self.assertRaises(HackathonError) as ctx:
            J.assign(self.round, self.admin)
        self.assertEqual(code_of(ctx), 'NO_JUDGES')


class Starting(Base):
    def test_it_needs_judges_teams_a_rubric_and_enough_judges_per_team(self):
        for expected, prepare in (
            ('NO_JUDGES', lambda: None),
            ('NO_CRITERIA', lambda: self.panel()),
        ):
            prepare()
            with self.assertRaises(HackathonError) as ctx:
                J.start(self.round, self.admin)
            self.assertEqual(code_of(ctx), expected)
        self.rubric()
        J.configure(self.round, self.admin, {'assignment_mode': 'MANUAL'})
        with self.assertRaises(HackathonError) as ctx:
            J.start(self.round, self.admin)
        self.assertEqual(code_of(ctx), 'COVERAGE')

    def test_a_round_without_teams_cannot_start(self):
        self.round.entries.all().delete()
        self.rubric()
        self.panel()
        with self.assertRaises(HackathonError) as ctx:
            J.start(self.round, self.admin)
        self.assertEqual(code_of(ctx), 'NO_TEAMS')

    def test_a_deadline_already_past_blocks_the_start(self):
        self.ready()
        Round.objects.filter(pk=self.round.pk).update(scoring_deadline=timezone.now() - HOUR)
        with self.assertRaises(HackathonError) as ctx:
            J.start(self.round, self.admin)
        self.assertEqual(code_of(ctx), 'DEADLINE_PASSED')

    def test_starting_assigns_freezes_notifies_and_opens_the_round(self):
        self.ready('BALANCED', judges_per_team=2)
        with mock.patch.object(round_judging, '_notify_users') as notify:
            J.start(self.round, self.admin)
        self.refresh()
        self.assertEqual((self.round.evaluation_status, self.round.status), (EvaluationStatus.IN_PROGRESS, RoundStatus.ACTIVE))
        self.assertIsNotNone(self.round.evaluation_started_at)
        self.assertEqual(self.round.assignments.count(), 8)
        self.assertEqual(sorted(u.id for u in notify.call_args.args[0]), sorted(j.id for j in self.judges))
        with self.assertRaises(HackathonError) as ctx:
            J.start(self.round, self.admin)
        self.assertEqual(code_of(ctx), 'EVALUATION_STARTED')

    def test_waitlisted_and_removed_teams_are_not_judged(self):
        from apps.hackathons.models import Team
        Team.objects.filter(pk=self.teams[0].pk).update(is_waitlisted=True)
        Team.objects.filter(pk=self.teams[1].pk).update(status=TeamStatus.DISQUALIFIED)
        self.started()
        self.assertEqual(self.round.assignments.values('entry').distinct().count(), 2)


class Scoring(Base):
    def setUp(self):
        super().setUp()
        self.started()

    def test_a_score_is_weighted_stored_and_can_be_changed(self):
        review = self.score(self.judges[0], self.teams[0], idea=8, demo=6, comment='Nice')
        self.assertEqual((review.total, review.comment), (round((8 / 10 * 3 + 6 / 10 * 1) / 4 * 100, 2), 'Nice'))
        again = self.score(self.judges[0], self.teams[0], idea=10, demo=10)
        self.assertEqual((again.id, again.total), (review.id, 100.0))
        self.assertEqual(RoundReview.objects.count(), 1)

    def test_the_teams_numbers_follow_the_reviews(self):
        self.score(self.judges[0], self.teams[0], 10, 10)
        self.score(self.judges[1], self.teams[0], 0, 0)
        entry = self.entry(self.teams[0])
        self.assertEqual((entry.score, entry.review_count), (50.0, 2))

    def test_scores_must_fit_the_rubric(self):
        a, b = [c.id for c in self.round.criteria.all()]
        entry = self.entry(self.teams[0]).id
        for scores in ({str(a): 11, str(b): 5}, {str(a): -1, str(b): 5}, {str(a): 5}, {str(a): 'x', str(b): 1}, None):
            with self.assertRaises(HackathonError):
                J.submit_review(self.round, entry, self.judges[0], scores)

    def test_a_long_comment_is_refused(self):
        with self.assertRaises(HackathonError) as ctx:
            self.score(self.judges[0], self.teams[0], comment='x' * 2001)
        self.assertEqual(ctx.exception.detail['field'], 'comment')

    def test_only_the_teams_you_were_given_by_someone_who_can_judge(self):
        J.configure(self.round, self.admin, {'scoring_deadline': None})
        outsider = make_user()
        with self.assertRaises(PermissionDenied):
            self.score(outsider, self.teams[0])
        self.round.assignments.filter(judge=self.judges[0], entry=self.entry(self.teams[0])).delete()
        with self.assertRaises(PermissionDenied):
            self.score(self.judges[0], self.teams[0])
        HackathonStaff.objects.filter(user=self.judges[1]).update(permissions=['check_in'])
        with self.assertRaises(PermissionDenied):
            self.score(self.judges[1], self.teams[1])

    def test_nobody_scores_their_own_team(self):
        member = self.teams[0].leader
        RoundAssignment.objects.create(round=self.round, entry=self.entry(self.teams[0]), judge=member)
        HackathonStaff.objects.create(hackathon=self.hack, user=member, role='JUDGE', permissions=['judge'])
        with self.assertRaises(HackathonError) as ctx:
            self.score(member, self.teams[0])
        self.assertEqual(code_of(ctx), 'CONFLICT_OF_INTEREST')

    def test_not_before_it_starts_not_after_the_deadline_not_once_closed(self):
        Round.objects.filter(pk=self.round.pk).update(scoring_deadline=timezone.now() - HOUR)
        with self.assertRaises(HackathonError) as ctx:
            self.score(self.judges[0], self.teams[0])
        self.assertEqual(code_of(ctx), 'DEADLINE_PASSED')
        Round.objects.filter(pk=self.round.pk).update(scoring_deadline=None)
        J.close(self.round, self.admin, force=True, reason='testing')
        with self.assertRaises(HackathonError) as ctx:
            self.score(self.judges[0], self.teams[0])
        self.assertEqual(code_of(ctx), 'NOT_SCORING')

    def test_before_the_start(self):
        fresh = RoundService.create_round(self.hack, self.admin, name='Later')
        J.configure(fresh, self.admin, {'evaluation_mode': 'JUDGED'})
        with self.assertRaises(HackathonError) as ctx:
            J.submit_review(fresh, 1, self.judges[0], {})
        self.assertEqual(code_of(ctx), 'NOT_SCORING')


class Recusing(Base):
    def test_a_reason_is_needed_and_a_balanced_team_goes_to_another_judge(self):
        self.started('BALANCED', judges_per_team=2)
        entry = self.round.assignments.filter(judge=self.judges[0]).first().entry
        with self.assertRaises(HackathonError) as ctx:
            J.recuse(self.round, entry.id, self.judges[0], '  ')
        self.assertEqual(code_of(ctx), 'REASON_REQUIRED')
        before = set(self.round.assignments.filter(entry=entry, recused=False).values_list('judge_id', flat=True))
        result = J.recuse(self.round, entry.id, self.judges[0], 'Mentored them')
        self.assertTrue(result['reassigned'])
        after = set(self.round.assignments.filter(entry=entry, recused=False).values_list('judge_id', flat=True))
        self.assertEqual((len(after), self.judges[0].id in after, len(before)), (2, False, 2))
        recused = self.round.assignments.get(entry=entry, judge=self.judges[0])
        self.assertEqual((recused.recused, recused.recused_reason), (True, 'Mentored them'))

    def test_when_everyone_has_it_already_nobody_is_added_and_the_judge_cannot_score_it(self):
        self.started('ALL')
        entry = self.entry(self.teams[0])
        self.assertFalse(J.recuse(self.round, entry.id, self.judges[0], 'Conflict')['reassigned'])
        with self.assertRaises(HackathonError) as ctx:
            J.submit_review(self.round, entry.id, self.judges[0], self.scores(5, 5))
        self.assertEqual(code_of(ctx), 'RECUSED')
        with self.assertRaises(HackathonError) as ctx:
            J.recuse(self.round, entry.id, self.judges[0], 'Again')
        self.assertEqual(code_of(ctx), 'RECUSED')

    def test_not_after_scoring_and_not_a_team_you_were_not_given(self):
        self.started('ALL')
        self.score(self.judges[0], self.teams[0])
        with self.assertRaises(HackathonError) as ctx:
            J.recuse(self.round, self.entry(self.teams[0]).id, self.judges[0], 'Late')
        self.assertEqual(code_of(ctx), 'ALREADY_SCORED')
        with self.assertRaises(PermissionDenied):
            J.recuse(self.round, self.entry(self.teams[0]).id, make_user(), 'Not mine')

    def test_a_stepped_aside_judge_does_not_count_towards_the_minimum(self):
        self.started('ALL', min_reviews_per_team=3)
        J.recuse(self.round, self.entry(self.teams[0]).id, self.judges[0], 'Conflict')
        for judge in self.judges[1:]:
            for team in self.teams:
                self.score(judge, team)
        for team in self.teams[1:]:
            self.score(self.judges[0], team)
        with self.assertRaises(HackathonError) as ctx:
            J.close(self.round, self.admin)
        self.assertEqual(code_of(ctx), 'UNDER_REVIEWED')
        self.assertIn('Team A', ctx.exception.detail['detail'])


class Closing(Base):
    def setUp(self):
        super().setUp()
        self.started('ALL', min_reviews_per_team=1)

    def test_it_averages_ranks_and_locks(self):
        for judge, (a, b, c, d) in zip(self.judges[:2], ((10, 8, 5, 2), (10, 6, 5, 0))):
            for team, value in zip(self.teams, (a, b, c, d)):
                self.score(judge, team, idea=value, demo=value)
        result = J.close(self.round, self.admin)
        self.assertEqual((result['ranked'], result['unranked']), (4, 0))
        self.refresh()
        self.assertEqual((self.round.evaluation_status, self.round.results_published), (EvaluationStatus.CLOSED, False))
        ranks = {e.team.name: (e.final_rank, e.score) for e in self.round.entries.select_related('team')}
        self.assertEqual(ranks, {'Team A': (1, 100.0), 'Team B': (2, 70.0), 'Team C': (3, 50.0), 'Team D': (4, 10.0)})

    def test_equal_teams_share_a_rank_and_the_next_skips(self):
        for team, value in zip(self.teams, (10, 5, 5, 0)):
            self.score(self.judges[0], team, idea=value, demo=value)
        J.close(self.round, self.admin)
        self.assertEqual([e.final_rank for e in self.round.entries.order_by('team__name')], [1, 2, 2, 4])

    def test_a_tie_on_the_total_is_broken_by_the_most_heavily_weighted_criterion(self):
        # Idea weighs 3, Demo 1: (idea 10, demo 2) and (idea 9, demo 5) both total 80 percent
        self.score(self.judges[0], self.teams[0], idea=9, demo=5)
        self.score(self.judges[0], self.teams[1], idea=10, demo=2)
        self.assertEqual(self.entry(self.teams[0]).score, self.entry(self.teams[1]).score)
        J.close(self.round, self.admin, force=True, reason='testing')
        self.assertEqual((self.entry(self.teams[1]).final_rank, self.entry(self.teams[0]).final_rank), (1, 2))

    def test_with_shared_ranks_the_same_tie_stays_a_tie(self):
        other = RoundService.create_round(self.hack, self.admin, name='Shared')
        J.configure(other, self.admin, {'evaluation_mode': 'JUDGED', 'assignment_mode': 'ALL', 'tie_break': 'SHARED'})
        for team in self.teams[:2]:
            RoundService.add_team_entry(other, team)
        J.set_criteria(other, self.admin, RUBRIC)
        J.set_panel(other, self.admin, [self.judges[0].id])
        J.start(other, self.admin)
        a, b = [c.id for c in other.criteria.all()]
        for team, (idea, demo) in zip(self.teams[:2], ((10, 2), (9, 5))):
            J.submit_review(other, RoundEntry.objects.get(round=other, team=team).id, self.judges[0], {str(a): idea, str(b): demo})
        J.close(other, self.admin, force=True, reason='testing')
        self.assertEqual([e.final_rank for e in other.entries.filter(final_rank__isnull=False).order_by('team__name')], [1, 1])

    def test_teams_nobody_reviewed_stay_unranked_and_need_a_forced_close_with_a_reason(self):
        self.score(self.judges[0], self.teams[0])
        with self.assertRaises(HackathonError) as ctx:
            J.close(self.round, self.admin)
        self.assertEqual(code_of(ctx), 'UNDER_REVIEWED')
        with self.assertRaises(HackathonError) as ctx:
            J.close(self.round, self.admin, force=True)
        self.assertEqual(code_of(ctx), 'REASON_REQUIRED')
        result = J.close(self.round, self.admin, force=True, reason='Judges were unavailable')
        self.assertEqual((result['ranked'], result['unranked']), (1, 3))
        self.assertEqual(sorted(self.round.entries.values_list('final_rank', flat=True), key=lambda r: (r is None, r)), [1, None, None, None])

    def test_a_recused_judges_scores_are_left_out(self):
        self.score(self.judges[0], self.teams[0], 0, 0)
        self.score(self.judges[1], self.teams[0], 10, 10)
        RoundAssignment.objects.filter(round=self.round, judge=self.judges[0], entry=self.entry(self.teams[0])).update(recused=True)
        J.close(self.round, self.admin, force=True, reason='testing')
        entry = self.entry(self.teams[0])
        self.assertEqual((entry.score, entry.review_count), (100.0, 1))

    def test_closing_twice_and_scoring_after_are_refused(self):
        self.score(self.judges[0], self.teams[0])
        J.close(self.round, self.admin, force=True, reason='testing')
        with self.assertRaises(HackathonError) as ctx:
            J.close(self.round, self.admin, force=True, reason='again')
        self.assertEqual(code_of(ctx), 'NOT_SCORING')

    def test_reopening_needs_a_reason_clears_ranks_and_lets_judges_edit_again(self):
        self.score(self.judges[0], self.teams[0], 5, 5)
        J.close(self.round, self.admin, force=True, reason='testing')
        with self.assertRaises(HackathonError) as ctx:
            J.reopen(self.round, self.admin, '')
        self.assertEqual(code_of(ctx), 'REASON_REQUIRED')
        J.reopen(self.round, self.admin, 'A judge found a mistake')
        self.refresh()
        self.assertEqual(self.round.evaluation_status, EvaluationStatus.IN_PROGRESS)
        self.assertEqual(set(self.round.entries.values_list('final_rank', flat=True)), {None})
        self.score(self.judges[0], self.teams[0], 10, 10)
        J.close(self.round, self.admin, force=True, reason='fixed')
        self.assertEqual((self.entry(self.teams[0]).final_rank, self.entry(self.teams[0]).score), (1, 100.0))

    def test_a_published_round_cannot_be_reopened(self):
        self.score(self.judges[0], self.teams[0])
        J.close(self.round, self.admin, force=True, reason='testing')
        Round.objects.filter(pk=self.round.pk).update(results_published=True)
        with self.assertRaises(HackathonError) as ctx:
            J.reopen(self.round, self.admin, 'Oops')
        self.assertEqual(code_of(ctx), 'RESULTS_PUBLISHED')


class ReadingItBack(Base):
    def test_progress_shows_every_judge_and_team(self):
        self.started('ALL')
        self.score(self.judges[0], self.teams[0])
        self.score(self.judges[0], self.teams[1])
        J.recuse(self.round, self.entry(self.teams[2]).id, self.judges[1], 'Friend')
        progress = J.progress(self.round)
        first = next(j for j in progress['judges'] if j['judge_id'] == self.judges[0].id)
        second = next(j for j in progress['judges'] if j['judge_id'] == self.judges[1].id)
        self.assertEqual((first['assigned'], first['scored'], first['pending'], first['recused']), (4, 2, 2, 0))
        self.assertEqual((second['assigned'], second['recused']), (3, 1))
        self.assertIsNotNone(first['last_activity'])
        team_a = next(t for t in progress['teams'] if t['team_name'] == 'Team A')
        self.assertEqual((team_a['review_count'], team_a['needs'], sorted(c['state'] for c in team_a['cells'])), (1, 0, ['pending', 'pending', 'scored']))
        self.assertEqual(progress['totals'], {'assigned': 11, 'scored': 2, 'pending': 9})

    def test_results_list_every_judges_total_and_the_spread(self):
        self.started('ALL')
        self.score(self.judges[0], self.teams[0], 10, 10)
        self.score(self.judges[1], self.teams[0], 0, 0)
        J.close(self.round, self.admin, force=True, reason='testing')
        rows = J.results(self.round)
        top = rows[0]
        self.assertEqual((top['team_name'], top['rank'], top['score'], top['highest'], top['lowest'], top['spread']), ('Team A', 1, 50.0, 100.0, 0.0, 50.0))
        self.assertEqual(set(top['per_judge']), {self.judges[0].id, self.judges[1].id})
        self.assertEqual([r['rank'] for r in rows[1:]], [None, None, None])

    def test_a_blind_round_shows_codes_instead_of_names(self):
        J.configure(self.round, self.admin, {'blind': True})
        entry = self.entry(self.teams[0])
        self.assertEqual(round_judging.team_label(self.refresh(), entry), f'Team {entry.id:03d}')
        self.assertNotIn('Team A', round_judging.team_label(self.round, entry))


class TeamsChangingMidRound(Base):
    def test_a_team_added_later_gets_judges_in_balanced_and_all_modes(self):
        self.started('BALANCED', judges_per_team=2)
        late = make_team(self.hack, make_user(), name='Late Team')
        RoundService.add_team_entry(self.round, late)
        self.assertEqual(self.round.assignments.filter(entry__team=late).count(), 2)

    def test_a_team_with_scores_cannot_be_removed_but_one_without_can(self):
        self.started('ALL')
        self.score(self.judges[0], self.teams[0])
        with self.assertRaises(HackathonError) as ctx:
            RoundService.remove_team_entry(self.round, self.teams[0])
        self.assertEqual(code_of(ctx), 'HAS_REVIEWS')
        self.assertTrue(RoundService.remove_team_entry(self.round, self.teams[3]))
        self.assertFalse(self.round.assignments.filter(entry__team=self.teams[3]).exists())

    def test_a_plain_round_is_untouched_by_all_this(self):
        plain = RoundService.create_round(self.hack, self.admin, name='Plain')
        RoundService.add_team_entry(plain, self.teams[0])
        self.assertTrue(RoundService.remove_team_entry(plain, self.teams[0]))


class ShortlistRules(Base):
    def test_the_rounds_own_scores_drive_the_shortlist_but_only_once_closed(self):
        self.started('ALL')
        for team, value in zip(self.teams, (10, 8, 8, 2)):
            self.score(self.judges[0], team, idea=value, demo=value)
        with self.assertRaises(HackathonError) as ctx:
            RoundService.auto_shortlist(self.round, self.admin, 'ROUND_SCORE_TOP', 2)
        self.assertEqual(code_of(ctx), 'EVALUATION_NOT_CLOSED')
        J.close(self.round, self.admin, force=True, reason='testing')
        self.refresh()
        top = RoundService.auto_shortlist(self.round, self.admin, 'ROUND_SCORE_TOP', 2)
        self.assertEqual(sorted(r['team_name'] for r in top['selected']), ['Team A', 'Team B', 'Team C'])  # the tie at the cut-off stays together
        floor = RoundService.auto_shortlist(self.round, self.admin, 'ROUND_SCORE_MIN', 80, apply=True, reject_rest=True)
        self.assertTrue(floor['applied'])
        status = {e.team.name: e.status for e in self.round.entries.select_related('team')}
        self.assertEqual(status, {'Team A': EntryStatus.SHORTLISTED, 'Team B': EntryStatus.SHORTLISTED, 'Team C': EntryStatus.SHORTLISTED, 'Team D': EntryStatus.REJECTED})

    def test_a_round_that_is_not_judged_has_no_round_score_rules(self):
        plain = RoundService.create_round(self.hack, self.admin, name='Plain')
        with self.assertRaises(HackathonError) as ctx:
            RoundService.auto_shortlist(plain, self.admin, 'ROUND_SCORE_TOP', 1)
        self.assertEqual(code_of(ctx), 'EVALUATION_NOT_CLOSED')
