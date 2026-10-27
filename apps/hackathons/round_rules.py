"""
Rules that pick who goes through a round from facts the platform already has (idea and project scores, idea
approval, a submitted project, check-in), so an organizer previews a shortlist instead of ticking boxes.
"""
from __future__ import annotations

from collections import Counter

from .errors import HackathonError
from . import check_in_sessions
from .models import IdeaStatus, RoundEntry, TeamMember

TOP, MIN, FLAG = 'top', 'min', 'flag'

# rule -> (kind, label, what the value means)
RULES: dict[str, tuple[str, str, str]] = {
    'PROJECT_SCORE_TOP': (TOP, 'Best project scores', 'How many teams go through'),
    'PROJECT_SCORE_MIN': (MIN, 'Project score at least', 'Minimum score out of 100'),
    'IDEA_SCORE_TOP': (TOP, 'Best idea scores', 'How many teams go through'),
    'IDEA_SCORE_MIN': (MIN, 'Idea score at least', 'Minimum score out of 100'),
    'IDEA_APPROVED': (FLAG, 'Idea approved', ''),
    'HAS_PROJECT': (FLAG, 'Submitted a project', ''),
    'CHECKED_IN': (FLAG, 'At least one member checked in', ''),
    'ALL_CHECKED_IN': (FLAG, 'Every member checked in', ''),
}


def _related(team, name):
    """The team's one-to-one idea / submission, or None."""
    return getattr(team, name, None) if hasattr(team, name) else None


def _metric(entry: RoundEntry, rule: str, checked_in_teams: set[int], fully_checked_in_teams: set[int] = frozenset()):
    """(passes_flag_or_None, number_or_None) for one entry under a rule."""
    team = entry.team
    if rule.startswith('PROJECT_SCORE'):
        project = _related(team, 'submission')
        return None, (project.score if project and project.review_count else None)
    if rule.startswith('IDEA_SCORE'):
        idea = _related(team, 'idea')
        return None, (idea.score if idea and idea.review_count else None)
    if rule == 'IDEA_APPROVED':
        idea = _related(team, 'idea')
        return bool(idea and idea.status == IdeaStatus.APPROVED), None
    if rule == 'HAS_PROJECT':
        return _related(team, 'submission') is not None, None
    if rule == 'ALL_CHECKED_IN':
        return team.id in fully_checked_in_teams, None
    return team.id in checked_in_teams, None


def _fully_checked_in_teams(hackathon) -> set[int]:
    """Teams where every member has arrived (a team with no members never qualifies)."""
    members = Counter(TeamMember.objects.filter(hackathon=hackathon).values_list('team_id', flat=True))
    arrived = Counter(check_in_sessions.main_check_ins(hackathon).values_list('team_id', flat=True))
    return {team_id for team_id, count in members.items() if arrived.get(team_id, 0) >= count}


def evaluate(round_obj, rule: str, value) -> list[dict]:
    """Every entry in the round with whether the rule selects it. Ties at a "top N" cut-off are all kept."""
    if rule not in RULES:
        raise HackathonError(f"Unknown rule. Choose one of: {', '.join(RULES)}.", 'INVALID', field='rule')
    kind = RULES[rule][0]
    number = None
    if kind in (TOP, MIN):
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise HackathonError('Give a number for this rule.', 'INVALID', field='value')
        if kind == TOP and (number < 1 or number != int(number)):
            raise HackathonError('How many teams must be a whole number of 1 or more.', 'INVALID', field='value')
        if kind == MIN and not 0 <= number <= 100:
            raise HackathonError('The minimum score is between 0 and 100.', 'INVALID', field='value')

    entries = list(
        round_obj.entries.select_related('team', 'team__idea', 'team__submission').order_by('team__name')
    )
    checked_in = set(check_in_sessions.main_check_ins(round_obj.hackathon).values_list('team_id', flat=True))
    fully_checked_in = _fully_checked_in_teams(round_obj.hackathon)
    rows = []
    for entry in entries:
        flag, metric = _metric(entry, rule, checked_in, fully_checked_in)
        rows.append({'entry': entry, 'flag': flag, 'metric': metric})

    if kind == FLAG:
        for row in rows:
            row['selected'] = bool(row['flag'])
    elif kind == MIN:
        for row in rows:
            row['selected'] = row['metric'] is not None and row['metric'] >= number
    else:
        scored = sorted((row['metric'] for row in rows if row['metric'] is not None), reverse=True)
        cutoff = scored[int(number) - 1] if len(scored) >= int(number) else (scored[-1] if scored else None)
        for row in rows:
            row['selected'] = cutoff is not None and row['metric'] is not None and row['metric'] >= cutoff

    return [
        {
            'entry_id': row['entry'].id, 'team_id': row['entry'].team_id, 'team_name': row['entry'].team.name,
            'status': row['entry'].status, 'metric': row['metric'], 'selected': row['selected'],
        }
        for row in rows
    ]
