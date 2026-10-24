from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import User
from apps.accounts.services import email_verification


class Command(BaseCommand):
    help = (
        'Break-glass: mark one account\'s email as verified without the emailed link. Use it when someone cannot receive '
        'mail (or every admin is blocked and email is down). The action is written to the audit log.'
    )

    def add_arguments(self, parser):
        parser.add_argument('email', help='The account\'s email address.')

    def handle(self, *args, email, **options):
        user = User.objects.filter(email__iexact=email.strip()).first()
        if user is None:
            raise CommandError(f'No account uses {email}.')
        changed = email_verification.mark_verified(user, via='management-command')
        self.stdout.write(self.style.SUCCESS(f'{user.email}: {"marked verified" if changed else "was already verified"}.'))
