from django.core.management.base import BaseCommand

from apps.accounts.services import email_verification


class Command(BaseCommand):
    help = (
        'Re-plan deadlines for every unverified account in daily waves (most recently active first). Run it when you are '
        'about to announce the block, so existing members get a fresh, staggered window instead of whatever the '
        'rollout migration set.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--start-days', type=int, default=None, help='Days until the first wave (default EMAIL_VERIFY_EXISTING_START_DAYS).')
        parser.add_argument('--batch', type=int, default=None, help='Accounts per day (default EMAIL_VERIFY_EXISTING_BATCH).')
        parser.add_argument('--only-unscheduled', action='store_true', help='Leave accounts that already have a deadline alone.')

    def handle(self, *args, start_days, batch, only_unscheduled, **options):
        count = email_verification.schedule_deadlines(start_days=start_days, batch=batch, only_unscheduled=only_unscheduled)
        self.stdout.write(self.style.SUCCESS(f'Scheduled deadlines for {count} account(s).'))
