"""Read malware-scan verdicts for form uploads (FORM_UPLOAD_SCAN=guardduty). Also on the admin Maintenance page."""

from django.core.management.base import BaseCommand, CommandError

from apps.core import maintenance
from apps.forms import upload_maintenance


class Command(BaseCommand):
    help = "Mark scanned form files clean, and remove any that GuardDuty found malware in."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=3, help="Only files uploaded in the last N days.")
        parser.add_argument("--batch", type=int, default=500)

    def handle(self, *args, **options):
        if not upload_maintenance.scanning_enabled():
            raise CommandError("FORM_UPLOAD_SCAN is off.")
        try:
            with maintenance.recorded("scan_form_uploads", trigger="command") as run:
                run.summary = upload_maintenance.scan_pending(days=options["days"], batch=options["batch"])
        except maintenance.AlreadyRunning:
            raise CommandError("This task is already running.")
        result = run.summary
        self.stdout.write(self.style.SUCCESS(f"{result['clean']} clean, {result['infected']} infected (removed), {result['waiting']} still waiting."))
