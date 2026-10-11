from django.db import migrations


def seed_badges(apps, schema_editor):
    """Seed the badge catalog idempotently. Kept in a data migration so the
    definitions exist immediately after deploy; admins can later tune
    thresholds from the Django admin without a code change."""
    from apps.codequest.gamification import DEFAULT_BADGES, get_gamification_config

    BadgeDefinition = apps.get_model('codequest', 'BadgeDefinition')
    config = get_gamification_config()
    for spec in DEFAULT_BADGES:
        threshold = spec.get('threshold', config.get(spec.get('threshold_key'), 1))
        BadgeDefinition.objects.update_or_create(
            code=spec['code'],
            defaults={
                'name': spec['name'],
                'description': spec['description'].format(threshold=threshold),
                'category': spec['category'],
                'metric': spec['metric'],
                'threshold': threshold,
                'icon': spec.get('icon', ''),
            },
        )


def unseed_badges(apps, schema_editor):
    # Definitions may already have UserBadge rows referencing them; deleting
    # on reverse would cascade earned achievements, so leave them intact.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('codequest', '0004_badgedefinition_codingstreak_userbadge_xpreward_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_badges, unseed_badges),
    ]
