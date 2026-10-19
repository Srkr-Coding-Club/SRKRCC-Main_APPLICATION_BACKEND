from django.core.management.base import BaseCommand

from apps.hackathons.services import AnnouncementService


class Command(BaseCommand):
    help = "Email hackathon announcements whose publish time has arrived (safe to run from cron; each is sent once)."

    def handle(self, *args, **options):
        sent = AnnouncementService.send_due_emails()
        self.stdout.write(self.style.SUCCESS(f"Dispatched emails to {sent} recipient(s)."))
