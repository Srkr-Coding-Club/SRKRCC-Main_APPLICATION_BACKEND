import os
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from apps.accounts.models import User, UserRole, MembershipStatus, PasswordStatus


class Command(BaseCommand):
    help = 'Idempotently creates or updates the default Super Admin user for production and development.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--email',
            type=str,
            default=os.getenv('DJANGO_SUPERUSER_EMAIL', 'admin@srkr.ac.in'),
            help='Super Admin email address (defaults to DJANGO_SUPERUSER_EMAIL or admin@srkr.ac.in)'
        )
        parser.add_argument(
            '--password',
            type=str,
            default=None,
            help='Super Admin password (defaults to DJANGO_SUPERUSER_PASSWORD; there is no default outside DEBUG)'
        )
        parser.add_argument(
            '--username',
            type=str,
            default=os.getenv('DJANGO_SUPERUSER_USERNAME', 'admin'),
            help='Super Admin username (defaults to DJANGO_SUPERUSER_USERNAME or admin)'
        )
        parser.add_argument(
            '--first-name',
            type=str,
            default=os.getenv('DJANGO_SUPERUSER_FIRST_NAME', 'System'),
            help='Super Admin first name'
        )
        parser.add_argument(
            '--last-name',
            type=str,
            default=os.getenv('DJANGO_SUPERUSER_LAST_NAME', 'Admin'),
            help='Super Admin last name'
        )
        parser.add_argument(
            '--reset-password',
            action='store_true',
            help='Force password reset if user already exists'
        )

    DEV_FALLBACK_PASSWORD = 'Admin@123'
    KNOWN_WEAK_PASSWORDS = {'admin@123', 'admin123', 'password', 'password123', 'changeme', 'letmein', 'admin'}

    def _resolve_password(self, from_flag):
        password = from_flag or os.getenv('DJANGO_SUPERUSER_PASSWORD')
        if not password:
            if settings.DEBUG:
                self.stderr.write(self.style.WARNING(
                    'No DJANGO_SUPERUSER_PASSWORD set: using the development password. Never do this in production.'
                ))
                return self.DEV_FALLBACK_PASSWORD
            raise CommandError(
                'DJANGO_SUPERUSER_PASSWORD is not set. There is no default admin password outside DEBUG: '
                'set a strong one in the environment (or pass --password).'
            )
        if not settings.DEBUG and password.lower() in self.KNOWN_WEAK_PASSWORDS:
            raise CommandError('That admin password is on the list of well-known passwords. Choose another.')
        return password

    def handle(self, *args, **options):
        email = options['email'].strip().lower()
        username = options['username'].strip()
        first_name = options['first_name'].strip()
        last_name = options['last_name'].strip()
        force_reset = options['reset_password']

        # The password is only used when the admin is created or reset, so it is only checked
        # then. An existing admin must not fail a redeploy over an unused environment value.
        password = None
        if force_reset or not User.objects.filter(email=email).exists():
            password = self._resolve_password(options['password'])

        with transaction.atomic():
            user, created = User.objects.get_or_create(
                email=email,
                defaults={
                    'username': username,
                    'first_name': first_name,
                    'last_name': last_name,
                    'role': UserRole.ADMIN,
                    'is_staff': True,
                    'is_superuser': True,
                    'membership_status': MembershipStatus.ACTIVE,
                    'password_status': PasswordStatus.ACTIVE,
                    'branch': 'CSE',
                    'year': 4,
                    'roll_number': 'ADMIN-001',
                    'created_from': 'ADMIN',
                }
            )

            if created:
                # `password` is already set unless another process created the admin between
                # the existence check above and get_or_create.
                if password is None:
                    password = self._resolve_password(options['password'])
                user.set_password(password)
                user.save()
                self.stdout.write(
                    self.style.SUCCESS(
                        f" Successfully created Super Admin: {user.email} (Username: {user.username})"
                    )
                )
            else:
                # Ensure privileges are active
                user.role = UserRole.ADMIN
                user.is_staff = True
                user.is_superuser = True
                user.membership_status = MembershipStatus.ACTIVE
                user.password_status = PasswordStatus.ACTIVE
                # An existing admin's password is only ever replaced on request, so a changed
                # password survives redeploys.
                if force_reset:
                    user.set_password(password)
                user.save()
                self.stdout.write(
                    self.style.SUCCESS(
                        f" Super Admin already exists: {user.email}. Privileges verified."
                    )
                )
