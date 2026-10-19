import logging
from .models import AuditLog

logger = logging.getLogger(__name__)

def log_audit_event(actor=None, action="", target_model="", target_id="", details=None):
    """
    Safely records an audit log event without interrupting application execution.
    """
    try:
        user = actor if (actor and getattr(actor, 'is_authenticated', False)) else None
        return AuditLog.objects.create(
            actor=user,
            action=action,
            target_model=target_model,
            target_id=str(target_id) if target_id else "",
            details=details or {}
        )
    except Exception:
        # Never break the caller's action, but never lose the fact that an audit record was lost.
        logger.exception('Could not write audit event %r (target %s %s)', action, target_model, target_id)
        return None
