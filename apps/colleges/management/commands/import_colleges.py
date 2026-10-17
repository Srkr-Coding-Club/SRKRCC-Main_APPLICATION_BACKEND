from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.colleges.importer import import_colleges, write_clean_csv

BUNDLED_CATALOGUE = Path(__file__).resolve().parents[2] / 'data' / 'colleges.csv'


class Command(BaseCommand):
    help = (
        "Loads the college catalogue (idempotent upsert keyed on the catalogue code). "
        "Defaults to the bundled apps/colleges/data/colleges.csv; --file also accepts the raw national export."
    )

    def add_arguments(self, parser):
        parser.add_argument('--file', type=Path, default=BUNDLED_CATALOGUE)
        parser.add_argument('--dry-run', action='store_true', help="Parse and report without writing.")
        parser.add_argument(
            '--write-clean', type=Path, metavar='PATH',
            help="Convert --file into the cleaned UTF-8 catalogue at PATH and exit without touching the database.",
        )

    def handle(self, *args, **options):
        source = options['file']
        if not source.is_file():
            raise CommandError(f"Catalogue file not found: {source}")

        if options['write_clean']:
            report = write_clean_csv(source, options['write_clean'])
            self.stdout.write(self.style.SUCCESS(f"Wrote {report.parsed} colleges ({report.skipped} skipped)."))
            return

        report = import_colleges(source, dry_run=options['dry_run'])
        verb = 'Would import' if options['dry_run'] else 'Imported'
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {report.parsed} colleges: {report.created} created, {report.updated} updated, {report.skipped} skipped."
        ))
