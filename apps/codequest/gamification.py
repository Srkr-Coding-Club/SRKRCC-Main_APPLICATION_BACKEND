"""Configurable defaults for the CodeQuest gamification system.

Every value here is a default, not a fixed business rule. Deployments can
override any key by defining a ``CODEQUEST_GAMIFICATION`` dict in
``config/settings.py`` (see settings.py) — overrides are shallow-merged over
these defaults so a project can tune a single knob without restating the rest.

The level progression formula is the single source of truth for levels and
must be used anywhere a level is derived from lifetime XP (backend services
and serializers). The frontend never calculates levels; it renders the
server-provided numbers.
"""

DEFAULT_GAMIFICATION_CONFIG = {
    # XP awarded once per (user, problem) at first accepted review.
    'XP_EASY': 10,
    'XP_MEDIUM': 25,
    'XP_HARD': 50,
    # One-time bonus per scheduled POTD the user completes.
    'XP_POTD_BONUS': 15,
    # XP required to advance from level n -> n+1 is LEVEL_BASE_XP * n,
    # so cumulative XP to *reach* level L is LEVEL_BASE_XP * (L-1) * L / 2.
    'LEVEL_BASE_XP': 100,
    # Displayed "next streak milestone" targets.
    'STREAK_MILESTONES': [3, 7, 14, 30, 50, 100],
    # Default per-user coding goals (distinct problems solved) used when a
    # member has no CodingGoal row.
    'GOAL_DAILY_PROBLEMS': 1,
    'GOAL_WEEKLY_PROBLEMS': 5,
    # Weekly missions (derived from activity, not stored per-user).
    'MISSION_WEEKLY_POTD': 3,
    'MISSION_WEEKLY_ACTIVE_DAYS': 5,
    # Leaderboard size returned by the stats API.
    'LEADERBOARD_SIZE': 10,
    # Seed-time thresholds for configurable badges (BadgeDefinition rows;
    # editing the rows later does not require a code change).
    'BADGE_MEDIUM_MILESTONE': 10,
    'BADGE_HARD_MODE': 5,
    'BADGE_XP_COLLECTOR': 500,
    'BADGE_LEVEL_UP': 5,
}


def get_gamification_config():
    """Return the effective gamification config (defaults + settings override)."""
    from django.conf import settings

    config = dict(DEFAULT_GAMIFICATION_CONFIG)
    override = getattr(settings, 'CODEQUEST_GAMIFICATION', None)
    if isinstance(override, dict):
        config.update(override)
    return config


def xp_for_level(level):
    """Cumulative lifetime XP required to reach ``level`` (level 1 == 0 XP)."""
    if level < 1:
        level = 1
    base = get_gamification_config()['LEVEL_BASE_XP']
    return base * (level - 1) * level // 2


def level_for_xp(lifetime_xp):
    """Highest level whose cumulative XP threshold is <= ``lifetime_xp``."""
    level = 1
    while xp_for_level(level + 1) <= lifetime_xp:
        level += 1
    return level


def get_level_info(lifetime_xp):
    """Level, progress within the level, and XP remaining to the next level."""
    level = level_for_xp(lifetime_xp)
    current_level_base = xp_for_level(level)
    next_level_at = xp_for_level(level + 1)
    return {
        'level': level,
        'lifetime_xp': lifetime_xp,
        'current_level_xp': lifetime_xp - current_level_base,
        'next_level_xp': next_level_at - current_level_base,
        'xp_to_next_level': max(next_level_at - lifetime_xp, 0),
    }


def xp_amount_for_difficulty(difficulty, config=None):
    config = config or get_gamification_config()
    return {
        'EASY': config['XP_EASY'],
        'MEDIUM': config['XP_MEDIUM'],
        'HARD': config['XP_HARD'],
    }.get(difficulty, config['XP_EASY'])


