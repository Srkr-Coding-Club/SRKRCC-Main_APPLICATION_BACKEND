from django.core.management.base import BaseCommand

from apps.events import reminders


class Command(BaseCommand):
    help = (
        "Remind registered members a day and an hour before an event starts, in the notification bell and by email "
        "(safe to run from cron every hour; each reminder is sent once per person)."
    )

    def handle(self, *args, **options):
        sent = reminders.send_due()
        self.stdout.write(self.style.SUCCESS(f"Sent {sent['DAY']} day-before and {sent['HOUR']} hour-before reminder(s)."))
