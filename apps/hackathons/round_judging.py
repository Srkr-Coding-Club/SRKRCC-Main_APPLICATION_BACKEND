"""
Judging one round: the panel, who scores which team, the scores, and closing the round into a ranking.

A round is *judged* when ``evaluation_mode`` is JUDGED and the hackathon has ``features.round_judging``. The life of a judged round:

    NOT_STARTED  - the organizer picks the teams (the round's entries), the panel, the rubric and how teams are shared out
    IN_PROGRESS  - ``start``: the rubric is frozen, every team has enough judges, the panel is told; judges score, may edit
                   their scores until the deadline and may step aside (recuse) from a team they have not scored
    CLOSED       - ``close``: scores are averaged, teams ranked (ties broken by the most heavily weighted criteria), and the
                   round's own scores can drive the shortlist (rules ROUND_SCORE_TOP / ROUND_SCORE_MIN)

Everything here is separate from the older hackathon-wide idea and project judging, which is left exactly as it was.
"""
from __future__ import annotations

import statistics
from collections import Counter

from django.db import transaction
from django.db.models import Avg, Count
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import PermissionDenied

from apps.audit.utils import log_audit_event

from . import access
from .errors import HackathonError
from .judging import MAX_COMMENT, MAX_CRITERIA, CriteriaService, JudgingService
from .models import (
    AssignmentMode, CriterionTarget, EvaluationMode, EvaluationStatus, JudgingCriterion, Round, RoundAssignment, RoundEntry, RoundJudge,
    RoundReview, RoundStatus, ScoringTarget, TeamStatus, TieBreak,
)
from .services import _notify_users, is_member

MAX_PANEL = 50
MAX_PER_TEAM = 20
MAX_REASON = 300
REMOVED_TEAMS = (TeamStatus.DISQUALIFIED, TeamStatus.WITHDRAWN)
SETTINGS_FIELDS = ('evaluation_mode', 'scoring_target', 'assignment_mode', 'judges_per_team', 'min_reviews_per_team', 'scoring_deadline', 'blind', 'tie_break')


# ---------------------------------------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------------------------------------

def _require_feature(round_obj: Round) -> None:
    if not round_obj.hackathon.feature('round_judging'):
        raise HackathonError('Round judging is not switched on for this hackathon.', 'FEATURE_OFF')


def _require_judged(round_obj: Round) -> None:
    _require_feature(round_obj)
    if round_obj.evaluation_mode != EvaluationMode.JUDGED:
        raise HackathonError('This round is not judged by a panel.', 'NOT_JUDGED')


def _locked(round_obj: Round) -> Round:
    """The round row, locked for the rest of the transaction so two organizers cannot start or close it at once."""
    return Round.objects.select_for_update(of=('self',)).select_related('hackathon').get(pk=round_obj.pk)


def rubric_target(round_obj: Round) -> str:
    return CriterionTarget.IDEA if round_obj.scoring_target == ScoringTarget.IDEA else CriterionTarget.PROJECT


def criteria_of(round_obj: Round) -> list[JudgingCriterion]:
    """The rubric this round scores against: its own, else the hackathon-wide one for what is scored."""
    own = list(round_obj.criteria.all())
    if own:
        return own
    return list(round_obj.hackathon.criteria.filter(target=rubric_target(round_obj), round__isnull=True))


def _active_entries(round_obj: Round) -> list[RoundEntry]:
    return list(
        round_obj.entries.select_related('team').exclude(team__status__in=REMOVED_TEAMS).filter(team__is_waitlisted=False)
        .order_by('team__name', 'id')
    )


def panel_judges(round_obj: Round) -> list:
    return [row.judge for row in round_obj.panel.select_related('judge').order_by('id')]


def team_label(round_obj: Round, entry: RoundEntry) -> str:
    """What judges see for a team: its name, or a short code in a blind round."""
    return f'Team {entry.id:03d}' if round_obj.blind else entry.team.name


def _audit(actor, action: str, round_obj: Round, **details) -> None:
    log_audit_event(
        actor=actor, action=action, target_model='HackathonRound', target_id=round_obj.pk,
        details={'hackathon': round_obj.hackathon.slug, 'round': round_obj.name, **details},
    )


def _moment(value):
    if value in (None, ''):
        return None
    when = parse_datetime(str(value))
    if when is None or timezone.is_naive(when):
        raise HackathonError('Give the deadline as a full date and time.', 'INVALID', field='scoring_deadline')
    return when


