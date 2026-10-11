"""Read-only aggregation services backing the CodeQuest stats API.

Everything here is scoped to one user by the caller (the view passes
``request.user``). Queries use ``values``/``annotate`` aggregation instead of
per-row loops to avoid N+1 patterns, and all day bucketing uses
``TruncDate`` under the configured ``Asia/Kolkata`` timezone.
"""

from collections import Counter
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate, TruncMonth, TruncWeek
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .gamification import get_gamification_config, get_level_info, level_for_xp
from .models import (
    BadgeCategory,
    BadgeDefinition,
    CodingGoal,
    CodingStreak,
    Problem,
    Submission,
    UserBadge,
    UserStreak,
    XPReward,
)
from .services import user_gamification_metrics

# Heatmap intensity is defined on *accepted unique problems* per day; the
# full submission/solved/XP breakdown is only exposed through the tooltip
# payload, never used to colour the grid.
HEATMAP_INTENSITY_RULES = [
    {'level': 0, 'label': 'No activity', 'min_accepted': 0},
    {'level': 1, 'label': '1 accepted', 'min_accepted': 1},
    {'level': 2, 'label': '2-3 accepted', 'min_accepted': 2},
    {'level': 3, 'label': '4-6 accepted', 'min_accepted': 4},
    {'level': 4, 'label': '7+ accepted', 'min_accepted': 7},
]

MAX_ANALYTICS_DAYS = 366


def _intensity_for(accepted_count):
    if accepted_count >= 7:
        return 4
    if accepted_count >= 4:
        return 3
    if accepted_count >= 2:
        return 2
    if accepted_count >= 1:
        return 1
    return 0


def _daterange(start, end):
    cursor = start
    while cursor <= end:
        yield cursor
        cursor += timedelta(days=1)


def parse_date_param(value, field, default=None):
    if value in (None, ''):
        if default is None:
            raise ValidationError({field: 'This date is required (YYYY-MM-DD).'})
        return default
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValidationError({field: 'Enter a valid date in YYYY-MM-DD format.'}) from None


def _accepted_by_day(user, start, end, extra_filter=None):
    qs = Submission.objects.filter(
        user=user, is_correct=True, created_at__date__gte=start, created_at__date__lte=end
    )
    if extra_filter:
        qs = qs.filter(**extra_filter)
    return {
        row['day']: row['n']
        for row in qs.annotate(day=TruncDate('created_at'))
        .values('day')
        .annotate(n=Count('problem', distinct=True))
    }


def get_daily_stats(user, target_date):
    today = timezone.localdate()
    if target_date > today:
        raise ValidationError({'date': 'Statistics are not available for future dates.'})

    submissions = (
        Submission.objects.filter(user=user, created_at__date=target_date)
        .select_related('problem')
        .order_by('-created_at')
    )
    total = submissions.count()
    accepted_qs = submissions.filter(is_correct=True)
    accepted = accepted_qs.count()
    rejected = total - accepted
    unique_attempted = submissions.values('problem').distinct().count()
    unique_solved = accepted_qs.values('problem').distinct().count()

    solved_by_difficulty = {'EASY': 0, 'MEDIUM': 0, 'HARD': 0}
    for row in (
        accepted_qs.values('problem__difficulty')
        .annotate(n=Count('problem', distinct=True))
    ):
        solved_by_difficulty[row['problem__difficulty']] = row['n']

    potd_problem = Problem.objects.filter(scheduled_date=target_date).first()
    potd = None
    if potd_problem:
        potd = {
            'scheduled': True,
            'problem_id': potd_problem.id,
            'title': potd_problem.title,
            'slug': potd_problem.slug,
            'difficulty': potd_problem.difficulty,
            'external_url': potd_problem.external_url,
            'completed': accepted_qs.filter(problem=potd_problem).exists(),
        }

    xp_earned = (
        XPReward.objects.filter(user=user, created_at__date=target_date)
        .aggregate(total=Sum('amount'))['total']
        or 0
    )

    history = [
        {
            'id': sub.id,
            'problem_id': sub.problem_id,
            'problem_title': sub.problem.title,
            'problem_slug': sub.problem.slug,
            'difficulty': sub.problem.difficulty,
            'external_url': sub.problem.external_url,
            'language': sub.language,
            'is_correct': sub.is_correct,
            'verdict': 'ACCEPTED' if sub.is_correct else 'REJECTED',
            'created_at': sub.created_at,
        }
        for sub in submissions
    ]

    coding_streak = CodingStreak.objects.filter(user=user).first()
    is_active = total > 0
    return {
        'date': target_date,
        'is_today': target_date == today,
        'total_submissions': total,
        'accepted_submissions': accepted,
        'rejected_submissions': rejected,
        'verdict_breakdown': [
            {'verdict': 'ACCEPTED', 'count': accepted},
            {'verdict': 'REJECTED', 'count': rejected},
        ],
        'unique_attempted': unique_attempted,
        'unique_solved': unique_solved,
        'solved_by_difficulty': solved_by_difficulty,
        'potd': potd,
        'xp_earned': xp_earned,
        'is_active_day': is_active,
        'streak_maintained': is_active,
        'coding_streak': {
            'current': coding_streak.current_streak if coding_streak else 0,
            'longest': coding_streak.max_streak if coding_streak else 0,
        },
        'submissions': history,
    }


