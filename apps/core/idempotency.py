import hashlib
import json
from rest_framework.response import Response as DRFResponse
from .models import IdempotencyRecord

def get_idempotency_key(request):
    """Extracts idempotency key from HTTP headers or request payload."""
    key = (
        request.headers.get('Idempotency-Key')
        or request.headers.get('X-Idempotency-Key')
        or request.META.get('HTTP_IDEMPOTENCY_KEY')
        or request.META.get('HTTP_X_IDEMPOTENCY_KEY')
    )
    if not key and isinstance(request.data, dict):
        key = request.data.get('idempotency_key')
    return str(key).strip() if key else None

def compute_request_hash(request):
    """Computes a deterministic SHA256 hash of the request path and payload."""
    data_str = ""
    if hasattr(request, 'data') and request.data:
        try:
            data_str = json.dumps(request.data, sort_keys=True, default=str)
        except Exception:
            data_str = str(request.data)
    hash_input = f"{request.path}:{data_str}"
    return hashlib.sha256(hash_input.encode('utf-8')).hexdigest()

def check_idempotent_response(key, request):
    """
    Checks if an idempotency record already exists for the key.
    Returns a DRFResponse if cached, else None.
    """
    if not key:
        return None

    record = IdempotencyRecord.objects.filter(key=key).first()
    if record:
        return DRFResponse(
            data=record.response_body,
            status=record.response_status,
            headers={'X-Idempotent-Replay': 'true', 'Idempotency-Key': key}
        )
    return None

def store_idempotent_response(key, request, response_status, response_body):
    """Stores the response status and body under the specified idempotency key."""
    if not key:
        return

    try:
        user = request.user if request.user and request.user.is_authenticated else None
        req_hash = compute_request_hash(request)
        IdempotencyRecord.objects.update_or_create(
            key=key,
            defaults={
                'user': user,
                'path': request.path,
                'request_hash': req_hash,
                'response_status': response_status,
                'response_body': response_body,
            }
        )
    except Exception:
        # Fail safe if concurrent duplicate write
        pass
