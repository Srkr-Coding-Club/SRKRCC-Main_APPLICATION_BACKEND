"""Remove form uploads nobody attached in time. Also on the admin Maintenance page; the bucket lifecycle rule is the safety net."""

from django.core.management.base import BaseCommand, CommandError

from apps.core import maintenance
from apps.forms import upload_maintenance


class Command(BaseCommand):
    help = "Delete expired pending/uploaded form files and forget old finished upload rows."

    def add_arguments(self, parser):
        parser.add_argument("--keep-rows-days", type=int, default=30)
        parser.add_argument("--batch", type=int, default=500)

    def handle(self, *args, **options):
        try:
            with maintenance.recorded("sweep_form_uploads", trigger="command") as run:
                run.summary = upload_maintenance.sweep_expired(batch=options["batch"], keep_rows_days=options["keep_rows_days"])
        except maintenance.AlreadyRunning:
            raise CommandError("This task is already running.")
        result = run.summary
        self.stdout.write(self.style.SUCCESS(f"{result['expired']} expired upload(s) removed, {result['rows_forgotten']} old row(s) forgotten."))
