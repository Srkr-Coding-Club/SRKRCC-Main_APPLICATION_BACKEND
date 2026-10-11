"""Tests for the CodeQuest Daily Stats & Gamification system.

Covers zero-states, XP idempotency, POTD bonus uniqueness, streak edge cases
(consecutive/inactive/timezone), badge dedupe, level consistency, date
filters, and per-user scoping.
"""

from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from .gamification import get_level_info, level_for_xp, xp_for_level
from apps.audit.models import AuditLog
from .models import (
    BadgeDefinition,
    CodingGoal,
    CodingStreak,
    Difficulty,
    Problem,
    Submission,
    UserBadge,
    UserStreak,
    XPReward,
)
from .services import (
    award_rewards_for_acceptance,
    evaluate_achievements,
    rebuild_coding_streak,
    rebuild_user_streak,
)

User = get_user_model()


def make_day(offset_days, hour=12):
    """Aware datetime at noon (avoid tz-boundary flakiness) N days from today."""
    day = timezone.localdate() + timedelta(days=offset_days)
    return timezone.make_aware(datetime.combine(day, time(hour, 0)))


class GamificationTestBase(APITestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.member = User.objects.create_user(
            email='member@example.com', username='member', password='safe-password'
        )
        self.other = User.objects.create_user(
            email='other@example.com', username='other', password='safe-password'
        )
        self.admin = User.objects.create_user(
            email='admin@example.com', username='admin', password='safe-password', role='ADMIN'
        )

    def make_problem(self, offset_days, difficulty=Difficulty.EASY, title=None):
        day = self.today + timedelta(days=offset_days)
        return Problem.objects.create(
            title=title or f'Challenge {day.isoformat()}',
            slug=f'challenge-{day.isoformat()}-{difficulty.lower()}',
            difficulty=difficulty,
            statement='Solve it.',
            scheduled_date=day,
        )

    def make_submission(self, user, problem, is_correct, created_offset=0, created_hour=12):
        submission = Submission.objects.create(
            user=user, problem=problem, code='print(1)', language='python', is_correct=is_correct
        )
        Submission.objects.filter(pk=submission.pk).update(
            created_at=make_day(created_offset, created_hour)
        )
        submission.refresh_from_db()
        return submission


class ZeroStateTests(GamificationTestBase):
    def test_no_submissions_daily_stats_are_zero(self):
        self.client.force_authenticate(self.member)
        response = self.client.get(reverse('codequest-stats-daily'), {'date': self.today.isoformat()})
        self.assertEqual(response.status_code, 200)
        data = response.data
        self.assertEqual(data['total_submissions'], 0)
        self.assertEqual(data['accepted_submissions'], 0)
        self.assertEqual(data['unique_solved'], 0)
        self.assertEqual(data['xp_earned'], 0)
        self.assertFalse(data['is_active_day'])
        self.assertIsNone(data['potd'])

    def test_overview_and_badges_zero_state(self):
        self.client.force_authenticate(self.member)
        overview = self.client.get(reverse('codequest-stats-overview'))
        self.assertEqual(overview.status_code, 200)
        self.assertEqual(overview.data['xp']['lifetime'], 0)
        self.assertEqual(overview.data['streak']['current'], 0)

        badges = self.client.get(reverse('codequest-stats-badges'))
        self.assertEqual(badges.status_code, 200)
        self.assertEqual(badges.data['earned_count'], 0)
        self.assertGreater(badges.data['total_count'], 0)


class XPRewardTests(GamificationTestBase):
    def test_solve_xp_awarded_once_per_problem(self):
        problem = self.make_problem(0, Difficulty.MEDIUM)
        submission = self.make_submission(self.member, problem, is_correct=True)

        award_rewards_for_acceptance(submission)
        award_rewards_for_acceptance(submission)  # duplicate acceptance
        submission2 = self.make_submission(self.member, problem, is_correct=True)
        award_rewards_for_acceptance(submission2)  # second accepted submission

        solve_rewards = XPReward.objects.filter(user=self.member, reward_type='SOLVE')
        self.assertEqual(solve_rewards.count(), 1)
        self.assertEqual(solve_rewards.first().amount, 25)

    def test_potd_bonus_awarded_once_per_scheduled_challenge(self):
        problem = self.make_problem(0, Difficulty.EASY)
        submission = self.make_submission(self.member, problem, is_correct=True)
        award_rewards_for_acceptance(submission)
        # A second (different) accepted submission for the same day's problem.
        submission2 = self.make_submission(self.member, problem, is_correct=True)
        award_rewards_for_acceptance(submission2)

        bonuses = XPReward.objects.filter(user=self.member, reward_type='POTD_BONUS')
        self.assertEqual(bonuses.count(), 1)
        self.assertEqual(bonuses.first().amount, 15)
        self.assertEqual(bonuses.first().scheduled_date, self.today)

    @override_settings(CODEQUEST_GAMIFICATION={'XP_HARD': 77})
    def test_xp_amounts_are_configurable(self):
        problem = self.make_problem(0, Difficulty.HARD)
        submission = self.make_submission(self.member, problem, is_correct=True)
        award_rewards_for_acceptance(submission)
        reward = XPReward.objects.get(user=self.member, reward_type='SOLVE')
        self.assertEqual(reward.amount, 77)

    def test_review_endpoint_awards_xp_once(self):
        problem = self.make_problem(0, Difficulty.EASY)
        submission = self.make_submission(self.member, problem, is_correct=False)
        self.client.force_authenticate(self.admin)
        url = reverse('codequest-submission-review', args=[submission.pk])

        first = self.client.post(url, {'is_correct': True}, format='json')
        second = self.client.post(url, {'is_correct': True}, format='json')

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(XPReward.objects.filter(user=self.member, reward_type='SOLVE').count(), 1)
        self.assertEqual(XPReward.objects.filter(user=self.member, reward_type='POTD_BONUS').count(), 1)


class LevelProgressionTests(GamificationTestBase):
    def test_level_formula_is_consistent(self):
        self.assertEqual(level_for_xp(0), 1)
        self.assertEqual(xp_for_level(1), 0)
        self.assertEqual(level_for_xp(xp_for_level(2)), 2)
        self.assertEqual(level_for_xp(xp_for_level(3) - 1), 2)
        self.assertEqual(level_for_xp(xp_for_level(3)), 3)

    def test_level_info_progress(self):
        info = get_level_info(xp_for_level(3) + 10)
        self.assertEqual(info['level'], 3)
        self.assertEqual(info['current_level_xp'], 10)
        self.assertEqual(info['xp_to_next_level'], info['next_level_xp'] - 10)


class StreakTests(GamificationTestBase):
    def test_coding_streak_consecutive_days(self):
        for offset in (-2, -1, 0):
            problem = self.make_problem(offset)
            self.make_submission(self.member, problem, is_correct=False, created_offset=offset)
        streak = rebuild_coding_streak(self.member)
        self.assertEqual(streak.current_streak, 3)
        self.assertEqual(streak.max_streak, 3)

    def test_coding_streak_survives_without_coding_today(self):
        for offset in (-3, -2, -1):
            problem = self.make_problem(offset)
            self.make_submission(self.member, problem, is_correct=False, created_offset=offset)
        streak = rebuild_coding_streak(self.member)
        # Yesterday still counts, so the streak has not been reset yet.
        self.assertEqual(streak.current_streak, 3)

    def test_coding_streak_resets_after_a_skipped_day(self):
        for offset in (-4, -3):
            problem = self.make_problem(offset)
            self.make_submission(self.member, problem, is_correct=False, created_offset=offset)
        streak = rebuild_coding_streak(self.member)
        self.assertEqual(streak.current_streak, 0)
        self.assertEqual(streak.max_streak, 2)

    def test_duplicate_submissions_same_day_do_not_inflate_streak(self):
        problem = self.make_problem(0)
        for _ in range(4):
            self.make_submission(self.member, problem, is_correct=False, created_offset=0)
        streak = rebuild_coding_streak(self.member)
        self.assertEqual(streak.current_streak, 1)
        self.assertEqual(streak.max_streak, 1)

    def test_potd_streak_uses_scheduled_dates(self):
        for offset in (-2, -1, 0):
            problem = self.make_problem(offset)
            self.make_submission(self.member, problem, is_correct=True, created_offset=offset)
        streak = rebuild_user_streak(self.member)
        self.assertEqual(streak.current_streak, 3)
        self.assertEqual(streak.max_streak, 3)

    def test_potd_streak_not_broken_by_rejected_attempt(self):
        accepted = self.make_problem(-1, title='Solved yesterday')
        self.make_submission(self.member, accepted, is_correct=True, created_offset=-1)
        today_problem = self.make_problem(0, title='Today')
        self.make_submission(self.member, today_problem, is_correct=False, created_offset=0)
        # Rejected today counts as coding activity but not POTD completion.
        self.assertEqual(rebuild_coding_streak(self.member).current_streak, 2)
        self.assertEqual(rebuild_user_streak(self.member).current_streak, 1)


class BadgeTests(GamificationTestBase):
    def test_badges_are_not_duplicated(self):
        for offset in range(-2, 1):
            problem = self.make_problem(offset)
            self.make_submission(self.member, problem, is_correct=True, created_offset=offset)
        rebuild_coding_streak(self.member)
        rebuild_user_streak(self.member)
        evaluate_achievements(self.member)
        evaluate_achievements(self.member)

        self.assertEqual(
            UserBadge.objects.filter(user=self.member, badge__code='first-solve').count(), 1
        )
        self.assertEqual(
            UserBadge.objects.filter(user=self.member, badge__code='getting-started').count(), 1
        )

    def test_badge_threshold_is_configurable(self):
        badge = BadgeDefinition.objects.get(code='hard-mode')
        badge.threshold = 1
        badge.save(update_fields=['threshold'])
        problem = self.make_problem(0, Difficulty.HARD)
        self.make_submission(self.member, problem, is_correct=True, created_offset=0)
        evaluate_achievements(self.member)
        self.assertTrue(
            UserBadge.objects.filter(user=self.member, badge=badge).exists()
        )


class StatsEndpointTests(GamificationTestBase):
    def test_daily_stats_date_filter(self):
        yesterday_problem = self.make_problem(-1)
        self.make_submission(self.member, yesterday_problem, is_correct=True, created_offset=-1)
        today_problem = self.make_problem(0)
        self.make_submission(self.member, today_problem, is_correct=False, created_offset=0)

        self.client.force_authenticate(self.member)
        yesterday = self.client.get(
            reverse('codequest-stats-daily'),
            {'date': (self.today - timedelta(days=1)).isoformat()},
        )
        today = self.client.get(reverse('codequest-stats-daily'), {'date': self.today.isoformat()})
        self.assertEqual(yesterday.data['accepted_submissions'], 1)
        self.assertEqual(yesterday.data['total_submissions'], 1)
        self.assertEqual(today.data['accepted_submissions'], 0)
        self.assertEqual(today.data['rejected_submissions'], 1)

    def test_future_date_is_rejected(self):
        self.client.force_authenticate(self.member)
        response = self.client.get(
            reverse('codequest-stats-daily'),
            {'date': (self.today + timedelta(days=1)).isoformat()},
        )
        self.assertEqual(response.status_code, 400)

    def test_stats_are_scoped_to_the_authenticated_user(self):
        problem = self.make_problem(0)
        self.make_submission(self.other, problem, is_correct=True, created_offset=0)
        self.client.force_authenticate(self.member)
        response = self.client.get(reverse('codequest-stats-daily'), {'date': self.today.isoformat()})
        self.assertEqual(response.data['total_submissions'], 0)
        self.assertEqual(response.data['xp_earned'], 0)

    def test_heatmap_uses_accepted_for_intensity(self):
        problem = self.make_problem(0)
        self.make_submission(self.member, problem, is_correct=True, created_offset=0)
        self.client.force_authenticate(self.member)
        response = self.client.get(reverse('codequest-stats-heatmap'))
        self.assertEqual(response.status_code, 200)
        today_cell = next(cell for cell in response.data['days'] if cell['date'] == self.today)
        self.assertEqual(today_cell['intensity'], 1)
        self.assertEqual(today_cell['accepted'], 1)

    def test_potd_streak_endpoint(self):
        self.client.force_authenticate(self.member)
        response = self.client.get(reverse('codequest-stats-potd-streak'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('completion_percentage', response.data)

    def test_analytics_rejects_oversized_range(self):
        self.client.force_authenticate(self.member)
        response = self.client.get(reverse('codequest-stats-analytics'), {
            'start': (self.today - timedelta(days=400)).isoformat(),
            'end': self.today.isoformat(),
        })
        self.assertEqual(response.status_code, 400)

    def test_stats_require_authentication(self):
        response = self.client.get(reverse('codequest-stats-overview'))
        self.assertIn(response.status_code, (401, 403))


class GoalTests(GamificationTestBase):
    def test_zero_state_uses_configured_defaults(self):
        self.client.force_authenticate(self.member)
        response = self.client.get(reverse('codequest-stats-goals'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['daily']['target'], 1)
        self.assertEqual(response.data['weekly']['target'], 5)
        self.assertEqual(response.data['daily']['progress'], 0)
        self.assertFalse(response.data['daily']['completed'])

    def test_goals_track_distinct_problem_solves(self):
        problem = self.make_problem(0)
        self.make_submission(self.member, problem, is_correct=True, created_offset=0)
        self.make_submission(self.member, problem, is_correct=True, created_offset=0)  # duplicate
        self.client.force_authenticate(self.member)
        response = self.client.get(reverse('codequest-stats-goals'))
        self.assertEqual(response.data['daily']['progress'], 1)
        self.assertEqual(response.data['daily']['percentage'], 100)
        self.assertTrue(response.data['daily']['completed'])

    def test_goal_overrides_persist(self):
        self.client.force_authenticate(self.member)
        response = self.client.post(
            reverse('codequest-stats-goals'),
            {'daily_problems': 3, 'weekly_problems': 12},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['daily']['target'], 3)
        self.assertEqual(response.data['weekly']['target'], 12)
        self.assertEqual(CodingGoal.objects.get(user=self.member).weekly_problems, 12)
        self.assertEqual(
            self.client.get(reverse('codequest-stats-goals')).data['weekly']['target'], 12
        )

    def test_goal_validation_rejects_invalid_values(self):
        self.client.force_authenticate(self.member)
        response = self.client.post(
            reverse('codequest-stats-goals'),
            {'daily_problems': 0, 'weekly_problems': 'abc'},
            format='json',
        )
        self.assertEqual(response.status_code, 400)


class WeeklyReportTests(GamificationTestBase):
    def test_zero_state_report(self):
        self.client.force_authenticate(self.member)
        response = self.client.get(reverse('codequest-stats-report'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['current']['submissions'], 0)
        self.assertEqual(response.data['current']['xp'], 0)
        self.assertEqual(len(response.data['days']), 7)
        self.assertEqual(len(response.data['missions']), 3)
        self.assertFalse(any(mission['completed'] for mission in response.data['missions']))

    def test_report_compares_current_and_previous_week(self):
        previous_problem = self.make_problem(-7, title='Previous week')
        self.make_submission(self.member, previous_problem, is_correct=True, created_offset=-7)
        current_problem = self.make_problem(0, title='Current week')
        self.make_submission(self.member, current_problem, is_correct=True, created_offset=0)

        self.client.force_authenticate(self.member)
        response = self.client.get(reverse('codequest-stats-report'))
        self.assertEqual(response.data['current']['solved'], 1)
        self.assertEqual(response.data['previous']['solved'], 1)
        self.assertEqual(response.data['current']['active_days'], 1)
        self.assertEqual(response.data['deltas']['solved'], 0)

    def test_weekly_problems_mission_uses_goal_target(self):
        self.client.force_authenticate(self.member)
        self.client.post(
            reverse('codequest-stats-goals'),
            {'daily_problems': 1, 'weekly_problems': 1},
            format='json',
        )
        problem = self.make_problem(0)
        self.make_submission(self.member, problem, is_correct=True, created_offset=0)
        response = self.client.get(reverse('codequest-stats-report'))
        mission = next(m for m in response.data['missions'] if m['code'] == 'weekly-problems')
        self.assertEqual(mission['target'], 1)
        self.assertEqual(mission['progress'], 1)
        self.assertTrue(mission['completed'])


class LeaderboardTests(GamificationTestBase):
    def award(self, user, difficulty, day_offset):
        problem = self.make_problem(
            day_offset, difficulty, title=f'{user.username}-{difficulty}-{day_offset}'
        )
        submission = self.make_submission(user, problem, is_correct=True, created_offset=0)
        award_rewards_for_acceptance(submission)

    def test_empty_leaderboard(self):
        self.client.force_authenticate(self.member)
        response = self.client.get(reverse('codequest-stats-leaderboard'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['entries'], [])
        self.assertEqual(response.data['me']['rank'], 1)
        self.assertEqual(response.data['me']['lifetime_xp'], 0)

    def test_leaderboard_orders_by_lifetime_xp(self):
        self.award(self.other, Difficulty.HARD, -2)
        self.award(self.member, Difficulty.MEDIUM, -1)
        self.client.force_authenticate(self.member)
        response = self.client.get(reverse('codequest-stats-leaderboard'))
        entries = response.data['entries']
        self.assertEqual(
            [entry['name'] for entry in entries],
            [self.other.full_name, self.member.full_name],
        )
        self.assertGreater(entries[0]['lifetime_xp'], entries[1]['lifetime_xp'])
        self.assertTrue(entries[1]['is_me'])
        self.assertEqual(response.data['me']['rank'], 2)

    def test_leaderboard_hides_private_fields(self):
        self.award(self.other, Difficulty.EASY, 0)
        self.client.force_authenticate(self.member)
        response = self.client.get(reverse('codequest-stats-leaderboard'))
        entry = response.data['entries'][0]
        self.assertNotIn('email', entry)
        self.assertNotIn('user', entry)
        self.assertIn('name', entry)

    def test_leaderboard_limit_and_self_rank(self):
        self.award(self.other, Difficulty.HARD, -2)
        self.award(self.member, Difficulty.MEDIUM, -1)
        self.client.force_authenticate(self.member)
        response = self.client.get(reverse('codequest-stats-leaderboard'), {'limit': 1})
        self.assertEqual(len(response.data['entries']), 1)
        self.assertEqual(response.data['entries'][0]['name'], self.other.full_name)
        self.assertFalse(response.data['me']['in_top'])

    def test_leaderboard_rejects_invalid_limit(self):
        self.client.force_authenticate(self.member)
        response = self.client.get(reverse('codequest-stats-leaderboard'), {'limit': 'abc'})
        self.assertEqual(response.status_code, 400)

    def test_leaderboard_requires_authentication(self):
        response = self.client.get(reverse('codequest-stats-leaderboard'))
        self.assertIn(response.status_code, (401, 403))


class ReviewProvenanceTests(GamificationTestBase):
    def test_review_records_reviewer_and_timestamp(self):
        problem = self.make_problem(0, Difficulty.EASY)
        submission = self.make_submission(self.member, problem, is_correct=False)
        self.assertIsNone(submission.reviewed_at)

        self.client.force_authenticate(self.admin)
        url = reverse('codequest-submission-review', args=[submission.pk])
        response = self.client.post(url, {'is_correct': True}, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['is_reviewed'])
        self.assertEqual(response.data['reviewed_by'], self.admin.id)
        self.assertEqual(response.data['reviewed_by_name'], self.admin.full_name)
        self.assertIsNotNone(response.data['reviewed_at'])

        submission.refresh_from_db()
        self.assertEqual(submission.reviewed_by, self.admin)
        self.assertIsNotNone(submission.reviewed_at)

    def test_member_cannot_review_submissions(self):
        problem = self.make_problem(0, Difficulty.EASY)
        submission = self.make_submission(self.member, problem, is_correct=False)
        self.client.force_authenticate(self.member)
        url = reverse('codequest-submission-review', args=[submission.pk])
        response = self.client.post(url, {'is_correct': True}, format='json')
        self.assertIn(response.status_code, (403, 404))


class AdminOverviewTests(GamificationTestBase):
    def test_member_is_denied_admin_overview(self):
        self.client.force_authenticate(self.member)
        response = self.client.get(reverse('codequest-stats-admin-overview'))
        self.assertEqual(response.status_code, 403)

    def test_admin_sees_club_wide_aggregates(self):
        problem = self.make_problem(0, Difficulty.MEDIUM)
        submission = self.make_submission(self.member, problem, is_correct=True)
        award_rewards_for_acceptance(submission)

        self.client.force_authenticate(self.admin)
        response = self.client.get(reverse('codequest-stats-admin-overview'))

        self.assertEqual(response.status_code, 200)
        totals = response.data['totals']
        self.assertEqual(totals['participants_total'], 1)
        self.assertEqual(totals['submissions_total'], 1)
        self.assertEqual(totals['submissions_accepted'], 1)
        self.assertEqual(totals['submissions_pending'], 1)
        self.assertEqual(totals['acceptance_rate'], 100.0)
        self.assertGreater(totals['xp_awarded_total'], 0)
        self.assertEqual(totals['potd_completed_total'], 1)
        self.assertEqual(len(response.data['daily']), 30)

    def test_admin_overview_days_is_validated(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get(reverse('codequest-stats-admin-overview'), {'days': 'abc'})
        self.assertEqual(response.status_code, 400)




class AdminMemberDirectoryTests(GamificationTestBase):
    def test_member_is_denied_directory(self):
        self.client.force_authenticate(self.member)
        response = self.client.get(reverse('codequest-stats-admin-members'))
        self.assertEqual(response.status_code, 403)

    def test_admin_sees_member_aggregates_and_sorting(self):
        problem = self.make_problem(-1, Difficulty.HARD)
        solved = self.make_submission(self.member, problem, is_correct=True)
        award_rewards_for_acceptance(solved)
        self.make_submission(self.other, problem, is_correct=False)

        self.client.force_authenticate(self.admin)
        response = self.client.get(reverse('codequest-stats-admin-members'), {'sort': 'solved'})

        self.assertEqual(response.status_code, 200)
        members = response.data['members']
        self.assertEqual(response.data['count'], 2)
        self.assertEqual(members[0]['id'], self.member.id)
        self.assertEqual(members[0]['solved'], 1)
        self.assertGreater(members[0]['lifetime_xp'], 0)
        self.assertEqual(members[1]['id'], self.other.id)
        self.assertEqual(members[1]['solved'], 0)

    def test_directory_search_filters_members(self):
        problem = self.make_problem(-1, Difficulty.EASY)
        self.make_submission(self.member, problem, is_correct=False)
        self.make_submission(self.other, problem, is_correct=False)

        self.client.force_authenticate(self.admin)
        response = self.client.get(
            reverse('codequest-stats-admin-members'), {'q': 'member@example.com'}
        )

        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['members'][0]['id'], self.member.id)

    def test_member_detail_returns_overview_and_analytics(self):
        problem = self.make_problem(0, Difficulty.MEDIUM)
        submission = self.make_submission(self.member, problem, is_correct=True)
        award_rewards_for_acceptance(submission)

        self.client.force_authenticate(self.admin)
        url = reverse('codequest-stats-admin-member-detail', args=[self.member.id])
        response = self.client.get(url, {'days': 30})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['member']['id'], self.member.id)
        self.assertIn('overview', response.data)
        self.assertEqual(len(response.data['analytics']['daily_series']), 30)

    def test_member_detail_unknown_user_yields_404(self):
        self.client.force_authenticate(self.admin)
        url = reverse('codequest-stats-admin-member-detail', args=[999999])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)


class AdminAnalyticsTests(GamificationTestBase):
    def test_member_is_denied_analytics(self):
        self.client.force_authenticate(self.member)
        response = self.client.get(reverse('codequest-stats-admin-analytics'))
        self.assertEqual(response.status_code, 403)

    def test_admin_sees_club_analytics_for_range(self):
        problem = self.make_problem(0, Difficulty.EASY)
        submission = self.make_submission(self.member, problem, is_correct=True)
        award_rewards_for_acceptance(submission)

        self.client.force_authenticate(self.admin)
        response = self.client.get(
            reverse('codequest-stats-admin-analytics'),
            {'start': (self.today - timedelta(days=6)).isoformat(), 'end': self.today.isoformat()},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['range']['days'], 7)
        self.assertEqual(len(response.data['daily']), 7)
        self.assertEqual(response.data['summary']['submissions'], 1)
        self.assertEqual(response.data['summary']['accepted'], 1)
        self.assertEqual(response.data['summary']['acceptance_rate'], 100.0)
        self.assertEqual(response.data['summary']['potd_completed'], 1)
        self.assertEqual(response.data['summary']['active_users'], 1)
        self.assertEqual(response.data['top_members'][0]['solved'], 1)

    def test_analytics_rejects_inverted_range(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get(
            reverse('codequest-stats-admin-analytics'),
            {'start': self.today.isoformat(), 'end': (self.today - timedelta(days=3)).isoformat()},
        )
        self.assertEqual(response.status_code, 400)


class AdminStreakMonitoringTests(GamificationTestBase):
    def test_member_is_denied_streak_monitoring(self):
        self.client.force_authenticate(self.member)
        response = self.client.get(reverse('codequest-stats-admin-streaks'))
        self.assertEqual(response.status_code, 403)

    def test_admin_sees_streak_and_potd_snapshot(self):
        CodingStreak.objects.create(
            user=self.member,
            current_streak=4,
            max_streak=9,
            last_active_date=self.today - timedelta(days=1),
        )
        UserStreak.objects.create(
            user=self.member,
            current_streak=2,
            max_streak=6,
            last_solved_date=self.today,
        )
        problem = self.make_problem(0, Difficulty.EASY)
        submission = self.make_submission(self.member, problem, is_correct=True)
        award_rewards_for_acceptance(submission)

        self.client.force_authenticate(self.admin)
        response = self.client.get(reverse('codequest-stats-admin-streaks'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['coding']['active'], 1)
        self.assertEqual(response.data['coding']['top'][0]['current_streak'], 4)
        self.assertEqual(len(response.data['at_risk']), 1)
        self.assertEqual(response.data['at_risk'][0]['id'], self.member.id)
        self.assertEqual(response.data['potd_today']['problem_slug'], problem.slug)
        self.assertEqual(response.data['potd_today']['completions'], 1)
        self.assertEqual(len(response.data['potd_history']), 14)


class AdminGamificationTests(GamificationTestBase):
    def test_member_is_denied_gamification_catalogue(self):
        self.client.force_authenticate(self.member)
        response = self.client.get(reverse('codequest-stats-admin-gamification'))
        self.assertEqual(response.status_code, 403)

    def test_admin_sees_badge_catalogue_with_earned_counts(self):
        badge = BadgeDefinition.objects.first()
        UserBadge.objects.create(user=self.member, badge=badge)

        self.client.force_authenticate(self.admin)
        response = self.client.get(reverse('codequest-stats-admin-gamification'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['badges']), BadgeDefinition.objects.count())
        earned = next(b for b in response.data['badges'] if b['id'] == badge.id)
        self.assertEqual(earned['earned_count'], 1)
        self.assertIn('XP_EASY', response.data['config'])


class ProblemStatsTests(GamificationTestBase):
    def test_problem_list_exposes_submission_and_solved_counts(self):
        problem = self.make_problem(0, Difficulty.EASY)
        self.make_submission(self.member, problem, is_correct=True)
        self.make_submission(self.other, problem, is_correct=False)

        self.client.force_authenticate(self.admin)
        response = self.client.get(reverse('codequest-problem-detail', args=[problem.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['submissions_count'], 2)
        self.assertEqual(response.data['solved_count'], 1)

    def test_public_problem_list_includes_counts(self):
        problem = self.make_problem(0, Difficulty.EASY)
        self.make_submission(self.member, problem, is_correct=True)

        response = self.client.get(reverse('codequest-problem-list'))

        self.assertEqual(response.status_code, 200)
        results = response.data['results'] if isinstance(response.data, dict) else response.data
        match = next(item for item in results if item['slug'] == problem.slug)
        self.assertEqual(match['solved_count'], 1)


class AdminReportExportTests(GamificationTestBase):
    def csv_body(self, response):
        return response.content.decode('utf-8-sig')

    def test_member_is_denied_export(self):
        self.client.force_authenticate(self.member)
        response = self.client.get(
            reverse('codequest-stats-admin-export'), {'dataset': 'members'}
        )
        self.assertEqual(response.status_code, 403)

    def test_members_export_streams_csv_attachment(self):
        problem = self.make_problem(0, Difficulty.EASY)
        submission = self.make_submission(self.member, problem, is_correct=True)
        award_rewards_for_acceptance(submission)

        self.client.force_authenticate(self.admin)
        response = self.client.get(reverse('codequest-stats-admin-export'), {'dataset': 'members'})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response['Content-Type'].startswith('text/csv'))
        self.assertIn('codequest_members_', response['Content-Disposition'])
        body = response.content.decode('utf-8-sig')
        self.assertIn('Lifetime XP', body)
        self.assertIn('member@example.com', body)

    def test_submissions_export_respects_verdict_filter(self):
        problem = self.make_problem(0, Difficulty.EASY)
        self.make_submission(self.member, problem, is_correct=False)

        self.client.force_authenticate(self.admin)
        response = self.client.get(
            reverse('codequest-stats-admin-export'),
            {'dataset': 'submissions', 'verdict': 'pending'},
        )

        self.assertEqual(response.status_code, 200)
        body = response.content.decode('utf-8-sig')
        self.assertIn(problem.title, body)

    def test_unknown_dataset_is_rejected(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get(
            reverse('codequest-stats-admin-export'), {'dataset': 'secrets'}
        )
        self.assertEqual(response.status_code, 400)

    def test_export_is_recorded_on_the_audit_trail(self):
        self.client.force_authenticate(self.admin)
        self.client.get(
            reverse('codequest-stats-admin-export'), {'dataset': 'problems'}
        )

        self.assertTrue(
            AuditLog.objects.filter(
                action='codequest.report_exported', target_id='problems'
            ).exists()
        )