def get_heatmap(user, year):
    today = timezone.localdate()
    if year is None:
        year = today.year
    if year > today.year or year < 2015:
        raise ValidationError({'year': 'Request a year between 2015 and the current year.'})

    start = date(year, 1, 1)
    end = min(date(year, 12, 31), today)
    if start > end:
        return {
            'year': year,
            'days': [],
            'totals': {'active_days': 0, 'submissions': 0, 'accepted': 0, 'solved': 0, 'xp': 0},
            'most_active_day': None,
            'intensity_rules': HEATMAP_INTENSITY_RULES,
            'available_years': [today.year],
        }

    base = Submission.objects.filter(user=user, created_at__date__gte=start, created_at__date__lte=end)
    activity = {
        row['day']: row
        for row in base.annotate(day=TruncDate('created_at'))
        .values('day')
        .annotate(submissions=Count('id'), accepted=Count('id', filter=Q(is_correct=True)))
    }
    solved_by_day = _accepted_by_day(user, start, end)
    xp_by_day = {
        row['day']: row['xp']
        for row in XPReward.objects.filter(user=user, created_at__date__gte=start, created_at__date__lte=end)
        .annotate(day=TruncDate('created_at'))
        .values('day')
        .annotate(xp=Sum('amount'))
    }
    potd_completed_days = set(
        Submission.objects.filter(
            user=user,
            is_correct=True,
            problem__scheduled_date__gte=start,
            problem__scheduled_date__lte=end,
        ).values_list('problem__scheduled_date', flat=True)
    )
    potd_scheduled_days = set(
        Problem.objects.filter(scheduled_date__gte=start, scheduled_date__lte=end)
        .values_list('scheduled_date', flat=True)
    )

    days = []
    totals = {'active_days': 0, 'submissions': 0, 'accepted': 0, 'solved': 0, 'xp': 0}
    most_active_day = None
    for day in _daterange(start, end):
        row = activity.get(day)
        accepted = row['accepted'] if row else 0
        solved = solved_by_day.get(day, 0)
        submissions = row['submissions'] if row else 0
        xp = xp_by_day.get(day, 0)
        active = submissions > 0
        if active:
            totals['active_days'] += 1
        totals['submissions'] += submissions
        totals['accepted'] += accepted
        totals['solved'] += solved
        totals['xp'] += xp
        if accepted and (most_active_day is None or accepted > most_active_day['accepted']):
            most_active_day = {'date': day, 'accepted': accepted}
        days.append({
            'date': day,
            'submissions': submissions,
            'accepted': accepted,
            'solved': solved,
            'xp': xp,
            'active': active,
            'potd_scheduled': day in potd_scheduled_days,
            'potd_completed': day in potd_completed_days,
            'intensity': _intensity_for(accepted),
        })

    first_submission = (
        Submission.objects.filter(user=user).annotate(day=TruncDate('created_at'))
        .order_by('day').values_list('day', flat=True).first()
    )
    first_year = first_submission.year if first_submission else today.year
    available_years = list(range(first_year, today.year + 1)) or [today.year]

    return {
        'year': year,
        'days': days,
        'totals': totals,
        'most_active_day': most_active_day,
        'intensity_rules': HEATMAP_INTENSITY_RULES,
        'available_years': available_years,
    }