def _whole(value, field: str, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise HackathonError('This must be a whole number.', 'INVALID', field=field)
    if not low <= number <= high:
        raise HackathonError(f'Choose a number from {low} to {high}.', 'INVALID', field=field)
    return number


def _choice(value, choices, field: str) -> str:
    if value not in choices.values:
        raise HackathonError(f"Choose one of: {', '.join(choices.values)}.", 'INVALID', field=field)
    return value


# ---------------------------------------------------------------------------------------------------------------------------
# Setting a round up
# ---------------------------------------------------------------------------------------------------------------------------

class RoundJudgingService:

    @staticmethod
    @transaction.atomic
    def configure(round_obj: Round, actor, data: dict) -> Round:
        """Change how the round is judged. Once judges are scoring, only the deadline may move."""
        round_obj = _locked(round_obj)
        _require_feature(round_obj)
        if round_obj.evaluation_status == EvaluationStatus.CLOSED:
            raise HackathonError('This round is closed. Reopen it to change how it is judged.', 'EVALUATION_CLOSED', status=409)
        started = round_obj.evaluation_status == EvaluationStatus.IN_PROGRESS
        changes = {}
        for field in SETTINGS_FIELDS:
            if field not in data:
                continue
            if started and field != 'scoring_deadline':
                raise HackathonError('Judges are already scoring, so only the deadline can change.', 'EVALUATION_STARTED', field=field, status=409)
            value = data[field]
            if field == 'evaluation_mode':
                value = _choice(value, EvaluationMode, field)
            elif field == 'scoring_target':
                value = _choice(value, ScoringTarget, field)
            elif field == 'assignment_mode':
                value = _choice(value, AssignmentMode, field)
            elif field == 'tie_break':
                value = _choice(value, TieBreak, field)
            elif field in ('judges_per_team', 'min_reviews_per_team'):
                value = _whole(value, field, 1, MAX_PER_TEAM)
            elif field == 'blind':
                if not isinstance(value, bool):
                    raise HackathonError('This must be on or off.', 'INVALID', field=field)
            elif field == 'scoring_deadline':
                value = _moment(value)
            changes[field] = value
        merged = {**{f: getattr(round_obj, f) for f in SETTINGS_FIELDS}, **changes}
        if merged['assignment_mode'] == AssignmentMode.BALANCED and merged['min_reviews_per_team'] > merged['judges_per_team']:
            raise HackathonError('The minimum reviews cannot be more than the judges per team.', 'INVALID', field='min_reviews_per_team')
        if merged['scoring_deadline'] and merged['scoring_deadline'] <= timezone.now() and 'scoring_deadline' in changes:
            raise HackathonError('The deadline must be in the future.', 'INVALID', field='scoring_deadline')
        for field, value in changes.items():
            setattr(round_obj, field, value)
        round_obj.save(update_fields=[*changes, 'updated_at'])
        _audit(actor, 'Configured Round Judging', round_obj, changes=sorted(changes))
        return round_obj

    @staticmethod
    @transaction.atomic
    def set_criteria(round_obj: Round, actor, items) -> list[JudgingCriterion]:
        """The round's own rubric, replacing any earlier one. An empty list means "use the hackathon rubric"."""
        round_obj = _locked(round_obj)
        _require_judged(round_obj)
        if round_obj.evaluation_status != EvaluationStatus.NOT_STARTED:
            raise HackathonError('The rubric is frozen once judging has started.', 'EVALUATION_STARTED', status=409)
        if not isinstance(items, list) or len(items) > MAX_CRITERIA:
            raise HackathonError(f'Send a list of at most {MAX_CRITERIA} criteria.', 'INVALID')
        cleaned = []
        for item in items:
            if not isinstance(item, dict):
                raise HackathonError('Every criterion must be an object.', 'INVALID')
            cleaned.append(CriteriaService.clean_item(item))
        round_obj.criteria.all().delete()
        created = [
            JudgingCriterion.objects.create(
                hackathon=round_obj.hackathon, round=round_obj, target=rubric_target(round_obj),
                name=name, description=description, max_score=max_score, weight=weight, order=order,
            )
            for order, (name, description, max_score, weight) in enumerate(cleaned)
        ]
        _audit(actor, 'Set Round Rubric', round_obj, criteria=[c.name for c in created])
        return created

    @staticmethod
    @transaction.atomic
    def set_panel(round_obj: Round, actor, judge_ids) -> list[RoundJudge]:
        """The judges of this round, replacing the earlier list. A judge who has already scored cannot be taken off."""
        from django.contrib.auth import get_user_model

        round_obj = _locked(round_obj)
        _require_judged(round_obj)
        if round_obj.evaluation_status == EvaluationStatus.CLOSED:
            raise HackathonError('This round is closed, so its panel cannot change.', 'EVALUATION_CLOSED', status=409)
        if not isinstance(judge_ids, list) or len(judge_ids) > MAX_PANEL:
            raise HackathonError(f'Send a list of at most {MAX_PANEL} judges.', 'INVALID', field='judge_ids')
        try:
            wanted = list(dict.fromkeys(int(i) for i in judge_ids))
        except (TypeError, ValueError):
            raise HackathonError('Judges are given by id.', 'INVALID', field='judge_ids')
        hackathon = round_obj.hackathon
        users = {u.id: u for u in get_user_model().objects.filter(id__in=wanted)}
        for judge_id in wanted:
            user = users.get(judge_id)
            if user is None or not access.can_judge(hackathon, user):
                raise HackathonError('Everyone on the panel must be a judge of this hackathon. Invite them first.', 'NOT_A_JUDGE', field='judge_ids')
            if hackathon.memberships.filter(user=user).exists():
                raise HackathonError(f'{user.first_name or user.email} is a participant, so cannot judge.', 'CONFLICT', field='judge_ids')

        current = {row.judge_id: row for row in round_obj.panel.all()}
        for judge_id, row in current.items():
            if judge_id in wanted:
                continue
            if RoundReview.objects.filter(assignment__round=round_obj, assignment__judge_id=judge_id).exists():
                raise HackathonError('That judge has already scored teams in this round, so they cannot be removed.', 'HAS_REVIEWS', field='judge_ids', status=409)
            round_obj.assignments.filter(judge_id=judge_id).delete()
            row.delete()
        for judge_id in wanted:
            if judge_id not in current:
                RoundJudge.objects.create(round=round_obj, judge=users[judge_id], added_by=actor)
        _audit(actor, 'Set Round Panel', round_obj, judges=[users[i].email for i in wanted])
        panel = list(round_obj.panel.select_related('judge'))
        if round_obj.evaluation_status == EvaluationStatus.IN_PROGRESS and round_obj.assignment_mode != AssignmentMode.MANUAL:
            _fill(round_obj)
        return panel

    # -----------------------------------------------------------------------------------------------------------------------
    # Who scores whom
    # -----------------------------------------------------------------------------------------------------------------------

    @staticmethod
    @transaction.atomic
    def assign(round_obj: Round, actor, manual=None) -> dict:
        """
        Share the teams out. ALL and BALANCED fill in what is missing (nothing already given is taken away). MANUAL takes
        ``manual``: ``[{entry_id, judge_ids}]``, which sets exactly those teams' judges (a judge who already scored stays).
        """
        round_obj = _locked(round_obj)
        _require_judged(round_obj)
        if round_obj.evaluation_status == EvaluationStatus.CLOSED:
            raise HackathonError('This round is closed.', 'EVALUATION_CLOSED', status=409)
        if not round_obj.panel.exists():
            raise HackathonError('Choose the judges first.', 'NO_JUDGES')
        skipped = []
        if round_obj.assignment_mode == AssignmentMode.MANUAL:
            if manual is not None:
                skipped = _apply_manual(round_obj, manual)
        else:
            _fill(round_obj)
        _audit(actor, 'Assigned Round Judges', round_obj, mode=round_obj.assignment_mode)
        return {**coverage(round_obj), 'skipped': skipped}

    # -----------------------------------------------------------------------------------------------------------------------
    # Starting, scoring, stepping aside
    # -----------------------------------------------------------------------------------------------------------------------

    @staticmethod
    @transaction.atomic
    def start(round_obj: Round, actor) -> Round:
        """Open scoring: the rubric is frozen, every team must have enough judges, and the panel is told."""
        round_obj = _locked(round_obj)
        _require_judged(round_obj)
        if round_obj.evaluation_status != EvaluationStatus.NOT_STARTED:
            raise HackathonError('Judging has already started for this round.', 'EVALUATION_STARTED', status=409)
        judges = panel_judges(round_obj)
        if not judges:
            raise HackathonError('Choose at least one judge first.', 'NO_JUDGES')
        entries = _active_entries(round_obj)
        if not entries:
            raise HackathonError('There are no teams in this round to judge.', 'NO_TEAMS')
        criteria = criteria_of(round_obj)
        if not criteria:
            raise HackathonError('Set the judging criteria first.', 'NO_CRITERIA')
        if round_obj.deadline_passed():
            raise HackathonError('The scoring deadline has already passed. Move it first.', 'DEADLINE_PASSED', field='scoring_deadline')
        if round_obj.assignment_mode != AssignmentMode.MANUAL:
            _fill(round_obj)
        short = coverage(round_obj)['short']
        if short:
            names = ', '.join(row['team_name'] for row in short[:8]) + (' and more' if len(short) > 8 else '')
            raise HackathonError(
                f'Every team needs at least {round_obj.min_reviews_per_team} judge(s) before judging starts. Short: {names}.', 'COVERAGE',
            )
        if not round_obj.criteria.exists():
            for order, criterion in enumerate(criteria):  # freeze: later edits to the hackathon rubric cannot change this round
                JudgingCriterion.objects.create(
                    hackathon=round_obj.hackathon, round=round_obj, target=rubric_target(round_obj), name=criterion.name,
                    description=criterion.description, max_score=criterion.max_score, weight=criterion.weight, order=order,
                )
        round_obj.evaluation_status = EvaluationStatus.IN_PROGRESS
        round_obj.evaluation_started_at = timezone.now()
        update = ['evaluation_status', 'evaluation_started_at', 'updated_at']
        if round_obj.status == RoundStatus.UPCOMING:
            round_obj.status = RoundStatus.ACTIVE
            update.append('status')
        round_obj.save(update_fields=update)
        _audit(actor, 'Started Round Judging', round_obj, judges=len(judges), teams=len(entries))
        _notify_users(
            judges, f'{round_obj.name}: scoring has started',
            f'You can now score your teams for {round_obj.name} in {round_obj.hackathon.title}.', round_obj.hackathon, actor,
        )
        return round_obj

    @staticmethod
    @transaction.atomic
    def submit_review(round_obj: Round, entry_id, judge, scores, comment: str = '') -> RoundReview:
        """A judge's scores for one team they were given. They may send it again to change it until the round closes."""
        round_obj = _locked(round_obj)
        _require_judged(round_obj)
        if round_obj.evaluation_status != EvaluationStatus.IN_PROGRESS:
            message = 'Judging has not started for this round yet.' if round_obj.evaluation_status == EvaluationStatus.NOT_STARTED else 'This round is closed, so scores are final.'
            raise HackathonError(message, 'NOT_SCORING', status=409)
        if round_obj.deadline_passed():
            raise HackathonError('The scoring deadline has passed.', 'DEADLINE_PASSED', status=409)
        if not access.can_judge(round_obj.hackathon, judge):
            raise PermissionDenied('You are not a judge for this hackathon.')
        assignment = round_obj.assignments.select_related('entry__team').filter(entry_id=entry_id, judge=judge).first()
        if assignment is None:
            raise PermissionDenied('This team was not given to you.')
        if assignment.recused:
            raise HackathonError('You stepped aside from this team.', 'RECUSED')
        if is_member(assignment.entry.team, judge):
            raise HackathonError('You cannot score your own team.', 'CONFLICT_OF_INTEREST')
        criteria = criteria_of(round_obj)
        if not criteria:
            raise HackathonError('The organizers have not set judging criteria yet.', 'NO_CRITERIA')
        comment = str(comment or '').strip()
        if len(comment) > MAX_COMMENT:
            raise HackathonError(f'Comments are limited to {MAX_COMMENT} characters.', 'INVALID', field='comment')
        cleaned = JudgingService._clean_scores(criteria, scores)
        review, _ = RoundReview.objects.update_or_create(
            assignment=assignment,
            defaults={'scores': cleaned, 'comment': comment, 'total': JudgingService.weighted_total(criteria, cleaned), 'submitted_at': timezone.now()},
        )
        _refresh_entry(assignment.entry)
        return review

    @staticmethod
    @transaction.atomic
    def recuse(round_obj: Round, entry_id, judge, reason) -> dict:
        """A judge steps aside from a team they have not scored (a conflict of interest, say). The team may go to another judge."""
        round_obj = _locked(round_obj)
        _require_judged(round_obj)
        if round_obj.evaluation_status != EvaluationStatus.IN_PROGRESS:
            raise HackathonError('Judging is not open for this round.', 'NOT_SCORING', status=409)
        reason = ' '.join(str(reason or '').split())
        if not reason:
            raise HackathonError('Say why you are stepping aside.', 'REASON_REQUIRED', field='reason')
        if len(reason) > MAX_REASON:
            raise HackathonError(f'Keep the reason under {MAX_REASON} characters.', 'INVALID', field='reason')
        assignment = round_obj.assignments.select_related('entry__team').filter(entry_id=entry_id, judge=judge).first()
        if assignment is None:
            raise PermissionDenied('This team was not given to you.')
        if assignment.recused:
            raise HackathonError('You already stepped aside from this team.', 'RECUSED')
        if RoundReview.objects.filter(assignment=assignment).exists():
            raise HackathonError('You have already scored this team. Ask the organizers if you need to step aside.', 'ALREADY_SCORED', status=409)
        assignment.recused, assignment.recused_reason, assignment.recused_at = True, reason, timezone.now()
        assignment.save(update_fields=['recused', 'recused_reason', 'recused_at', 'updated_at'])
        replacement = None
        if round_obj.assignment_mode == AssignmentMode.BALANCED:
            replacement = _replace_judge(round_obj, assignment.entry)
        _audit(judge, 'Recused From Round Team', round_obj, team=assignment.entry.team.name, reason=reason, reassigned_to=replacement.email if replacement else None)
        return {'reassigned': replacement is not None, 'reassigned_to': (replacement.first_name or replacement.email) if replacement else None}

    # -----------------------------------------------------------------------------------------------------------------------
    # Closing
    # -----------------------------------------------------------------------------------------------------------------------

    @staticmethod
    @transaction.atomic
    def close(round_obj: Round, actor, *, force: bool = False, reason: str = '') -> dict:
        """Average each team's scores, rank them and lock the round. Refused while a team is under-reviewed unless forced with a reason."""
        round_obj = _locked(round_obj)
        _require_judged(round_obj)
        if round_obj.evaluation_status != EvaluationStatus.IN_PROGRESS:
            raise HackathonError('This round is not being judged.', 'NOT_SCORING', status=409)
        entries = _active_entries(round_obj)
        counts = Counter(RoundReview.objects.filter(assignment__round=round_obj, assignment__recused=False).values_list('assignment__entry_id', flat=True))
        under = [e for e in entries if counts.get(e.id, 0) < round_obj.min_reviews_per_team]
        reason = ' '.join(str(reason or '').split())
        if under and not force:
            names = ', '.join(e.team.name for e in under[:8]) + (' and more' if len(under) > 8 else '')
            raise HackathonError(
                f'{len(under)} team(s) have fewer than {round_obj.min_reviews_per_team} review(s): {names}.', 'UNDER_REVIEWED', status=409,
            )
        if under and force and not reason:
            raise HackathonError('Say why the round is being closed with teams under-reviewed.', 'REASON_REQUIRED', field='reason')
        ranked = _rank(round_obj, entries)
        round_obj.evaluation_status = EvaluationStatus.CLOSED
        round_obj.evaluation_closed_at = timezone.now()
        round_obj.save(update_fields=['evaluation_status', 'evaluation_closed_at', 'updated_at'])
        _audit(actor, 'Closed Round Judging', round_obj, teams=len(entries), under_reviewed=len(under), forced=bool(under and force), reason=reason)
        return {'ranked': len(ranked), 'unranked': len(entries) - len(ranked), 'under_reviewed': [e.team.name for e in under]}

    @staticmethod
    @transaction.atomic
    def reopen(round_obj: Round, actor, reason) -> Round:
        """Let judges edit again. Not once the round's results are published to the teams."""
        round_obj = _locked(round_obj)
        _require_judged(round_obj)
        if round_obj.evaluation_status != EvaluationStatus.CLOSED:
            raise HackathonError('This round is not closed.', 'NOT_CLOSED', status=409)
        if round_obj.results_published:
            raise HackathonError('This round\'s results are published. Unpublish them first.', 'RESULTS_PUBLISHED', status=409)
        reason = ' '.join(str(reason or '').split())
        if not reason:
            raise HackathonError('Say why the round is being reopened.', 'REASON_REQUIRED', field='reason')
        round_obj.evaluation_status = EvaluationStatus.IN_PROGRESS
        round_obj.evaluation_closed_at = None
        round_obj.save(update_fields=['evaluation_status', 'evaluation_closed_at', 'updated_at'])
        round_obj.entries.update(final_rank=None)
        _audit(actor, 'Reopened Round Judging', round_obj, reason=reason)
        return round_obj

    # -----------------------------------------------------------------------------------------------------------------------
    # Reading it back
    # -----------------------------------------------------------------------------------------------------------------------

    @staticmethod
    def progress(round_obj: Round) -> dict:
        """The judge-by-team grid: who has scored, who is still to, who stepped aside."""
        panel = panel_judges(round_obj)
        assignments = list(round_obj.assignments.select_related('entry__team', 'judge'))
        reviews = {
            row['assignment_id']: row['submitted_at']
            for row in RoundReview.objects.filter(assignment__round=round_obj).values('assignment_id', 'submitted_at')
        }
        by_entry: dict[int, list] = {}
        for assignment in assignments:
            by_entry.setdefault(assignment.entry_id, []).append(assignment)

        def state(assignment: RoundAssignment) -> str:
            return 'recused' if assignment.recused else 'scored' if assignment.id in reviews else 'pending'

        judges = []
        for judge in panel:
            mine = [a for a in assignments if a.judge_id == judge.id]
            states = [state(a) for a in mine]
            last = max((reviews[a.id] for a in mine if a.id in reviews), default=None)
            judges.append({
                'judge_id': judge.id, 'name': f'{judge.first_name} {judge.last_name}'.strip() or judge.email, 'email': judge.email,
                'assigned': len(mine) - states.count('recused'), 'scored': states.count('scored'), 'pending': states.count('pending'),
                'recused': states.count('recused'), 'last_activity': last,
            })
        teams = []
        for entry in _active_entries(round_obj):
            cells = [{'judge_id': a.judge_id, 'state': state(a)} for a in by_entry.get(entry.id, [])]
            scored = sum(1 for c in cells if c['state'] == 'scored')
            teams.append({
                'entry_id': entry.id, 'team_id': entry.team_id, 'team_name': entry.team.name, 'label': team_label(round_obj, entry),
                'review_count': scored, 'needs': max(round_obj.min_reviews_per_team - scored, 0), 'cells': cells,
            })
        total = sum(j['assigned'] for j in judges)
        done = sum(j['scored'] for j in judges)
        return {
            'status': round_obj.evaluation_status, 'started_at': round_obj.evaluation_started_at, 'closed_at': round_obj.evaluation_closed_at,
            'deadline': round_obj.scoring_deadline, 'min_reviews': round_obj.min_reviews_per_team,
            'judges': judges, 'teams': teams, 'totals': {'assigned': total, 'scored': done, 'pending': total - done},
        }

    @staticmethod
    def results(round_obj: Round) -> list[dict]:
        """Every team of the round with its score, rank, each judge's total and the spread between judges (organizers only)."""
        totals: dict[int, dict[int, float]] = {}
        for entry_id, judge_id, total in RoundReview.objects.filter(assignment__round=round_obj, assignment__recused=False).values_list(
            'assignment__entry_id', 'assignment__judge_id', 'total',
        ):
            totals.setdefault(entry_id, {})[judge_id] = total
        rows = []
        for entry in _active_entries(round_obj):
            per_judge = totals.get(entry.id, {})
            values = list(per_judge.values())
            rows.append({
                'entry_id': entry.id, 'team_id': entry.team_id, 'team_name': entry.team.name, 'status': entry.status,
                'rank': entry.final_rank, 'score': entry.score if values else None, 'review_count': len(values),
                'per_judge': per_judge, 'highest': max(values) if values else None, 'lowest': min(values) if values else None,
                'spread': round(statistics.pstdev(values), 2) if len(values) > 1 else 0.0 if values else None,
            })
        rows.sort(key=lambda r: (r['rank'] is None, r['rank'] or 0, -(r['score'] or 0), r['team_name']))
        return rows

    # -----------------------------------------------------------------------------------------------------------------------
    # Hooks for the shortlisting side
    # -----------------------------------------------------------------------------------------------------------------------

    @staticmethod
    def on_entries_added(round_obj: Round) -> None:
        """Teams joined a round that is being judged: give them judges."""
        if round_obj.evaluation_mode == EvaluationMode.JUDGED and round_obj.evaluation_status == EvaluationStatus.IN_PROGRESS \
                and round_obj.assignment_mode != AssignmentMode.MANUAL:
            _fill(round_obj)

    @staticmethod
    def guard_remove(round_obj: Round, team) -> None:
        """A team with scores cannot silently vanish from a round that is being judged."""
        if round_obj.evaluation_mode == EvaluationMode.JUDGED and RoundReview.objects.filter(
            assignment__round=round_obj, assignment__entry__team=team,
        ).exists():
            raise HackathonError('Judges have already scored this team in this round, so it cannot be removed.', 'HAS_REVIEWS', status=409)


# ---------------------------------------------------------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------------------------------------------------------

def coverage(round_obj: Round) -> dict:
    """How many (not stepped-aside) judges each team has, and which teams have fewer than the round needs."""
    active = Counter(round_obj.assignments.filter(recused=False).values_list('entry_id', flat=True))
    need = round_obj.min_reviews_per_team
    short = [
        {'entry_id': e.id, 'team_name': e.team.name, 'judges': active.get(e.id, 0)}
        for e in _active_entries(round_obj) if active.get(e.id, 0) < need
    ]
    return {'assignments': sum(active.values()), 'teams': len(_active_entries(round_obj)), 'short': short}


def _fill(round_obj: Round) -> int:
    """ALL / BALANCED: add the missing assignments. Existing ones (scored or not) are never removed. Returns how many were added."""
    judges = panel_judges(round_obj)
    if not judges:
        return 0
    entries = _active_entries(round_obj)
    rows = list(round_obj.assignments.values_list('entry_id', 'judge_id', 'recused'))
    given: dict[int, set[int]] = {}
    for entry_id, judge_id, _ in rows:
        given.setdefault(entry_id, set()).add(judge_id)
    load = Counter(judge_id for _, judge_id, recused in rows if not recused)
    active_by_entry = Counter(entry_id for entry_id, _, recused in rows if not recused)
    created = []
    if round_obj.assignment_mode == AssignmentMode.ALL:
        for entry in entries:
            for judge in judges:
                if judge.id not in given.get(entry.id, set()):
                    created.append(RoundAssignment(round=round_obj, entry=entry, judge=judge))
    else:
        per_team = min(round_obj.judges_per_team, len(judges))
        for entry in entries:
            need = per_team - active_by_entry.get(entry.id, 0)
            if need <= 0:
                continue
            candidates = sorted((j for j in judges if j.id not in given.get(entry.id, set())), key=lambda j: (load[j.id], j.id))
            for judge in candidates[:need]:
                created.append(RoundAssignment(round=round_obj, entry=entry, judge=judge))
                load[judge.id] += 1
    RoundAssignment.objects.bulk_create(created, ignore_conflicts=True)
    return len(created)


def _replace_judge(round_obj: Round, entry: RoundEntry):
    """A judge stepped aside: hand the team to the least busy panel judge who does not have it. None when nobody is left."""
    given = set(round_obj.assignments.filter(entry=entry).values_list('judge_id', flat=True))
    load = Counter(round_obj.assignments.filter(recused=False).values_list('judge_id', flat=True))
    candidates = sorted((j for j in panel_judges(round_obj) if j.id not in given), key=lambda j: (load[j.id], j.id))
    if not candidates:
        return None
    RoundAssignment.objects.create(round=round_obj, entry=entry, judge=candidates[0])
    return candidates[0]


def _apply_manual(round_obj: Round, manual) -> list[dict]:
    """Set exactly the judges of the named teams. A judge who already scored a team stays on it; a recused one is not brought back."""
    if not isinstance(manual, list):
        raise HackathonError('Send a list of {entry_id, judge_ids}.', 'INVALID')
    entries = {e.id: e for e in _active_entries(round_obj)}
    panel = {j.id: j for j in panel_judges(round_obj)}
    skipped = []
    for item in manual:
        if not isinstance(item, dict) or item.get('entry_id') not in entries:
            raise HackathonError('Pick teams that are in this round.', 'INVALID', field='entry_id')
        try:
            wanted = set(int(i) for i in item.get('judge_ids') or [])
        except (TypeError, ValueError):
            raise HackathonError('Judges are given by id.', 'INVALID', field='judge_ids')
        if not wanted <= set(panel):
            raise HackathonError('Every judge must be on this round\'s panel.', 'INVALID', field='judge_ids')
        entry = entries[item['entry_id']]
        existing = {a.judge_id: a for a in round_obj.assignments.filter(entry=entry)}
        scored = set(RoundReview.objects.filter(assignment__round=round_obj, assignment__entry=entry).values_list('assignment__judge_id', flat=True))
        for judge_id, assignment in existing.items():
            if judge_id not in wanted and judge_id not in scored and not assignment.recused:
                assignment.delete()
        for judge_id in wanted:
            if judge_id in existing:
                if existing[judge_id].recused:
                    skipped.append({'entry_id': entry.id, 'judge_id': judge_id, 'why': 'stepped aside'})
                continue
            RoundAssignment.objects.create(round=round_obj, entry=entry, judge=panel[judge_id])
    return skipped


def _refresh_entry(entry: RoundEntry) -> None:
    stats = RoundReview.objects.filter(assignment__entry=entry, assignment__recused=False).aggregate(avg=Avg('total'), count=Count('id'))
    entry.score = round(stats['avg'] or 0.0, 2)
    entry.review_count = stats['count']
    entry.save(update_fields=['score', 'review_count', 'updated_at'])


def _rank(round_obj: Round, entries: list[RoundEntry]) -> list[RoundEntry]:
    """Fix every team's score and rank. Teams with no review stay unranked. Equal teams share a rank (1, 2, 2, 4)."""
    criteria = sorted(criteria_of(round_obj), key=lambda c: (-c.weight, c.order, c.id))
    by_entry: dict[int, list[RoundReview]] = {}
    for review in RoundReview.objects.filter(assignment__round=round_obj, assignment__recused=False).select_related('assignment'):
        by_entry.setdefault(review.assignment.entry_id, []).append(review)

    keyed = []
    for entry in entries:
        reviews = by_entry.get(entry.id, [])
        entry.review_count = len(reviews)
        entry.score = round(sum(r.total for r in reviews) / len(reviews), 2) if reviews else 0.0
        entry.final_rank = None
        if reviews:
            fractions = tuple(
                round(sum(float(r.scores.get(str(c.id), 0)) / c.max_score for r in reviews) / len(reviews), 4) for c in criteria
            )
            keyed.append((entry, (entry.score, fractions if round_obj.tie_break == TieBreak.CRITERIA else ())))
    keyed.sort(key=lambda pair: (-pair[1][0], tuple(-f for f in pair[1][1]), pair[0].team.name))
    previous, rank = None, 0
    for position, (entry, key) in enumerate(keyed, start=1):
        if key != previous:
            rank, previous = position, key
        entry.final_rank = rank
    RoundEntry.objects.bulk_update(entries, ['score', 'review_count', 'final_rank'])
    return [entry for entry, _ in keyed]


# ---------------------------------------------------------------------------------------------------------------------------
# What the screens read: the organizer's setup view and the judge's own view
# ---------------------------------------------------------------------------------------------------------------------------

def _person(user) -> dict:
    return {'judge_id': user.id, 'name': f'{user.first_name} {user.last_name}'.strip() or user.email, 'email': user.email}


def _criterion(criterion: JudgingCriterion) -> dict:
    return {'id': criterion.id, 'name': criterion.name, 'description': criterion.description, 'max_score': criterion.max_score, 'weight': criterion.weight, 'order': criterion.order}


def readiness(round_obj: Round) -> list[dict]:
    """Everything that must be true before judging can start, as a checklist (nothing here raises)."""
    judges = len(panel_judges(round_obj))
    teams = len(_active_entries(round_obj))
    manual = round_obj.assignment_mode == AssignmentMode.MANUAL
    short = coverage(round_obj)['short'] if manual else []
    need = round_obj.min_reviews_per_team
    return [
        {'key': 'teams', 'label': 'Teams are in the round', 'ok': teams > 0, 'detail': f'{teams} team(s)'},
        {'key': 'judges', 'label': 'Judges are chosen', 'ok': judges > 0, 'detail': f'{judges} judge(s)'},
        {'key': 'rubric', 'label': 'Judging criteria are set', 'ok': bool(criteria_of(round_obj)), 'detail': f'{len(criteria_of(round_obj))} criteria'},
        {
            'key': 'coverage', 'label': f'Every team can get {need} judge(s)',
            'ok': (not short) if manual else judges >= need and judges > 0,
            'detail': f'{len(short)} team(s) short' if manual else f'{judges} judge(s) for a minimum of {need}',
        },
        {'key': 'deadline', 'label': 'The deadline is still ahead', 'ok': not round_obj.deadline_passed(), 'detail': ''},
    ]


def evaluation_payload(round_obj: Round) -> dict:
    """The organizer's whole view of how a round is judged: settings, rubric, panel, coverage and the start checklist."""
    own = list(round_obj.criteria.all())
    checks = readiness(round_obj)
    return {
        'round_id': round_obj.id, 'name': round_obj.name, 'enabled': round_obj.hackathon.feature('round_judging'),
        'settings': {field: getattr(round_obj, field) for field in SETTINGS_FIELDS},
        'status': round_obj.evaluation_status, 'started_at': round_obj.evaluation_started_at, 'closed_at': round_obj.evaluation_closed_at,
        'results_published': round_obj.results_published,
        'criteria_source': 'round' if own else 'hackathon', 'criteria': [_criterion(c) for c in criteria_of(round_obj)],
        'panel': [_person(user) for user in panel_judges(round_obj)],
        'coverage': coverage(round_obj), 'readiness': checks, 'can_start': all(check['ok'] for check in checks),
    }


def judge_rounds(hackathon, judge) -> list[dict]:
    """The judged rounds this person sits on, with their own counts."""
    rounds = list(hackathon.rounds.filter(evaluation_mode=EvaluationMode.JUDGED, panel__judge=judge).distinct().order_by('order'))
    mine = Counter()
    for round_id, recused, review_id in RoundAssignment.objects.filter(round__in=rounds, judge=judge).values_list('round_id', 'recused', 'review__id'):
        mine[(round_id, 'recused' if recused else 'scored' if review_id else 'pending')] += 1
    return [{
        'round_id': r.id, 'name': r.name, 'description': r.description, 'status': r.evaluation_status, 'deadline': r.scoring_deadline,
        'scoring_target': r.scoring_target, 'blind': r.blind,
        'assigned': mine[(r.id, 'scored')] + mine[(r.id, 'pending')], 'scored': mine[(r.id, 'scored')],
        'pending': mine[(r.id, 'pending')], 'recused': mine[(r.id, 'recused')],
        'can_score': r.evaluation_status == EvaluationStatus.IN_PROGRESS and not r.deadline_passed(),
    } for r in rounds]


def _require_on_panel(round_obj: Round, judge) -> None:
    _require_judged(round_obj)
    if not access.can_judge(round_obj.hackathon, judge) or not round_obj.panel.filter(judge=judge).exists():
        raise PermissionDenied('You are not a judge for this round.')


def judge_round(round_obj: Round, judge) -> dict:
    """One round as its judge sees it: the rubric and only the teams given to them (none until judging starts)."""
    _require_on_panel(round_obj, judge)
    teams = []
    if round_obj.evaluation_status != EvaluationStatus.NOT_STARTED:
        for assignment in round_obj.assignments.filter(judge=judge).select_related('entry__team').order_by('entry__team__name', 'id'):
            review = RoundReview.objects.filter(assignment=assignment).first()
            teams.append({
                'entry_id': assignment.entry_id, 'label': team_label(round_obj, assignment.entry),
                'state': 'recused' if assignment.recused else 'scored' if review else 'pending',
                'my_total': review.total if review else None, 'scored_at': review.submitted_at if review else None,
            })
    summary = next((r for r in judge_rounds(round_obj.hackathon, judge) if r['round_id'] == round_obj.id), {})
    return {**summary, 'criteria': [_criterion(c) for c in criteria_of(round_obj)], 'teams': teams}


def _material(round_obj: Round, entry: RoundEntry) -> dict:
    """What a judge may read about a team: its idea, project and problem. In a blind round nothing names the team or its people."""
    from .models import IdeaSubmission, Submission

    team = entry.team
    idea = IdeaSubmission.objects.filter(team=team).first()
    project = Submission.objects.filter(team=team).first()
    problem = None
    if team.problem_statement_id:
        statement = team.problem_statement
        problem = {'title': statement.title, 'description': statement.description, 'domain': statement.domain}
    elif team.is_open_innovation:
        problem = {'title': team.custom_problem_title, 'description': team.custom_problem_description, 'domain': team.custom_problem_domain}
    return {
        'problem': problem,
        'idea': None if idea is None else {
            'id': idea.id, 'title': idea.title, 'problem': idea.problem, 'solution': idea.solution, 'plan': idea.plan, 'tech_stack': idea.tech_stack,
            'deck_url': idea.deck_url, 'has_deck_file': bool(idea.deck_file_id), 'linkedin_url': None if round_obj.blind else idea.linkedin_url,
        },
        'project': None if project is None else {
            'id': project.id, 'title': project.project_title, 'tagline': project.tagline, 'description': project.description,
            'repo_url': project.repo_url, 'demo_url': project.demo_url, 'video_url': project.video_url, 'slides_url': project.slides_url,
            'tech_stack': project.tech_stack,
        },
    }


def judge_team(round_obj: Round, entry_id, judge) -> dict:
    """One team the judge was given: what to read and the judge's own scores so far."""
    _require_on_panel(round_obj, judge)
    if round_obj.evaluation_status == EvaluationStatus.NOT_STARTED:
        raise HackathonError('Judging has not started for this round yet.', 'NOT_SCORING', status=409)
    assignment = round_obj.assignments.select_related('entry__team__problem_statement').filter(entry_id=entry_id, judge=judge).first()
    if assignment is None:
        raise HackathonError('That team was not found.', 'NOT_FOUND', status=404)
    review = RoundReview.objects.filter(assignment=assignment).first()
    entry = assignment.entry
    return {
        'entry_id': entry.id, 'label': team_label(round_obj, entry), 'scoring_target': round_obj.scoring_target, 'blind': round_obj.blind,
        'recused': assignment.recused, 'recused_reason': assignment.recused_reason,
        'criteria': [_criterion(c) for c in criteria_of(round_obj)],
        'my_review': None if review is None else {'scores': review.scores, 'comment': review.comment, 'total': review.total, 'submitted_at': review.submitted_at},
        'can_score': round_obj.evaluation_status == EvaluationStatus.IN_PROGRESS and not round_obj.deadline_passed() and not assignment.recused,
        'material': _material(round_obj, entry),
    }
