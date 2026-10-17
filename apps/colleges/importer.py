import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .models import College, build_search_name

BATCH_SIZE = 2000
CLEAN_COLUMNS = ['code', 'name', 'university', 'college_type', 'state', 'district']
SOURCE_ENCODINGS = ('utf-8-sig', 'cp1252')

_ID_SUFFIX = re.compile(r'\s*\(Id:\s*([A-Z]-\d+)\)\s*$')


@dataclass(frozen=True)
class ParsedCollege:
    code: str
    name: str
    university: str
    college_type: str
    state: str
    district: str


@dataclass
class ImportReport:
    parsed: int = 0
    skipped: int = 0
    created: int = 0
    updated: int = 0


def _squash(value: str) -> str:
    return ' '.join((value or '').split())


def _split_id(value: str):
    squashed = _squash(value)
    match = _ID_SUFFIX.search(squashed)
    if not match:
        return squashed, ''
    return squashed[:match.start()], match.group(1)


def _parse_row(row: dict):
    if 'College Name' in row:
        name, code = _split_id(row['College Name'])
        university, _ = _split_id(row.get('University Name', ''))
        college_type = row.get('College Type', '')
        state, district = row.get('State Name', ''), row.get('District Name', '')
    else:
        name, code = _squash(row.get('name', '')), _squash(row.get('code', ''))
        university = row.get('university', '')
        college_type = row.get('college_type', '')
        state, district = row.get('state', ''), row.get('district', '')
    if not (code and name):
        return None
    return ParsedCollege(code, name, _squash(university), _squash(college_type), _squash(state), _squash(district))


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in SOURCE_ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"{path} is not valid UTF-8 or Windows-1252 text.")


def read_colleges(path: Path, report: ImportReport) -> Iterator[ParsedCollege]:
    """Accepts either the raw national catalogue export or the cleaned bundled file."""
    seen = set()
    for row in csv.DictReader(_read_text(path).splitlines(keepends=True)):
        parsed = _parse_row(row)
        if parsed is None or parsed.code in seen:
            report.skipped += 1
            continue
        seen.add(parsed.code)
        report.parsed += 1
        yield parsed


def write_clean_csv(source: Path, destination: Path) -> ImportReport:
    report = ImportReport()
    with destination.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(CLEAN_COLUMNS)
        for item in read_colleges(source, report):
            writer.writerow([item.code, item.name, item.university, item.college_type, item.state, item.district])
    return report


def _to_model(item: ParsedCollege) -> College:
    return College(
        code=item.code,
        name=item.name,
        search_name=build_search_name(item.code, item.name),
        university=item.university,
        college_type=item.college_type,
        state=item.state,
        district=item.district,
    )


def _flush(batch, report: ImportReport):
    codes = [college.code for college in batch]
    existing = College.objects.filter(code__in=codes).count()
    College.objects.bulk_create(
        batch,
        update_conflicts=True,
        unique_fields=['code'],
        update_fields=['name', 'search_name', 'university', 'college_type', 'state', 'district'],
    )
    report.updated += existing
    report.created += len(batch) - existing


def import_colleges(path: Path, dry_run: bool = False) -> ImportReport:
    report = ImportReport()
    batch = []
    for item in read_colleges(path, report):
        if dry_run:
            continue
        batch.append(_to_model(item))
        if len(batch) >= BATCH_SIZE:
            _flush(batch, report)
            batch = []
    if batch:
        _flush(batch, report)
    return report