def get_streak_summary(user):
    today = timezone.localdate()
    streak = CodingStreak.objects.filter(user=user).first()
    current = streak.current_streak if streak else 0
    longest = streak.max_streak if streak else 0

    active_dates = set(
        Submission.objects.filter(user=user)
        .annotate(day=TruncDate('created_at'))
        .values_list('day', flat=True)
    )
    total_active_days = len(active_dates)

    milestones = get_gamification_config()['STREAK_MILESTONES']
    next_milestone = next((m for m in milestones if m > longest), None)

    recent = []
    for offset in range(13, -1, -1):
        day = today - timedelta(days=offset)
        recent.append({'date': day, 'active': day in active_dates})

    return {
        'current_streak': current,
        'longest_streak': longest,
        'total_active_days': total_active_days,
        'last_active_date': streak.last_active_date if streak else None,
        'next_milestone': next_milestone,
        'milestones': [{'target': m, 'achieved': longest >= m} for m in milestones],
        'recent_activity': recent,
    }


def get_potd_streak_summary(user):
    today = timezone.localdate()
    streak = UserStreak.objects.filter(user=user).first()
    total_completed = (
        Submission.objects.filter(user=user, is_correct=True)
        .values('problem__scheduled_date').distinct().count()
    )
    total_eligible = Problem.objects.filter(scheduled_date__lte=today).count()
    percentage = round(total_completed / total_eligible * 100, 1) if total_eligible else 0.0

    recent = []
    scheduled_by_date = {
        problem.scheduled_date: problem
        for problem in Problem.objects.filter(
            scheduled_date__gte=today - timedelta(days=13), scheduled_date__lte=today
        )
    }
    completed_dates = set(
        Submission.objects.filter(user=user, is_correct=True)
        .filter(problem__scheduled_date__gte=today - timedelta(days=13))
        .values_list('problem__scheduled_date', flat=True)
    )
    for offset in range(13, -1, -1):
        day = today - timedelta(days=offset)
        problem = scheduled_by_date.get(day)
        recent.append({
            'date': day,
            'scheduled': problem is not None,
            'completed': day in completed_dates,
            'title': problem.title if problem else None,
        })

    potd_milestones = [10, 30, 100]
    next_milestone = next((m for m in potd_milestones if m > total_completed), None)

    return {
        'current_streak': streak.current_streak if streak else 0,
        'longest_streak': streak.max_streak if streak else 0,
        'total_completed': total_completed,
        'total_eligible': total_eligible,
        'completion_percentage': percentage,
        'last_completed_date': streak.last_solved_date if streak else None,
        'next_milestone': next_milestone,
        'milestones': [{'target': m, 'achieved': total_completed >= m} for m in potd_milestones],
        'recent_history': recent,
    }


def get_xp_summary(user):
    today = timezone.localdate()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    def amount_since(start=None):
        qs = XPReward.objects.filter(user=user)
        if start is not None:
            qs = qs.filter(created_at__date__gte=start)
        return qs.aggregate(total=Sum('amount'))['total'] or 0

    lifetime = amount_since()
    breakdown = {
        row['reward_type']: row['total']
        for row in XPReward.objects.filter(user=user)
        .values('reward_type')
        .annotate(total=Sum('amount'))
    }

    history = [
        {
            'id': reward.id,
            'reward_type': reward.reward_type,
            'amount': reward.amount,
            'difficulty': reward.difficulty,
            'scheduled_date': reward.scheduled_date,
            'problem_title': reward.problem.title if reward.problem else None,
            'created_at': reward.created_at,
        }
        for reward in XPReward.objects.filter(user=user).select_related('problem')[:20]
    ]

    return {
        'lifetime_xp': lifetime,
        'xp_today': amount_since(today),
        'xp_this_week': amount_since(week_start),
        'xp_this_month': amount_since(month_start),
        'breakdown': {
            'solve': breakdown.get('SOLVE', 0),
            'potd_bonus': breakdown.get('POTD_BONUS', 0),
        },
        'level': get_level_info(lifetime),
        'history': history,
    }


