"""Organizer insight: the participation funnel and who the participants are."""
from __future__ import annotations

from collections import Counter

from django.utils import timezone

from django.db.models import Count
from django.db.models.functions import TruncDate

from . import check_in_sessions, payments
from .models import EntryStatus, Hackathon, IdeaStatus, IdeaSubmission, PaymentStatus, RoundEntry, Submission, TeamStatus

TOP_BREAKDOWN_ITEMS = 12


def _college(user) -> str:
    return user.college.name if user.college_id else (user.other_college_name or 'Not given')


def _breakdown(values) -> list[dict]:
    counts = Counter(value or 'Not given' for value in values)
    return [{'label': label, 'count': count} for label, count in counts.most_common(TOP_BREAKDOWN_ITEMS)]


def previous_edition(hackathon: Hackathon, now=None) -> dict | None:
    """How the edition this one was cloned from stood at the same point of its registration, and where it ended.

    "The same point" is the time since registration opened, capped at that edition's own close. Teams that later withdrew
    or were disqualified are not counted then or now, so the comparison errs a little low for the earlier edition.
    """
    source = hackathon.cloned_from
    if source is None or not hackathon.registration_opens_at or not source.registration_opens_at:
        return None
    now = now or timezone.now()
    if now < hackathon.registration_opens_at:
        return None
    point = source.registration_opens_at + (now - hackathon.registration_opens_at)
    if source.registration_closes_at and point > source.registration_closes_at:
        point = source.registration_closes_at

    def counts(until=None):
        teams = source.teams.exclude(status__in=payments.PAYMENT_STATUSES + (TeamStatus.DISQUALIFIED, TeamStatus.WITHDRAWN)).filter(is_waitlisted=False)
        members = source.memberships.filter(team__in=teams)
        if until is not None:
            teams, members = teams.filter(created_at__lte=until), members.filter(joined_at__lte=until)
        return {'teams': teams.count(), 'participants': members.count()}

    then, final = counts(point), counts()
    return {
        'title': source.title, 'slug': source.slug, 'point': point,
        'teams_then': then['teams'], 'participants_then': then['participants'],
        'teams_final': final['teams'], 'participants_final': final['participants'],
    }


def overview(hackathon: Hackathon) -> dict:
    # A team still waiting for its payment check, or turned down, is not a team of this hackathon yet.
    teams = hackathon.teams.exclude(status__in=payments.PAYMENT_STATUSES)
    placed = teams.filter(is_waitlisted=False)
    counted = hackathon.memberships.exclude(team__status__in=payments.PAYMENT_STATUSES)
    memberships = list(counted.select_related('user', 'user__college', 'team'))
    placed_members = [m for m in memberships if not m.team.is_waitlisted]
    submitted = Submission.objects.filter(team__hackathon=hackathon).count()
    shortlisted = RoundEntry.objects.filter(
        round__hackathon=hackathon, status=EntryStatus.SHORTLISTED,
    ).values('team_id').distinct().count()
    registered = placed.filter(status=TeamStatus.REGISTERED).count()
    funnel = [
        {'key': 'participants', 'label': 'Participants joined a team', 'count': len(memberships)},
        {'key': 'teams', 'label': 'Teams created', 'count': teams.count()},
        {'key': 'registered', 'label': 'Teams registered', 'count': registered},
        {'key': 'ideas', 'label': 'Teams submitted an idea', 'count': IdeaSubmission.objects.filter(team__hackathon=hackathon).count()},
        {'key': 'ideas_approved', 'label': 'Teams with an approved idea', 'count': IdeaSubmission.objects.filter(team__hackathon=hackathon, status=IdeaStatus.APPROVED).count()},
        {'key': 'submitted', 'label': 'Teams submitted a project', 'count': submitted},
        {'key': 'scored', 'label': 'Projects scored by a judge', 'count': Submission.objects.filter(team__hackathon=hackathon, review_count__gt=0).count()},
        {'key': 'shortlisted', 'label': 'Teams shortlisted in a round', 'count': shortlisted},
        {'key': 'winners', 'label': 'Teams with an award', 'count': hackathon.winners.values('team_id').distinct().count()},
    ]
    per_day = (
        counted.annotate(day=TruncDate('joined_at')).values('day').annotate(count=Count('id')).order_by('day')
    )
    return {
        'funnel': funnel,
        'previous_edition': previous_edition(hackathon),
        'checked_in': check_in_sessions.main_check_ins(hackathon).count(),
        'waitlisted_teams': teams.filter(is_waitlisted=True).count(),
        'payments': {status: hackathon.team_payments.filter(status=status).count() for status in payments.QUEUE_STATUSES},
        'by_branch': _breakdown(m.user.branch for m in placed_members),
        'by_year': _breakdown(f'Year {m.user.year}' if m.user.year else None for m in placed_members),
        'by_college': _breakdown(_college(m.user) for m in placed_members),
        'joined_per_day': [{'date': row['day'].isoformat(), 'count': row['count']} for row in per_day if row['day']],
        'capacity': {
            'max_teams': hackathon.max_teams, 'max_participants': hackathon.max_participants,
            'placed_teams': placed.exclude(status__in=(TeamStatus.DISQUALIFIED, TeamStatus.WITHDRAWN)).count(),
            'placed_participants': len(placed_members),
        },
    }


def submission_rows(hackathon: Hackathon) -> list[dict]:
    return [
        {
            'id': s.id, 'team_id': s.team_id, 'team': s.team.name, 'team_status': s.team.status,
            'project_title': s.project_title, 'tagline': s.tagline, 'description': s.description,
            'repo_url': s.repo_url, 'demo_url': s.demo_url or '', 'video_url': s.video_url or '', 'slides_url': s.slides_url or '',
            'tech_stack': s.tech_stack, 'is_public': s.is_public, 'score': s.score, 'review_count': s.review_count,
            'submitted_at': s.created_at, 'updated_at': s.updated_at,
        }
        for s in Submission.objects.filter(team__hackathon=hackathon).select_related('team').order_by('-score', 'team__name')
    ]