# Badge catalog seed. `threshold_key` names a gamification-config key so
# configurable thresholds follow settings overrides at seed time; keys that
# are plain integers are used literally.
DEFAULT_BADGES = [
    # Problem-solving
    {'code': 'first-solve', 'name': 'First Solve', 'description': 'Solved your first problem.', 'category': 'SOLVING', 'metric': 'UNIQUE_SOLVED', 'threshold': 1, 'icon': 'trophy'},
    {'code': 'century-solver', 'name': 'Century Solver', 'description': 'Solved 100 unique problems.', 'category': 'SOLVING', 'metric': 'UNIQUE_SOLVED', 'threshold': 100, 'icon': 'trophy'},
    {'code': 'half-thousand', 'name': 'Half Thousand', 'description': 'Solved 500 unique problems.', 'category': 'SOLVING', 'metric': 'UNIQUE_SOLVED', 'threshold': 500, 'icon': 'trophy'},
    # Consistency
    {'code': 'getting-started', 'name': 'Getting Started', 'description': 'Coded on 3 different days.', 'category': 'CONSISTENCY', 'metric': 'ACTIVE_DAYS', 'threshold': 3, 'icon': 'calendar'},
    {'code': 'week-warrior', 'name': 'Week Warrior', 'description': 'Maintained a 7-day coding streak.', 'category': 'CONSISTENCY', 'metric': 'CODING_STREAK', 'threshold': 7, 'icon': 'flame'},
    {'code': 'monthly-master', 'name': 'Monthly Master', 'description': 'Maintained a 30-day coding streak.', 'category': 'CONSISTENCY', 'metric': 'CODING_STREAK', 'threshold': 30, 'icon': 'flame'},
    {'code': 'consistency-champion', 'name': 'Consistency Champion', 'description': 'Maintained a 100-day coding streak.', 'category': 'CONSISTENCY', 'metric': 'CODING_STREAK', 'threshold': 100, 'icon': 'flame'},
    # POTD
    {'code': 'potd-pioneer', 'name': 'POTD Pioneer', 'description': 'Completed 10 Problems of the Day.', 'category': 'POTD', 'metric': 'POTD_COMPLETED', 'threshold': 10, 'icon': 'sun'},
    {'code': 'daily-challenger', 'name': 'Daily Challenger', 'description': 'Completed 30 Problems of the Day.', 'category': 'POTD', 'metric': 'POTD_COMPLETED', 'threshold': 30, 'icon': 'sun'},
    {'code': 'potd-master', 'name': 'POTD Master', 'description': 'Completed 100 Problems of the Day.', 'category': 'POTD', 'metric': 'POTD_COMPLETED', 'threshold': 100, 'icon': 'sun'},
    # Difficulty (thresholds configurable via settings)
    {'code': 'medium-milestone', 'name': 'Medium Milestone', 'description': 'Solved {threshold} Medium problems.', 'category': 'DIFFICULTY', 'metric': 'MEDIUM_SOLVED', 'threshold_key': 'BADGE_MEDIUM_MILESTONE', 'icon': 'zap'},
    {'code': 'hard-mode', 'name': 'Hard Mode', 'description': 'Solved {threshold} Hard problems.', 'category': 'DIFFICULTY', 'metric': 'HARD_SOLVED', 'threshold_key': 'BADGE_HARD_MODE', 'icon': 'zap'},
    # XP and levels (thresholds configurable via settings)
    {'code': 'xp-collector', 'name': 'XP Collector', 'description': 'Earned {threshold} lifetime XP.', 'category': 'XP_LEVEL', 'metric': 'LIFETIME_XP', 'threshold_key': 'BADGE_XP_COLLECTOR', 'icon': 'star'},
    {'code': 'level-up', 'name': 'Level Up', 'description': 'Reached level {threshold}.', 'category': 'XP_LEVEL', 'metric': 'LEVEL', 'threshold_key': 'BADGE_LEVEL_UP', 'icon': 'star'},
]