def get_badges_summary(user):
    metrics = user_gamification_metrics(user)
    earned = {
        row.badge_id: row.earned_at for row in UserBadge.objects.filter(user=user)
    }
    category_labels = dict(BadgeCategory.choices)
    grouped = {key: [] for key in category_labels}
    for badge in BadgeDefinition.objects.all():
        current = metrics.get(badge.metric, 0)
        grouped.setdefault(badge.category, []).append({
            'code': badge.code,
            'name': badge.name,
            'description': badge.description,
            'category': badge.category,
            'metric': badge.metric,
            'threshold': badge.threshold,
            'icon': badge.icon,
            'earned': badge.id in earned,
            'earned_at': earned.get(badge.id),
            'progress': min(current, badge.threshold),
            'percentage': min(100, round(current / badge.threshold * 100)) if badge.threshold else 100,
        })
    categories = [
        {'key': key, 'label': label, 'badges': grouped[key]}
        for key, label in category_labels.items()
        if grouped[key]
    ]
    return {
        'categories': categories,
        'earned_count': len(earned),
        'total_count': BadgeDefinition.objects.count(),
    }


def _solved_between(user, start, end):
    return (
        Submission.objects.filter(
            user=user, is_correct=True, created_at__date__gte=start, created_at__date__lte=end
        )
        .values('problem')
        .distinct()
        .count()
    )


def _goal_progress(progress, target):
    return {
        'target': target,
        'progress': progress,
        'completed': progress >= target,
        'percentage': min(100, round(progress / target * 100)) if target else 100,
    }


def get_goals(user):
    """Daily/weekly coding goals with live progress (distinct problems solved)."""
    config = get_gamification_config()
    goal = CodingGoal.objects.filter(user=user).first()
    daily_target = goal.daily_problems if goal else config['GOAL_DAILY_PROBLEMS']
    weekly_target = goal.weekly_problems if goal else config['GOAL_WEEKLY_PROBLEMS']

    today = timezone.localdate()
    week_start = today - timedelta(days=today.weekday())
    return {
        'date': today,
        'week_start': week_start,
        'daily': _goal_progress(_solved_between(user, today, today), daily_target),
        'weekly': _goal_progress(_solved_between(user, week_start, today), weekly_target),
    }


def set_goals(user, daily_problems, weekly_problems):
    """Persist a member's goal overrides. Returns the recomputed goal summary."""
    CodingGoal.objects.update_or_create(
        user=user,
        defaults={'daily_problems': daily_problems, 'weekly_problems': weekly_problems},
    )
    return get_goals(user)


def _week_window_summary(user, start, end):
    submissions = Submission.objects.filter(
        user=user, created_at__date__gte=start, created_at__date__lte=end
    )
    accepted = submissions.filter(is_correct=True)
    xp = (
        XPReward.objects.filter(
            user=user, created_at__date__gte=start, created_at__date__lte=end
        ).aggregate(total=Sum('amount'))['total']
        or 0
    )
    potd_completed = (
        Problem.objects.filter(scheduled_date__gte=start, scheduled_date__lte=end)
        .filter(submissions__user=user, submissions__is_correct=True)
        .distinct()
        .count()
    )
    return {
        'submissions': submissions.count(),
        'accepted': accepted.count(),
        'solved': accepted.values('problem').distinct().count(),
        'active_days': submissions.annotate(day=TruncDate('created_at')).values('day').distinct().count(),
        'potd_completed': potd_completed,
        'xp': xp,
    }


