"""Judging criteria, judges' reviews, the leaderboard, winners and publishing results."""
from __future__ import annotations

from django.db import transaction
from django.db.models import Avg, Count
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied

from apps.audit.utils import log_audit_event
from apps.core.permissions import _is_admin_or_club_lead

from . import config_validation as validate
from . import access, celebrate
from .access import can_judge
from .errors import HackathonError
from .models import (
    CriterionTarget, Hackathon, HackathonStaff, HackathonWinner, IdeaReview, IdeaStatus, IdeaSubmission, JudgingCriterion,
    StaffRole, Submission, SubmissionReview, Team, TeamStatus,
)
from .services import _notify_users, is_member

MAX_CRITERIA = 10
MAX_STAFF_PER_ROLE = 50
MAX_WINNERS = 30
MAX_COMMENT = 2000
DEFAULT_CRITERIA = {
    CriterionTarget.PROJECT: (
        ('Innovation', 'How original and creative is the idea?', 10, 3),
        ('Technical depth', 'Quality and difficulty of the implementation.', 10, 3),
        ('Impact', 'Does it solve a real problem for real people?', 10, 2),
        ('Presentation', 'Clarity of the demo, video and write-up.', 10, 2),
    ),
    CriterionTarget.IDEA: (
        ('Problem clarity', 'Is the problem real, specific and worth solving?', 10, 3),
        ('Originality', 'Is the approach fresh or a clear improvement on what exists?', 10, 2),
        ('Feasibility', 'Can this team build it in the time and with the skills they have?', 10, 3),
        ('Plan quality', 'Are the milestones and the split of work believable?', 10, 2),
    ),
}


def clean_target(raw) -> str:
    target = str(raw or CriterionTarget.PROJECT).upper()
    if target not in CriterionTarget.values:
        raise HackathonError('Target must be PROJECT or IDEA.', 'INVALID', field='target')
    return target


def _guard(fn):
    try:
        return fn()
    except serializers.ValidationError as exc:
        raise HackathonError(' '.join(str(message) for message in exc.detail), 'INVALID')


def _require_judging_on(hackathon: Hackathon):
    if not hackathon.feature('judging'):
        raise HackathonError('Judging is not enabled for this hackathon.', 'FEATURE_OFF')


class CriteriaService:

    @staticmethod
    @transaction.atomic
    def replace(hackathon: Hackathon, actor, items, target: str = CriterionTarget.PROJECT) -> list[JudgingCriterion]:
        """Set one rubric (project or idea). Items with an ``id`` keep it (and their scores); anything omitted is removed."""
        target = clean_target(target)
        if not isinstance(items, list) or len(items) > MAX_CRITERIA:
            raise HackathonError(f'Send a list of at most {MAX_CRITERIA} criteria.', 'INVALID')
        existing = {c.id: c for c in hackathon.criteria.filter(target=target)}
        kept_ids, result = set(), []
        for order, item in enumerate(items):
            if not isinstance(item, dict):
                raise HackathonError('Every criterion must be an object.', 'INVALID')
            name = _guard(lambda: validate._text(item.get('name'), 'Criterion name', 100, required=True))
            description = _guard(lambda: validate._text(item.get('description'), 'Description', 300))
            try:
                max_score, weight = int(item.get('max_score', 10)), int(item.get('weight', 1))
            except (TypeError, ValueError):
                raise HackathonError('Max score and weight must be whole numbers.', 'INVALID')
            if not 1 <= max_score <= 100 or not 1 <= weight <= 10:
                raise HackathonError('Max score must be 1-100 and weight 1-10.', 'INVALID')
            criterion = existing.get(item.get('id')) or JudgingCriterion(hackathon=hackathon, target=target)
            criterion.name, criterion.description = name, description
            criterion.max_score, criterion.weight, criterion.order = max_score, weight, order
            criterion.save()
            kept_ids.add(criterion.id)
            result.append(criterion)
        hackathon.criteria.filter(target=target).exclude(id__in=kept_ids).delete()
        JudgingService.recalculate_all(hackathon, target)
        log_audit_event(
            actor=actor, action='Updated Judging Criteria', target_model='Hackathon', target_id=hackathon.slug,
            details={'target': target, 'criteria': [c.name for c in result]},
        )
        return result

    @staticmethod
    def seed_defaults(hackathon: Hackathon, actor, target: str = CriterionTarget.PROJECT) -> list[JudgingCriterion]:
        target = clean_target(target)
        if hackathon.criteria.filter(target=target).exists():
            raise HackathonError('This hackathon already has this rubric.', 'ALREADY_CONFIGURED')
        return CriteriaService.replace(hackathon, actor, [
            {'name': name, 'description': description, 'max_score': max_score, 'weight': weight}
            for name, description, max_score, weight in DEFAULT_CRITERIA[target]
        ], target)


