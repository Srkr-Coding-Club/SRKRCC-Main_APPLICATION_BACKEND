from django.db import migrations


def backfill_problem_domains(apps, schema_editor):
    """Seed each hackathon's domain list from the domains its statements already use (first-seen spelling wins)."""
    Hackathon = apps.get_model('hackathons', 'Hackathon')
    ProblemStatement = apps.get_model('hackathons', 'ProblemStatement')
    for hackathon in Hackathon.objects.all():
        domains = []
        seen = set()
        for domain in ProblemStatement.objects.filter(hackathon=hackathon).order_by('order', 'id').values_list('domain', flat=True):
            name = ' '.join((domain or '').split())
            if name and name.lower() not in seen:
                seen.add(name.lower())
                domains.append(name)
        if domains:
            hackathon.problem_domains = domains[:30]
            hackathon.save(update_fields=['problem_domains'])


class Migration(migrations.Migration):

    dependencies = [('hackathons', '0015_problem_domains')]

    operations = [migrations.RunPython(backfill_problem_domains, migrations.RunPython.noop)]
