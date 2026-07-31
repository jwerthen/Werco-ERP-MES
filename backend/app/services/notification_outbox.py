"""Transactional-outbox session listeners for the notification pipeline (§3.1).

``OperationalEventService.emit`` marks catalog-mapped, flushed event ids on
``Session.info["pending_notification_event_ids"]``. These module-level SQLAlchemy
``Session`` listeners then:

* on ``after_commit`` — the events are now durable, so route an enqueue of
  ``dispatch_notification_job(event_id=...)`` for each. An enqueue failure NEVER fails the
  just-committed request (the 5-min relay sweeper is the backstop);
* on ``after_rollback`` / ``after_soft_rollback`` — the events never happened, so drop the
  pending list (GHOST prevention).

Enqueue routing (the whole point — ``enqueue_job_best_effort`` calls ``asyncio.run`` and
RuntimeErrors inside a running loop):
* a running event loop exists (async request handler) → ``loop.create_task(enqueue_job(...))``;
* no running loop (sync ``def`` handler on a threadpool worker) → ``enqueue_job_best_effort``.

Importing this module attaches the listeners; it is imported at both API startup
(``app.main``) and worker startup (``app.worker``) so the tee is active in every process
that commits operational events.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Set

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.queue import enqueue_job_best_effort, enqueue_job_fire_and_forget_fastfail

logger = logging.getLogger(__name__)

_PENDING_KEY = "pending_notification_event_ids"
_DISPATCH_JOB = "dispatch_notification_job"

# Hold references to fire-and-forget enqueue tasks so the event loop does not GC them
# mid-flight; each removes itself on completion.
_background_tasks: Set[asyncio.Task] = set()


def _enqueue_dispatch(event_id: int) -> None:
    """Route the dispatch-job enqueue to the correct mechanism for this context."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        # Fast-fail + self-swallowing so a Redis outage neither stalls the loop nor
        # surfaces as an unhandled task exception; the relay sweeper is the backstop.
        task = loop.create_task(enqueue_job_fire_and_forget_fastfail(_DISPATCH_JOB, event_id=event_id))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    else:
        enqueue_job_best_effort(_DISPATCH_JOB, event_id=event_id, fast_fail=True)


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    event_ids = session.info.pop(_PENDING_KEY, None)
    if not event_ids:
        return
    for event_id in event_ids:
        try:
            _enqueue_dispatch(event_id)
        except Exception:  # noqa: BLE001 - enqueue failure must never fail a committed request
            logger.warning(
                "notification outbox: failed to enqueue dispatch for event %s (sweeper will retry)",
                event_id,
                exc_info=True,
            )


@event.listens_for(Session, "after_rollback")
def _after_rollback(session: Session) -> None:
    session.info.pop(_PENDING_KEY, None)


@event.listens_for(Session, "after_soft_rollback")
def _after_soft_rollback(session: Session, previous_transaction) -> None:
    session.info.pop(_PENDING_KEY, None)
