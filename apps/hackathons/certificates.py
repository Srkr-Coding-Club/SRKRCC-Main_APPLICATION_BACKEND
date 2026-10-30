"""Certificates: who has earned one, its wording (admin-written template) and a signed code anyone can verify."""
from __future__ import annotations

from django.core import signing

from .config_validation import certificate_settings
from .errors import HackathonError
from . import check_in_sessions
from .models import Hackathon, HackathonWinner, TeamMember, TeamStatus

CODE_SALT = 'hackathon-certificate'
KIND_PARTICIPATION = 'PARTICIPATION'
KIND_WINNER = 'WINNER'


def _display_name(user) -> str:
    return f'{user.first_name} {user.last_name}'.strip() or user.email


def _render(template: str, **values) -> str:
    out = template
    for key, value in values.items():
        out = out.replace('{' + key + '}', str(value))
    return out


def _earned(hackathon: Hackathon, membership: TeamMember) -> bool:
    team = membership.team
    if team.status != TeamStatus.REGISTERED or team.is_waitlisted:
        return False
    rule = certificate_settings(hackathon)['eligibility']
    if rule == 'SUBMITTED':
        return hasattr(team, 'submission')
    if rule == 'CHECKED_IN':
        return check_in_sessions.has_arrived(hackathon, membership.user_id)
    return True


def _payload(hackathon: Hackathon, user, team_name: str, kind: str, award: HackathonWinner | None) -> dict:
    config = certificate_settings(hackathon)
    name = _display_name(user)
    body = _render(config['body'], name=name, team=team_name, hackathon=hackathon.title)
    title = config['title']
    if kind == KIND_WINNER and award is not None:
        title = f'Certificate of Achievement: {award.title}'
        body = f'{body} Awarded: {award.title}.'
    return {
        'kind': kind, 'title': title, 'body': body, 'name': name, 'team': team_name,
        'hackathon': hackathon.title, 'organizer': hackathon.organizer_name or 'SRKR Coding Club',
        'date': hackathon.end_date, 'signatory_name': config['signatory_name'], 'signatory_title': config['signatory_title'],
    }


def certificate_for(hackathon: Hackathon, user) -> dict:
    if not hackathon.feature('certificates'):
        raise HackathonError('Certificates are not enabled for this hackathon.', 'FEATURE_OFF')
    if not certificate_settings(hackathon)['released']:
        raise HackathonError('Certificates have not been released yet.', 'NOT_RELEASED')
    membership = TeamMember.objects.select_related('team').filter(hackathon=hackathon, user=user).first()
    if membership is None or not _earned(hackathon, membership):
        raise HackathonError('You are not eligible for a certificate for this hackathon.', 'NOT_ELIGIBLE', status=403)
    award = HackathonWinner.objects.filter(hackathon=hackathon, team=membership.team).order_by('rank').first()
    kind = KIND_WINNER if award else KIND_PARTICIPATION
    data = _payload(hackathon, user, membership.team.name, kind, award)
    data['code'] = signing.dumps({'h': hackathon.id, 'u': user.id, 'k': kind, 'a': award.title if award else ''}, salt=CODE_SALT, compress=True)
    return data


def verify(code: str) -> dict:
    """Public check that a certificate code is genuine; returns only what is printed on the certificate."""
    from django.contrib.auth import get_user_model

    try:
        payload = signing.loads(str(code or '').strip(), salt=CODE_SALT)
    except signing.BadSignature:
        raise HackathonError('This certificate code is not valid.', 'INVALID_CODE', status=404)
    hackathon = Hackathon.objects.filter(pk=payload.get('h')).first()
    user = get_user_model().objects.filter(pk=payload.get('u')).first()
    if hackathon is None or user is None:
        raise HackathonError('This certificate code is not valid.', 'INVALID_CODE', status=404)
    membership = TeamMember.objects.select_related('team').filter(hackathon=hackathon, user=user).first()
    return {
        'valid': True, 'name': _display_name(user), 'hackathon': hackathon.title, 'kind': payload.get('k'),
        'award': payload.get('a') or None, 'team': membership.team.name if membership else '',
    }


def preview(hackathon: Hackathon, actor) -> dict:
    """What a certificate looks like with the current wording, using the admin's own name."""
    return _payload(hackathon, actor, 'Team Example', KIND_PARTICIPATION, None) | {'code': 'PREVIEW'}


def eligible_count(hackathon: Hackathon) -> int:
    memberships = TeamMember.objects.select_related('team').filter(
        hackathon=hackathon, team__status=TeamStatus.REGISTERED, team__is_waitlisted=False,
    )
    return sum(1 for membership in memberships if _earned(hackathon, membership))