class StaffService:
    """Per-hackathon roles. Each person gets a preset for their role that an organizer can tune permission by permission."""

    @staticmethod
    def _clean_permissions(raw, role: str) -> list[str]:
        if raw in (None, ''):
            return access.preset_permissions(role)
        if not isinstance(raw, list) or any(key not in access.PERMISSIONS for key in raw):
            raise HackathonError('Unknown permission.', 'INVALID', field='permissions')
        ordered = [key for key in access.ALL_PERMISSIONS if key in raw]
        if not ordered:
            raise HackathonError('Give this person at least one permission, or remove them.', 'INVALID', field='permissions')
        return ordered

    @staticmethod
    def _require_can_grant(hackathon: Hackathon, actor, granted: list[str]) -> None:
        """Delegates can only hand out what they hold themselves."""
        if access.is_organizer(actor):
            return
        beyond = set(granted) - set(access.my_permissions(hackathon, actor))
        if beyond:
            raise PermissionDenied('You cannot grant permissions you do not have: ' + ', '.join(sorted(beyond)) + '.')

    @staticmethod
    @transaction.atomic
    def add(hackathon: Hackathon, actor, email: str, role: str, permissions=None) -> HackathonStaff:
        from django.contrib.auth import get_user_model

        if role not in StaffRole.values:
            raise HackathonError('Unknown role.', 'INVALID', field='role')
        granted = StaffService._clean_permissions(permissions, role)
        StaffService._require_can_grant(hackathon, actor, granted)
        if hackathon.staff.filter(role=role).count() >= MAX_STAFF_PER_ROLE:
            raise HackathonError(f'At most {MAX_STAFF_PER_ROLE} people per role.', 'LIMIT_REACHED')
        user = get_user_model().objects.filter(email__iexact=(email or '').strip()).first()
        if user is None:
            raise HackathonError('No account uses that email.', 'USER_NOT_FOUND', field='email')
        if 'judge' in granted and hackathon.memberships.filter(user=user).exists():
            raise HackathonError('That person is a participant, so they cannot judge this hackathon.', 'CONFLICT', field='email')
        staff, created = HackathonStaff.objects.get_or_create(
            hackathon=hackathon, user=user, role=role, defaults={'added_by': actor, 'permissions': granted},
        )
        if created:
            log_audit_event(
                actor=actor, action='Added Hackathon Staff', target_model='Hackathon', target_id=hackathon.slug,
                details={'user': user.email, 'role': role, 'permissions': granted},
            )
            _notify_users(
                [user], f'You are {staff.get_role_display().lower()} for {hackathon.title}',
                'Open the hackathon page to get started.', hackathon, actor,
            )
        return staff

    @staticmethod
    @transaction.atomic
    def update_permissions(hackathon: Hackathon, actor, staff_id: int, permissions) -> HackathonStaff:
        staff = hackathon.staff.select_related('user').filter(pk=staff_id).first()
        if staff is None:
            raise HackathonError('That person is not on the team.', 'NOT_FOUND', status=404)
        if staff.user_id == actor.id and not access.is_organizer(actor):
            raise PermissionDenied('You cannot change your own permissions.')
        granted = StaffService._clean_permissions(permissions, staff.role)
        StaffService._require_can_grant(hackathon, actor, granted)
        if not access.is_organizer(actor):
            held = set(staff.permissions or access.preset_permissions(staff.role))
            beyond_actor = held - set(access.my_permissions(hackathon, actor))
            if beyond_actor:
                raise PermissionDenied('This person holds permissions you do not have, so you cannot change them.')
        if 'judge' in granted and hackathon.memberships.filter(user=staff.user).exists():
            raise HackathonError('That person is a participant, so they cannot judge this hackathon.', 'CONFLICT')
        before = list(staff.permissions or access.preset_permissions(staff.role))
        staff.permissions = granted
        staff.save(update_fields=['permissions', 'updated_at'])
        log_audit_event(
            actor=actor, action='Changed Hackathon Staff Permissions', target_model='Hackathon', target_id=hackathon.slug,
            details={'user': staff.user.email, 'role': staff.role, 'before': before, 'after': granted},
        )
        return staff

    @staticmethod
    def remove(hackathon: Hackathon, actor, staff_id: int) -> None:
        staff = hackathon.staff.select_related('user').filter(pk=staff_id).first()
        if staff is None:
            raise HackathonError('That person is not on the staff list.', 'NOT_FOUND', status=404)
        if not access.is_organizer(actor):
            if staff.user_id == actor.id:
                raise PermissionDenied('You cannot remove yourself.')
            beyond_actor = set(staff.permissions or access.preset_permissions(staff.role)) - set(access.my_permissions(hackathon, actor))
            if beyond_actor:
                raise PermissionDenied('This person holds permissions you do not have, so you cannot remove them.')
        details = {'user': staff.user.email, 'role': staff.role}
        staff.delete()
        log_audit_event(
            actor=actor, action='Removed Hackathon Staff', target_model='Hackathon', target_id=hackathon.slug, details=details,
        )


