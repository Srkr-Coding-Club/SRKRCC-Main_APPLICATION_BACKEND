"""DRF throttles keyed on apps.core.client_ip instead of DRF's spoofable X-Forwarded-For handling."""
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
