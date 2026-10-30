"""Celebrate someone's registration for an event (see apps.celebrations)."""
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.celebrations import services as celebrations
from apps.forms.models import Response

from .models import Event


@receiver(post_save, sender=Response)
def celebrate_event_registration(sender, instance, created, **kwargs):
    """A signed-in person submitted an event's registration form themselves (not a preview, not an admin's manual entry)."""
    if not created or instance.user_id is None or instance.is_test_submission or instance.created_by_admin_id is not None:
        return
    event = Event.objects.filter(registration_form_id=instance.form_id).first()
    if event is None:
        return
    celebrations.award_after_commit(
        [instance.user], 'event.registered', f'event-registered:{event.pk}', link_url=f'/events/{event.slug}', event=event.title,
    )
