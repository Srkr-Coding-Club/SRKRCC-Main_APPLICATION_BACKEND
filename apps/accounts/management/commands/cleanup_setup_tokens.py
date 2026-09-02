from django.core.management.base import BaseCommand
from apps.accounts.services.password_setup_service import PasswordSetupService


class Command(BaseCommand):
    help = "Cleans up expired and used password setup tokens older than the specified retention window."

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=30,
            help="Retention period in days (default: 30 days).",
        )

    def handle(self, *args, **options):
        days = options.get('days', 30)
        deleted = PasswordSetupService.cleanup_setup_tokens(retention_days=days)
        self.stdout.write(self.style.SUCCESS(f"Successfully cleaned up {deleted} password setup token(s)."))
