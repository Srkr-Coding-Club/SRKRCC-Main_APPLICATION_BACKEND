"""
Single source of truth for account field rules.

These are mirrored verbatim by the frontend in
`src/lib/validation/auth.ts` — if a rule changes here, change it there too,
otherwise the signup form accepts input the API then rejects.
"""
import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

# Letters (incl. accented), spaces, hyphens, apostrophes and periods only.
# Explicitly excludes digits — the signup form previously accepted "John123".
NAME_REGEX = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿ]+(?:[ '\-.][A-Za-zÀ-ÖØ-öø-ÿ]+)*$")
NAME_MIN_LENGTH = 2
NAME_MAX_LENGTH = 60

# Exactly 10 alphanumeric characters, stored uppercase (e.g. 21B91A0501).
ROLL_NUMBER_REGEX = re.compile(r"^[A-Z0-9]{10}$")
ROLL_NUMBER_LENGTH = 10

# Digits only, exactly 10 characters (e.g. 9876543210) — no country code,
# spaces, or symbols.
PHONE_NUMBER_REGEX = re.compile(r"^\d{10}$")
PHONE_NUMBER_LENGTH = 10

# Pragmatic e-mail shape check layered on top of Django's EmailValidator:
# rejects consecutive/leading/trailing dots and demands a >=2 char TLD.
EMAIL_REGEX = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
    r"(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,}$"
)
EMAIL_MAX_LENGTH = 254

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 72  # bcrypt/argon practical ceiling; also blocks DoS-by-long-password


def normalize_email(value: str) -> str:
    """Lowercases and trims an email so uniqueness is case-insensitive."""
    return (value or "").strip().lower()


def normalize_name(value: str) -> str:
    """Trims and collapses internal whitespace runs in a person's name."""
    return re.sub(r"\s+", " ", (value or "").strip())


def normalize_roll_number(value: str) -> str:
    """Uppercases and strips all whitespace from a roll number."""
    return re.sub(r"\s+", "", (value or "")).upper()


def normalize_phone_number(value: str) -> str:
    """Strips everything but digits from a phone number."""
    return re.sub(r"\D", "", value or "")


class ComplexPasswordValidator:
    """
    Requires a mix of character classes.

    Registered in AUTH_PASSWORD_VALIDATORS so it applies to every path that
    sets a password — self-registration, the one-time password-setup link,
    and `manage.py changepassword` — not just the signup serializer.
    """

    def validate(self, password, user=None):
        errors = []
        if len(password or "") < PASSWORD_MIN_LENGTH:
            errors.append(_("Password must be at least %d characters long.") % PASSWORD_MIN_LENGTH)
        if len(password or "") > PASSWORD_MAX_LENGTH:
            errors.append(_("Password must be at most %d characters long.") % PASSWORD_MAX_LENGTH)
        if not re.search(r"[a-z]", password or ""):
            errors.append(_("Password must contain at least one lowercase letter."))
        if not re.search(r"[A-Z]", password or ""):
            errors.append(_("Password must contain at least one uppercase letter."))
        if not re.search(r"\d", password or ""):
            errors.append(_("Password must contain at least one number."))
        if not re.search(r"[^A-Za-z0-9]", password or ""):
            errors.append(_("Password must contain at least one special character."))
        if re.search(r"\s", password or ""):
            errors.append(_("Password must not contain spaces."))
        if errors:
            raise ValidationError(errors, code="password_not_complex")

    def get_help_text(self):
        return _(
            "Your password must be at least 8 characters and include an uppercase letter, "
            "a lowercase letter, a number, and a special character."
        )
