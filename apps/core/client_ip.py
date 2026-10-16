"""One definition of "the client's IP", used by every throttle and rate limiter.

Two sources, strongest first:

1. The Next.js BFF proxy, which is the real HTTP client of this API and would otherwise make every
   visitor share one address. It sends ``X-Client-IP`` together with ``X-BFF-Secret``; the IP is only
   believed when the secret matches ``settings.CLIENT_IP_SHARED_SECRET``.
2. Otherwise ``X-Forwarded-For`` counted from the RIGHT by ``settings.TRUSTED_PROXY_COUNT`` hops (the
   proxies we run append the address they saw; everything to the left is client-controlled).
"""
import hmac
import ipaddress

from django.conf import settings

SECRET_HEADER = 'HTTP_X_BFF_SECRET'
IP_HEADER = 'HTTP_X_CLIENT_IP'


def _valid_ip(value: str) -> str | None:
    try:
        return str(ipaddress.ip_address((value or '').strip()))
    except ValueError:
        return None


def get_client_ip(request) -> str | None:
    meta = request.META
    secret = getattr(settings, 'CLIENT_IP_SHARED_SECRET', '')
    if secret and hmac.compare_digest(meta.get(SECRET_HEADER, '').encode(), secret.encode()):
        forwarded = _valid_ip(meta.get(IP_HEADER, ''))
        if forwarded:
            return forwarded

    remote = meta.get('REMOTE_ADDR')
    hops = getattr(settings, 'TRUSTED_PROXY_COUNT', 1)
    forwarded_for = meta.get('HTTP_X_FORWARDED_FOR', '')
    if not hops or not forwarded_for:
        return remote
    addresses = [part.strip() for part in forwarded_for.split(',') if part.strip()]
    if not addresses:
        return remote
    return _valid_ip(addresses[-min(hops, len(addresses))]) or remote
