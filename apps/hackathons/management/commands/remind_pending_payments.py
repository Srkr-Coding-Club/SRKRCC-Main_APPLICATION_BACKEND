from django.core.management.base import BaseCommand

from apps.hackathons import payments


class Command(BaseCommand):
    help = (
        "Tell the organizers about registration payments that have waited more than a day for a check "
        "(one digest per hackathon, repeated daily until handled; safe to run from cron every hour)."
    )

    def handle(self, *args, **options):
        mentioned = payments.remind_organizers()
        self.stdout.write(self.style.SUCCESS(f"{mentioned} payment(s) waiting for a check."))
