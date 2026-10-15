from django.db import migrations

DEFAULT_CODE = 'C-24163'


def backfill_default_college(apps, schema_editor):
    College = apps.get_model('colleges', 'College')
    User = apps.get_model('accounts', 'User')
    default = College.objects.get(code=DEFAULT_CODE)
    User.objects.filter(college__isnull=True).update(college=default)


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0008_user_college'),
        ('colleges', '0002_seed_default_college'),
    ]

    operations = [migrations.RunPython(backfill_default_college, migrations.RunPython.noop)]
