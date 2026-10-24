from django.core.management.base import BaseCommand

from apps.accounts.models import User
from apps.core.permissions import _is_admin_or_club_lead

# Addresses that cannot receive mail. A person with one of these can never verify, so fix them BEFORE switching the
# block on (especially admins and club leads: nobody is exempt).
RESERVED_DOMAINS = {'example.com', 'example.org', 'example.net', 'test.com', 'localhost', 'invalid.com'}
RESERVED_SUFFIXES = ('.local', '.localhost', '.test', '.invalid', '.example', '.internal')


def looks_undeliverable(address: str) -> str:
    local, _, domain = (address or '').lower().partition('@')
    if not local or not domain:
        return 'not an email address'
    if domain in RESERVED_DOMAINS or domain.endswith(RESERVED_SUFFIXES):
        return f'reserved domain ({domain})'
    if '.' not in domain:
        return f'no dot in the domain ({domain})'
    return ''


class Command(BaseCommand):
    help = 'List unverified accounts whose email address cannot receive mail (reserved or malformed). Run before enabling the block.'

    def add_arguments(self, parser):
        parser.add_argument('--staff-only', action='store_true', help='Only admins, club leads and staff.')

    def handle(self, *args, staff_only=False, **options):
        found = 0
        for user in User.objects.filter(email_verified_at__isnull=True, is_active=True).order_by('id'):
            staff = _is_admin_or_club_lead(user)
            if staff_only and not staff:
                continue
            reason = looks_undeliverable(user.email)
            if reason:
                found += 1
                self.stdout.write(f'{"[STAFF] " if staff else ""}id={user.pk} {user.email} - {reason}')
        style = self.style.WARNING if found else self.style.SUCCESS
        self.stdout.write(style(f'{found} account(s) cannot receive verification mail.' if found else 'No undeliverable addresses found.'))
