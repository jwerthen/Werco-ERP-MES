"""Dispatch v2: the WRITE DISCIPLINE every dispatch-order writer shares (spec 3a).

The other Dispatch v2 files prove WHAT each writer does. This one pins HOW, at the
statement level, because these properties are invisible to a behavioural test on a
single-connection SQLite database and a regression in any of them passes everything else:

1. THE LOCK COMES FIRST. Every writer takes the ``dispatch_order`` advisory lock BEFORE
   the reads it compares against (the compare-and-swap state, the notice "before"
   snapshot). Taken after them, two planners -- or a planner and a machine move -- can
   interleave a read and a write and silently overwrite each other on Postgres, while
   every SQLite test still passes (the lock is a no-op there). The existing tests only
   prove the lock is CALLED; these prove it is called before the guarded read.
   The two work-order schedule routes also take it before their ROW locks: one lock
   order on every path (dispatch lock -> work order / operation rows -> material rows ->
   audit chain), or Postgres aborts one side of the cycle as a deadlock.
2. WRITE ORDER: rank UPDATEs -> audit row -> notice event. The audit chain lock is
   global, so writing the audit row before the rank UPDATEs risks a deadlock against a
   completion; the notice describes the committed change and must follow the record.
3. ONLY ROWS WHOSE VALUE CHANGES are written.
4. The notice seam NEVER calls ``expire_all`` (the move paths hold un-committed ORM state).
5. Tenancy of the WHOLE-LIST clear: one company's plan save never touches another's plan.
6. Plan positions are gap-free over LIVE planned work orders (a finished job leaves no hole).
7. The lanes read is a PURE read: it does not run the reconcile-on-read promotion.
8. RBAC on the three new routes: exactly the planner tier, every other role refused.
"""

import re
from datetime import date
from types import SimpleNamespace
from typing import Callable, List, Optional, Tuple

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

import app.services.notification_outbox as notification_outbox
from app.api.endpoints import scheduling as scheduling_endpoint
from app.models.user import UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.schemas.dispatch import DispatchChangeKind
from app.services import dispatch_plan_service, dispatch_service
from tests.api.dispatch_plan_helpers import (
    BOARD_URL,
    LANES_URL,
    PLAN_URL,
    lane_for,
    make_plan_wo,
    operation_order_url,
    plan,
    ready,
    run_order_url,
)
from tests.api.kiosk_test_helpers import COMPANY_A, COMPANY_B, ensure_company, make_user, make_work_center, user_headers
from tests.api.test_dispatch_operation_order import MOVE_PATHS, _move

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

LOCK = "<<dispatch_order lock>>"

# Any read of the tables a planner verb compares against.
_DISPATCH_TABLES = re.compile(r"\b(work_orders|work_order_operations|work_centers)\b")
# The effective-rank CASE -- present in every shared queue read (the notice snapshot,
# the CAS tuples, the queue sort) and in nothing else. ORM loads of WorkOrder select
# ``work_orders.dispatch_rank`` too, so the bare column name would not discriminate.
_QUEUE_READ = re.compile(r"THEN work_orders\.dispatch_rank", re.IGNORECASE)


@pytest.fixture(autouse=True)
def _no_outbox_enqueue(monkeypatch):
    """Keep the after-commit outbox tee off Redis."""
    monkeypatch.setattr(notification_outbox, "_enqueue_dispatch", lambda event_id: None)


def _manager(db: Session):
    return make_user(db, role=UserRole.MANAGER, first_name="Jon", last_name="Werthen")


def _record(
    db: Session, monkeypatch, fn: Callable[[], object], log: Optional[List[str]] = None
) -> Tuple[object, List[str]]:
    """Run ``fn`` recording every SQL statement AND the dispatch lock, in one ordered list.

    ``fn`` must touch no expired fixture object (resolve ids to plain ints first): a
    lazy refresh of a test's own object would be recorded as if the request had read it.
    Pass ``log`` to share the list with another spy's marker, so one list orders both.
    """
    log = [] if log is None else log
    monkeypatch.setattr(dispatch_service, "acquire_generator_lock", lambda db_, namespace, company_id: log.append(LOCK))

    def _listen(conn, cursor, statement, parameters, context, executemany):
        # Parameters ride along so an event INSERT can be told apart by its event_type.
        log.append(f"{statement}\n-- params: {parameters!r}")

    bind = db.get_bind()
    event.listen(bind, "after_cursor_execute", _listen)
    try:
        result = fn()
    finally:
        event.remove(bind, "after_cursor_execute", _listen)
    return result, log


