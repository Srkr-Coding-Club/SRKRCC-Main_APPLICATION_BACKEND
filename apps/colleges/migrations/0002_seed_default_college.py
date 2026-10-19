from django.db import migrations

DEFAULT_CODE = 'C-24163'


def seed_default_college(apps, schema_editor):
    College = apps.get_model('colleges', 'College')
    College.objects.get_or_create(
        code=DEFAULT_CODE,
        defaults={
            'name': 'SRKR Engineering College',
            'search_name': 'srkr engineering college sagi rama krishnam raju engineering college',
            'university': 'Andhra University, Visakhapatnam',
            'college_type': 'Affiliated College',
            'state': 'Andhra Pradesh',
            'district': 'West Godavari',
        },
    )


class Migration(migrations.Migration):

    dependencies = [('colleges', '0001_initial')]

    operations = [migrations.RunPython(seed_default_college, migrations.RunPython.noop)]
