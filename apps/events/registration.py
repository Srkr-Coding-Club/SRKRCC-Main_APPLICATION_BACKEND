"""Cancelling an event registration.

A registration is a form response, and both the seat count and the one-response-per-person rule count responses,
so deleting the response is what frees the seat and lets the person register again. Each cancellation is written
to the audit log because the response itself is gone afterwards.
"""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.audit.utils import log_audit_event
from apps.forms.models import Response

from . import capacity
from . import feedback as attendance
from .models import Event

NOT_REGISTERED = 'NOT_REGISTERED'
EVENT_STARTED = 'EVENT_STARTED'
ALREADY_ATTENDED = 'ALREADY_ATTENDED'
CANCELLATION_LOCKED = 'CANCELLATION_LOCKED'
CANCELLATION_CLOSED = 'CANCELLATION_CLOSED'

MESSAGES = {
    NOT_REGISTERED: 'You are not registered for this event.',
    EVENT_STARTED: 'Registration can no longer be cancelled because the event has started.',
    ALREADY_ATTENDED: 'Registration cannot be cancelled after you have been checked in.',
    CANCELLATION_LOCKED: 'The organizers have closed cancellations for this event. Ask them if you cannot come.',
    CANCELLATION_CLOSED: 'Cancellations for this event have closed. Ask the organizers if you cannot come.',
}


class CancellationError(Exception):
    def __init__(self, code: str, status: int = 409):
        super().__init__(MESSAGES[code])
        self.code, self.message, self.status = code, MESSAGES[code], status


def has_started(event: Event, now=None) -> bool:
    return event.start_time is not None and event.start_time <= (now or timezone.now())


def cancellation_closes_at(event: Event):
    """When cancelling stops: the start, or the configured number of hours before it. None for an event with no date."""
    if event.start_time is None:
        return None
    return event.start_time - timedelta(hours=event.cancellation_cutoff_hours)


def cancellation_state(event: Event, now=None) -> dict:
    """Whether registered people can still cancel on their own: ``allowed``, and when not the ``reason`` code."""
    now = now or timezone.now()
    closes_at = cancellation_closes_at(event)
    if has_started(event, now):
        reason = EVENT_STARTED
    elif event.cancellation_locked:
        reason = CANCELLATION_LOCKED
    elif closes_at is not None and closes_at <= now:
        reason = CANCELLATION_CLOSED
    else:
        reason = None
    # Only a configured cutoff is worth telling people about; with none, "until the event starts" is the plain default.
    show_closing = reason is None and event.cancellation_cutoff_hours > 0
    return {'allowed': reason is None, 'reason': reason, 'closes_at': closes_at if show_closing else None}


def _registrations(event: Event, user):
    if event.registration_form_id is None:
        return Response.objects.none()
    return Response.objects.filter(form_id=event.registration_form_id, user=user, is_test_submission=False)


def cancel(event: Event, user, now=None) -> None:
    """Delete the person's own registration, or raise CancellationError saying why not."""
    with transaction.atomic():
        responses = list(_registrations(event, user).select_related('form').select_for_update(of=('self',)))
        if not responses:
            raise CancellationError(NOT_REGISTERED, status=404)
        if attendance.has_attended(event, user):
            raise CancellationError(ALREADY_ATTENDED)
        state = cancellation_state(event, now)
        if state['reason']:
            raise CancellationError(state['reason'])
        form = responses[0].form
        was_full = capacity.is_full(form)
        for response in responses:
            response.delete()
        capacity.reopen_if_room(form, was_full)
        log_audit_event(
            actor=user, action='Cancelled Event Registration', target_model='Event', target_id=event.slug,
            details={'title': event.title, 'registrations_removed': len(responses)},
        )
