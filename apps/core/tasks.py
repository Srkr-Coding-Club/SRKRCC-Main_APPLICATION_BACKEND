"""
apps/core/tasks.py
-------------------
Background job execution — plain Python threads, no Celery/Redis.

This app runs at college-club scale (dozens of admins, not a high-volume SaaS),
so a full task queue is more infrastructure than the workload justifies. Every
bulk operation (email campaigns, DMC exports) already writes a durable job row
(EmailJob/ExportJob/etc.) *before* dispatching, so the job's status always
survives even if the background thread dies — the tradeoff we're accepting
instead of a real queue is that a job doesn't survive a process restart mid-run
(no persistence/retry of the in-flight work itself, only of its last known
status). Reasonable for this app's scale; revisit if job volume ever grows
enough to need real retries or multi-process workers.
"""

import logging
import threading

from django.db import connections

logger = logging.getLogger(__name__)


def run_in_background(fn) -> None:
    """
    Runs `fn` (a zero-arg callable) on a daemon thread and returns immediately.

    `fn` should look up everything it needs by id (job id, etc.) rather than
    closing over ORM instances loaded on the caller's connection/transaction —
    the background thread gets its own database connection, so any objects
    passed by reference could be stale or (inside a test's atomic transaction)
    simply invisible to it.
    """
    def _runner():
        try:
            fn()
        except Exception:
            logger.exception("Background job raised an unhandled exception")
        finally:
            # `connections` is thread-local, so this only closes connection(s)
            # opened by THIS thread. Unconditional close (not close_old_connections,
            # which only closes connections it heuristically considers stale) —
            # a daemon thread has no request/response cycle to do this for it,
            # and a leaked-open connection can block test-database teardown.
            connections.close_all()

    threading.Thread(target=_runner, daemon=True).start()


def process_email_job(job_id):
    """Background execution of a previously-created EmailJob (see EmailNotificationService)."""
    from apps.core.models import EmailJob
    from apps.core.services.email_service import EmailNotificationService

    job = EmailJob.objects.get(id=job_id)
    EmailNotificationService.process_email_job(job)