def get_weekly_report(user):
    """Current-week vs previous-week progress plus derived weekly missions."""
    config = get_gamification_config()
    today = timezone.localdate()
    week_start = today - timedelta(days=today.weekday())
    prev_start = week_start - timedelta(days=7)
    prev_end = week_start - timedelta(days=1)

    current = _week_window_summary(user, week_start, today)
    previous = _week_window_summary(user, prev_start, prev_end)

    per_day = {
        row['day']: row
        for row in Submission.objects.filter(
            user=user, created_at__date__gte=week_start, created_at__date__lte=today
        )
        .annotate(day=TruncDate('created_at'))
        .values('day')
        .annotate(submissions=Count('id'), accepted=Count('id', filter=Q(is_correct=True)))
    }
    solved_by_day = _accepted_by_day(user, week_start, today)

    days = []
    for offset in range(7):
        day = week_start + timedelta(days=offset)
        row = per_day.get(day)
        days.append({
            'date': day,
            'weekday': day.strftime('%A'),
            'submissions': row['submissions'] if row else 0,
            'accepted': row['accepted'] if row else 0,
            'solved': solved_by_day.get(day, 0),
            'future': day > today,
        })

    weekly_target = CodingGoal.objects.filter(user=user).values_list('weekly_problems', flat=True).first()
    if weekly_target is None:
        weekly_target = config['GOAL_WEEKLY_PROBLEMS']
    mission_potd = config['MISSION_WEEKLY_POTD']
    mission_days = config['MISSION_WEEKLY_ACTIVE_DAYS']

    missions = [
        {
            'code': 'weekly-problems',
            'label': f'Solve {weekly_target} problems this week',
            'target': weekly_target,
            'progress': current['solved'],
            'completed': current['solved'] >= weekly_target,
        },
        {
            'code': 'weekly-potd',
            'label': f'Complete {mission_potd} Problems of the Day',
            'target': mission_potd,
            'progress': current['potd_completed'],
            'completed': current['potd_completed'] >= mission_potd,
        },
        {
            'code': 'weekly-active',
            'label': f'Code on {mission_days} days',
            'target': mission_days,
            'progress': current['active_days'],
            'completed': current['active_days'] >= mission_days,
        },
    ]

    return {
        'week_start': week_start,
        'week_end': week_start + timedelta(days=6),
        'today': today,
        'current': current,
        'previous': previous,
        'deltas': {
            'submissions': current['submissions'] - previous['submissions'],
            'solved': current['solved'] - previous['solved'],
            'active_days': current['active_days'] - previous['active_days'],
            'xp': current['xp'] - previous['xp'],
        },
        'days': days,
        'missions': missions,
    }


def get_leaderboard(user, limit=None):
    """Club ranking by lifetime XP. Exposes only public-safe aggregates
    (display name, club id, level, XP, solved count, coding streak) — never
    email or other private profile data — and is scoped to authenticated
    callers by the view."""
    config = get_gamification_config()
    size = max(1, min(limit or config['LEADERBOARD_SIZE'], 50))
    User = get_user_model()

    xp_rows = list(
        XPReward.objects.values('user')
        .annotate(xp=Sum('amount'))
        .order_by('-xp')[:size]
    )
    ranked_ids = [row['user'] for row in xp_rows]
    solved_map = {
        row['user']: row['n']
        for row in Submission.objects.filter(user_id__in=ranked_ids, is_correct=True)
        .values('user')
        .annotate(n=Count('problem', distinct=True))
    }
    streak_map = {
        row.user_id: row.current_streak
        for row in CodingStreak.objects.filter(user_id__in=ranked_ids)
    }
    members = User.objects.in_bulk(ranked_ids)

    entries = []
    for index, row in enumerate(xp_rows, start=1):
        member = members.get(row['user'])
        if member is None:
            continue
        entries.append({
            'rank': index,
            'name': member.full_name,
            'club_id': member.club_id or '',
            'level': level_for_xp(row['xp']),
            'lifetime_xp': row['xp'],
            'solved': solved_map.get(row['user'], 0),
            'current_streak': streak_map.get(row['user'], 0),
            'is_me': row['user'] == user.id,
        })

    my_xp = XPReward.objects.filter(user=user).aggregate(total=Sum('amount'))['total'] or 0
    my_rank = (
        XPReward.objects.values('user').annotate(xp=Sum('amount')).filter(xp__gt=my_xp).count() + 1
    )
    return {
        'entries': entries,
        'size': size,
        'me': {
            'rank': my_rank,
            'level': level_for_xp(my_xp),
            'lifetime_xp': my_xp,
            'solved': Submission.objects.filter(user=user, is_correct=True).values('problem').distinct().count(),
            'current_streak': CodingStreak.objects.filter(user=user).values_list('current_streak', flat=True).first() or 0,
            'in_top': any(entry['is_me'] for entry in entries),
        },
    }


