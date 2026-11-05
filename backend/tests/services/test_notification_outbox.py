"""Transactional-outbox correctness for the notification pipeline (§3.1).

The load-bearing property: emitting a catalog-mapped ``OperationalEvent`` enqueues a
dispatch job IFF the session's OUTERMOST transaction actually COMMITS. A rollback must
enqueue NOTHING (no ghost notifications for a transition that never happened -- rollbacks
are a DESIGNED path here, e.g. StaleDataError -> 409 on the contended WO/op/TimeEntry
writes).

``OperationalEventService.emit`` marks the flushed event id on
``Session.info["pending_notification_event_ids"]``; the module-level ``Session`` listeners
in ``notification_outbox`` enqueue on the root transaction's ``after_commit`` and drop the
pending list when the root ends any other way.

SAVEPOINTS ARE NOT COMMITS. SQLAlchemy fires ``after_commit`` on a ``begin_nested()``
release and its rollback events on a savepoint rollback, and ``AuditService`` alone opens a
savepoint per audit write. The savepoint section below pins that a release enqueues
nothing (the enclosing commit does), and that a savepoint rollback drops exactly the ids
emitted inside it -- never the ones the still-live outer transaction owns.

The enqueue is recorded at ``enqueue_job_best_effort`` -- the call that would reach Redis
-- so the real ``_enqueue_dispatch`` routing runs and no Redis is touched; the earlier
tests record one seam higher, at ``_enqueue_dispatch``, as the rest of the suite does.
"""

import asyncio

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.services.notification_outbox as outbox
from app.models.operational_event import OperationalEvent
from app.services.audit_service import AuditService
from app.services.import_batch_service import _connection as joined_connection
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


def _emit(db: Session, *, event_type: str = CATALOGED_EVENT_TYPE) -> int:
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


def _event_exists(db: Session, event_id: int) -> bool:
    return db.query(OperationalEvent.id).filter(OperationalEvent.id == event_id).first() is not None


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


def test_after_commit_listener_no_pending_is_safe_noop(db_session: Session, monkeypatch):
    """after_commit with no pending ids must not enqueue or raise."""
    recorder = _EnqueueRecorder()
    monkeypatch.setattr(outbox, "_enqueue_dispatch", recorder)
    db_session.info.pop(_PENDING_KEY, None)
    outbox._after_commit(db_session)
    assert recorder.event_ids == []


# ---------------------------------------------------------------------------
# Savepoints are not commits
# ---------------------------------------------------------------------------


@pytest.fixture
def enqueued(db_session: Session, monkeypatch):
    """The ``dispatch_notification_job`` enqueues that would reach Redis, in order.

    Each entry is ``(event_id, savepoint_open_at_enqueue)``: the second half is how a test
    tells "enqueued at the real commit" from "enqueued at a savepoint release that happened
    inside the same ``Session.commit()`` call" -- a savepoint left open (``AuditService``
    leaves one) is released by ``commit()`` a moment before the root COMMIT runs.
    """
    calls = []

    def _record(job_name, *, event_id, fast_fail):
        assert job_name == "dispatch_notification_job"
        calls.append((event_id, db_session.in_nested_transaction()))

    monkeypatch.setattr(outbox, "enqueue_job_best_effort", _record)
    return calls


class TestSavepointRelease:
    def test_an_event_emitted_before_a_released_savepoint_is_enqueued_once_at_the_outer_commit(
        self, db_session: Session, enqueued
    ):
        event_id = _emit(db_session)

        savepoint = db_session.begin_nested()
        savepoint.commit()  # RELEASE SAVEPOINT fires after_commit -- the request has NOT committed

        assert enqueued == []
        assert db_session.info[_PENDING_KEY] == [event_id]

        db_session.commit()

        assert enqueued == [(event_id, False)]
        assert _PENDING_KEY not in db_session.info

    def test_an_event_emitted_inside_a_released_savepoint_is_enqueued_once_at_the_outer_commit(
        self, db_session: Session, enqueued
    ):
        savepoint = db_session.begin_nested()
        event_id = _emit(db_session)
        savepoint.commit()

        assert enqueued == []
        assert db_session.info[_PENDING_KEY] == [event_id]

        db_session.commit()

        assert enqueued == [(event_id, False)]
        assert _event_exists(db_session, event_id)

    def test_an_audit_write_after_the_emit_does_not_enqueue_before_the_real_commit(self, db_session: Session, enqueued):
        """The shape most request handlers have: emit, then audit, then commit.

        ``AuditService`` writes each row inside a ``begin_nested()`` it never releases
        itself, so ``Session.commit()`` releases that savepoint first -- firing
        ``after_commit`` -- and only then COMMITs. The enqueue must wait for the COMMIT.
        """
        event_id = _emit(db_session)
        AuditService(db_session, company_id=1).log_create("outbox_probe", 1, "OUTBOX-PROBE")
        assert db_session.in_nested_transaction()  # the audit savepoint is still open

        db_session.commit()

        assert enqueued == [(event_id, False)]


