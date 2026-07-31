"""Transactional-outbox correctness for the notification pipeline (§3.1).

The load-bearing property: emitting a catalog-mapped ``OperationalEvent`` enqueues a
dispatch job IFF the surrounding transaction actually COMMITS. A rollback must enqueue
NOTHING (no ghost notifications for a transition that never happened -- rollbacks are a
DESIGNED path here, e.g. StaleDataError -> 409 on the contended WO/op/TimeEntry writes).

``OperationalEventService.emit`` marks the flushed event id on
``Session.info["pending_notification_event_ids"]``; the module-level ``Session`` listeners
in ``notification_outbox`` then enqueue on ``after_commit`` and DROP the pending list on
``after_rollback`` / ``after_soft_rollback``.

The enqueue is spied (``_enqueue_dispatch`` patched) so no Redis is touched -- mirroring how
the rest of the suite stubs ``enqueue_job`` / ``enqueue_job_best_effort``.
"""

import pytest
from sqlalchemy.orm import Session

import app.services.notification_outbox as outbox
from app.services.notification_outbox import _PENDING_KEY
from app.services.operational_event_service import OperationalEventService

pytestmark = [pytest.mark.requires_db]

# A catalog-mapped source event type (work_order_released -> wo.released) and an
# uncataloged one used to prove the tee ignores non-notifying events.
CATALOGED_EVENT_TYPE = "work_order_released"
UNCATALOGED_EVENT_TYPE = "totally_unmapped_event_type"


class _EnqueueRecorder:
    def __init__(self):
        self.event_ids = []

    def __call__(self, event_id: int) -> None:
        self.event_ids.append(event_id)


def _emit(db: Session, *, event_type: str) -> int:
    """Emit a bare operational event (no WO/op refs so no existence check)."""
    event = OperationalEventService(db).emit(
        company_id=1,
        event_type=event_type,
        source_module="work_orders",
        entity_type="work_order",
        entity_id=4242,
        user_id=None,
        event_payload={"work_order_number": "WO-OUTBOX-1"},
    )
    return event.id


# ---------------------------------------------------------------------------
# Commit enqueues exactly once
# ---------------------------------------------------------------------------


def test_commit_enqueues_dispatch_exactly_once(db_session: Session, monkeypatch):
    recorder = _EnqueueRecorder()
    monkeypatch.setattr(outbox, "_enqueue_dispatch", recorder)

    event_id = _emit(db_session, event_type=CATALOGED_EVENT_TYPE)
    # Marked on the session, not yet enqueued (transaction still open).
    assert db_session.info.get(_PENDING_KEY) == [event_id]
    assert recorder.event_ids == []

    db_session.commit()

    # after_commit fired -> enqueued once, pending list drained.
    assert recorder.event_ids == [event_id]
    assert _PENDING_KEY not in db_session.info


# ---------------------------------------------------------------------------
# Rollback enqueues NOTHING (no ghost) and clears the pending list
# ---------------------------------------------------------------------------


def test_rollback_enqueues_nothing_and_clears_pending(db_session: Session, monkeypatch):
    recorder = _EnqueueRecorder()
    monkeypatch.setattr(outbox, "_enqueue_dispatch", recorder)

    event_id = _emit(db_session, event_type=CATALOGED_EVENT_TYPE)
    assert db_session.info.get(_PENDING_KEY) == [event_id]

    db_session.rollback()

    # The event never happened -> no enqueue, pending list dropped (ghost prevention).
    assert recorder.event_ids == []
    assert _PENDING_KEY not in db_session.info

    # And a subsequent COMMIT of the (now-empty) session enqueues nothing either.
    db_session.commit()
    assert recorder.event_ids == []


def test_rollback_then_reemit_and_commit_enqueues_the_committed_event(db_session: Session, monkeypatch):
    """After a rollback drops the ghost, a fresh emit + commit still enqueues once."""
    recorder = _EnqueueRecorder()
    monkeypatch.setattr(outbox, "_enqueue_dispatch", recorder)

    _emit(db_session, event_type=CATALOGED_EVENT_TYPE)
    db_session.rollback()
    assert recorder.event_ids == []

    event_id = _emit(db_session, event_type=CATALOGED_EVENT_TYPE)
    db_session.commit()
    assert recorder.event_ids == [event_id]


# ---------------------------------------------------------------------------
# Uncataloged event types are ignored by the tee
# ---------------------------------------------------------------------------


def test_uncataloged_event_type_is_not_marked_or_enqueued(db_session: Session, monkeypatch):
    recorder = _EnqueueRecorder()
    monkeypatch.setattr(outbox, "_enqueue_dispatch", recorder)

    _emit(db_session, event_type=UNCATALOGED_EVENT_TYPE)
    # Not a notifying event -> never marked on the session.
    assert _PENDING_KEY not in db_session.info

    db_session.commit()
    assert recorder.event_ids == []


# ---------------------------------------------------------------------------
# The three listener functions each clear the pending list on rollback
# ---------------------------------------------------------------------------


def test_after_rollback_listener_clears_pending(db_session: Session):
    db_session.info[_PENDING_KEY] = [1, 2, 3]
    outbox._after_rollback(db_session)
    assert _PENDING_KEY not in db_session.info


def test_after_soft_rollback_listener_clears_pending(db_session: Session):
    db_session.info[_PENDING_KEY] = [9]
    # after_soft_rollback's signature is (session, previous_transaction).
    outbox._after_soft_rollback(db_session, None)
    assert _PENDING_KEY not in db_session.info


def test_after_commit_listener_no_pending_is_safe_noop(db_session: Session, monkeypatch):
    """after_commit with no pending ids must not enqueue or raise."""
    recorder = _EnqueueRecorder()
    monkeypatch.setattr(outbox, "_enqueue_dispatch", recorder)
    db_session.info.pop(_PENDING_KEY, None)
    outbox._after_commit(db_session)
    assert recorder.event_ids == []