def get_overview(user):
    today = timezone.localdate()
    streak = get_streak_summary(user)
    potd = get_potd_streak_summary(user)
    xp = get_xp_summary(user)
    badges = get_badges_summary(user)

    today_submissions = Submission.objects.filter(user=user, created_at__date=today)
    recent_unlocks = [
        {
            'code': row.badge.code,
            'name': row.badge.name,
            'description': row.badge.description,
            'icon': row.badge.icon,
            'earned_at': row.earned_at,
        }
        for row in UserBadge.objects.filter(
            user=user, earned_at__gte=timezone.now() - timedelta(days=3)
        ).select_related('badge')
    ]

    return {
        'streak': {
            'current': streak['current_streak'],
            'longest': streak['longest_streak'],
            'total_active_days': streak['total_active_days'],
            'next_milestone': streak['next_milestone'],
        },
        'potd_streak': {
            'current': potd['current_streak'],
            'longest': potd['longest_streak'],
            'total_completed': potd['total_completed'],
            'total_eligible': potd['total_eligible'],
            'completion_percentage': potd['completion_percentage'],
        },
        'xp': {
            'lifetime': xp['lifetime_xp'],
            'today': xp['xp_today'],
            'level': xp['level'],
        },
        'badges': {'earned': badges['earned_count'], 'total': badges['total_count']},
        'today': {
            'submissions': today_submissions.count(),
            'accepted': today_submissions.filter(is_correct=True).count(),
            'solved': today_submissions.filter(is_correct=True).values('problem').distinct().count(),
            'xp': xp['xp_today'],
            'potd_completed': potd['recent_history'][-1]['completed'] if potd['recent_history'] else False,
        },
        'recent_unlocks': recent_unlocks,
    }