class TestSavepointRollback:
    def test_a_rolled_back_savepoint_drops_only_the_ids_emitted_inside_it(self, db_session: Session, enqueued):
        before = _emit(db_session)

        savepoint = db_session.begin_nested()
        inside = _emit(db_session)
        savepoint.rollback()

        # The outer transaction still owns ``before``; ``inside`` was rolled back. (Checked
        # before the next emit: SQLite hands a rolled-back row's id to the next INSERT.)
        assert db_session.info[_PENDING_KEY] == [before]
        assert not _event_exists(db_session, inside)
        assert enqueued == []

        after = _emit(db_session)
        db_session.commit()

        assert enqueued == [(before, False), (after, False)]
        assert _event_exists(db_session, before)
        assert _event_exists(db_session, after)

    def test_nested_savepoints_drop_exactly_the_rolled_back_subtree(self, db_session: Session, enqueued):
        outer = _emit(db_session)
        sp1 = db_session.begin_nested()
        in_sp1 = _emit(db_session)
        sp2 = db_session.begin_nested()
        in_sp2 = _emit(db_session)
        sp2.commit()  # released into sp1
        sp3 = db_session.begin_nested()
        in_sp3 = _emit(db_session)
        sp3.rollback()

        assert db_session.info[_PENDING_KEY] == [outer, in_sp1, in_sp2]

        sp1.rollback()  # takes in_sp1 and the already-released in_sp2 with it

        assert db_session.info[_PENDING_KEY] == [outer]

        db_session.commit()

        assert enqueued == [(outer, False)]
        assert [eid for eid in (outer, in_sp1, in_sp2, in_sp3) if _event_exists(db_session, eid)] == [outer]

    def test_a_flush_failure_inside_a_savepoint_keeps_the_ids_the_outer_transaction_owns(
        self, db_session: Session, enqueued
    ):
        """The flush's own subtransaction rolls the savepoint back (``after_rollback``
        fires from inside ``flush()``), and the caller then rolls the dead savepoint back
        itself -- the second ``after_soft_rollback`` must not cost the outer ids either."""
        before = _emit(db_session)

        savepoint = db_session.begin_nested()
        inside = _emit(db_session)
        db_session.add(OperationalEvent(company_id=1, event_type=None, source_module="work_orders"))
        with pytest.raises(IntegrityError):
            db_session.flush()
        savepoint.rollback()

        assert db_session.info[_PENDING_KEY] == [before]

        db_session.commit()

        assert enqueued == [(before, False)]
        assert not _event_exists(db_session, inside)

    def test_a_rolled_back_savepoint_with_nothing_pending_before_it_leaves_no_pending_list(
        self, db_session: Session, enqueued
    ):
        savepoint = db_session.begin_nested()
        _emit(db_session)
        savepoint.rollback()

        assert _PENDING_KEY not in db_session.info

        db_session.commit()
        assert enqueued == []


