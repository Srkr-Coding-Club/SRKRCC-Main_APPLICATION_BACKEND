"""A person's notification choices. No stored row means they have not chosen, which means everything is on."""
from .models import NotificationPreference


def event_reminder_emails_enabled(user) -> bool:
    preference = NotificationPreference.objects.filter(user=user).values_list('event_reminder_emails', flat=True).first()
    return True if preference is None else preference


def set_event_reminder_emails(user, enabled: bool) -> bool:
    NotificationPreference.objects.update_or_create(user=user, defaults={'event_reminder_emails': enabled})
    return enabled
