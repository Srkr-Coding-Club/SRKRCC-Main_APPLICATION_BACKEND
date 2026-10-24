"""
The email-verification gate: one check, applied to every API request (see CoreConfig.ready).

Which endpoints it protects is decided here, not scattered over sixty views:
* public endpoints (an anonymous visitor would be let through) are never touched, so public pages stay open to everyone;
* a view can carry ``verification_exempt = True`` (needed to verify or leave: profile read, sign out, the verification
  endpoints), ``verification_reads_need_verified = True`` (a read-only private feature such as a certificate) or
  ``verification_writes_ok_in_grace = True`` (writes allowed until the deadline, e.g. editing your own profile);
* the rules themselves are in ``apps.accounts.services.email_verification.gate_decision``.
"""
from types import SimpleNamespace

from django.contrib.auth.models import AnonymousUser
from rest_framework.exceptions import NotAuthenticated, PermissionDenied
from rest_framework.views import APIView

from apps.accounts.services import email_verification

_installed = False


class EmailVerificationRequired(PermissionDenied):
    """403 whose body is returned exactly as given (see apps.core.exceptions.api_exception_handler)."""

    def __init__(self, payload: dict):
        super().__init__(detail=payload.get('detail'), code=payload.get('code'))
        self.payload = payload


def _is_public(view, request) -> bool:
    """
    Would an anonymous visitor be let through this endpoint, for this very request? Then it is a public page.
    (Asking the view's own permission classes covers "anyone may read, only staff may write" and the like, so a
    new permission class never needs to be added to a list here.)
    """
    try:
        anonymous = SimpleNamespace(
            method=request.method, user=AnonymousUser(), auth=None, META=request.META, data=request.data,
            query_params=request.query_params, GET=getattr(request, 'GET', {}),
        )
        classes = view.get_permissions()
        return bool(classes) and all(permission.has_permission(anonymous, view) for permission in classes)
    except Exception:  # noqa: BLE001 - never let the gate itself break a request; unsure means "not public"
        return False


def _exempt(view, method: str) -> bool:
    flag = getattr(view, 'verification_exempt', False)
    if isinstance(flag, (set, frozenset, tuple, list)):
        return method.upper() in flag
    return bool(flag)


def enforce(view, request) -> None:
    user = getattr(request, 'user', None)
    if user is None or not getattr(user, 'is_authenticated', False) or email_verification.mode() == 'off':
        return
    method = request.method or 'GET'
    arguments = dict(
        method=method, exempt=_exempt(view, method),
        reads_need_verified=bool(getattr(view, 'verification_reads_need_verified', False)),
        writes_ok_in_grace=bool(getattr(view, 'verification_writes_ok_in_grace', False)),
    )
    decision = email_verification.gate_decision(user, is_public=False, **arguments)
    if decision.allowed:
        return
    # Only now (a refusal is on the cards) is it worth asking whether the page is public after all.
    if _is_public(view, request):
        return
    raise EmailVerificationRequired({
        'detail': decision.message,
        'code': email_verification.REQUIRED_CODE,
        'reason': decision.reason,
        'blocked': decision.reason == 'deadline',
        'days_left': email_verification.days_left(user),
    })


def require_verified(request) -> None:
    """
    For an endpoint that is open to everyone for reading but whose WRITE is a private feature (submitting a form).
    Once verification is in force the caller must be a signed-in, verified account.
    """
    if not email_verification.mode_at_least('features'):
        return
    user = getattr(request, 'user', None)
    if user is None or not getattr(user, 'is_authenticated', False):
        raise NotAuthenticated(detail={'detail': 'Sign in to do this.', 'code': 'SIGN_IN_REQUIRED'})
    if email_verification.is_verified(user):
        return
    blocked = email_verification.mode_at_least('block') and email_verification.is_past_deadline(user)
    raise EmailVerificationRequired({
        'detail': 'Verify your email address to use this feature.',
        'code': email_verification.REQUIRED_CODE,
        'reason': 'deadline' if blocked else 'feature',
        'blocked': blocked,
        'days_left': email_verification.days_left(user),
    })


def install() -> None:
    """Wrap APIView.check_permissions once, so the gate runs right after authentication and the view's own permissions."""
    global _installed
    if _installed:
        return
    original = APIView.check_permissions

    def check_permissions(self, request):
        original(self, request)
        enforce(self, request)

    APIView.check_permissions = check_permissions
    _installed = True
