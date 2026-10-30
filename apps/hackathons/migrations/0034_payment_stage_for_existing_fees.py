"""Hackathons that already ask for a fee with the idea keep doing so: their stage is "with the idea"."""
from django.db import migrations


def keep_idea_payments(apps, schema_editor):
    Hackathon = apps.get_model('hackathons', 'Hackathon')
    Hackathon.objects.filter(idea_fee_amount__gt=0).update(payment_stage='IDEA')


class Migration(migrations.Migration):

    dependencies = [
        ('hackathons', '0033_team_payments'),
    ]

    operations = [
        migrations.RunPython(keep_idea_payments, migrations.RunPython.noop),
    ]