class JudgingService:

    @staticmethod
    def weighted_total(criteria, scores: dict) -> float:
        """0-100: each criterion's score as a fraction of its max, weighted."""
        weight_sum = sum(c.weight for c in criteria)
        if not weight_sum:
            return 0.0
        earned = sum((float(scores.get(str(c.id), 0)) / c.max_score) * c.weight for c in criteria)
        return round(earned / weight_sum * 100, 2)

    @staticmethod
    def _clean_scores(criteria, raw) -> dict:
        raw = raw if isinstance(raw, dict) else {}
        cleaned = {}
        for criterion in criteria:
            value = raw.get(str(criterion.id), raw.get(criterion.id))
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise HackathonError(f'Give a score for "{criterion.name}".', 'SCORE_REQUIRED', field=str(criterion.id))
            if not 0 <= number <= criterion.max_score:
                raise HackathonError(f'"{criterion.name}" is scored from 0 to {criterion.max_score}.', 'SCORE_RANGE', field=str(criterion.id))
            cleaned[str(criterion.id)] = round(number, 2)
        return cleaned

    @staticmethod
    @transaction.atomic
    def review(submission: Submission, judge, scores, comment: str = '') -> SubmissionReview:
        hackathon = submission.team.hackathon
        _require_judging_on(hackathon)
        if not can_judge(hackathon, judge):
            raise PermissionDenied('You are not a judge for this hackathon.')
        if hackathon.results_published_at:
            raise HackathonError('Results are published, so scores are final. Ask the organizers to unpublish first.', 'RESULTS_PUBLISHED')
        if is_member(submission.team, judge):
            raise HackathonError('You cannot score your own team.', 'CONFLICT_OF_INTEREST')
        criteria = list(hackathon.criteria.filter(target=CriterionTarget.PROJECT))
        if not criteria:
            raise HackathonError('The organizers have not set judging criteria yet.', 'NO_CRITERIA')
        comment = str(comment or '').strip()
        if len(comment) > MAX_COMMENT:
            raise HackathonError(f'Comments are limited to {MAX_COMMENT} characters.', 'INVALID', field='comment')
        cleaned = JudgingService._clean_scores(criteria, scores)
        review, _ = SubmissionReview.objects.update_or_create(
            submission=submission, judge=judge,
            defaults={'scores': cleaned, 'comment': comment, 'total': JudgingService.weighted_total(criteria, cleaned)},
        )
        JudgingService.recalculate(submission)
        return review

    @staticmethod
    def recalculate(submission: Submission) -> None:
        stats = submission.reviews.aggregate(avg=Avg('total'), count=Count('id'))
        submission.score = round(stats['avg'] or 0.0, 2)
        submission.review_count = stats['count']
        submission.save(update_fields=['score', 'review_count', 'updated_at'])

    @staticmethod
    @transaction.atomic
    def recalculate_all(hackathon: Hackathon, target: str = CriterionTarget.PROJECT) -> None:
        """Re-total every review of one rubric after the rubric changed."""
        criteria = list(hackathon.criteria.filter(target=target))
        if target == CriterionTarget.IDEA:
            reviews, owners = IdeaReview.objects.filter(idea__team__hackathon=hackathon), IdeaSubmission.objects.filter(team__hackathon=hackathon)
        else:
            reviews, owners = SubmissionReview.objects.filter(submission__team__hackathon=hackathon), Submission.objects.filter(team__hackathon=hackathon)
        for review in reviews:
            valid = {str(c.id): min(float(review.scores.get(str(c.id), 0)), c.max_score) for c in criteria}
            review.scores, review.total = valid, JudgingService.weighted_total(criteria, valid)
            review.save(update_fields=['scores', 'total', 'updated_at'])
        for owner in owners:
            JudgingService.recalculate_idea(owner) if target == CriterionTarget.IDEA else JudgingService.recalculate(owner)

    # -- ideas: the same rubric mechanics against the idea rubric ---------------------------------

    @staticmethod
    @transaction.atomic
    def review_idea(idea: IdeaSubmission, judge, scores, comment: str = '') -> IdeaReview:
        hackathon = idea.team.hackathon
        _require_judging_on(hackathon)
        if not hackathon.feature('ideas'):
            raise HackathonError('Idea submission is not enabled for this hackathon.', 'FEATURE_OFF')
        if not can_judge(hackathon, judge):
            raise PermissionDenied('You are not a judge for this hackathon.')
        if hackathon.results_published_at:
            raise HackathonError('Results are published, so scores are final. Ask the organizers to unpublish first.', 'RESULTS_PUBLISHED')
        if is_member(idea.team, judge):
            raise HackathonError('You cannot score your own team.', 'CONFLICT_OF_INTEREST')
        criteria = list(hackathon.criteria.filter(target=CriterionTarget.IDEA))
        if not criteria:
            raise HackathonError('The organizers have not set the idea scoring criteria yet.', 'NO_CRITERIA')
        comment = str(comment or '').strip()
        if len(comment) > MAX_COMMENT:
            raise HackathonError(f'Comments are limited to {MAX_COMMENT} characters.', 'INVALID', field='comment')
        cleaned = JudgingService._clean_scores(criteria, scores)
        review, _ = IdeaReview.objects.update_or_create(
            idea=idea, judge=judge,
            defaults={'scores': cleaned, 'comment': comment, 'total': JudgingService.weighted_total(criteria, cleaned)},
        )
        JudgingService.recalculate_idea(idea)
        return review

    @staticmethod
    def recalculate_idea(idea: IdeaSubmission) -> None:
        stats = idea.reviews.aggregate(avg=Avg('total'), count=Count('id'))
        idea.score = round(stats['avg'] or 0.0, 2)
        idea.review_count = stats['count']
        idea.save(update_fields=['score', 'review_count', 'updated_at'])

    @staticmethod
    def idea_leaderboard(hackathon: Hackathon) -> list[dict]:
        ideas = list(
            IdeaSubmission.objects.filter(team__hackathon=hackathon, team__is_waitlisted=False)
            .exclude(team__status__in=(TeamStatus.DISQUALIFIED, TeamStatus.WITHDRAWN))
            .exclude(status=IdeaStatus.REJECTED).select_related('team').order_by('-score', 'team__name')
        )
        rows, previous_score, rank = [], None, 0
        for position, idea in enumerate(ideas, start=1):
            if idea.score != previous_score:
                rank, previous_score = position, idea.score
            rows.append({
                'rank': rank, 'idea_id': idea.id, 'team_id': idea.team_id, 'team_name': idea.team.name, 'title': idea.title,
                'status': idea.status, 'score': idea.score, 'review_count': idea.review_count,
            })
        return rows

    @staticmethod
    def leaderboard(hackathon: Hackathon) -> list[dict]:
        """Registered teams' submissions, best first; equal scores share a rank."""
        submissions = list(
            Submission.objects.filter(team__hackathon=hackathon, team__is_waitlisted=False)
            .exclude(team__status__in=(TeamStatus.DISQUALIFIED, TeamStatus.WITHDRAWN))
            .select_related('team').order_by('-score', 'team__name')
        )
        rows, previous_score, rank = [], None, 0
        for position, submission in enumerate(submissions, start=1):
            if submission.score != previous_score:
                rank, previous_score = position, submission.score
            rows.append({
                'rank': rank, 'submission_id': submission.id, 'team_id': submission.team_id, 'team_name': submission.team.name,
                'project_title': submission.project_title, 'score': submission.score, 'review_count': submission.review_count,
            })
        return rows