def get_analytics(user, start, end):
    if start > end:
        raise ValidationError({'start': 'Start date must be on or before the end date.'})
    if (end - start).days + 1 > MAX_ANALYTICS_DAYS:
        raise ValidationError({'start': f'Date range cannot exceed {MAX_ANALYTICS_DAYS} days.'})
    today = timezone.localdate()
    if end > today:
        end = today
    if start > end:
        raise ValidationError({'start': 'No analytics available for the requested range.'})

    submissions = Submission.objects.filter(
        user=user, created_at__date__gte=start, created_at__date__lte=end
    )
    total_submissions = submissions.count()
    accepted_submissions = submissions.filter(is_correct=True).count()
    unique_attempted = submissions.values('problem').distinct().count()
    unique_solved = submissions.filter(is_correct=True).values('problem').distinct().count()

    per_day = {
        row['day']: row
        for row in submissions.annotate(day=TruncDate('created_at'))
        .values('day')
        .annotate(submissions=Count('id'), accepted=Count('id', filter=Q(is_correct=True)))
    }
    solved_by_day = _accepted_by_day(user, start, end)

    daily_series = []
    for day in _daterange(start, end):
        row = per_day.get(day)
        daily_series.append({
            'date': day,
            'submissions': row['submissions'] if row else 0,
            'accepted': row['accepted'] if row else 0,
            'solved': solved_by_day.get(day, 0),
            'active': bool(row),
        })

    weekly_series = [
        {'week_start': row['week'].date(), 'submissions': row['submissions'], 'accepted': row['accepted']}
        for row in submissions.annotate(week=TruncWeek('created_at'))
        .values('week')
        .annotate(submissions=Count('id'), accepted=Count('id', filter=Q(is_correct=True)))
        .order_by('week')
    ]
    monthly_series = [
        {'month': row['month'].date(), 'submissions': row['submissions'], 'accepted': row['accepted']}
        for row in submissions.annotate(month=TruncMonth('created_at'))
        .values('month')
        .annotate(submissions=Count('id'), accepted=Count('id', filter=Q(is_correct=True)))
        .order_by('month')
    ]

    solved_by_difficulty = {'EASY': 0, 'MEDIUM': 0, 'HARD': 0}
    for row in (
        submissions.filter(is_correct=True).values('problem__difficulty')
        .annotate(n=Count('problem', distinct=True))
    ):
        solved_by_difficulty[row['problem__difficulty']] = row['n']

    # Cumulative XP uses the lifetime baseline so the curve reflects the true
    # account total entering the window, not just the window's earnings.
    baseline = (
        XPReward.objects.filter(user=user, created_at__date__lt=start)
        .aggregate(total=Sum('amount'))['total'] or 0
    )
    xp_by_day = {
        row['day']: row['xp']
        for row in XPReward.objects.filter(user=user, created_at__date__gte=start, created_at__date__lte=end)
        .annotate(day=TruncDate('created_at'))
        .values('day')
        .annotate(xp=Sum('amount'))
    }
    xp_growth = []
    running = baseline
    for day in _daterange(start, end):
        running += xp_by_day.get(day, 0)
        xp_growth.append({'date': day, 'lifetime_xp': running})

    potd_series = []
    for problem in Problem.objects.filter(scheduled_date__gte=start, scheduled_date__lte=end).order_by('scheduled_date'):
        completed = Submission.objects.filter(user=user, is_correct=True, problem=problem).exists()
        potd_series.append({
            'date': problem.scheduled_date,
            'title': problem.title,
            'difficulty': problem.difficulty,
            'completed': completed,
        })

    tags = Counter()
    for problem_tags in (
        submissions.filter(is_correct=True).values_list('problem__tags', flat=True).distinct()
    ):
        if isinstance(problem_tags, list):
            for tag in problem_tags:
                if isinstance(tag, str) and tag.strip():
                    tags[tag.strip()] += 1
    topic_performance = [
        {'topic': tag, 'solved': count} for tag, count in tags.most_common(15)
    ]

    coding_streak = CodingStreak.objects.filter(user=user).first()
    potd_streak = UserStreak.objects.filter(user=user).first()
    weekday_counter = Counter()
    for row in (
        submissions.filter(is_correct=True).annotate(day=TruncDate('created_at'))
        .values('day').annotate(n=Count('id', filter=Q(is_correct=True)))
    ):
        weekday_counter[row['day'].strftime('%A')] += row['n']
    best_day = max(daily_series, key=lambda d: d['accepted'], default=None)
    most_productive_weekday = (
        weekday_counter.most_common(1)[0] if weekday_counter else None
    )

    return {
        'range': {'start': start, 'end': end, 'days': (end - start).days + 1},
        'summary': {
            'total_submissions': total_submissions,
            'accepted_submissions': accepted_submissions,
            'unique_attempted': unique_attempted,
            'unique_solved': unique_solved,
            'acceptance_rate': round(accepted_submissions / total_submissions * 100, 1) if total_submissions else 0.0,
        },
        'solved_by_difficulty': solved_by_difficulty,
        'daily_series': daily_series,
        'weekly_series': weekly_series,
        'monthly_series': monthly_series,
        'xp_growth': xp_growth,
        'potd_series': potd_series,
        'topic_performance': topic_performance,
        'records': {
            'longest_coding_streak': coding_streak.max_streak if coding_streak else 0,
            'longest_potd_streak': potd_streak.max_streak if potd_streak else 0,
            'best_day': {'date': best_day['date'], 'accepted': best_day['accepted']} if best_day and best_day['accepted'] else None,
            'most_productive_weekday': (
                {'weekday': most_productive_weekday[0], 'accepted': most_productive_weekday[1]}
                if most_productive_weekday else None
            ),
        },
    }
