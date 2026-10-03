from django.db import migrations


def copy_members(apps, schema_editor):
    """Move the old plain M2M Team.members (+ Team.leader) into TeamMember rows.

    TeamMember enforces one team per user per hackathon, which the old
    schema never did — if a user sits in several teams of the same
    hackathon, the earliest team (by id) keeps them and later ones drop
    them. Leaders are written first so a user who leads one team and is a
    plain member of another stays with the team they lead.

    Team names are also de-duplicated case-insensitively per hackathon
    (suffixing " (2)", " (3)", …) so the unique constraint added in the
    next migration can be created on existing data.
    """
    Team = apps.get_model('hackathons', 'Team')
    TeamMember = apps.get_model('hackathons', 'TeamMember')

    taken = set()  # (hackathon_id, user_id)

    def add(team, user_id, role):
        key = (team.hackathon_id, user_id)
        if key in taken:
            return
        taken.add(key)
        TeamMember.objects.create(team=team, hackathon_id=team.hackathon_id, user_id=user_id, role=role)

    teams = list(Team.objects.order_by('id'))
    for team in teams:
        if team.leader_id:
            add(team, team.leader_id, 'LEADER')
    for team in teams:
        for user_id in team.members.values_list('id', flat=True):
            add(team, user_id, 'MEMBER')

    # Pre-existing teams were created outside the new registration rules;
    # treat them as registered rather than stuck in FORMING.
    Team.objects.update(status='REGISTERED')

    seen_names = {}
    for team in teams:
        key = (team.hackathon_id, team.name.strip().lower())
        count = seen_names.get(key, 0) + 1
        seen_names[key] = count
        if count > 1:
            team.name = f"{team.name} ({count})"
            team.save(update_fields=['name'])


class Migration(migrations.Migration):

    dependencies = [
        ('hackathons', '0004_team_formation_rounds_announcements'),
    ]

    operations = [
        migrations.RunPython(copy_members, migrations.RunPython.noop),
    ]
