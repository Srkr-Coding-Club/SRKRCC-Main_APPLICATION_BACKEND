"""
The happy moments of a hackathon, as celebrations (see ``apps.celebrations``).

Each function is called from the place where the moment really happens and only decides who is celebrated, with which
key (so nobody is celebrated twice for the same moment) and where the card leads. Rejections and withdrawals are never
celebrated.
"""
from __future__ import annotations

from apps.celebrations import services as celebrations

from .models import Hackathon, HackathonWinner, IdeaSubmission, Round, Submission, Team, TeamMember


def _people(team: Team) -> list:
    return [m.user for m in team.memberships.select_related('user')]


def _link(hackathon: Hackathon) -> str:
    return f'/hackathons/{hackathon.slug}/dashboard'


def _name(user) -> str:
    return f'{user.first_name} {user.last_name}'.strip() or user.email


def team_registered(team: Team) -> None:
    """The team holds a place and meets its minimum size: everyone on it."""
    celebrations.award_after_commit(
        _people(team), 'hackathon.team_registered', f'team-registered:{team.pk}',
        link_url=_link(team.hackathon), team=team.name, hackathon=team.hackathon.title,
    )


def waitlist_promoted(team: Team) -> None:
    celebrations.award_after_commit(
        _people(team), 'hackathon.waitlist_promoted', f'waitlist-promoted:{team.pk}',
        link_url=_link(team.hackathon), team=team.name, hackathon=team.hackathon.title,
    )


def joined(team: Team, newcomer) -> None:
    """Someone joined: they get a welcome, the others hear who."""
    hackathon = team.hackathon
    celebrations.award_after_commit(
        [newcomer], 'hackathon.joined_team', f'joined-team:{team.pk}:{newcomer.pk}',
        link_url=_link(hackathon), team=team.name, hackathon=hackathon.title,
    )
    others = [person for person in _people(team) if person.pk != newcomer.pk]
    celebrations.award_after_commit(
        others, 'hackathon.member_joined', f'member-joined:{team.pk}:{newcomer.pk}',
        link_url=_link(hackathon), member=_name(newcomer), team=team.name,
    )


def idea_approved(idea: IdeaSubmission) -> None:
    team = idea.team
    celebrations.award_after_commit(
        _people(team), 'hackathon.idea_approved', f'idea-approved:{idea.pk}',
        link_url=_link(team.hackathon), team=team.name, hackathon=team.hackathon.title,
    )


def project_submitted(team: Team, submission: Submission) -> None:
    celebrations.award_after_commit(
        _people(team), 'hackathon.project_submitted', f'project-submitted:{team.pk}',
        link_url=_link(team.hackathon), team=team.name, hackathon=team.hackathon.title,
    )


def shortlisted(round_obj: Round, team_ids) -> None:
    """These teams were shortlisted and the round's results are visible to them."""
    hackathon = round_obj.hackathon
    for team in hackathon.teams.filter(pk__in=list(team_ids)):
        celebrations.award_after_commit(
            _people(team), 'hackathon.shortlisted', f'shortlisted:{round_obj.pk}:{team.pk}',
            link_url=_link(hackathon), team=team.name, round=round_obj.name, hackathon=hackathon.title,
        )


def shortlist_withdrawn(round_obj: Round, team_ids=None) -> None:
    """The result was taken back or changed before the team saw it: nothing to celebrate any more."""
    teams = round_obj.entries.values_list('team_id', flat=True) if team_ids is None else team_ids
    for team_id in list(teams):
        celebrations.retract(f'shortlisted:{round_obj.pk}:{team_id}')


def winners(hackathon: Hackathon) -> None:
    for winner in hackathon.winners.select_related('team'):
        celebrations.award_after_commit(
            _people(winner.team), 'hackathon.winner', f'winner:{winner.pk}',
            link_url=f'/hackathons/{hackathon.slug}#results', team=winner.team.name, award=winner.title, hackathon=hackathon.title,
        )


def winners_withdrawn(hackathon: Hackathon) -> None:
    for winner_id in hackathon.winners.values_list('pk', flat=True):
        celebrations.retract(f'winner:{winner_id}')


def certificate_ready(hackathon: Hackathon) -> None:
    """Certificates were released: everyone who earned one."""
    from . import certificates

    memberships = TeamMember.objects.select_related('team', 'user').filter(
        hackathon=hackathon, team__status='REGISTERED', team__is_waitlisted=False,
    )
    people = [m.user for m in memberships if certificates._earned(hackathon, m)]
    celebrations.award_after_commit(
        people, 'hackathon.certificate_ready', f'certificate-ready:{hackathon.pk}',
        link_url=f'/hackathons/{hackathon.slug}/certificate', hackathon=hackathon.title,
    )


def checked_in(user, hackathon: Hackathon) -> None:
    celebrations.award_after_commit(
        [user], 'hackathon.checked_in', f'hackathon-check-in:{hackathon.pk}',
        link_url=_link(hackathon), hackathon=hackathon.title,
    )


def team_gone(team: Team) -> None:
    """The team was withdrawn or disqualified: cards its members have not seen yet would now be wrong."""
    celebrations.retract(f'team-registered:{team.pk}')
    celebrations.retract(f'waitlist-promoted:{team.pk}')
    for round_id in team.round_entries.values_list('round_id', flat=True):
        celebrations.retract(f'shortlisted:{round_id}:{team.pk}')
    for winner_id in HackathonWinner.objects.filter(team=team).values_list('pk', flat=True):
        celebrations.retract(f'winner:{winner_id}')

