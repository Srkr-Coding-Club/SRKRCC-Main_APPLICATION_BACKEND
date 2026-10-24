"""API error handling: DRF's default, with a friendlier, machine-readable answer for a throttled request."""
import math

from rest_framework.exceptions import Throttled
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler


def api_exception_handler(exc, context):
    from apps.core.verification_gate import EmailVerificationRequired
    if isinstance(exc, EmailVerificationRequired):
        return Response(exc.payload, status=403)
    response = drf_exception_handler(exc, context)
    if response is not None and isinstance(exc, Throttled):
        wait = max(1, math.ceil(exc.wait)) if exc.wait else 1
        response.data = {
            'detail': f'You are going a little too fast. Please wait {wait} second{"s" if wait != 1 else ""} and try again.',
            'code': 'THROTTLED',
            'retry_after': wait,
        }
    return response
