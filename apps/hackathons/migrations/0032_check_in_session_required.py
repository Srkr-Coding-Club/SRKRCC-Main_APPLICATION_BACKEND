"""Once every check-in belongs to a session: the session is required and uniqueness is per person per session."""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('hackathons', '0031_check_in_main_sessions'),
    ]

    operations = [
        migrations.AlterField(
            model_name='checkin',
            name='session',
            field=models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='check_ins', to='hackathons.checkinsession'),
        ),
        migrations.RemoveConstraint(model_name='checkin', name='uniq_check_in_per_user'),
    ]
