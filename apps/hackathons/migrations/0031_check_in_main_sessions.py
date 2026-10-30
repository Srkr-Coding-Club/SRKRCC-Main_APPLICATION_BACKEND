"""Every hackathon that uses check-in gets its main (arrival) session, and every existing check-in joins it."""
from django.db import migrations
from django.utils import timezone


def create_main_sessions(apps, schema_editor):
    Hackathon = apps.get_model('hackathons', 'Hackathon')
    CheckIn = apps.get_model('hackathons', 'CheckIn')
    CheckInSession = apps.get_model('hackathons', 'CheckInSession')

    has_check_ins = set(CheckIn.objects.values_list('hackathon_id', flat=True))
    for hackathon in Hackathon.objects.all().iterator():
        if not (hackathon.features or {}).get('check_in') and hackathon.id not in has_check_ins:
            continue
        start = hackathon.start_date
        session, _ = CheckInSession.objects.get_or_create(
            hackathon=hackathon, day_index=0, slot=0,  # 0 is the morning slot
            defaults={
                'is_main': True, 'name': 'Check-in', 'needs_arrival': False, 'counts_for_certificate': True,
                'date': timezone.localtime(start).date() if start else timezone.localdate(),
            },
        )
        CheckIn.objects.filter(hackathon=hackathon, session__isnull=True).update(session=session)
    # A check-in an organizer let in despite the team's state keeps saying so.
    CheckIn.objects.exclude(override_reason='').update(via='OVERRIDE')


class Migration(migrations.Migration):

    dependencies = [
        ('hackathons', '0030_check_in_sessions_schema'),
    ]

    operations = [
        migrations.RunPython(create_main_sessions, migrations.RunPython.noop),
    ]
