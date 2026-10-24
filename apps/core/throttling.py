"""DRF throttles keyed on apps.core.client_ip instead of DRF's spoofable X-Forwarded-For handling.

Two things matter for a campus site, where hundreds of students share a few public addresses:

* Signed-in people are counted per account, never per address. Only anonymous traffic is counted per
  address, so those limits are set generously (see ``config/throttle_config.py``).
* A sign-in is also counted per account *and* address, so one person mistyping a password a few times is
  stopped quickly without locking the whole lab out, and without letting a stranger lock someone else out.
"""
import hashlib

from rest_framework import throttling

from apps.core.client_ip import get_client_ip


class ClientIPMixin:
    def get_ident(self, request):
        return get_client_ip(request) or 'unknown'


class AnonRateThrottle(ClientIPMixin, throttling.AnonRateThrottle):
    pass


class UserRateThrottle(ClientIPMixin, throttling.UserRateThrottle):
    pass


class ScopedRateThrottle(ClientIPMixin, throttling.ScopedRateThrottle):
    pass


class AuthAwareScopedThrottle(ScopedRateThrottle):
    """Like ScopedRateThrottle, but an anonymous caller is counted against ``<scope>_anon`` when that rate exists.

    The signed-in rate is per account; the anonymous one is per address and can be set differently (usually higher,
    because many people share one address).
    """

    def allow_request(self, request, view):
        scope = getattr(view, 'throttle_scope', None)
        if not scope:
            return True
        user = getattr(request, 'user', None)
        if not (user and user.is_authenticated) and f'{scope}_anon' in self.THROTTLE_RATES:
            scope = f'{scope}_anon'
        self.scope = scope
        self.rate = self.get_rate()
        self.num_requests, self.duration = self.parse_rate(self.rate)
        return super(throttling.ScopedRateThrottle, self).allow_request(request, view)


class LoginAttemptThrottle(ClientIPMixin, throttling.SimpleRateThrottle):
    """Sign-in attempts per (account, address). Scope ``login_account``."""

    scope = 'login_account'

    def get_cache_key(self, request, view):
        data = getattr(request, 'data', None)
        email = data.get('email') if hasattr(data, 'get') else None
        if not isinstance(email, str) or not email.strip():
            return None  # nothing to count; the address-wide limit still applies
        who = hashlib.sha256(f'{email.strip().lower()}|{self.get_ident(request)}'.encode()).hexdigest()
        return self.cache_format % {'scope': self.scope, 'ident': who}
