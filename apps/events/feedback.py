"""Who may rate an event, and what the organizers see once people have.

Only people who were scanned in (an AttendanceRecord on their own registration) can leave feedback,
and only once the event is over.
"""
from datetime import timedelta

from django.db.models import Avg, Count, Q
from django.utils import timezone

from apps.core.dmc.contracts import ColumnDefinition
from apps.core.dmc.export_service import format_export_datetime
from apps.forms.models import Response

from .models import Event, EventFeedback

EVENT_NOT_ENDED = 'EVENT_NOT_ENDED'
NOT_ATTENDED = 'NOT_ATTENDED'

UNTIMED_EVENT_DURATION = timedelta(hours=1)
RATINGS = (1, 2, 3, 4, 5)
COMMENT_LIMIT = 100


def event_end(event: Event):
    """When the event is over: its end time, or an hour after the start when only that is set."""
    if event.end_time:
        return event.end_time
    if event.start_time:
        return event.start_time + UNTIMED_EVENT_DURATION
    return None


def has_ended(event: Event, now=None) -> bool:
    end = event_end(event)
    return end is not None and end <= (now or timezone.now())


def _attended_responses(event: Event):
    return Response.objects.filter(
        form_id=event.registration_form_id, is_test_submission=False,
        attendance_badge__records__isnull=False,
    )


def has_attended(event: Event, user) -> bool:
    if event.registration_form_id is None:
        return False
    return _attended_responses(event).filter(user=user).exists()


def attended_count(event: Event) -> int:
    if event.registration_form_id is None:
        return 0
    return _attended_responses(event).values('id').distinct().count()


def blocked_reason(event: Event, user, now=None) -> str | None:
    """Why this person cannot leave feedback right now, or None when they can."""
    if not has_ended(event, now):
        return EVENT_NOT_ENDED
    if not has_attended(event, user):
        return NOT_ATTENDED
    return None


def summary(event: Event) -> dict:
    """Averages, the 1-5 spread, the recommend rate and how many attendees answered."""
    feedback = EventFeedback.objects.filter(event=event)
    totals = feedback.aggregate(count=Count('id'), average=Avg('rating'), recommend=Count('id', filter=Q(would_recommend=True)))
    spread = {row['rating']: row['n'] for row in feedback.values('rating').annotate(n=Count('id'))}
    attendees = attended_count(event)
    count = totals['count']
    return {
        'count': count,
        'average_rating': round(totals['average'], 2) if count else None,
        'recommend_rate': round(totals['recommend'] / count, 3) if count else None,
        'distribution': {str(rating): spread.get(rating, 0) for rating in RATINGS},
        'attendees': attendees,
        'response_rate': round(count / attendees, 3) if attendees else None,
    }


def comments(event: Event):
    """Newest first, only the entries that actually say something, capped for the admin screen."""
    return (
        EventFeedback.objects.filter(event=event).exclude(went_well='', to_improve='')
        .select_related('user').order_by('-created_at')[:COMMENT_LIMIT]
    )


def person_name(user) -> str:
    return f'{user.first_name} {user.last_name}'.strip() or user.email


def export_columns() -> list[ColumnDefinition]:
    return [
        ColumnDefinition(key='name', label='Name', type='text'),
        ColumnDefinition(key='email', label='Email', type='text'),
        ColumnDefinition(key='rating', label='Rating (1-5)', type='number'),
        ColumnDefinition(key='would_recommend', label='Would recommend', type='text'),
        ColumnDefinition(key='went_well', label='What went well', type='text'),
        ColumnDefinition(key='to_improve', label='What to improve', type='text'),
        ColumnDefinition(key='submitted', label='Submitted', type='text'),
    ]


def export_rows(event: Event):
    for item in EventFeedback.objects.filter(event=event).select_related('user').order_by('created_at'):
        yield {
            'name': person_name(item.user),
            'email': item.user.email,
            'rating': item.rating,
            'would_recommend': 'Yes' if item.would_recommend else 'No',
            'went_well': item.went_well,
            'to_improve': item.to_improve,
            'submitted': format_export_datetime(item.created_at),
        }
