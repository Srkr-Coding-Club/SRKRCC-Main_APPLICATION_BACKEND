from datetime import timedelta

from django.conf import settings
from django.db import migrations
from django.db.models import F
from django.utils import timezone


def stagger_deadlines(apps, schema_editor):
    """
    Every account that exists when email verification ships must verify (nobody is grandfathered). Locking them all out
    on day one would be unfair and would ask the mail provider for thousands of messages at once, so deadlines come in
    daily waves: the first wave ends EXISTING_START_DAYS after the rollout, then BATCH accounts per day. Members who
    were active most recently come first (they are the likeliest to see the banner and act).
    """
    User = apps.get_model('accounts', 'User')
    start = int(getattr(settings, 'EMAIL_VERIFY_EXISTING_START_DAYS', 7))
    batch = max(1, int(getattr(settings, 'EMAIL_VERIFY_EXISTING_BATCH', 100)))
    now = timezone.now()

    ids = list(
        User.objects.filter(email_verified_at__isnull=True)
        .order_by(F('last_login').desc(nulls_last=True), 'id')
        .values_list('id', flat=True)
    )
    for index, user_id in enumerate(ids):
        User.objects.filter(pk=user_id).update(email_verify_deadline=now + timedelta(days=start + index // batch))


class Migration(migrations.Migration):
    dependencies = [('accounts', '0011_email_verification')]
    operations = [migrations.RunPython(stagger_deadlines, migrations.RunPython.noop)]
