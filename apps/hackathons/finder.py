"""Team finder: solo participants list themselves, teams that are recruiting say what they need."""
from __future__ import annotations

from django.db import transaction

from apps.core.permissions import _is_admin_or_club_lead

from . import config_validation as validate
from .errors import HackathonError
from .models import Hackathon, Team, TeamMember, TeamSeeker, TeamStatus
from .services import (
    ACTIVE_TEAM_STATUSES, TeamService, _notify_users, _require_registration_open, is_leader, problem_label,
)


def _require_finder_on(hackathon: Hackathon):
    if not hackathon.feature('team_finder'):
        raise HackathonError('The team finder is not enabled for this hackathon.', 'FEATURE_OFF')


def _person(user) -> str:
    return f'{user.first_name} {user.last_name}'.strip() or user.email.split('@')[0]


def _profile_line(user) -> str:
    college = user.college.name if user.college_id else (user.other_college_name or '')
    return ' · '.join(part for part in (user.branch, f'Year {user.year}' if user.year else '', college) if part)


class FinderService:

    @staticmethod
    @transaction.atomic
    def save_profile(hackathon: Hackathon, user, data: dict) -> TeamSeeker:
        _require_finder_on(hackathon)
        _require_registration_open(hackathon)
        if TeamMember.objects.filter(hackathon=hackathon, user=user).exists():
            raise HackathonError('You are already on a team.', 'ALREADY_IN_TEAM')
        try:
            skills = validate._text(data.get('skills'), 'Skills', 200)
            note = validate._text(data.get('note'), 'Note', 300)
        except Exception as exc:
            raise HackathonError(' '.join(str(m) for m in getattr(exc, 'detail', [exc])), 'INVALID')
        seeker, _ = TeamSeeker.objects.update_or_create(
            hackathon=hackathon, user=user, defaults={'skills': skills, 'note': note, 'is_active': True},
        )
        return seeker

    @staticmethod
    def withdraw_profile(hackathon: Hackathon, user) -> None:
        TeamSeeker.objects.filter(hackathon=hackathon, user=user).update(is_active=False)

    @staticmethod
    def listing(hackathon: Hackathon, viewer) -> dict:
        _require_finder_on(hackathon)
        in_team = set(hackathon.memberships.values_list('user_id', flat=True))
        seekers = [
            {
                'id': seeker.id, 'name': _person(seeker.user), 'skills': seeker.skills, 'note': seeker.note,
                'profile': _profile_line(seeker.user), 'is_me': seeker.user_id == viewer.id,
            }
            for seeker in hackathon.seekers.select_related('user', 'user__college').filter(is_active=True)
            if seeker.user_id not in in_team
        ]
        teams = []
        for team in (
            hackathon.teams.filter(looking_for_members=True, status__in=ACTIVE_TEAM_STATUSES)
            .select_related('problem_statement', 'leader').prefetch_related('memberships')
        ):
            size = team.memberships.count()
            if size >= hackathon.max_team_size:
                continue
            teams.append({
                'id': team.id, 'name': team.name, 'looking_for': team.looking_for, 'members': size,
                'max_members': hackathon.max_team_size, 'problem': problem_label(team),
                'leader': _person(team.leader) if team.leader else '',
            })
        mine = TeamSeeker.objects.filter(hackathon=hackathon, user=viewer, is_active=True).first()
        return {
            'seekers': seekers, 'teams': teams,
            'my_profile': {'skills': mine.skills, 'note': mine.note} if mine else None,
        }

    @staticmethod
    def invite_seeker(hackathon: Hackathon, actor, seeker_id: int) -> None:
        """A team leader invites a listed participant; the participant's email is never shown, only used here."""
        _require_finder_on(hackathon)
        seeker = hackathon.seekers.select_related('user').filter(pk=seeker_id, is_active=True).first()
        if seeker is None:
            raise HackathonError('That person is no longer looking for a team.', 'NOT_FOUND', status=404)
        membership = TeamMember.objects.select_related('team').filter(hackathon=hackathon, user=actor).first()
        if membership is None or not is_leader(membership.team, actor):
            raise HackathonError('Create a team first, then invite people to it.', 'NOT_A_LEADER')
        TeamService.invite(membership.team, actor, seeker.user.email)

    @staticmethod
    def request_to_join(hackathon: Hackathon, user, team_id: int, message: str = '') -> None:
        _require_finder_on(hackathon)
        _require_registration_open(hackathon)
        team = hackathon.teams.select_related('leader').filter(
            pk=team_id, looking_for_members=True, status__in=ACTIVE_TEAM_STATUSES,
        ).first()
        if team is None or team.leader is None:
            raise HackathonError('That team is not recruiting.', 'NOT_FOUND', status=404)
        if TeamMember.objects.filter(hackathon=hackathon, user=user).exists():
            raise HackathonError('You are already on a team.', 'ALREADY_IN_TEAM')
        try:
            note = validate._text(message, 'Message', 300)
        except Exception as exc:
            raise HackathonError(' '.join(str(m) for m in getattr(exc, 'detail', [exc])), 'INVALID', field='message')
        seeker = TeamSeeker.objects.filter(hackathon=hackathon, user=user, is_active=True).first()
        skills = f' Skills: {seeker.skills}.' if seeker and seeker.skills else ''
        _notify_users(
            [team.leader], f'{_person(user)} wants to join {team.name}',
            f"{_person(user)} ({_profile_line(user) or 'no profile details'}) asked to join your team.{skills} {note} "
            'Find them under Team finder and send an invite.'.strip(),
            hackathon, user,
        )
