from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.services import email_verification


class Command(BaseCommand):
    help = (
        'Remove accounts that never verified and were never used, N days after their deadline. Frees addresses held by '
        'someone who signed up with another person\'s email. Accounts with ANY activity are kept.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=30, help='Days after the deadline (default 30).')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, days, dry_run, **options):
        cutoff = timezone.now() - timedelta(days=days)
        candidates = User.objects.filter(
            email_verified_at__isnull=True, email_verify_deadline__lt=cutoff, is_staff=False, is_superuser=False,
        )
        removed = kept = 0
        for user in candidates.iterator():
            if email_verification.has_activity(user):
                kept += 1
                continue
            removed += 1
            if not dry_run:
                user.delete()
        verb = 'Would remove' if dry_run else 'Removed'
        self.stdout.write(self.style.SUCCESS(f'{verb} {removed} unused unverified account(s); kept {kept} with activity.'))
