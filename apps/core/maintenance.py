"""
Housekeeping tasks an admin can run from the Maintenance page (or a command), with a record of every run.

There is no scheduler in this app, so each task says how much work is waiting (``pending``) and how often it should
be run (``every_hours``); the page turns those into a plain "needs running / run soon / up to date" indicator.
Tasks are registered by the app that owns them (see ``apps.forms.maintenance_tasks``).
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable, Iterator, Optional

from django.db import transaction
from django.utils import timezone

from apps.core.models import MaintenanceRun, MaintenanceRunStatus
from apps.core.tasks import run_in_background

logger = logging.getLogger(__name__)

# A run that has been "running" this long died with its process; it no longer blocks another run.
STALE_AFTER = timedelta(minutes=15)

NEEDED, DUE, OK, UNAVAILABLE = 'needed', 'due', 'ok', 'unavailable'


@dataclass(frozen=True)
class Task:
    key: str
    label: str
    description: str
    run: Callable[[], dict]
    describe: Callable[[dict], str]
    pending: Callable[[], int]
    pending_label: str
    #: How often it should run even when nothing is waiting; None = only when work is waiting.
    every_hours: Optional[int] = None
    #: A reason the task cannot run right now (a feature that is switched off), or None.
    unavailable: Callable[[], Optional[str]] = lambda: None  # noqa: E731


_TASKS: dict[str, Task] = {}


def register(task: Task) -> None:
    _TASKS[task.key] = task


def tasks() -> list[Task]:
    return list(_TASKS.values())


def get(key: str) -> Optional[Task]:
    return _TASKS.get(key)


class AlreadyRunning(Exception):
    pass


def _fail_stale(key: str) -> None:
    MaintenanceRun.objects.filter(
        task=key, status=MaintenanceRunStatus.RUNNING, started_at__lt=timezone.now() - STALE_AFTER,
    ).update(status=MaintenanceRunStatus.FAILED, finished_at=timezone.now(), error='Interrupted before it finished.')


def last_run(key: str) -> Optional[MaintenanceRun]:
    return MaintenanceRun.objects.filter(task=key).order_by('-started_at').first()


def last_finished(key: str) -> Optional[MaintenanceRun]:
    return MaintenanceRun.objects.filter(task=key, status=MaintenanceRunStatus.SUCCESS).order_by('-started_at').first()


def status_of(task: Task, now=None) -> dict:
    """Everything the page shows for one task."""
    now = now or timezone.now()
    _fail_stale(task.key)
    blocked = task.unavailable()
    latest, good = last_run(task.key), last_finished(task.key)
    pending = 0
    try:
        pending = task.pending() if not blocked else 0
    except Exception:  # noqa: BLE001 - a broken count must not take the whole page down
        logger.warning('Could not count pending work for %s', task.key, exc_info=True)

    if blocked:
        state = UNAVAILABLE
    elif pending > 0:
        state = NEEDED
    elif task.every_hours and (good is None or now - good.started_at > timedelta(hours=task.every_hours)):
        state = DUE
    else:
        state = OK
    return {
        'key': task.key, 'label': task.label, 'description': task.description,
        'state': state, 'unavailable_reason': blocked,
        'pending': pending, 'pending_label': task.pending_label,
        'every_hours': task.every_hours,
        'running': bool(latest and latest.status == MaintenanceRunStatus.RUNNING),
        'last_run': serialize_run(task, latest),
        'last_success_at': good.started_at if good else None,
    }


def serialize_run(task: Task, run: Optional[MaintenanceRun]) -> Optional[dict]:
    if run is None:
        return None
    return {
        'id': run.pk, 'status': run.status, 'trigger': run.trigger, 'started_at': run.started_at, 'finished_at': run.finished_at,
        'result': task.describe(run.summary) if run.status == MaintenanceRunStatus.SUCCESS else '',
        'error': run.error,
        'by': (run.triggered_by.get_full_name() or run.triggered_by.username) if run.triggered_by_id else None,
    }


def overview() -> dict:
    items = [status_of(task) for task in tasks()]
    return {'tasks': items, 'attention': sum(1 for i in items if i['state'] in (NEEDED, DUE))}


@contextmanager
def recorded(key: str, *, trigger: str, user=None) -> Iterator[MaintenanceRun]:
    """Keep a record of one run. Set ``run.summary`` inside the block; an exception marks the run failed and is re-raised."""
    _fail_stale(key)
    with transaction.atomic():
        if MaintenanceRun.objects.select_for_update().filter(task=key, status=MaintenanceRunStatus.RUNNING).exists():
            raise AlreadyRunning(key)
        run = MaintenanceRun.objects.create(task=key, trigger=trigger, triggered_by=user)
    try:
        yield run
    except Exception as error:
        run.status, run.finished_at, run.error = MaintenanceRunStatus.FAILED, timezone.now(), str(error)[:500] or error.__class__.__name__
        run.save(update_fields=['status', 'finished_at', 'error'])
        raise
    run.status, run.finished_at = MaintenanceRunStatus.SUCCESS, timezone.now()
    run.save(update_fields=['status', 'finished_at', 'summary'])


def execute(task: Task, *, trigger: str, user=None) -> dict:
    """Run now, recorded. Returns the task's summary."""
    with recorded(task.key, trigger=trigger, user=user) as run:
        run.summary = task.run()
    return run.summary


def start_in_background(task: Task, user) -> MaintenanceRun:
    """Begin a run for the admin page and return at once; the page polls for the result."""
    _fail_stale(task.key)
    with transaction.atomic():
        if MaintenanceRun.objects.select_for_update().filter(task=task.key, status=MaintenanceRunStatus.RUNNING).exists():
            raise AlreadyRunning(task.key)
        run = MaintenanceRun.objects.create(task=task.key, trigger='admin', triggered_by=user)

    def work():
        fresh = MaintenanceRun.objects.get(pk=run.pk)
        try:
            fresh.summary = task.run()
            fresh.status = MaintenanceRunStatus.SUCCESS
        except Exception as error:  # noqa: BLE001
            logger.exception('Maintenance task %s failed', task.key)
            fresh.status, fresh.error = MaintenanceRunStatus.FAILED, str(error)[:500] or error.__class__.__name__
        fresh.finished_at = timezone.now()
        fresh.save(update_fields=['status', 'finished_at', 'summary', 'error'])

    run_in_background(work)
    return run
