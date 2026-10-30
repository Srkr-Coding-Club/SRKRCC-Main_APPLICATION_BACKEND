from datetime import timedelta

from django.db import transaction
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.celebrations import services as celebrations

from .models import Submission, UserStreak

#: A streak is celebrated on the very day it reaches one of these lengths.
STREAK_MILESTONES = (7, 30, 100)


def celebrate_progress(user, streak) -> None:
    """After a correct verdict: the first solved problem, and a streak that has just reached a milestone."""
    if Submission.objects.filter(user=user, is_correct=True).count() == 1:
        celebrations.award_after_commit([user], 'codequest.first_solve', 'first-solve', link_url='/codequest')
    if streak.current_streak in STREAK_MILESTONES:
        celebrations.award_after_commit(
            [user], 'codequest.streak', f'streak:{streak.current_streak}', link_url='/codequest', days=streak.current_streak,
        )


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
