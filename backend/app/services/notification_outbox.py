"""Transactional-outbox session listeners for the notification pipeline (§3.1).

``OperationalEventService.emit`` marks catalog-mapped, flushed event ids on
``Session.info["pending_notification_event_ids"]``. These module-level SQLAlchemy
``Session`` listeners enqueue ``dispatch_notification_job(event_id=...)`` for each id
**if and only if the session's OUTERMOST transaction commits**, and nothing otherwise.

Why "outermost" has to be said out loud
---------------------------------------
SQLAlchemy dispatches ``after_commit`` on a SAVEPOINT release as well as on the real
COMMIT, and ``after_rollback`` / ``after_soft_rollback`` on a savepoint rollback as well as
on the real ROLLBACK (``orm/session.py``: ``SessionTransaction.commit`` / ``.rollback`` fire
for ``self._parent is None or self.nested``). ``AuditService`` opens a savepoint per audit
write and many services open their own, so listeners that took those events at face value
would (a) enqueue an event at a savepoint release, BEFORE the request commits -- a worker
that looks the event up before the COMMIT lands finds no row and drops the job, and one
whose transaction later rolls back was never real -- and (b) drop the whole pending list at
a savepoint rollback, discarding ids the still-live outer transaction owns. Both were real until this module learned the
difference; the 5-minute relay sweeper was the only thing recovering them.

The rules
---------
* ``after_transaction_create`` on a SAVEPOINT -- remember the pending ids as they stood
  when it opened (a per-savepoint snapshot, held in ``Session.info``).
* ``after_commit`` on a SAVEPOINT (a RELEASE) -- forget that snapshot. The ids emitted
  inside it now belong to its parent; nothing is enqueued.
* ``after_transaction_end`` on a SAVEPOINT that was never released (rolled back, or closed
  under a session close) -- put the pending list back to its snapshot, so exactly the ids
  emitted inside that savepoint, and inside any savepoint nested in it, are dropped.
* ``after_commit`` on the OUTERMOST transaction -- the events are now durable: drain the
  list and route one enqueue per id. An enqueue failure NEVER fails the just-committed
  request (the 5-min relay sweeper is the backstop).
* ``after_transaction_end`` on the OUTERMOST transaction -- drop whatever is still pending.
  After a commit that is nothing (``after_commit`` drained it); after a rollback, or a
  session close with the transaction still open, those events never happened (GHOST
  prevention).

"Outermost" means the session's root ``SessionTransaction``. Session-internal
subtransactions (the one ``flush()`` opens) are neither savepoints nor roots, and every
listener here ignores them.

A ``Session`` bound to a connection whose transaction someone else owns -- joined into
another session's transaction, as ``import_batch_service``'s
``join_transaction_mode="create_savepoint"`` session is -- commits its root while the
durable commit still lies ahead, and the listeners cannot see past the session boundary.
Such a session sets ``session.info[FORWARD_TO_KEY]`` to a list it owns: its root commit
then appends the committed ids there instead of enqueuing, and the owner hands them to the
enclosing session once its own unit of work lands. Everything else above -- savepoint
snapshots, dropping on rollback or close -- applies inside that session unchanged.

Enqueue routing (``enqueue_job_best_effort`` calls ``asyncio.run`` and RuntimeErrors inside
a running loop):
* a running event loop exists (async request handler) → ``loop.create_task(enqueue_job(...))``;
* no running loop (sync ``def`` handler on a threadpool worker) → ``enqueue_job_best_effort``.

Importing this module attaches the listeners; it is imported at both API startup
(``app.main``) and worker startup (``app.worker``) so the tee is active in every process
that commits operational events.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, Set, Tuple

from sqlalchemy import event
from sqlalchemy.orm import Session, SessionTransaction

from app.core.queue import enqueue_job_best_effort, enqueue_job_fire_and_forget_fastfail

logger = logging.getLogger(__name__)

_PENDING_KEY = "pending_notification_event_ids"
# ``{savepoint SessionTransaction: pending ids when it opened}``. Entries live only while
# the savepoint is open: a release forgets its entry, any other end restores from it.
_SAVEPOINT_SNAPSHOTS_KEY = "notification_outbox_savepoint_snapshots"
# Set by a session whose root commit is not the durable one (see the module docstring):
# a list that receives the ids its root commit would otherwise have enqueued.
FORWARD_TO_KEY = "notification_outbox_forward_to"
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


def _savepoint_snapshots(session: Session) -> Dict[SessionTransaction, Tuple[int, ...]]:
    return session.info.setdefault(_SAVEPOINT_SNAPSHOTS_KEY, {})


@event.listens_for(Session, "after_transaction_create")
def _after_transaction_create(session: Session, transaction: SessionTransaction) -> None:
    if transaction.nested:
        _savepoint_snapshots(session)[transaction] = tuple(session.info.get(_PENDING_KEY) or ())


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    # ``after_commit`` fires inside ``SessionTransaction.commit`` before that transaction
    # closes, and only for a savepoint or the root. Savepoints are a stack whose innermost
    # open one is ``get_nested_transaction()``, and committing any transaction commits the
    # ones above it first -- so a savepoint is open here exactly when one is being released.
    savepoint = session.get_nested_transaction()
    if savepoint is not None:
        # RELEASE SAVEPOINT: nothing is durable yet. Its ids now belong to its parent.
        _savepoint_snapshots(session).pop(savepoint, None)
        return

    event_ids = session.info.pop(_PENDING_KEY, None)
    if not event_ids:
        return
    forward_to = session.info.get(FORWARD_TO_KEY)
    if forward_to is not None:
        # This root commit is not durable either; the session's owner decides.
        forward_to.extend(event_ids)
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


@event.listens_for(Session, "after_transaction_end")
def _after_transaction_end(session: Session, transaction: SessionTransaction) -> None:
    if transaction.nested:
        snapshots = session.info.get(_SAVEPOINT_SNAPSHOTS_KEY)
        if not snapshots or transaction not in snapshots:
            return  # released: ``after_commit`` already handed its ids to the parent
        # Never released -> everything emitted inside it was rolled back with it.
        before = snapshots.pop(transaction)
        if before:
            session.info[_PENDING_KEY] = list(before)
        else:
            session.info.pop(_PENDING_KEY, None)
    elif transaction.parent is None:
        # The outermost transaction is over. A successful commit already drained the list
        # in ``after_commit``; anything left belongs to a rollback (or a close with the
        # transaction still open), and those events never happened.
        session.info.pop(_PENDING_KEY, None)
        session.info.pop(_SAVEPOINT_SNAPSHOTS_KEY, None)
