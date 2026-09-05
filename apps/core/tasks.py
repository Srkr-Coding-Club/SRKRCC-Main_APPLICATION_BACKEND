import threading

from celery import shared_task


@shared_task
def process_email_job_task(job_id):
    """Background execution of a previously-created EmailJob (see EmailNotificationService)."""
    from apps.core.models import EmailJob
    from apps.core.services.email_service import EmailNotificationService

    job = EmailJob.objects.get(id=job_id)
    EmailNotificationService.process_email_job(job)


def try_dispatch_with_timeout(async_call, timeout: float = 3.0) -> bool:
    """
    Runs `async_call` (a Celery .delay()/.apply_async() call) in a background thread,
    bounded to `timeout` seconds, and reports whether it completed successfully within
    that window. Callers should fall back to processing synchronously when this
    returns False.

    Why this exists: every .delay() call site in this app already wraps itself in
    try/except with a sync fallback for when Celery/Redis is unreachable, on the
    assumption that a connection failure raises promptly. In practice a down broker
    can leave the underlying redis client blocked for minutes despite tight
    broker_connection_timeout / broker_connection_max_retries / socket_connect_timeout
    settings (observed: 'localhost' resolving to both ::1 and 127.0.0.1 alone roughly
    doubles the effective timeout, and retry policies compound further). Bounding the
    call from the *outside*, in a thread, guarantees the caller is never blocked
    longer than `timeout` regardless of what Celery/kombu is doing internally. The
    thread is left to finish (or hang) in the background — a bounded resource cost,
    never a blocked request.
    """
    outcome = {'done': False, 'error': None}

    def _run():
        try:
            async_call()
            outcome['done'] = True
        except Exception as ex:  # noqa: BLE001 - deliberately broad, this is a best-effort dispatch
            outcome['error'] = ex

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout)
    return outcome['done'] and outcome['error'] is None
