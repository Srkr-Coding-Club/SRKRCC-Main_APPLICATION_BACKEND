"""API rate limits, resolved from environment variables.

Each limit can be changed without a code change, for example ``THROTTLE_REGISTER=100/hour`` for a
sign-up event. A blank or missing variable keeps the default below. A value DRF cannot parse is
ignored with a warning, so a typo cannot turn every request into an error.

Variable name: ``THROTTLE_`` plus the scope in capitals (THROTTLE_LOGIN, THROTTLE_FORM_SUBMIT, ...).
"""
import re
import warnings
from collections.abc import Mapping

DEFAULT_RATES = {
    'anon': '100/minute',
    'user': '1000/minute',
    # Credential stuffing / mass sign-up.
    'login': '10/minute',
    'register': '10/hour',
    # Public form submission is anonymous-writable, so it is kept below the global anon rate.
    'form_submit': '20/minute',
    # Hackathon teammate lookup by exact email: limited so it cannot enumerate accounts.
    'hackathon_lookup': '30/minute',
    # Team leaders emailing people with no account yet: it sends mail to arbitrary addresses.
    'hackathon_email_invite': '20/hour',
    # Public college dropdown on the signup page: one request per debounced keystroke burst.
    'college_search': '120/minute',
}

# The test runner shares one client IP, so the sign-in and sign-up limits are lifted for it.
TEST_RATES = {'login': '100000/minute', 'register': '100000/hour'}

# DRF reads "<count>/<period>" and only looks at the first letter of the period.
_RATE_PATTERN = re.compile(r'^\d+/[smhd][a-z]*$')


def resolve_throttle_rates(env: Mapping[str, str], testing: bool = False) -> dict[str, str]:
    rates = dict(DEFAULT_RATES)
    if testing:
        # The test runner ignores THROTTLE_* so a developer's .env cannot change test results.
        rates.update(TEST_RATES)
        return rates

    for scope in DEFAULT_RATES:
        name = f'THROTTLE_{scope.upper()}'
        value = (env.get(name) or '').strip().lower()
        if not value:
            continue
        if _RATE_PATTERN.match(value):
            rates[scope] = value
        else:
            warnings.warn(
                f'{name}={value!r} is not a valid rate (use e.g. 10/minute, 100/hour); '
                f'keeping {rates[scope]}.',
                RuntimeWarning,
                stacklevel=2,
            )
    return rates
