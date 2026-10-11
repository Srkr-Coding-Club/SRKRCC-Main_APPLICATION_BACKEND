"""CSV report builders for the CodeQuest admin console.

Aggregation lives in ``admin_stats_services``; this module only reshapes those
results into ordered CSV so the view stays a thin HTTP boundary. Every builder
returns ``(headers, rows)`` where ``headers`` fixes the column order and
``rows`` is a list of mappings keyed by the same header labels — an empty
dataset therefore still emits its header row.
"""

import csv
import io
from datetime import timedelta

from django.db.models import Count, Q
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from . import admin_stats_services, stats_services
from .models import Problem, Submission

MEMBERS_HEADERS = (
    'ID', 'Name', 'Email', 'Club ID', 'Role', 'Level', 'Lifetime XP',
    'Solved', 'Submissions', 'Current Streak', 'Longest Streak',
    'POTD Completed', 'Badges Earned', 'Last Active',
)
SUBMISSIONS_HEADERS = (
    'ID', 'Problem', 'Scheduled Date', 'Member', 'Email', 'Language',
    'Correct', 'Reviewed', 'Reviewed By', 'Reviewed At', 'Submitted At',
)
PROBLEMS_HEADERS = (
    'ID', 'Title', 'Slug', 'Difficulty', 'Scheduled Date',
    'Submissions', 'Solved', 'Platform', 'External URL',
)
ANALYTICS_HEADERS = ('Date', 'Submissions', 'Accepted', 'Solved', 'Active Users')

SUBMISSION_VERDICTS = ('all', 'pending', 'accepted', 'rejected')
PROBLEM_SCOPES = ('all', 'upcoming', 'archived')


def _members_report(params):
    search = params.get('q') or None
    directory = admin_stats_services.get_admin_members(search=search)
    rows = [
        {
            'ID': member['id'],
            'Name': member['name'],
            'Email': member['email'],
            'Club ID': member['club_id'],
            'Role': member['role'],
            'Level': member['level'],
            'Lifetime XP': member['lifetime_xp'],
            'Solved': member['solved'],
            'Submissions': member['submissions'],
            'Current Streak': member['current_streak'],
            'Longest Streak': member['longest_streak'],
            'POTD Completed': member['potd_completed'],
            'Badges Earned': member['badges_earned'],
            'Last Active': member['last_active'].isoformat() if member['last_active'] else '',
        }
        for member in directory['members']
    ]
    return MEMBERS_HEADERS, rows


def _submissions_report(params):
    queryset = Submission.objects.select_related('problem', 'user', 'reviewed_by').order_by(
        '-created_at'
    )

    verdict = (params.get('verdict') or 'all').lower()
    if verdict not in SUBMISSION_VERDICTS:
        raise ValidationError(
            {'verdict': "Use 'all', 'pending', 'accepted' or 'rejected'."}
        )
    if verdict == 'pending':
        queryset = queryset.filter(reviewed_at__isnull=True)
    elif verdict == 'accepted':
        queryset = queryset.filter(is_correct=True)
    elif verdict == 'rejected':
        queryset = queryset.filter(is_correct=False, reviewed_at__isnull=False)

    rows = [
        {
            'ID': submission.id,
            'Problem': submission.problem.title,
            'Scheduled Date': submission.problem.scheduled_date.isoformat()
            if submission.problem.scheduled_date
            else '',
            'Member': submission.user.full_name,
            'Email': submission.user.email,
            'Language': submission.language,
            'Correct': 'Yes' if submission.is_correct else 'No',
            'Reviewed': 'Yes' if submission.reviewed_at else 'No',
            'Reviewed By': submission.reviewed_by.full_name if submission.reviewed_by else '',
            'Reviewed At': submission.reviewed_at.isoformat() if submission.reviewed_at else '',
            'Submitted At': submission.created_at.isoformat() if submission.created_at else '',
        }
        for submission in queryset
    ]
    return SUBMISSIONS_HEADERS, rows


def _problems_report(params):
    queryset = Problem.objects.annotate(
        submissions_count=Count('submissions', distinct=True),
        solved_count=Count(
            'submissions__user',
            filter=Q(submissions__is_correct=True),
            distinct=True,
        ),
    ).order_by('-scheduled_date')

    scope = (params.get('scope') or 'all').lower()
    if scope not in PROBLEM_SCOPES:
        raise ValidationError({'scope': "Use 'all', 'upcoming' or 'archived'."})
    today = timezone.localdate()
    if scope == 'upcoming':
        queryset = queryset.filter(scheduled_date__gt=today)
    elif scope == 'archived':
        queryset = queryset.filter(scheduled_date__lte=today)

    rows = [
        {
            'ID': problem.id,
            'Title': problem.title,
            'Slug': problem.slug,
            'Difficulty': problem.difficulty,
            'Scheduled Date': problem.scheduled_date.isoformat()
            if problem.scheduled_date
            else '',
            'Submissions': problem.submissions_count,
            'Solved': problem.solved_count,
            'Platform': problem.external_platform or '',
            'External URL': problem.external_url or '',
        }
        for problem in queryset
    ]
    return PROBLEMS_HEADERS, rows


def _analytics_report(params):
    today = timezone.localdate()
    end = stats_services.parse_date_param(params.get('end'), 'end', default=today)
    start = stats_services.parse_date_param(
        params.get('start'), 'start', default=end - timedelta(days=29)
    )
    analytics = admin_stats_services.get_admin_analytics(start, end)
    rows = [
        {
            'Date': day['date'].isoformat(),
            'Submissions': day['submissions'],
            'Accepted': day['accepted'],
            'Solved': day['solved'],
            'Active Users': day['active_users'],
        }
        for day in analytics['daily']
    ]
    return ANALYTICS_HEADERS, rows


REPORTS = {
    'members': _members_report,
    'submissions': _submissions_report,
    'problems': _problems_report,
    'analytics': _analytics_report,
}


def build_report(dataset, params):
    builder = REPORTS.get(dataset)
    if builder is None:
        raise ValidationError(
            {'dataset': f"Unknown dataset. Use one of: {', '.join(REPORTS)}."}
        )
    return builder(params)


def csv_response(dataset, headers, rows):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(headers))
    writer.writeheader()
    writer.writerows(rows)

    response = HttpResponse(
        buffer.getvalue().encode('utf-8-sig'),
        content_type='text/csv; charset=utf-8',
    )
    stamp = timezone.now().strftime('%Y%m%d_%H%M%S')
    response['Content-Disposition'] = (
        f'attachment; filename="codequest_{dataset}_{stamp}.csv"'
    )
    return response
