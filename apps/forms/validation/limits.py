"""
Hard ceilings applied regardless of what an admin configures in
``validation_rules``. They exist so no single answer (or form definition) can be
used to exhaust memory, CPU or database space, and so values stay storable in a
PostgreSQL JSON column. Admin-configured rules can only be tighter than these.
"""

from __future__ import annotations

from django.conf import settings

from apps.forms.models import FieldType

# --- Submission values ------------------------------------------------------
MAX_TEXT_LENGTH: dict[str, int] = {
    FieldType.TEXT: 1000,
    FieldType.PARAGRAPH: 10000,
    FieldType.EMAIL: 254,
    FieldType.PHONE: 32,
    FieldType.URL: 2048,
    FieldType.CLUB_ID: 32,
    FieldType.HIDDEN: 500,
    FieldType.RADIO: 500,
    FieldType.DROPDOWN: 500,
}
MAX_ITEM_LENGTH = 500          # a single checkbox selection / matrix key / matrix cell
MAX_COLLECTION_ITEMS = 200     # checkbox selections, matrix rows, files per answer

# --- Numbers ----------------------------------------------------------------
MAX_NUMBER_INPUT_LENGTH = 64
MAX_NUMBER_MAGNITUDE = 30      # |value| < 10**31
MAX_FRACTIONAL_DIGITS = 15     # keeps fractional values exactly representable as float

# --- Dates ------------------------------------------------------------------
DATE_MIN_YEAR = 1900
DATE_MAX_YEARS_AHEAD = 50

# --- Files / signatures (inline data-URL capture) ---------------------------
# Env-tunable (settings.FORM_UPLOAD_MAX_*): 4 MB on Lambda, 10 MB / 25 MB elsewhere.
MAX_INLINE_FILE_BYTES = getattr(settings, "FORM_UPLOAD_MAX_FILE_BYTES", 10 * 1024 * 1024)
MAX_TOTAL_FILE_BYTES = getattr(settings, "FORM_UPLOAD_MAX_TOTAL_BYTES", 25 * 1024 * 1024)  # all files in one answer
_BYTES_PER_MB = 1024 * 1024


def uploads_enabled() -> bool:
    """True when file questions send their files straight to S3 (settings.FORM_UPLOADS_BACKEND == 's3')."""
    return getattr(settings, "FORM_UPLOADS_BACKEND", "inline") == "s3"


def platform_file_limit_bytes() -> int:
    """The most one file may be on this platform: the S3 ceiling, or the inline (base64) limit."""
    if uploads_enabled():
        return int(settings.FORM_UPLOAD_CEILING_MB * _BYTES_PER_MB)
    return MAX_INLINE_FILE_BYTES


def file_limit_bytes(rules: dict | None) -> int:
    """One file's limit for a question: the admin's maxFileSizeMB, else the default; never above the platform limit."""
    try:
        chosen = float((rules or {}).get("maxFileSizeMB"))
    except (TypeError, ValueError):
        chosen = 0
    default = settings.FORM_UPLOAD_DEFAULT_MB if uploads_enabled() else MAX_INLINE_FILE_BYTES / _BYTES_PER_MB
    return int(min((chosen if chosen > 0 else default) * _BYTES_PER_MB, platform_file_limit_bytes()))


MAX_FILE_NAME_LENGTH = 255
MAX_FILE_URL_LENGTH = 2048
DEFAULT_MAX_FILES = 10
MAX_SIGNATURE_BYTES = 512 * 1024

# Never accepted unless the field's ``allowedFileTypes`` lists the extension
# explicitly: executables and types a browser would render as active content.
DEFAULT_BLOCKED_EXTENSIONS = frozenset({
    ".exe", ".msi", ".bat", ".cmd", ".com", ".scr", ".dll", ".vbs", ".ps1",
    ".html", ".htm", ".xhtml", ".svg",
})
BLOCKED_MIME_TYPES = frozenset({
    "text/html", "application/xhtml+xml", "image/svg+xml",
    "application/x-msdownload", "application/x-msdos-program",
})

# --- Form definitions -------------------------------------------------------
MAX_FIELDS_PER_FORM = 200
MAX_CONDITION_DEPTH = 8        # groups inside groups in one question's condition
MAX_OPTIONS_PER_FIELD = 200
MAX_OPTION_LENGTH = 255
MAX_MATRIX_ROWS = 100
MAX_SCALE_SPAN = 100
MAX_REGEX_LENGTH = 500
