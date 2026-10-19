"""Refuse to run development-only tooling (demo seeders with known passwords) in production."""
from django.conf import settings

MESSAGE = (
    '{what} creates accounts with well-known passwords and only runs with DEBUG=True. '
    'Refusing to run against a production configuration.'
)


class DevelopmentOnlyError(RuntimeError):
    pass


def require_development(what: str) -> None:
    if not settings.DEBUG:
        raise DevelopmentOnlyError(MESSAGE.format(what=what))
