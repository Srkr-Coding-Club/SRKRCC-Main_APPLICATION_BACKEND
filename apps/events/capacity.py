"""An event's seats live on its registration form (``max_total_responses``), and the form closes itself when the last one is taken.

When a seat comes back, by cancelling a registration or by raising the event's capacity, that automatic closing has to be
undone, or the freed seat could never be taken.
"""
from django.utils import timezone

from apps.forms.models import Form, FormStatus, Response


def registered_count(form: Form) -> int:
    return Response.objects.filter(form=form, is_test_submission=False).count()


def is_full(form: Form) -> bool:
    return form.max_total_responses is not None and registered_count(form) >= form.max_total_responses


def reopen_if_room(form: Form, was_full: bool, now=None) -> bool:
    """Publish a form again that closed because it filled up, now that it has room. True when it was reopened.

    Only a form that was full before the change is touched, so one an organizer closed on purpose stays closed.
    A form whose own closing time has passed stays closed too.
    """
    if not was_full or form.status != FormStatus.CLOSED or is_full(form):
        return False
    if form.close_at is not None and form.close_at <= (now or timezone.now()):
        return False
    form.status = FormStatus.PUBLISHED
    form.save(update_fields=['status', 'updated_at'])
    return True
