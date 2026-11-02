"""Reminders for people registered for an upcoming event: one a day before and one an hour before.

Run ``manage.py send_event_reminders`` hourly from cron. Every reminder is recorded in ``EventReminder`` before it is
sent, so running it more often, or twice at once, never sends the same reminder twice. Someone who registered after a
reminder's moment is not sent that reminder: they signed up knowing the event is close.
"""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.html import escape

from apps.core.services.email_service import EmailNotificationService
from apps.forms.models import Response
from apps.notifications import preferences
from apps.notifications.models import NotificationCategory, NotificationType
from apps.notifications.services import NotificationService

from .models import Event, EventReminder

LEADS = {
    EventReminder.Lead.DAY: timedelta(hours=24),
    EventReminder.Lead.HOUR: timedelta(hours=1),
}
HOUR_TEXT = 'in about an hour'


def _frontend_url(event: Event) -> str:
    return f"{getattr(settings, 'FRONTEND_URL', 'http://localhost:3000').rstrip('/')}/events/{event.slug}"


def _details(event: Event) -> str:
    start = timezone.localtime(event.start_time).strftime('%d %b %Y, %I:%M %p')
    return f'{start} IST' + (f' at {event.venue}' if event.venue else '')


def _recipients(event: Event, lead: str):
    """Everyone registered before the reminder's moment who has not had it yet."""
    registered_by = event.start_time - LEADS[lead]
    users = {}
    responses = Response.objects.filter(
        form_id=event.registration_form_id, user__isnull=False, user__is_active=True,
        is_test_submission=False, submitted_at__lte=registered_by,
    ).select_related('user')
    for response in responses:
        users[response.user_id] = response.user
    already = set(EventReminder.objects.filter(event=event, lead=lead).values_list('user_id', flat=True))
    return [user for user_id, user in users.items() if user_id not in already]


def _when(event: Event, lead: str, now) -> str:
    """How to say when the event starts: 'in about an hour', 'today' or 'tomorrow' (the day reminder goes out up to 24 hours ahead)."""
    if lead == EventReminder.Lead.HOUR:
        return HOUR_TEXT
    return 'today' if timezone.localtime(event.start_time).date() == timezone.localtime(now).date() else 'tomorrow'


def _send(event: Event, user, lead: str, now) -> bool:
    """Claim the reminder, then notify. Returns True when this call sent it."""
    _, claimed = EventReminder.objects.get_or_create(event=event, user=user, lead=lead)
    if not claimed:
        return False
    when = _when(event, lead, now)
    title = f'{event.title} starts {when}'
    NotificationService.create_notification(
        user, title, _details(event), type=NotificationType.INFO, category=NotificationCategory.EVENT, link_url=f'/events/{event.slug}',
    )
    if user.email and preferences.event_reminder_emails_enabled(user):
        url = _frontend_url(event)
        name = user.first_name or user.email.split('@')[0]
        text = f'Hi {name},\n\n{event.title} starts {when}: {_details(event)}.\n\n{url}\n\nYou can turn these emails off in your profile.\n'
        html = (
            f'<p>Hi {escape(name)},</p><p><strong>{escape(event.title)}</strong> starts {when}: {escape(_details(event))}.</p>'
            f'<p><a href="{escape(url)}">Open the event page</a></p><p>You can turn these emails off in your profile.</p>'
        )
        EmailNotificationService.send_email(user.email, f'Reminder: {title}', text, html)
    return True


def send_due(now=None) -> dict[str, int]:
    """Send every reminder that is due. Returns how many went out per lead."""
    now = now or timezone.now()
    sent = {lead.value: 0 for lead in LEADS}
    for lead, span in LEADS.items():
        events = Event.objects.filter(
            start_time__gt=now, start_time__lte=now + span, registration_form__isnull=False,
        ).select_related('registration_form')
        for event in events:
            sent[lead.value] += sum(_send(event, user, lead, now) for user in _recipients(event, lead))
    return sent
