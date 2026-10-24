"""Delete abandoned response drafts. Run daily; drafts older than the retention period are never offered again anyway."""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.forms.drafts import DRAFT_TTL_DAYS
from apps.forms.models import ResponseDraft


class Command(BaseCommand):
    help = "Delete response drafts that have not been touched for --days days."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=DRAFT_TTL_DAYS)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        stale = ResponseDraft.objects.filter(updated_at__lt=timezone.now() - timedelta(days=options["days"]))
        count = stale.count()
        if not options["dry_run"]:
            stale.delete()
        self.stdout.write(self.style.SUCCESS(f"{count} stale draft(s) {'found' if options['dry_run'] else 'deleted'}."))