class TestOuterRollback:
    def test_an_outer_rollback_enqueues_nothing_whatever_its_savepoints_did(self, db_session: Session, enqueued):
        _emit(db_session)
        released = db_session.begin_nested()
        _emit(db_session)
        released.commit()
        rolled_back = db_session.begin_nested()
        _emit(db_session)
        rolled_back.rollback()
        db_session.begin_nested()  # left open, as AuditService leaves one
        _emit(db_session)

        db_session.rollback()

        assert enqueued == []
        assert _PENDING_KEY not in db_session.info

        db_session.commit()
        assert enqueued == []

    def test_closing_the_session_mid_transaction_enqueues_nothing_later(self, db_session: Session, enqueued):
        """A close with the transaction still open is a rollback; the stale ids must not
        ride along into the next transaction on the same (reused) session."""
        _emit(db_session)

        db_session.close()

        assert _PENDING_KEY not in db_session.info

        event_id = _emit(db_session)
        db_session.commit()

        assert enqueued == [(event_id, False)]

    def test_a_commit_that_fails_at_its_own_flush_enqueues_nothing(self, db_session: Session, enqueued):
        """The enqueue waits for the COMMIT to succeed, not merely to start: ``commit()``
        fires ``before_commit`` and then flushes, and that flush can still fail. An
        enqueue taken any earlier than ``after_commit`` would announce this event."""
        event_id = _emit(db_session)
        db_session.add(OperationalEvent(company_id=1, event_type=None, source_module="work_orders"))

        with pytest.raises(IntegrityError):
            db_session.commit()
        assert enqueued == []

        db_session.rollback()

        assert enqueued == []
        assert _PENDING_KEY not in db_session.info
        assert not _event_exists(db_session, event_id)


class TestRunningEventLoop:
    """An ``async def`` handler commits with a running loop, where the enqueue must be a
    ``loop.create_task`` -- ``enqueue_job_best_effort`` calls ``asyncio.run``, which raises
    inside a running loop, and ``_after_commit`` would swallow that into a log line. The
    outermost-commit rule holds on this route too."""

    async def test_the_enqueue_is_scheduled_on_the_loop_only_at_the_outer_commit(
        self, db_session: Session, monkeypatch
    ):
        scheduled, delivered, blocking_calls = [], [], []

        async def _deliver(event_id):
            delivered.append(event_id)

        def _fire_and_forget(job_name, *, event_id):
            assert job_name == "dispatch_notification_job"
            scheduled.append((event_id, db_session.in_nested_transaction()))
            return _deliver(event_id)

        monkeypatch.setattr(outbox, "enqueue_job_fire_and_forget_fastfail", _fire_and_forget)
        monkeypatch.setattr(outbox, "enqueue_job_best_effort", lambda *args, **kwargs: blocking_calls.append(kwargs))

        event_id = _emit(db_session)
        savepoint = db_session.begin_nested()
        savepoint.commit()
        AuditService(db_session, company_id=1).log_create("outbox_probe", 1, "OUTBOX-PROBE")
        assert scheduled == []

        db_session.commit()

        assert scheduled == [(event_id, False)]
        assert blocking_calls == []
        # Scheduled, not awaited: the commit never waits on Redis. One loop turn runs it.
        assert delivered == []
        await asyncio.sleep(0)
        assert delivered == [event_id]


class TestForwardingSession:
    """A session joined into another session's transaction (``import_batch_service``'s
    ``join_transaction_mode="create_savepoint"``) commits its root while the durable COMMIT
    is still ahead. With ``FORWARD_TO_KEY`` set, its ROOT commit hands the committed ids to
    the owner's list -- never a savepoint release inside it, never a rollback -- and nothing
    is enqueued until the owner re-attaches them and the enclosing session commits."""

    def test_only_the_inner_root_commit_forwards_and_only_the_outer_commit_enqueues(
        self, db_session: Session, enqueued
    ):
        forwarded = []
        with Session(bind=joined_connection(db_session), join_transaction_mode="create_savepoint") as inner:
            inner.info[outbox.FORWARD_TO_KEY] = forwarded
            kept = _emit(inner)
            row = inner.begin_nested()
            _emit(inner)  # rolled back with ``row`` below
            inner.begin_nested().commit()  # a release (an audit write's, say) forwards nothing
            assert forwarded == []
            row.rollback()

            inner.commit()
            assert forwarded == [kept]

            _emit(inner)
            inner.rollback()
            assert forwarded == [kept]

        assert enqueued == []
        # The owner's half, as ``commit_batch`` does once the row's unit lands.
        db_session.info.setdefault(_PENDING_KEY, []).extend(forwarded)
        db_session.commit()

        assert enqueued == [(kept, False)]
        assert _event_exists(db_session, kept)
