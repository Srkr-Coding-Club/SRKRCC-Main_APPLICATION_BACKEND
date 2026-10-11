from datetime import timedelta

from django.db import transaction
from django.contrib.auth import get_user_model
from django.db.models import Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from .gamification import (
    DEFAULT_BADGES,
    get_gamification_config,
    level_for_xp,
    xp_amount_for_difficulty,
)
from .models import (
    BadgeDefinition,
    CodingStreak,
    RewardType,
    Submission,
    UserBadge,
    UserStreak,
    XPReward,
)


def _current_streak_from_dates(activity_dates, today):
    """Shared streak walk: input must be ascending distinct dates <= today.

    A streak survives until the day after the last activity — a user who has
    not coded *yet* today but coded yesterday still holds their streak."""
    longest = run = 0
    previous = None
    for activity_date in activity_dates:
        run = run + 1 if previous and activity_date == previous + timedelta(days=1) else 1
        longest = max(longest, run)
        previous = activity_date

    current = 0
    if previous and previous >= today - timedelta(days=1):
        current = run
    return current, longest, previous


def rebuild_user_streak(user):
    """Derive streaks from accepted daily problems, making review idempotent."""
    with transaction.atomic():
        # Serialise reviews for one member so one verdict cannot overwrite a
        # streak calculated from another concurrent verdict's stale snapshot.
        locked_user = get_user_model().objects.select_for_update().get(pk=user.pk)
        solved_dates = list(
            Submission.objects.filter(user=locked_user, is_correct=True, problem__scheduled_date__lte=timezone.localdate())
            .values_list('problem__scheduled_date', flat=True)
            .distinct()
            .order_by('problem__scheduled_date')
        )
        current = longest = run = 0
        previous = None
        for solved_date in solved_dates:
            run = run + 1 if previous and solved_date == previous + timedelta(days=1) else 1
            longest = max(longest, run)
            previous = solved_date

        if previous:
            cursor = timezone.localdate()
            while previous < cursor:
                cursor -= timedelta(days=1)
                if previous == cursor:
                    current = run
                    break
            if previous == timezone.localdate():
                current = run
        streak, _ = UserStreak.objects.select_for_update().get_or_create(user=user)
        streak.current_streak = current
        streak.max_streak = longest
        streak.last_solved_date = previous
        streak.save(update_fields=['current_streak', 'max_streak', 'last_solved_date', 'updated_at'])
    return streak


def rebuild_coding_streak(user):
    """Derive the general coding streak from submission activity (any verdict).

    An active day is a calendar day (Asia/Kolkata) with at least one
    submission. Recomputing from source rows keeps the rebuild idempotent, so
    repeated submissions on the same day can never inflate the streak."""
    with transaction.atomic():
        locked_user = get_user_model().objects.select_for_update().get(pk=user.pk)
        active_dates = list(
            Submission.objects.filter(user=locked_user)
            .annotate(day=TruncDate('created_at'))
            .values_list('day', flat=True)
            .distinct()
            .order_by('day')
        )
        today = timezone.localdate()
        current, longest, previous = _current_streak_from_dates(
            [d for d in active_dates if d <= today], today
        )
        streak, _ = CodingStreak.objects.select_for_update().get_or_create(user=user)
        streak.current_streak = current
        streak.max_streak = longest
        streak.last_active_date = previous
        streak.save(update_fields=['current_streak', 'max_streak', 'last_active_date', 'updated_at'])
    return streak


def award_rewards_for_acceptance(submission):
    """Write XP ledger rows for a freshly accepted submission. Idempotent.

    Must be called inside the caller's transaction (the review flow holds a
    row lock on the submission). Unique constraints guarantee at most one
    solve XP per (user, problem) and one POTD bonus per (user, scheduled day)
    even under concurrent reviews."""
    created = []
    problem = submission.problem
    user = submission.user
    config = get_gamification_config()

    solve_reward, solve_created = XPReward.objects.get_or_create(
        user=user,
        reward_type=RewardType.SOLVE,
        problem=problem,
        defaults={
            'amount': xp_amount_for_difficulty(problem.difficulty, config),
            'scheduled_date': problem.scheduled_date,
            'difficulty': problem.difficulty,
        },
    )
    if solve_created:
        created.append(solve_reward)

    # Every problem is a POTD (scheduled_date is unique per day), so the
    # completion bonus is credited to the scheduled day at most once.
    bonus, bonus_created = XPReward.objects.get_or_create(
        user=user,
        reward_type=RewardType.POTD_BONUS,
        scheduled_date=problem.scheduled_date,
        defaults={
            'amount': config['XP_POTD_BONUS'],
            'problem': problem,
            'difficulty': problem.difficulty,
        },
    )
    if bonus_created:
        created.append(bonus)
    return created


def seed_badges():
    """Idempotently sync BadgeDefinition rows with the default catalog.

    Safe to run from migrations or management commands; existing rows are
    updated in place so earned UserBadge links survive."""
    config = get_gamification_config()
    for spec in DEFAULT_BADGES:
        threshold = spec.get('threshold', config.get(spec.get('threshold_key'), 1))
        BadgeDefinition.objects.update_or_create(
            code=spec['code'],
            defaults={
                'name': spec['name'],
                'description': spec['description'].format(threshold=threshold),
                'category': spec['category'],
                'metric': spec['metric'],
                'threshold': threshold,
                'icon': spec.get('icon', ''),
            },
        )


def user_gamification_metrics(user):
    """Single-pass aggregates used by badge evaluation and stats endpoints."""
    accepted = Submission.objects.filter(user=user, is_correct=True)
    lifetime_xp = XPReward.objects.filter(user=user).aggregate(total=Sum('amount'))['total'] or 0
    coding_streak = CodingStreak.objects.filter(user=user).first()
    potd_streak = UserStreak.objects.filter(user=user).first()
    return {
        'UNIQUE_SOLVED': accepted.values('problem').distinct().count(),
        'ACTIVE_DAYS': Submission.objects.filter(user=user)
        .annotate(day=TruncDate('created_at')).values('day').distinct().count(),
        'CODING_STREAK': coding_streak.max_streak if coding_streak else 0,
        'POTD_COMPLETED': accepted.values('problem__scheduled_date').distinct().count(),
        'POTD_STREAK': potd_streak.max_streak if potd_streak else 0,
        'MEDIUM_SOLVED': accepted.filter(problem__difficulty='MEDIUM').values('problem').distinct().count(),
        'HARD_SOLVED': accepted.filter(problem__difficulty='HARD').values('problem').distinct().count(),
        'LIFETIME_XP': lifetime_xp,
        'LEVEL': level_for_xp(lifetime_xp),
    }


def evaluate_achievements(user):
    """Award any badges whose thresholds the user now meets. Idempotent.

    The only write path for achievements — views never award badges directly.
    Returns the list of newly earned UserBadge rows."""
    metrics = user_gamification_metrics(user)
    earned_codes = set(
        UserBadge.objects.filter(user=user).values_list('badge__code', flat=True)
    )
    to_award = []
    for badge in BadgeDefinition.objects.all():
        if badge.code in earned_codes:
            continue
        if metrics.get(badge.metric, 0) >= badge.threshold:
            to_award.append(UserBadge(user=user, badge=badge))
    if to_award:
        # ignore_conflicts backs the uniqueness guarantee if two concurrent
        # requests evaluate the same user at the same moment.
        UserBadge.objects.bulk_create(to_award, ignore_conflicts=True)
    return to_award
