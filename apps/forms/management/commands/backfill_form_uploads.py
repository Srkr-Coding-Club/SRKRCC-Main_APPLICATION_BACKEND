"""Move old base64 file answers into the upload bucket. Safe to repeat; try --dry-run first. Also on the admin Maintenance page."""

from django.core.management.base import BaseCommand, CommandError

from apps.core import maintenance
from apps.forms import upload_maintenance
from apps.forms.validation import limits


class Command(BaseCommand):
    help = "Upload inline (data:) file answers to the bucket and replace them with references."

    def add_arguments(self, parser):
        parser.add_argument("--form", help="Only this form (slug).")
        parser.add_argument("--limit", type=int, default=0, help="Stop after this many answers (0 = all).")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        if not limits.uploads_enabled():
            raise CommandError("Set FORM_UPLOADS_BACKEND=s3 (and the bucket) first.")
        arguments = dict(form_slug=options["form"], limit=options["limit"], dry_run=options["dry_run"])
        if options["dry_run"]:
            result = upload_maintenance.backfill_inline(**arguments)
        else:
            try:
                with maintenance.recorded("backfill_form_uploads", trigger="command") as run:
                    run.summary = upload_maintenance.backfill_inline(**arguments)
            except maintenance.AlreadyRunning:
                raise CommandError("This task is already running.")
            result = run.summary
        verb = "would move" if options["dry_run"] else "moved"
        self.stdout.write(self.style.SUCCESS(
            f"{result['answers']} answer(s) with inline files: {verb} {result['files_moved']} file(s), skipped {result['files_skipped']}."
        ))
