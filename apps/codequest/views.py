from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import decorators, permissions, response, status, viewsets
from rest_framework.exceptions import NotFound, ValidationError
from .models import Problem, Submission, UserStreak
from .serializers import ProblemSerializer, SubmissionSerializer, UserStreakSerializer, BatchScheduleSerializer, BatchScheduleProblemSerializer
from apps.core.permissions import IsAdminOrClubLead, IsAdminOrClubLeadOrReadOnly, IsOwnerOrAdminOrClubLead, _is_admin_or_club_lead
from apps.audit.utils import log_audit_event
from .services import (
    award_rewards_for_acceptance,
    evaluate_achievements,
    rebuild_coding_streak,
    rebuild_user_streak,
)
from . import admin_stats_services, exports, stats_services


def _positive_goal(value, field):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValidationError({field: 'Provide a whole number.'}) from None
    if parsed < 1 or parsed > 100:
        raise ValidationError({field: 'Value must be between 1 and 100.'})
    return parsed


class ProblemViewSet(viewsets.ModelViewSet):
    serializer_class = ProblemSerializer
    permission_classes = [IsAdminOrClubLeadOrReadOnly]
    lookup_field = 'slug'

    def get_queryset(self):
        queryset = (
            Problem.objects.annotate(
                submissions_count=Count('submissions', distinct=True),
                solved_count=Count(
                    'submissions__user',
                    filter=Q(submissions__is_correct=True),
                    distinct=True,
                ),
            ).order_by('-scheduled_date')
        )
        if _is_admin_or_club_lead(self.request.user):
            return queryset
        # The public route exposes today's challenge and the completed archive,
        # never the scheduling calendar. Future problems must not be discoverable
        # through either list or detail endpoints before their assigned club date.
        return queryset.filter(scheduled_date__lte=timezone.localdate())

    def perform_create(self, serializer):
        problem = serializer.save()
        log_audit_event(
            actor=self.request.user,
            action='codequest.problem_created',
            target_model='Problem',
            target_id=problem.id,
            details={'title': problem.title, 'scheduled_date': str(problem.scheduled_date)},
        )

    def perform_update(self, serializer):
        problem = serializer.save()
        log_audit_event(
            actor=self.request.user,
            action='codequest.problem_updated',
            target_model='Problem',
            target_id=problem.id,
            details={'title': problem.title, 'scheduled_date': str(problem.scheduled_date)},
        )

    def perform_destroy(self, instance):
        log_audit_event(
            actor=self.request.user,
            action='codequest.problem_deleted',
            target_model='Problem',
            target_id=instance.id,
            details={'title': instance.title, 'scheduled_date': str(instance.scheduled_date)},
        )
        instance.delete()