class ResultsService:

    @staticmethod
    @transaction.atomic
    def set_winners(hackathon: Hackathon, actor, items) -> list[HackathonWinner]:
        if not isinstance(items, list) or len(items) > MAX_WINNERS:
            raise HackathonError(f'Send a list of at most {MAX_WINNERS} awards.', 'INVALID')
        teams = {t.id: t for t in hackathon.teams.all()}
        winners, seen = [], set()
        for item in items:
            if not isinstance(item, dict):
                raise HackathonError('Every award must be an object.', 'INVALID')
            team = teams.get(item.get('team_id'))
            if team is None:
                raise HackathonError('Pick a team from this hackathon for every award.', 'INVALID', field='team_id')
            title = _guard(lambda: validate._text(item.get('title'), 'Award title', 120, required=True))
            if (team.id, title.lower()) in seen:
                raise HackathonError(f'{team.name} already has the award "{title}".', 'INVALID')
            seen.add((team.id, title.lower()))
            try:
                rank = int(item.get('rank', len(winners) + 1))
            except (TypeError, ValueError):
                raise HackathonError('Rank must be a whole number.', 'INVALID', field='rank')
            if rank < 1:
                raise HackathonError('Rank must be 1 or more.', 'INVALID', field='rank')
            winners.append(HackathonWinner(
                hackathon=hackathon, team=team, title=title, rank=rank,
                prize=_guard(lambda: validate._text(item.get('prize'), 'Prize', 100)),
                note=_guard(lambda: validate._text(item.get('note'), 'Note', 300)),
            ))
        hackathon.winners.all().delete()
        HackathonWinner.objects.bulk_create(winners)
        log_audit_event(
            actor=actor, action='Set Hackathon Winners', target_model='Hackathon', target_id=hackathon.slug,
            details={'awards': [f'{w.title}: {w.team.name}' for w in winners]},
        )
        return list(hackathon.winners.select_related('team'))

    @staticmethod
    @transaction.atomic
    def publish(hackathon: Hackathon, actor, *, notify: bool = True) -> Hackathon:
        if not hackathon.winners.exists() and not hackathon.show_leaderboard:
            raise HackathonError('Pick winners or turn on the leaderboard before publishing results.', 'NOTHING_TO_PUBLISH')
        hackathon.results_published_at = timezone.now()
        hackathon.save(update_fields=['results_published_at', 'updated_at'])
        log_audit_event(actor=actor, action='Published Hackathon Results', target_model='Hackathon', target_id=hackathon.slug, details={})
        celebrate.winners(hackathon)
        if notify:
            members = [
                m.user for m in hackathon.memberships.select_related('user')
                .filter(team__status=TeamStatus.REGISTERED, team__is_waitlisted=False)
            ]
            transaction.on_commit(lambda: _notify_users(
                members, f'{hackathon.title}: results are out', 'See the winners and thank you for taking part.', hackathon, actor,
            ))
        return hackathon

    @staticmethod
    def unpublish(hackathon: Hackathon, actor) -> Hackathon:
        hackathon.results_published_at = None
        hackathon.save(update_fields=['results_published_at', 'updated_at'])
        log_audit_event(actor=actor, action='Unpublished Hackathon Results', target_model='Hackathon', target_id=hackathon.slug, details={})
        celebrate.winners_withdrawn(hackathon)
        return hackathon
