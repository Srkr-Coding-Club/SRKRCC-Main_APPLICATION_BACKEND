# Data-cleanup migration inserted ahead of 0006_roll_number_unique (renamed
# from 0005_roll_number_unique — see that file's own history/comment) after
# a production deploy failed applying the unique=True constraint:
#
#   psycopg2.errors.UniqueViolation: could not create unique index
#   "accounts_user_roll_number_d4c665ff_uniq"
#   DETAIL:  Key (roll_number)=(xyz) is duplicated.
#
# Confirmed via the failing deploy's own traceback that this migration had
# never successfully applied anywhere but the dev database this branch was
# authored against (Django wraps each Postgres migration in its own
# transaction, so the failed attempt left production at "0004 applied, no
# further" — nothing to repair after the fact, just a graph to fix going
# forward).
#
# Policy (confirmed with the project owner, who has no way to inspect
# production data live from this session either — this is the safest
# interpretation available without that access):
#   1. Any roll_number that isn't exactly 10 alphanumeric characters (the
#      format apps/accounts/validators.py's ROLL_NUMBER_REGEX enforces for
#      every NEW signup) is almost certainly a placeholder/"unknown" value
#      from legacy data entry, not a real roll number — e.g. "xyz". Null
#      those out unconditionally, whether or not they're currently
#      duplicated, rather than guessing which one holder of a shared
#      placeholder value should keep it.
#   2. Safety net: if any *format-valid* value still collides after step 1
#      (an actual, unanticipated genuine duplicate the failing deploy's log
#      didn't reveal — Postgres only reports the first violation it hits),
#      keep the lowest-id row's value and null the rest, so this migration
#      is guaranteed to leave zero duplicates for 0006's unique index to
#      choke on, however messy the real data turns out to be.
#
# Irreversible by nature (nulling a value can't be undone without the
# original data) — the reverse is intentionally a no-op.
from django.db import migrations


def clear_placeholder_and_duplicate_roll_numbers(apps, schema_editor):
    import re

    User = apps.get_model('accounts', 'User')
    ROLL_NUMBER_RE = re.compile(r'^[A-Za-z0-9]{10}$')

    candidates = User.objects.exclude(roll_number__isnull=True).exclude(roll_number='')

    placeholder_ids = [u.id for u in candidates if not ROLL_NUMBER_RE.match(u.roll_number)]
    if placeholder_ids:
        User.objects.filter(id__in=placeholder_ids).update(roll_number=None)
        print(f'[0005_cleanup_duplicate_roll_numbers] Cleared {len(placeholder_ids)} '
              f'non-10-char-alphanumeric roll_number value(s) (placeholder/legacy data).')

    remaining = (
        User.objects.exclude(roll_number__isnull=True).exclude(roll_number='')
        .order_by('id').values_list('id', 'roll_number')
    )
    seen = {}
    duplicate_ids = []
    for user_id, roll in remaining:
        if roll in seen:
            duplicate_ids.append(user_id)
        else:
            seen[roll] = user_id
    if duplicate_ids:
        User.objects.filter(id__in=duplicate_ids).update(roll_number=None)
        print(f'[0005_cleanup_duplicate_roll_numbers] Cleared {len(duplicate_ids)} additional '
              f'value(s) that were still duplicated after the placeholder pass (kept the '
              f'earliest account in each group).')


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0004_user_password_status_passwordsetuptoken'),
    ]

    operations = [
        migrations.RunPython(clear_placeholder_and_duplicate_roll_numbers, reverse_code=migrations.RunPython.noop),
    ]
