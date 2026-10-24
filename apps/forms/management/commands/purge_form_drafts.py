"""Delete abandoned response drafts. Also available (with a record of the run) on the admin Maintenance page."""

from django.core.management.base import BaseCommand, CommandError

from apps.core import maintenance
from apps.forms import drafts
from apps.forms.drafts import DRAFT_TTL_DAYS


class Command(BaseCommand):
    help = "Delete response drafts that have not been touched for --days days."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=DRAFT_TTL_DAYS)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        if options["dry_run"]:
            count = drafts.stale_drafts(options["days"]).count()
            self.stdout.write(self.style.SUCCESS(f"{count} stale draft(s) found."))
            return
        try:
            with maintenance.recorded("purge_form_drafts", trigger="command") as run:
                run.summary = {"removed": drafts.purge_stale(options["days"])}
        except maintenance.AlreadyRunning:
            raise CommandError("This task is already running.")
        self.stdout.write(self.style.SUCCESS(f"{run.summary['removed']} stale draft(s) deleted."))
