from django.core.management.base import BaseCommand

from apps.celebrations import services


class Command(BaseCommand):
    help = 'Delete celebrations that expired unseen and seen ones older than a month (safe to run daily).'

    def handle(self, *args, **options):
        result = services.sweep()
        self.stdout.write(self.style.SUCCESS(f"Removed {result['expired']} expired and {result['old_seen']} old seen celebration(s)."))