class SubmissionViewSet(viewsets.ModelViewSet):
    serializer_class = SubmissionSerializer
    permission_classes = [permissions.IsAuthenticated, IsOwnerOrAdminOrClubLead]

    def get_queryset(self):
        qs = Submission.objects.select_related('user', 'problem').all().order_by('-created_at')
        if _is_admin_or_club_lead(self.request.user):
            return qs
        return qs.filter(user=self.request.user)

    def perform_create(self, serializer):
        if serializer.validated_data['problem'].scheduled_date != timezone.localdate():
            raise ValidationError({
                'problem': 'This challenge is not available for submission today.'
            })
        serializer.save(user=self.request.user)
        # Any submission (regardless of verdict) makes today an active coding
        # day and can unlock consistency badges; both are server-derived.
        rebuild_coding_streak(self.request.user)
        evaluate_achievements(self.request.user)

    @decorators.action(detail=True, methods=['post'], permission_classes=[IsAdminOrClubLead])
    def review(self, request, pk=None):
        is_correct = request.data.get('is_correct')
        if not isinstance(is_correct, bool):
            return response.Response({'is_correct': ['Provide a boolean verdict.']}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            submission = self.get_queryset().select_for_update().get(pk=pk)
            submission.is_correct = is_correct
            submission.reviewed_by = request.user
            submission.reviewed_at = timezone.now()
            submission.save(update_fields=['is_correct', 'reviewed_by', 'reviewed_at', 'updated_at'])
            # An accepted verdict awards solve/POTD XP (idempotent — a repeated
            # or concurrent review cannot double-award) and refreshes streaks
            # and achievements. XP already in the ledger is never revoked if a
            # verdict is later flipped back to rejected: it is an audit record.
            if is_correct:
                award_rewards_for_acceptance(submission)
            rebuild_user_streak(submission.user)
            rebuild_coding_streak(submission.user)
            evaluate_achievements(submission.user)
            log_audit_event(
                actor=request.user,
                action='codequest.submission_reviewed',
                target_model='Submission',
                target_id=submission.id,
                details={
                    'is_correct': is_correct,
                    'problem_id': submission.problem_id,
                    'submitted_by': submission.user_id,
                },
            )
        return response.Response(self.get_serializer(submission).data)

class UserStreakViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only: streaks have no user-facing write path (no automated updater exists yet)."""
    serializer_class = UserStreakSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = UserStreak.objects.all()
        if _is_admin_or_club_lead(self.request.user):
            return qs
        return qs.filter(user=self.request.user)


class BatchScheduleView(viewsets.ViewSet):
    """
    Batch schedule multiple Codequest problems with their dates.
    Accepts 1-5 problems at a time, each with title, date, difficulty, tags.
    Only accessible to Admin and Club Lead.
    """
    permission_classes = [IsAdminOrClubLeadOrReadOnly]

    def create(self, request):
        serializer = BatchScheduleSerializer(data=request.data)
        if not serializer.is_valid():
            return response.Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        validated = serializer.validated_data
        if 'problems' in validated:
            problem_entries = validated['problems']
        else:
            titles = validated['titles']
            dates = validated['scheduled_dates']
            difficulties = validated.get('difficulties', [])
            tags = validated.get('tags', [])
            urls = validated.get('external_urls', [])
            problem_entries = [
                {
                    'title': title,
                    'difficulty': difficulties[i] if i < len(difficulties) else 'EASY',
                    'statement': f'Codequest problem: {title}',
                    'tags': tags[i] if i < len(tags) else [],
                    'scheduled_date': dates[i],
                    'external_url': urls[i] if i < len(urls) else '',
                }
                for i, title in enumerate(titles)
            ]

        created_problems = []

        with transaction.atomic():
            for problem_data in problem_entries:
                problem_serializer = BatchScheduleProblemSerializer(data=problem_data)
                if problem_serializer.is_valid():
                    problem = problem_serializer.save()
                    created_problems.append({
                        'id': problem.id,
                        'title': problem.title,
                        'slug': problem.slug,
                        'scheduled_date': problem.scheduled_date,
                        'difficulty': problem.difficulty,
                    })
                else:
                    transaction.set_rollback(True)
                    return response.Response(
                        {'error': f'Failed to create problem "{problem_data["title"]}": {problem_serializer.errors}'},
                        status=status.HTTP_400_BAD_REQUEST
                    )

        log_audit_event(
            actor=request.user,
            action='codequest.problems_batch_scheduled',
            target_model='Problem',
            target_id='',
            details={
                'count': len(created_problems),
                'problems': [
                    {'id': p['id'], 'title': p['title'], 'scheduled_date': str(p['scheduled_date'])}
                    for p in created_problems
                ],
            },
        )
        return response.Response({
            'message': f'Successfully scheduled {len(created_problems)} problem(s)',
            'problems': created_problems,
        }, status=status.HTTP_201_CREATED)


class StatsViewSet(viewsets.ViewSet):
    """CodeQuest statistics.

    Member actions are scoped to ``request.user`` — there is no path parameter
    for a user id, so one member can never read another member's stats or
    reward history through the member endpoints. The ``admin-*`` actions are
    gated behind ``IsAdminOrClubLead`` and deliberately aggregate across the
    whole club."""
    permission_classes = [permissions.IsAuthenticated]

    @decorators.action(detail=False, methods=['get'])
    def overview(self, request):
        return response.Response(stats_services.get_overview(request.user))

    @decorators.action(detail=False, methods=['get'])
    def daily(self, request):
        target_date = stats_services.parse_date_param(
            request.query_params.get('date'), 'date', default=timezone.localdate()
        )
        return response.Response(stats_services.get_daily_stats(request.user, target_date))

    @decorators.action(detail=False, methods=['get'])
    def heatmap(self, request):
        raw_year = request.query_params.get('year')
        try:
            year = int(raw_year) if raw_year not in (None, '') else None
        except (TypeError, ValueError):
            raise ValidationError({'year': 'Provide a four-digit year.'}) from None
        return response.Response(stats_services.get_heatmap(request.user, year))

    @decorators.action(detail=False, methods=['get'])
    def streak(self, request):
        return response.Response(stats_services.get_streak_summary(request.user))

    @decorators.action(detail=False, methods=['get'], url_path='potd-streak')
    def potd_streak(self, request):
        return response.Response(stats_services.get_potd_streak_summary(request.user))

    @decorators.action(detail=False, methods=['get'])
    def xp(self, request):
        return response.Response(stats_services.get_xp_summary(request.user))

    @decorators.action(detail=False, methods=['get'])
    def badges(self, request):
        return response.Response(stats_services.get_badges_summary(request.user))

    @decorators.action(detail=False, methods=['get', 'post'])
    def goals(self, request):
        if request.method.lower() == 'post':
            daily = _positive_goal(request.data.get('daily_problems'), 'daily_problems')
            weekly = _positive_goal(request.data.get('weekly_problems'), 'weekly_problems')
            return response.Response(stats_services.set_goals(request.user, daily, weekly))
        return response.Response(stats_services.get_goals(request.user))

    @decorators.action(detail=False, methods=['get'])
    def leaderboard(self, request):
        raw_limit = request.query_params.get('limit')
        limit = None
        if raw_limit not in (None, ''):
            try:
                limit = int(raw_limit)
            except (TypeError, ValueError):
                raise ValidationError({'limit': 'Provide a whole number.'}) from None
            if limit < 1:
                raise ValidationError({'limit': 'Limit must be at least 1.'})
        return response.Response(stats_services.get_leaderboard(request.user, limit))

    @decorators.action(detail=False, methods=['get'])
    def report(self, request):
        return response.Response(stats_services.get_weekly_report(request.user))

    @decorators.action(detail=False, methods=['get'])
    def analytics(self, request):
        today = timezone.localdate()
        end = stats_services.parse_date_param(
            request.query_params.get('end'), 'end', default=today
        )
        start = stats_services.parse_date_param(
            request.query_params.get('start'), 'start', default=end - timedelta(days=89)
        )
        return response.Response(stats_services.get_analytics(request.user, start, end))

    @decorators.action(
        detail=False,
        methods=['get'],
        url_path='admin-overview',
        permission_classes=[IsAdminOrClubLead],
    )
    def admin_overview(self, request):
        """Club-wide CodeQuest dashboard aggregates for admins and club leads.

        Unlike the per-user stats actions above, this deliberately aggregates
        across every member, so it is gated behind an admin permission."""
        raw_days = request.query_params.get('days')
        try:
            days = int(raw_days) if raw_days not in (None, '') else 30
        except (TypeError, ValueError):
            raise ValidationError({'days': 'Provide a whole number.'}) from None
        return response.Response(admin_stats_services.get_admin_overview(days))

    @decorators.action(
        detail=False, methods=['get'], url_path='admin-members',
        permission_classes=[IsAdminOrClubLead],
    )
    def admin_members(self, request):
        search = request.query_params.get('q') or None
        sort = request.query_params.get('sort') or 'xp'
        return response.Response(admin_stats_services.get_admin_members(search=search, sort=sort))

    @decorators.action(
        detail=True, methods=['get'], url_path='member',
        permission_classes=[IsAdminOrClubLead],
    )
    def admin_member_detail(self, request, pk=None):
        member = get_user_model().objects.filter(pk=pk).first()
        if member is None:
            raise NotFound('Member not found.')
        raw_days = request.query_params.get('days')
        try:
            days = int(raw_days) if raw_days not in (None, '') else 90
        except (TypeError, ValueError):
            raise ValidationError({'days': 'Provide a whole number.'}) from None
        return response.Response(admin_stats_services.get_admin_member_detail(member, days))

    @decorators.action(
        detail=False, methods=['get'], url_path='admin-analytics',
        permission_classes=[IsAdminOrClubLead],
    )
    def admin_analytics(self, request):
        today = timezone.localdate()
        end = stats_services.parse_date_param(
            request.query_params.get('end'), 'end', default=today
        )
        start = stats_services.parse_date_param(
            request.query_params.get('start'), 'start', default=end - timedelta(days=29)
        )
        return response.Response(admin_stats_services.get_admin_analytics(start, end))

    @decorators.action(
        detail=False, methods=['get'], url_path='admin-streaks',
        permission_classes=[IsAdminOrClubLead],
    )
    def admin_streaks(self, request):
        return response.Response(admin_stats_services.get_admin_streak_monitoring())

    @decorators.action(
        detail=False, methods=['get'], url_path='admin-gamification',
        permission_classes=[IsAdminOrClubLead],
    )
    def admin_gamification(self, request):
        return response.Response(admin_stats_services.get_admin_gamification())

    @decorators.action(
        detail=False, methods=['get'], url_path='admin-export',
        permission_classes=[IsAdminOrClubLead],
    )
    def admin_export(self, request):
        """Stream a CodeQuest dataset to the admin as a CSV attachment.

        ``dataset`` selects the report (``members``, ``submissions``,
        ``problems``, ``analytics``); any other query parameter is forwarded to
        the matching builder as an optional filter. The export itself is
        recorded on the audit trail because it discloses member-level data."""
        dataset = (request.query_params.get('dataset') or 'members').lower()
        headers, rows = exports.build_report(dataset, request.query_params)
        log_audit_event(
            actor=request.user,
            action='codequest.report_exported',
            target_model='CodeQuestReport',
            target_id=dataset,
            details={'dataset': dataset, 'rows': len(rows), 'filters': request.query_params.dict()},
        )
        return exports.csv_response(dataset, headers, rows)