def _first(log: List[str], pattern: re.Pattern) -> int:
    for index, statement in enumerate(log):
        if statement != LOCK and pattern.search(statement):
            return index
    raise AssertionError(f"no statement matched {pattern.pattern!r}")


def _last(log: List[str], pattern: re.Pattern) -> int:
    matches = [index for index, statement in enumerate(log) if statement != LOCK and pattern.search(statement)]
    assert matches, f"no statement matched {pattern.pattern!r}"
    return matches[-1]


# --------------------------------------------------------------------------
# 1. The lock precedes every read it guards
# --------------------------------------------------------------------------


class TestTheLockComesFirst:
    def test_work_order_plan(self, client: TestClient, db_session: Session, monkeypatch):
        headers = user_headers(_manager(db_session))
        wc = make_work_center(db_session)
        a, _ = make_plan_wo(db_session, [ready(wc)], dispatch_rank=1)
        b, _ = make_plan_wo(db_session, [ready(wc)])
        a_id, b_id = a.id, b.id

        resp, log = _record(db_session, monkeypatch, lambda: plan(client, headers, [b_id, a_id], expected=[a_id]))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        self._assert_lock_precedes(log, _DISPATCH_TABLES)

    def test_operation_order(self, client: TestClient, db_session: Session, monkeypatch):
        headers = user_headers(_manager(db_session))
        wc = make_work_center(db_session)
        wo, (op10, op20) = make_plan_wo(db_session, [ready(wc, 10), ready(wc, 20)], sequential=False, dispatch_rank=1)
        url, x, y = operation_order_url(wo.id), op10.id, op20.id

        resp, log = _record(
            db_session,
            monkeypatch,
            lambda: client.put(url, json={"operation_ids": [y, x], "expected_operation_ids": [x, y]}, headers=headers),
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        self._assert_lock_precedes(log, _DISPATCH_TABLES)

    def test_column_rewrite(self, client: TestClient, db_session: Session, monkeypatch):
        headers = user_headers(_manager(db_session))
        wc = make_work_center(db_session)
        _, (a,) = make_plan_wo(db_session, [ready(wc)])
        _, (b,) = make_plan_wo(db_session, [ready(wc)])
        url, a_id, b_id = run_order_url(wc.id), a.id, b.id

        resp, log = _record(
            db_session,
            monkeypatch,
            lambda: client.put(
                url, json={"operation_ids": [b_id, a_id], "expected_operation_ids": [a_id, b_id]}, headers=headers
            ),
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        self._assert_lock_precedes(log, _DISPATCH_TABLES)

    @pytest.mark.parametrize("route", MOVE_PATHS)
    def test_machine_moves(self, client: TestClient, db_session: Session, monkeypatch, route):
        """A move must read the operation to know it IS a move, so the guarded read here is
        the queue snapshot (the notice "before" state), which must follow the lock."""
        headers = user_headers(_manager(db_session))
        source = make_work_center(db_session)
        target = make_work_center(db_session)
        make_plan_wo(db_session, [ready(target)])
        mover_wo, (mover,) = make_plan_wo(db_session, [ready(source)])
        op, wo_id, target_id = SimpleNamespace(id=mover.id, version=mover.version), mover_wo.id, target.id

        resp, log = _record(db_session, monkeypatch, lambda: _move(client, headers, route, op, wo_id, target_id))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        self._assert_lock_precedes(log, _QUEUE_READ)

    @staticmethod
    def _assert_lock_precedes(log: List[str], guarded: re.Pattern) -> None:
        assert log.count(LOCK) == 1, "every writer takes the dispatch lock exactly once"
        lock_at = log.index(LOCK)
        early = [statement for statement in log[:lock_at] if guarded.search(statement)]
        assert early == [], f"read before the dispatch lock: {early[0][:200]}"
        # Non-vacuity: the guarded read really happens, after the lock.
        assert _first(log, guarded) > lock_at


# --------------------------------------------------------------------------
# 1b. The schedule routes take the dispatch lock before their ROW locks
# --------------------------------------------------------------------------


READINESS = "<<load_locked_material_readiness>>"
SCHEDULE_ROUTES = ("schedule", "schedule_earliest")
# Any read of a work order or its operations -- including the row locks
# load_locked_material_readiness takes (SQLite renders them without FOR UPDATE).
_WORK_ORDER_ROWS = re.compile(r"\b(work_orders|work_order_operations)\b")
_ANY_NOTICE = re.compile(r"'dispatch_order_changed'")


def _spy_readiness(monkeypatch, log: List[str]) -> None:
    """Append READINESS to ``log`` each time a schedule route row-locks its work order."""
    real = scheduling_endpoint.load_locked_material_readiness

    def _spy(*args, **kwargs):
        log.append(READINESS)
        return real(*args, **kwargs)

    monkeypatch.setattr(scheduling_endpoint, "load_locked_material_readiness", _spy)


def _schedule_without_work_center(client: TestClient, headers: dict, route: str, wo_id: int):
    if route == "schedule":
        return client.put(
            f"/api/v1/scheduling/work-orders/{wo_id}/schedule",
            json={"scheduled_start": date.today().isoformat()},
            headers=headers,
        )
    return client.post(f"/api/v1/scheduling/work-orders/{wo_id}/schedule-earliest", json={}, headers=headers)


class TestScheduleRoutesLockBeforeRowLocks:
    """``load_locked_material_readiness`` row-locks the work order and every one of its
    operations (``SELECT ... FOR UPDATE``). Every other dispatch-order writer holds the
    dispatch lock and THEN writes those very rows (the plan's rank UPDATE, the operation
    order, the column rewrite, the other two move verbs). A schedule route that took the
    dispatch lock AFTER its row locks would close that cycle, and Postgres would abort one
    side with DeadlockDetected -- a failed request. Neither lock exists on SQLite, so only the recorded
    ORDER can show it."""

    @pytest.mark.parametrize("moves", [True, False], ids=["move", "same-machine-resend"])
    @pytest.mark.parametrize("route", SCHEDULE_ROUTES)
    def test_the_dispatch_lock_precedes_every_row_lock(
        self, client: TestClient, db_session: Session, monkeypatch, route, moves
    ):
        """Keyed on the request CARRYING a work center, not on the comparison: a
        same-machine re-send takes the lock too (it cannot know it is not a move until it
        has read the operation, and that read is what the lock must precede)."""
        headers = user_headers(_manager(db_session))
        source = make_work_center(db_session)
        target = make_work_center(db_session) if moves else source
        mover_wo, (mover,) = make_plan_wo(db_session, [ready(source)])
        op, wo_id, target_id = SimpleNamespace(id=mover.id, version=mover.version), mover_wo.id, target.id

        log: List[str] = []
        _spy_readiness(monkeypatch, log)
        resp, log = _record(
            db_session, monkeypatch, lambda: _move(client, headers, route, op, wo_id, target_id), log=log
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text

        assert log.count(LOCK) == 1, "the dispatch lock is taken exactly once"
        assert log.count(READINESS) == 1
        lock_at = log.index(LOCK)
        assert lock_at < log.index(READINESS), "the dispatch lock must precede the WO/operation row locks"
        early = [statement for statement in log[:lock_at] if _WORK_ORDER_ROWS.search(statement)]
        assert early == [], f"work-order read before the dispatch lock: {early[0][:200]}"
        # Non-vacuity: the work order really is read (and row-locked), after the lock.
        assert _first(log, _WORK_ORDER_ROWS) > lock_at
        # A re-send is not a move: no notice.
        assert any(_ANY_NOTICE.search(statement) for statement in log) is moves

    @pytest.mark.parametrize("route", SCHEDULE_ROUTES)
    def test_a_call_without_a_work_center_takes_no_lock_and_moves_nothing(
        self, client: TestClient, db_session: Session, monkeypatch, route
    ):
        """No work center in the request -> the call can never move the operation, so it
        behaves exactly as before: no dispatch lock, no notice, the operation stays put --
        and it still validates material under its row locks."""
        headers = user_headers(_manager(db_session))
        wc = make_work_center(db_session)
        wo, (operation,) = make_plan_wo(db_session, [ready(wc)])
        wo_id, op_id, wc_id = wo.id, operation.id, wc.id

        log: List[str] = []
        _spy_readiness(monkeypatch, log)
        resp, log = _record(
            db_session, monkeypatch, lambda: _schedule_without_work_center(client, headers, route, wo_id), log=log
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert LOCK not in log
        assert log.count(READINESS) == 1
        assert not any(_ANY_NOTICE.search(statement) for statement in log)
        assert resp.json()["work_center_id"] == wc_id

        db_session.expire_all()
        row = db_session.get(WorkOrderOperation, op_id)
        assert row.work_center_id == wc_id
        assert row.scheduled_start is not None, "the call still scheduled the operation"


# --------------------------------------------------------------------------
# 2. Write order: rank UPDATEs -> audit row -> notice event
# --------------------------------------------------------------------------


_AUDIT_INSERT = re.compile(r"^\s*INSERT INTO audit_logs\b", re.IGNORECASE)
# The dispatch notice's own INSERT (other writers on these paths emit other events).
_NOTICE_INSERT = re.compile(
    r"^\s*INSERT INTO operational_events\b.*'dispatch_order_changed'", re.IGNORECASE | re.DOTALL
)
# A column named in the SET clause (never the WHERE clause) of an UPDATE on a table.
_SET = r"^\s*UPDATE {table} SET (?:(?!\bWHERE\b).)*\b{column}="
_WO_RANK_UPDATE = re.compile(_SET.format(table="work_orders", column="dispatch_rank"), re.IGNORECASE | re.DOTALL)
_OP_RANK_UPDATE = re.compile(_SET.format(table="work_order_operations", column="run_order"), re.IGNORECASE | re.DOTALL)
_OP_MOVE_UPDATE = re.compile(
    _SET.format(table="work_order_operations", column="work_center_id"), re.IGNORECASE | re.DOTALL
)


class TestWriteOrder:
    def _assert_order(self, log: List[str], write: re.Pattern) -> None:
        last_write = _last(log, write)
        audit = _first(log, _AUDIT_INSERT)
        notice = _first(log, _NOTICE_INSERT)
        assert last_write < audit, "the rank/move write must precede the audit row (global audit-chain lock)"
        assert audit < notice, "the notice follows the audit row it describes"

    def test_work_order_plan(self, client: TestClient, db_session: Session, monkeypatch):
        headers = user_headers(_manager(db_session))
        wc = make_work_center(db_session)
        a, _ = make_plan_wo(db_session, [ready(wc)], dispatch_rank=1)
        b, _ = make_plan_wo(db_session, [ready(wc)])
        a_id, b_id = a.id, b.id
        resp, log = _record(db_session, monkeypatch, lambda: plan(client, headers, [b_id, a_id], expected=[a_id]))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        self._assert_order(log, _WO_RANK_UPDATE)

    def test_operation_order(self, client: TestClient, db_session: Session, monkeypatch):
        headers = user_headers(_manager(db_session))
        wc = make_work_center(db_session)
        wo, (op10, op20) = make_plan_wo(db_session, [ready(wc, 10), ready(wc, 20)], sequential=False, dispatch_rank=1)
        url, x, y = operation_order_url(wo.id), op10.id, op20.id
        resp, log = _record(
            db_session,
            monkeypatch,
            lambda: client.put(url, json={"operation_ids": [y, x], "expected_operation_ids": [x, y]}, headers=headers),
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        self._assert_order(log, _OP_RANK_UPDATE)

    def test_column_rewrite(self, client: TestClient, db_session: Session, monkeypatch):
        headers = user_headers(_manager(db_session))
        wc = make_work_center(db_session)
        _, (a,) = make_plan_wo(db_session, [ready(wc)])
        _, (b,) = make_plan_wo(db_session, [ready(wc)])
        url, a_id, b_id = run_order_url(wc.id), a.id, b.id
        resp, log = _record(
            db_session,
            monkeypatch,
            lambda: client.put(url, json={"operation_ids": [b_id, a_id]}, headers=headers),
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        self._assert_order(log, _OP_RANK_UPDATE)

    @pytest.mark.parametrize("route", MOVE_PATHS)
    def test_machine_moves(self, client: TestClient, db_session: Session, monkeypatch, route):
        headers = user_headers(_manager(db_session))
        source = make_work_center(db_session)
        target = make_work_center(db_session)
        make_plan_wo(db_session, [ready(target)])
        mover_wo, (mover,) = make_plan_wo(db_session, [ready(source)])
        op, wo_id, target_id = SimpleNamespace(id=mover.id, version=mover.version), mover_wo.id, target.id
        resp, log = _record(db_session, monkeypatch, lambda: _move(client, headers, route, op, wo_id, target_id))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        # The move is an ORM write flushed with (or before) the audit row; the notice
        # comes after both, and only once the move is visible to its after-snapshot.
        move = _first(log, _OP_MOVE_UPDATE)
        notice = _first(log, _NOTICE_INSERT)
        assert move < notice
        assert _first(log, _AUDIT_INSERT) < notice


# --------------------------------------------------------------------------
# 3. Only rows whose value changes are written
# --------------------------------------------------------------------------


class TestOnlyChangedRowsAreWritten:
    def test_the_plan_writes_only_moved_ranks_and_clears_only_departures(
        self, client: TestClient, db_session: Session, monkeypatch
    ):
        headers = user_headers(_manager(db_session))
        wc = make_work_center(db_session)
        a, _ = make_plan_wo(db_session, [ready(wc)], dispatch_rank=1)
        b, _ = make_plan_wo(db_session, [ready(wc)], dispatch_rank=2)
        c, _ = make_plan_wo(db_session, [ready(wc)], dispatch_rank=3)
        d, _ = make_plan_wo(db_session, [ready(wc)], dispatch_rank=4)

        captured = []
        real = dispatch_plan_service.work_order_rank_update_statements

        def _spy(company_id, to_rank, to_clear):
            captured.append((company_id, dict(to_rank), sorted(to_clear)))
            return real(company_id, to_rank, to_clear)

        monkeypatch.setattr(dispatch_plan_service, "work_order_rank_update_statements", _spy)
        resp = plan(client, headers, [a.id, c.id, b.id], expected=[a.id, b.id, c.id, d.id])
        assert resp.status_code == status.HTTP_200_OK, resp.text

        # a keeps rank 1 and is not written; c and b swap; d leaves the plan.
        assert captured == [(COMPANY_A, {c.id: 2, b.id: 3}, [d.id])]
        db_session.expire_all()
        assert [db_session.get(WorkOrder, wo.id).dispatch_rank for wo in (a, b, c, d)] == [1, 3, 2, None]

    def test_the_operation_order_writes_only_moved_ranks(self, client: TestClient, db_session: Session, monkeypatch):
        headers = user_headers(_manager(db_session))
        wc = make_work_center(db_session)
        wo, (x, y, z) = make_plan_wo(
            db_session,
            [ready(wc, 10, 1), ready(wc, 20, 2), ready(wc, 30, 3)],
            sequential=False,
            dispatch_rank=1,
        )
        captured = []
        real = dispatch_plan_service.operation_run_order_update_statement

        def _spy(company_id, to_write):
            captured.append((company_id, dict(to_write)))
            return real(company_id, to_write)

        monkeypatch.setattr(dispatch_plan_service, "operation_run_order_update_statement", _spy)
        resp = client.put(
            operation_order_url(wo.id),
            json={"operation_ids": [x.id, z.id, y.id], "expected_operation_ids": [x.id, y.id, z.id]},
            headers=headers,
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert captured == [(COMPANY_A, {z.id: 2, y.id: 3})]
        db_session.expire_all()
        assert [db_session.get(WorkOrderOperation, op.id).run_order for op in (x, y, z)] == [1, 3, 2]


# --------------------------------------------------------------------------
# 4. The notice seam never expires the session
# --------------------------------------------------------------------------


class TestTheNoticeSeamNeverExpires:
    def test_record_dispatch_change_flushes_but_never_expires(self, db_session: Session, monkeypatch):
        manager = _manager(db_session)
        wc = make_work_center(db_session)
        _, (a,) = make_plan_wo(db_session, [ready(wc)])
        _, (b,) = make_plan_wo(db_session, [ready(wc)])
        before = dispatch_service.snapshot_queue_orders(db_session, COMPANY_A, [wc.id])

        # A pending, UN-flushed ORM write -- exactly what a move path holds.
        b_row = db_session.get(WorkOrderOperation, b.id)
        b_row.run_order = 1
        assert b_row in db_session.dirty

        expired = []
        monkeypatch.setattr(Session, "expire_all", lambda self: expired.append("expire_all"))
        monkeypatch.setattr(Session, "expire", lambda self, *args, **kwargs: expired.append("expire"))

        changed = dispatch_service.record_dispatch_change(
            db_session, COMPANY_A, manager, before, DispatchChangeKind.MACHINE_ORDER
        )
        # The seam flushed the pending write first, so its after-snapshot saw b move up.
        assert changed == [wc.id]
        assert expired == []
        assert b_row.run_order == 1
        db_session.rollback()


# --------------------------------------------------------------------------
# 5. Tenancy of the whole-list authority
# --------------------------------------------------------------------------


class TestPlanTenancy:
    def test_a_plan_save_never_touches_another_companys_plan(self, client: TestClient, db_session: Session):
        ensure_company(db_session, COMPANY_B)
        headers = user_headers(_manager(db_session))
        wc_a = make_work_center(db_session)
        wc_b = make_work_center(db_session, company_id=COMPANY_B)
        mine, _ = make_plan_wo(db_session, [ready(wc_a)], dispatch_rank=1)
        theirs, (their_op,) = make_plan_wo(
            db_session, [ready(wc_b, run_order=4)], sequential=False, dispatch_rank=1, company_id=COMPANY_B
        )

        # Clearing company A's whole plan is "every OTHER ranked work order leaves the
        # plan" -- of THIS company. Company B's plan and its within-plan order survive.
        resp = plan(client, headers, [], expected=[mine.id])
        assert resp.status_code == status.HTTP_200_OK, resp.text
        db_session.expire_all()
        assert db_session.get(WorkOrder, mine.id).dispatch_rank is None
        assert db_session.get(WorkOrder, theirs.id).dispatch_rank == 1
        assert db_session.get(WorkOrderOperation, their_op.id).run_order == 4

    def test_a_foreign_operation_in_the_column_compare_and_swap_is_400(self, client: TestClient, db_session: Session):
        """A foreign id must be indistinguishable from a missing one -- not quietly dropped
        as "left the queue since the render", which would make the CAS an existence oracle."""
        ensure_company(db_session, COMPANY_B)
        headers = user_headers(_manager(db_session))
        wc = make_work_center(db_session)
        _, (a,) = make_plan_wo(db_session, [ready(wc)])
        _, (foreign,) = make_plan_wo(
            db_session,
            [(make_work_center(db_session, company_id=COMPANY_B), OperationStatus.COMPLETE, 10)],
            company_id=COMPANY_B,
        )
        resp = client.put(
            run_order_url(wc.id),
            json={"operation_ids": [a.id], "expected_operation_ids": [a.id, foreign.id]},
            headers=headers,
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        assert str(foreign.id) in resp.json()["detail"]
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, a.id).run_order is None


# --------------------------------------------------------------------------
# 6. Plan positions count LIVE planned work orders only
# --------------------------------------------------------------------------


class TestPlanPositionsAreGapFreeOverLiveWork:
    def test_a_finished_or_deleted_planned_job_leaves_no_gap(self, client: TestClient, db_session: Session):
        """Completing or deleting a planned job does not rewrite anyone's rank (no write path
        does that), so the stored ranks go sparse. The positions every surface shows must not:
        the next live job reads plan position 1, not 3."""
        manager = _manager(db_session)
        headers = user_headers(manager)
        wc = make_work_center(db_session)
        done, _ = make_plan_wo(
            db_session, [(wc, OperationStatus.COMPLETE, 10)], status=WorkOrderStatus.COMPLETE, dispatch_rank=1
        )
        gone, _ = make_plan_wo(db_session, [ready(wc)], dispatch_rank=2)
        db_session.get(WorkOrder, gone.id).soft_delete(manager.id)
        db_session.commit()
        live, (live_op,) = make_plan_wo(db_session, [ready(wc)], dispatch_rank=3)
        later, (later_op,) = make_plan_wo(db_session, [ready(wc)], dispatch_rank=4)

        lanes = client.get(LANES_URL, headers=headers).json()
        assert [lane["work_order_id"] for lane in lanes["work_orders"][:2]] == [live.id, later.id]
        assert [lane["dispatch_position"] for lane in lanes["work_orders"][:2]] == [1, 2]
        assert lanes["ranked_count"] == 2
        ids = {lane["work_order_id"] for lane in lanes["work_orders"]}
        assert done.id not in ids and gone.id not in ids

        board = client.get(BOARD_URL, headers=headers).json()
        (column,) = [column for column in board["work_centers"] if column["id"] == wc.id]
        assert [(row["operation_id"], row["dispatch_position"], row["run_order"]) for row in column["queue"]] == [
            (live_op.id, 1, 1),
            (later_op.id, 2, 2),
        ]


# --------------------------------------------------------------------------
# 7. The lanes read does not reconcile
# --------------------------------------------------------------------------


class TestLanesReadDoesNotReconcile:
    def test_a_stranded_pool_operation_is_reported_not_promoted(self, client: TestClient, db_session: Session):
        """A pooled work order whose OP20 sits PENDING behind a READY OP10 at the same
        machine is exactly what the reconcile-on-read heal promotes. The lanes read must
        report it (not blocked) and leave it PENDING -- the board GET writes nothing."""
        manager = _manager(db_session)
        headers = user_headers(manager)
        wc = make_work_center(db_session)
        wo, (_, stranded) = make_plan_wo(
            db_session, [ready(wc, 10), (wc, OperationStatus.PENDING, 20)], sequential=False, dispatch_rank=1
        )

        body = client.get(LANES_URL, headers=headers).json()
        lane_op = {op["operation_id"]: op for op in lane_for(body, wo.id)["operations"]}[stranded.id]
        assert lane_op["status"] == "pending"
        assert lane_op["blocked_by_predecessor"] is False
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, stranded.id).status == OperationStatus.PENDING

        # Non-vacuity: this IS a case the reconciling read heals.
        assert client.get(f"/api/v1/work-orders/{wo.id}", headers=headers).status_code == status.HTTP_200_OK
        db_session.expire_all()
        assert db_session.get(WorkOrderOperation, stranded.id).status == OperationStatus.READY

    def test_an_inactive_machine_is_listed_only_while_a_lane_operation_sits_on_it(
        self, client: TestClient, db_session: Session
    ):
        headers = user_headers(_manager(db_session))
        occupied = make_work_center(db_session)
        empty = make_work_center(db_session)
        make_plan_wo(db_session, [ready(occupied)])
        for wc_id in (occupied.id, empty.id):
            db_session.get(WorkCenter, wc_id).is_active = False
        db_session.commit()

        options = {wc["id"]: wc for wc in client.get(LANES_URL, headers=headers).json()["work_centers"]}
        assert occupied.id in options and options[occupied.id]["is_active"] is False
        assert empty.id not in options


# --------------------------------------------------------------------------
# 8. RBAC: exactly the planner tier
# --------------------------------------------------------------------------


PLANNER_ROLES = (UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR)
REFUSED_ROLES = (UserRole.OPERATOR, UserRole.QUALITY, UserRole.SHIPPING, UserRole.VIEWER)


class TestPlannerTierOnly:
    @pytest.mark.parametrize("role", PLANNER_ROLES, ids=lambda role: role.value)
    def test_every_planner_role_may_read_and_write(self, client: TestClient, db_session: Session, role):
        headers = user_headers(make_user(db_session, role=role))
        wc = make_work_center(db_session)
        wo, (op10, op20) = make_plan_wo(db_session, [ready(wc, 10), ready(wc, 20)], sequential=False)

        assert client.get(LANES_URL, headers=headers).status_code == status.HTTP_200_OK
        resp = plan(client, headers, [wo.id], expected=[])
        assert resp.status_code == status.HTTP_200_OK, resp.text
        resp = client.put(
            operation_order_url(wo.id),
            json={"operation_ids": [op20.id, op10.id], "expected_operation_ids": [op10.id, op20.id]},
            headers=headers,
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text

    @pytest.mark.parametrize("role", REFUSED_ROLES, ids=lambda role: role.value)
    def test_every_other_role_is_refused_and_changes_nothing(self, client: TestClient, db_session: Session, role):
        headers = user_headers(make_user(db_session, role=role))
        wc = make_work_center(db_session)
        wo, (op10, op20) = make_plan_wo(db_session, [ready(wc, 10), ready(wc, 20)], sequential=False, dispatch_rank=1)

        assert client.get(LANES_URL, headers=headers).status_code == status.HTTP_403_FORBIDDEN
        resp = client.put(PLAN_URL, json={"work_order_ids": [], "expected_work_order_ids": [wo.id]}, headers=headers)
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        resp = client.put(
            operation_order_url(wo.id),
            json={"operation_ids": [op20.id, op10.id], "expected_operation_ids": [op10.id, op20.id]},
            headers=headers,
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        db_session.expire_all()
        assert db_session.get(WorkOrder, wo.id).dispatch_rank == 1
        assert [db_session.get(WorkOrderOperation, op.id).run_order for op in (op10, op20)] == [None, None]
