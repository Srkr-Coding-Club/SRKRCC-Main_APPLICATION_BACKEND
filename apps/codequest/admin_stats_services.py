"""Club-wide aggregation services backing the CodeQuest admin dashboard.

Unlike ``stats_services`` (which is always scoped to a single member), these
functions aggregate across every participant and are therefore only reachable
through the admin-gated ``stats/admin-overview`` endpoint. Day bucketing uses
``TruncDate`` under the configured ``Asia/Kolkata`` timezone, and POTD
completion follows the same definition used elsewhere: at least one accepted
submission against a problem's ``scheduled_date``.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Count, Max, Q, Sum
from django.db.models.functions import TruncDate, TruncMonth, TruncWeek
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .gamification import get_gamification_config, level_for_xp
from .models import BadgeDefinition, CodingStreak, Problem, Submission, UserBadge, UserStreak, XPReward
from . import stats_services

MIN_RANGE_DAYS = 7
MAX_RANGE_DAYS = 365
RECENT_SUBMISSION_LIMIT = 8
RECENT_PROBLEM_LIMIT = 5
PENDING_QUEUE_LIMIT = 10
MAX_ANALYTICS_DAYS = 366
TOP_MEMBERS_LIMIT = 5
MEMBER_PREVIEW_LIMIT = 1000

DIFFICULTIES = ('EASY', 'MEDIUM', 'HARD')
STREAK_BUCKETS = ((0, 0), (1, 2), (3, 6), (7, 13), (14, None))


def _submission_summary(submission):
    return {
        'id': submission.id,
        'problem_title': submission.problem.title,
        'scheduled_date': submission.problem.scheduled_date,
        'user_name': submission.user.full_name,
        'user_email': submission.user.email,
        'language': submission.language,
        'is_correct': submission.is_correct,
        'is_reviewed': submission.reviewed_at is not None,
        'created_at': submission.created_at,
    }


def get_admin_overview(days=30):
    days = max(MIN_RANGE_DAYS, min(int(days), MAX_RANGE_DAYS))
    today = timezone.localdate()
    start = today - timedelta(days=days - 1)

    problems_total = Problem.objects.count()
    problems_published = Problem.objects.filter(scheduled_date__lte=today).count()
    problems_upcoming = Problem.objects.filter(scheduled_date__gt=today).count()

    submissions_total = Submission.objects.count()
    submissions_accepted = Submission.objects.filter(is_correct=True).count()
    submissions_pending = Submission.objects.filter(reviewed_at__isnull=True).count()
    acceptance_rate = (
        round(submissions_accepted / submissions_total * 100, 1) if submissions_total else 0.0
    )
    participants_total = Submission.objects.values('user').distinct().count()

    xp_awarded_total = XPReward.objects.aggregate(total=Sum('amount'))['total'] or 0
    potd_completed_total = (
        Submission.objects.filter(is_correct=True)
        .values('user', 'problem__scheduled_date')
        .distinct()
        .count()
    )
    active_streaks = CodingStreak.objects.filter(current_streak__gt=0).count()

    range_submissions = Submission.objects.filter(
        created_at__date__gte=start, created_at__date__lte=today
    )
    per_day = {
        row['day']: row
        for row in range_submissions.annotate(day=TruncDate('created_at'))
        .values('day')
        .annotate(
            submissions=Count('id'),
            accepted=Count('id', filter=Q(is_correct=True)),
            active_users=Count('user', distinct=True),
        )
    }
    potd_by_day = {
        row['problem__scheduled_date']: row['n']
        for row in Submission.objects.filter(
            is_correct=True,
            problem__scheduled_date__gte=start,
            problem__scheduled_date__lte=today,
        )
        .values('problem__scheduled_date')
        .annotate(n=Count('user', distinct=True))
    }

    daily = []
    active_dates = set()
    range_totals = {'submissions': 0, 'accepted': 0, 'active_users': 0, 'potd_completed': 0}
    for offset in range(days):
        day = start + timedelta(days=offset)
        row = per_day.get(day)
        submissions = row['submissions'] if row else 0
        accepted = row['accepted'] if row else 0
        active_users = row['active_users'] if row else 0
        potd_completed = potd_by_day.get(day, 0)
        if submissions:
            active_dates.add(day)
        range_totals['submissions'] += submissions
        range_totals['accepted'] += accepted
        range_totals['potd_completed'] += potd_completed
        daily.append({
            'date': day,
            'submissions': submissions,
            'accepted': accepted,
            'active_users': active_users,
            'potd_completed': potd_completed,
        })
    range_totals['active_users'] = (
        range_submissions.values('user').distinct().count()
    )

    solved_by_difficulty = {'EASY': 0, 'MEDIUM': 0, 'HARD': 0}
    for row in (
        Submission.objects.filter(is_correct=True)
        .values('problem__difficulty')
        .annotate(n=Count('problem', distinct=True))
    ):
        solved_by_difficulty[row['problem__difficulty']] = row['n']
    problems_by_difficulty = {
        row['difficulty']: row['n']
        for row in Problem.objects.values('difficulty').annotate(n=Count('id'))
    }
    by_difficulty = {
        level: {
            'problems': problems_by_difficulty.get(level, 0),
            'solved': solved_by_difficulty.get(level, 0),
        }
        for level in ('EASY', 'MEDIUM', 'HARD')
    }

    level_counts = {}
    for row in XPReward.objects.values('user').annotate(xp=Sum('amount')):
        level = level_for_xp(row['xp'])
        level_counts[level] = level_counts.get(level, 0) + 1
    xp_distribution = [
        {'level': level, 'users': level_counts[level]}
        for level in sorted(level_counts)
    ]

    recent_submissions = [
        _submission_summary(s)
        for s in Submission.objects.select_related('user', 'problem')
        .order_by('-created_at')[:RECENT_SUBMISSION_LIMIT]
    ]
    pending_queue = [
        _submission_summary(s)
        for s in Submission.objects.select_related('user', 'problem')
        .filter(reviewed_at__isnull=True)
        .order_by('-created_at')[:PENDING_QUEUE_LIMIT]
    ]
    recent_problems = [
        {
            'id': p.id,
            'title': p.title,
            'slug': p.slug,
            'difficulty': p.difficulty,
            'scheduled_date': p.scheduled_date,
        }
        for p in Problem.objects.order_by('-created_at')[:RECENT_PROBLEM_LIMIT]
    ]

    return {
        'generated_at': timezone.now(),
        'range': {'days': days, 'start': start, 'end': today},
        'totals': {
            'problems_total': problems_total,
            'problems_published': problems_published,
            'problems_upcoming': problems_upcoming,
            'participants_total': participants_total,
            'submissions_total': submissions_total,
            'submissions_pending': submissions_pending,
            'submissions_accepted': submissions_accepted,
            'acceptance_rate': acceptance_rate,
            'xp_awarded_total': xp_awarded_total,
            'potd_completed_total': potd_completed_total,
            'active_streaks': active_streaks,
        },
        'range_totals': range_totals,
        'daily': daily,
        'by_difficulty': by_difficulty,
        'xp_distribution': xp_distribution,
        'recent_submissions': recent_submissions,
        'recent_problems': recent_problems,
        'pending_queue': pending_queue,
    }


def _date_range(start, end):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def _validate_range(start, end):
    if start > end:
        raise ValidationError({'start': 'Start date must be on or before the end date.'})
    if (end - start).days + 1 > MAX_ANALYTICS_DAYS:
        raise ValidationError({'start': f'Date range cannot exceed {MAX_ANALYTICS_DAYS} days.'})
    today = timezone.localdate()
    if end > today:
        end = today
    if start > end:
        raise ValidationError({'start': 'No analytics available for the requested range.'})
    return start, end


def get_admin_members(search=None, sort='xp'):
    """Club participant directory with per-member gamification aggregates.

    Only members with at least one submission or XP award appear. Aggregates are
    built from grouped queries (no per-row counts) so the directory stays fast."""
    User = get_user_model()
    xp_map = {
        row['user']: row['xp']
        for row in XPReward.objects.values('user').annotate(xp=Sum('amount'))
    }
    submissions_map = {
        row['user']: row['n']
        for row in Submission.objects.values('user').annotate(n=Count('id'))
    }
    solved_map = {
        row['user']: row['n']
        for row in Submission.objects.filter(is_correct=True)
        .values('user')
        .annotate(n=Count('problem', distinct=True))
    }
    potd_map = {
        row['user']: row['n']
        for row in Submission.objects.filter(is_correct=True)
        .values('user')
        .annotate(n=Count('problem__scheduled_date', distinct=True))
    }
    badges_map = {
        row['user']: row['n']
        for row in UserBadge.objects.values('user').annotate(n=Count('id'))
    }
    last_active_map = {
        row['user']: row['last']
        for row in Submission.objects.values('user').annotate(last=Max('created_at'))
    }
    streaks = {streak.user_id: streak for streak in CodingStreak.objects.all()}

    user_ids = set(submissions_map) | set(xp_map)
    queryset = User.objects.filter(id__in=user_ids)
    if search:
        queryset = queryset.filter(
            Q(first_name__icontains=search)
            | Q(last_name__icontains=search)
            | Q(username__icontains=search)
            | Q(email__icontains=search)
            | Q(club_id__icontains=search)
        )

    members = []
    for member in queryset[:MEMBER_PREVIEW_LIMIT]:
        xp = xp_map.get(member.id, 0)
        streak = streaks.get(member.id)
        members.append({
            'id': member.id,
            'name': member.full_name,
            'email': member.email,
            'club_id': member.club_id or '',
            'role': member.role,
            'level': level_for_xp(xp),
            'lifetime_xp': xp,
            'solved': solved_map.get(member.id, 0),
            'submissions': submissions_map.get(member.id, 0),
            'current_streak': streak.current_streak if streak else 0,
            'longest_streak': streak.max_streak if streak else 0,
            'potd_completed': potd_map.get(member.id, 0),
            'badges_earned': badges_map.get(member.id, 0),
            'last_active': last_active_map.get(member.id),
        })

    sorters = {
        'xp': (lambda m: (-m['lifetime_xp'], m['name'].lower()), False),
        'solved': (lambda m: (-m['solved'], m['name'].lower()), False),
        'streak': (lambda m: (-m['current_streak'], m['name'].lower()), False),
        'recent': (lambda m: (m['last_active'] or ''), True),
        'name': (lambda m: (m['name'].lower(),), False),
    }
    key, reverse = sorters.get(sort, sorters['xp'])
    members.sort(key=key, reverse=reverse)

    return {'members': members, 'count': len(members), 'sort': sort}


def get_admin_member_detail(user, days=90):
    days = max(MIN_RANGE_DAYS, min(int(days), MAX_RANGE_DAYS))
    today = timezone.localdate()
    start = today - timedelta(days=days - 1)
    return {
        'member': {
            'id': user.id,
            'name': user.full_name,
            'email': user.email,
            'club_id': user.club_id or '',
            'role': user.role,
        },
        'overview': stats_services.get_overview(user),
        'analytics': stats_services.get_analytics(user, start, today),
    }


def get_admin_analytics(start, end):
    start, end = _validate_range(start, end)
    User = get_user_model()
    submissions = Submission.objects.filter(
        created_at__date__gte=start, created_at__date__lte=end
    )

    per_day = {
        row['day']: row
        for row in submissions.annotate(day=TruncDate('created_at'))
        .values('day')
        .annotate(
            submissions=Count('id'),
            accepted=Count('id', filter=Q(is_correct=True)),
            active_users=Count('user', distinct=True),
        )
    }
    solved_by_day = {
        row['day']: row['n']
        for row in submissions.filter(is_correct=True)
        .annotate(day=TruncDate('created_at'))
        .values('day')
        .annotate(n=Count('problem', distinct=True))
    }

    daily = []
    for day in _date_range(start, end):
        row = per_day.get(day)
        daily.append({
            'date': day,
            'submissions': row['submissions'] if row else 0,
            'accepted': row['accepted'] if row else 0,
            'active_users': row['active_users'] if row else 0,
            'solved': solved_by_day.get(day, 0),
        })

    weekly = [
        {
            'week_start': row['week'].date(),
            'submissions': row['submissions'],
            'accepted': row['accepted'],
        }
        for row in submissions.annotate(week=TruncWeek('created_at'))
        .values('week')
        .annotate(submissions=Count('id'), accepted=Count('id', filter=Q(is_correct=True)))
        .order_by('week')
    ]
    monthly = [
        {
            'month': row['month'].date(),
            'submissions': row['submissions'],
            'accepted': row['accepted'],
        }
        for row in submissions.annotate(month=TruncMonth('created_at'))
        .values('month')
        .annotate(submissions=Count('id'), accepted=Count('id', filter=Q(is_correct=True)))
        .order_by('month')
    ]

    attempted = {
        row['problem__difficulty']: row['n']
        for row in submissions.values('problem__difficulty').annotate(n=Count('id'))
    }
    accepted = {
        row['problem__difficulty']: row['n']
        for row in submissions.filter(is_correct=True)
        .values('problem__difficulty')
        .annotate(n=Count('id'))
    }
    solved = {
        row['problem__difficulty']: row['n']
        for row in submissions.filter(is_correct=True)
        .values('problem__difficulty')
        .annotate(n=Count('problem', distinct=True))
    }
    by_difficulty = {
        level: {
            'attempted': attempted.get(level, 0),
            'accepted': accepted.get(level, 0),
            'solved': solved.get(level, 0),
        }
        for level in DIFFICULTIES
    }

    top_rows = list(
        submissions.filter(is_correct=True)
        .values('user')
        .annotate(solved=Count('problem', distinct=True))
        .order_by('-solved')[:TOP_MEMBERS_LIMIT]
    )
    member_map = User.objects.in_bulk([row['user'] for row in top_rows])
    top_members = [
        {
            'id': row['user'],
            'name': member_map[row['user']].full_name if row['user'] in member_map else '',
            'solved': row['solved'],
        }
        for row in top_rows
    ]

    window = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=window - 1)
    previous_active = (
        Submission.objects.filter(
            created_at__date__gte=prev_start, created_at__date__lte=prev_end
        )
        .values('user')
        .distinct()
        .count()
    )

    submissions_total = submissions.count()
    accepted_total = submissions.filter(is_correct=True).count()
    scheduled_total = Problem.objects.filter(
        scheduled_date__gte=start, scheduled_date__lte=end
    ).count()
    potd_completed = (
        Submission.objects.filter(
            is_correct=True,
            problem__scheduled_date__gte=start,
            problem__scheduled_date__lte=end,
        )
        .values('user', 'problem__scheduled_date')
        .distinct()
        .count()
    )

    return {
        'range': {'start': start, 'end': end, 'days': window},
        'summary': {
            'submissions': submissions_total,
            'accepted': accepted_total,
            'acceptance_rate': round(accepted_total / submissions_total * 100, 1)
            if submissions_total
            else 0.0,
            'unique_solved': submissions.filter(is_correct=True)
            .values('problem')
            .distinct()
            .count(),
            'active_users': submissions.values('user').distinct().count(),
            'previous_active_users': previous_active,
            'potd_scheduled': scheduled_total,
            'potd_completed': potd_completed,
        },
        'daily': daily,
        'weekly': weekly,
        'monthly': monthly,
        'by_difficulty': by_difficulty,
        'top_members': top_members,
    }


def _bucket_streaks(streaks, accessor):
    buckets = []
    for low, high in STREAK_BUCKETS:
        if high is None:
            label = f'{low}+'
        elif low == high:
            label = str(low)
        else:
            label = f'{low}-{high}'
        count = sum(1 for streak in streaks if low <= accessor(streak) and (high is None or accessor(streak) <= high))
        buckets.append({'label': label, 'count': count})
    return buckets


def get_admin_streak_monitoring():
    """Coding and POTD streak health plus today's POTD completion snapshot."""
    today = timezone.localdate()
    coding_streaks = list(CodingStreak.objects.select_related('user'))
    potd_streaks = list(UserStreak.objects.select_related('user'))

    at_risk = sorted(
        (
            streak
            for streak in coding_streaks
            if streak.current_streak > 0
            and streak.last_active_date is not None
            and streak.last_active_date < today
        ),
        key=lambda streak: -streak.current_streak,
    )

    top_coding = sorted(coding_streaks, key=lambda streak: -streak.current_streak)[:TOP_MEMBERS_LIMIT]
    top_potd = sorted(potd_streaks, key=lambda streak: -streak.current_streak)[:TOP_MEMBERS_LIMIT]

    today_problem = Problem.objects.filter(scheduled_date=today).first()
    today_completions = (
        Submission.objects.filter(is_correct=True, problem__scheduled_date=today)
        .values('user')
        .distinct()
        .count()
    )
    today_participants = (
        Submission.objects.filter(created_at__date=today).values('user').distinct().count()
    )

    history_start = today - timedelta(days=13)
    scheduled_dates = set(
        Problem.objects.filter(scheduled_date__gte=history_start, scheduled_date__lte=today)
        .values_list('scheduled_date', flat=True)
    )
    completed_by_date = {
        row['problem__scheduled_date']: row['n']
        for row in Submission.objects.filter(
            is_correct=True,
            problem__scheduled_date__gte=history_start,
            problem__scheduled_date__lte=today,
        )
        .values('problem__scheduled_date')
        .annotate(n=Count('user', distinct=True))
    }
    potd_history = [
        {
            'date': day,
            'scheduled': day in scheduled_dates,
            'completions': completed_by_date.get(day, 0),
        }
        for day in _date_range(history_start, today)
    ]

    return {
        'coding': {
            'active': sum(1 for streak in coding_streaks if streak.current_streak > 0),
            'distribution': _bucket_streaks(coding_streaks, lambda s: s.current_streak),
            'top': [
                {
                    'id': streak.user_id,
                    'name': streak.user.full_name,
                    'current_streak': streak.current_streak,
                    'longest_streak': streak.max_streak,
                }
                for streak in top_coding
            ],
        },
        'potd': {
            'active': sum(1 for streak in potd_streaks if streak.current_streak > 0),
            'distribution': _bucket_streaks(potd_streaks, lambda s: s.current_streak),
            'top': [
                {
                    'id': streak.user_id,
                    'name': streak.user.full_name,
                    'current_streak': streak.current_streak,
                    'longest_streak': streak.max_streak,
                }
                for streak in top_potd
            ],
        },
        'at_risk': [
            {
                'id': streak.user_id,
                'name': streak.user.full_name,
                'current_streak': streak.current_streak,
                'last_active_date': streak.last_active_date,
            }
            for streak in at_risk[:PENDING_QUEUE_LIMIT * 2]
        ],
        'potd_today': {
            'date': today,
            'problem_title': today_problem.title if today_problem else None,
            'problem_slug': today_problem.slug if today_problem else None,
            'difficulty': today_problem.difficulty if today_problem else None,
            'completions': today_completions,
            'participants': today_participants,
        },
        'potd_history': potd_history,
    }


def get_admin_gamification():
    """Badge catalogue with earned counts plus the active scoring configuration."""
    badges = (
        BadgeDefinition.objects.annotate(earned_count=Count('earned_by'))
        .order_by('category', 'threshold')
    )
    return {
        'badges': [
            {
                'id': badge.id,
                'code': badge.code,
                'name': badge.name,
                'description': badge.description,
                'icon': badge.icon,
                'category': badge.category,
                'metric': badge.metric,
                'threshold': badge.threshold,
                'earned_count': badge.earned_count,
            }
            for badge in badges
        ],
        'config': get_gamification_config(),
    }


