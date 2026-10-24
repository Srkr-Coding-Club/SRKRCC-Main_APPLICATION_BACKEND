from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.accounts.models import User


class Command(BaseCommand):
    help = (
        'Give unverified accounts more time before the block (for example while email delivery is down). '
        'Adds N days to each deadline, or to now for deadlines already passed.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, required=True)
        parser.add_argument('--only-blocked', action='store_true', help='Only accounts whose deadline has already passed.')

    def handle(self, *args, days, only_blocked=False, **options):
        if days < 1 or days > 365:
            raise CommandError('--days must be between 1 and 365.')
        now = timezone.now()
        qs = User.objects.filter(email_verified_at__isnull=True, is_active=True)
        if only_blocked:
            qs = qs.filter(email_verify_deadline__lte=now)
        count = 0
        for user in qs.iterator():
            base = max(user.email_verify_deadline or now, now)
            User.objects.filter(pk=user.pk).update(email_verify_deadline=base + timedelta(days=days))
            count += 1
        self.stdout.write(self.style.SUCCESS(f'Extended {count} account(s) by {days} day(s).'))
