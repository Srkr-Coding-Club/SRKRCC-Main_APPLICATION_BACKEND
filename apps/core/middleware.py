from django.conf import settings

# Always in ALLOWED_HOSTS: config/security_config.py adds the loopback names in every mode.
SAFE_HEALTH_HOST = 'localhost'


class HealthCheckHostMiddleware:
    """Answers the platform's health check whatever ``Host`` header it sends.

    Load balancers often probe the server by its private IP, which ``ALLOWED_HOSTS`` rightly does
    not list, so Django would answer 400 and the platform would never mark the deploy healthy.
    For the paths in ``settings.HEALTH_CHECK_PATHS`` (GET or HEAD only) the Host is replaced with a
    host that is always allowed. Every other request keeps the strict host check.

    Must run before any middleware that reads the host, so it sits first in ``MIDDLEWARE``.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.paths = frozenset(getattr(settings, 'HEALTH_CHECK_PATHS', ()))

    def __call__(self, request):
        if request.method in ('GET', 'HEAD') and request.path_info in self.paths:
            request.META['HTTP_HOST'] = SAFE_HEALTH_HOST
            request.META.pop('HTTP_X_FORWARDED_HOST', None)
        return self.get_response(request)
