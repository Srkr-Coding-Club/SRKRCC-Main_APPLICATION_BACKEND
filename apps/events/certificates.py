"""Attendance certificates for events: who has earned one, its wording and a signed code anyone can verify.

Like hackathon certificates nothing is stored per person: the code is a signed (event, person) pair, so it
cannot be forged and needs no table. Event codes start with ``ev.`` so the public verify page can tell them
from hackathon codes.
"""
from django.contrib.auth import get_user_model
from django.core import signing

from . import feedback as attendance
from .models import Event

CODE_SALT = 'event-certificate'
CODE_PREFIX = 'ev.'
TITLE = 'Certificate of Participation'
DEFAULT_BODY = '{name} attended {event}.'
DEFAULT_ORGANIZER = 'SRKR Coding Club'

NOT_RELEASED = 'NOT_RELEASED'
NOT_ELIGIBLE = 'NOT_ELIGIBLE'


class CertificateError(Exception):
    def __init__(self, code: str, message: str, status: int = 403):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _render(template: str, **values) -> str:
    out = template
    for key, value in values.items():
        out = out.replace('{' + key + '}', str(value))
    return out


def _event_date(event: Event):
    return event.end_time or event.start_time


def _payload(event: Event, name: str) -> dict:
    return {
        'kind': 'PARTICIPATION', 'title': TITLE, 'name': name, 'event': event.title,
        'body': _render(event.certificate_body or DEFAULT_BODY, name=name, event=event.title),
        'organizer': DEFAULT_ORGANIZER, 'date': _event_date(event),
        'signatory_name': event.certificate_signatory_name, 'signatory_title': event.certificate_signatory_title,
    }


def certificate_for(event: Event, user) -> dict:
    if not event.certificates_released:
        raise CertificateError(NOT_RELEASED, 'Certificates have not been released yet.')
    if not attendance.has_attended(event, user):
        raise CertificateError(NOT_ELIGIBLE, 'Only people who were checked in at the event can get a certificate.')
    data = _payload(event, attendance.person_name(user))
    data['code'] = CODE_PREFIX + signing.dumps({'e': event.id, 'u': user.id}, salt=CODE_SALT, compress=True)
    return data


def verify(code: str) -> dict:
    """Public check that a code is genuine; returns only what is printed on the certificate."""
    invalid = CertificateError('INVALID_CODE', 'This certificate code is not valid.', status=404)
    text = str(code or '').strip()
    if not text.startswith(CODE_PREFIX):
        raise invalid
    try:
        payload = signing.loads(text[len(CODE_PREFIX):], salt=CODE_SALT)
    except signing.BadSignature:
        raise invalid
    event = Event.objects.filter(pk=payload.get('e')).first()
    user = get_user_model().objects.filter(pk=payload.get('u')).first()
    if event is None or user is None:
        raise invalid
    return {'valid': True, 'kind': 'PARTICIPATION', 'name': attendance.person_name(user), 'event': event.title, 'date': _event_date(event)}


def preview(event: Event, actor) -> dict:
    """The certificate with the current wording, using the organizer's own name."""
    return _payload(event, attendance.person_name(actor)) | {'code': 'PREVIEW'}


def eligible_count(event: Event) -> int:
    return attendance.attended_count(event)
