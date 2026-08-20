from .models import AuditLog

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
        return None
