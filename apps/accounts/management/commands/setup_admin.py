import os
from django.core.management.base import BaseCommand
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
            default=os.getenv('DJANGO_SUPERUSER_PASSWORD', 'Admin@123'),
            help='Super Admin password (defaults to DJANGO_SUPERUSER_PASSWORD or Admin@123)'
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

    def handle(self, *args, **options):
        email = options['email'].strip().lower()
        password = options['password']
        username = options['username'].strip()
        first_name = options['first_name'].strip()
        last_name = options['last_name'].strip()
        force_reset = options['reset_password']

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
                if force_reset or options.get('password') != 'Admin@123':
                    user.set_password(password)
                user.save()
                self.stdout.write(
                    self.style.SUCCESS(
                        f" Super Admin already exists: {user.email}. Privileges verified."
                    )
                )
