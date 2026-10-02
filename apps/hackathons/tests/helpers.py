from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.hackathons.models import Hackathon, ProblemStatement, Team, TeamMember, TeamStatus

User = get_user_model()
_n = {'i': 0}


def make_user(role='NON_AFFILIATE', **kw):
    _n['i'] += 1
    i = _n['i']
    defaults = dict(
        username=f'user{i}', email=f'user{i}@srkr.ac.in', password='pw12345!', role=role,
        first_name=f'First{i}', last_name='Last', phone_number='9876543210', branch='CSE', year=2,
    )
    defaults.update(kw)
    return User.objects.create_user(**defaults)


def make_hackathon(**kw):
    _n['i'] += 1
    start = timezone.now() + timezone.timedelta(days=14)
    defaults = dict(
        title=f'Hack {_n["i"]}', slug=f'hack-{_n["i"]}', theme='t', description='d',
        start_date=start, end_date=start + timezone.timedelta(days=1),
        min_team_size=2, max_team_size=3,
    )
    defaults.update(kw)
    return Hackathon.objects.create(**defaults)


def make_ps(hackathon, code='PS1', **kw):
    return ProblemStatement.objects.create(hackathon=hackathon, code=code, title=f'Problem {code}', **kw)


def make_team(hackathon, leader, *members, name=None, status=TeamStatus.REGISTERED, ps=None):
    team = Team.objects.create(
        hackathon=hackathon, name=name or f'Team of {leader.username}', leader=leader,
        status=status, problem_statement=ps,
    )
    TeamMember.objects.create(team=team, user=leader, role='LEADER')
    for m in members:
        TeamMember.objects.create(team=team, user=m)
    return team
