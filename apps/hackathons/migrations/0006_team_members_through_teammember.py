from django.conf import settings
from django.db import migrations, models
import django.db.models.functions.text


class Migration(migrations.Migration):
    """Swap Team.members from a plain M2M to one routed through TeamMember.

    Django can't AlterField an M2M to add `through=`, so the old auto table
    is dropped (its data was copied into TeamMember by 0005) and the field
    is re-added pointing at TeamMember, which adds no new table.
    """

    dependencies = [
        ('hackathons', '0005_copy_team_members_to_teammember'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveField(model_name='team', name='members'),
        migrations.AddField(
            model_name='team',
            name='members',
            field=models.ManyToManyField(
                blank=True, related_name='hackathon_teams',
                through='hackathons.TeamMember', to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddConstraint(
            model_name='team',
            constraint=models.UniqueConstraint(
                django.db.models.functions.text.Lower('name'), 'hackathon',
                name='uniq_team_name_per_hackathon_ci',
            ),
        ),
    ]
