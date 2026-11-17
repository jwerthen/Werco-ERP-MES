"""POST /work-orders/bulk-release -- several DRAFT work orders released in one call.

The bulk door is the single release route's rule applied per item
(``work_order_release_service.apply_work_order_release``), so these tests pin:

* per-item results in REQUEST order (after de-duplication), HTTP 200 even when every
  item errors, the single route's own refusal texts, and ONE indistinguishable
  "Work order not found" for a foreign, soft-deleted or absent id (no existence oracle);
* every release is the single route's release: RELEASED + ``released_by`` / ``released_at``,
  first operation READY, one ``work_order_released`` event and one STATUS_CHANGE audit row
  per work order -- all in ONE commit;
* a per-item failure rolls back that item only (its own savepoint): no status change, no
  audit row, no event, and an event it emitted before failing is never enqueued;
* the lock order: each item locks its own rows -- operations, then the work order,
  ``FOR NO KEY UPDATE`` -- inside its own savepoint before its first write, and every audit
  row is written after the last item (the global audit-chain lock is the last lock taken);
* a lost lock race (40P01 / 40001) while locking one item fails THAT item only, with the
  stale-write retry text; the items before and after it still release, commit and audit;
* one tenant-scoped scheduling run over the union of released work orders, after the commit;
* request validation (1-50 positive ids, no extra keys) and the role gate.

Tests run on SQLite, which renders no ``FOR UPDATE``; the lock statements' Postgres shape
is asserted by dialect-compiling them, never by executing them.
"""

from typing import Any, Dict, List

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

import app.api.endpoints.work_orders as work_orders_endpoint
import app.services.notification_outbox as notification_outbox
import app.services.work_order_release_service as release_service
from app.db.mixins import STALE_WRITE_CONFLICT_DETAIL
from app.models.audit_log import AuditLog
from app.models.operational_event import OperationalEvent
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services.audit_service import AuditService
from tests.lean_phase1_helpers import (
    COMPANY_A,
    COMPANY_B,
    headers_for,
    make_op,
    make_part,
    make_user,
    make_wo,
    make_work_center,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

URL = "/api/v1/work-orders/bulk-release"


class _SchedulingSpy:
    """Stands in for ``SchedulingService`` in the router: records how it was built and run."""

    instances: List["_SchedulingSpy"] = []

    def __init__(self, db: Session, company_id: Any = None) -> None:
        self.db = db
        self.company_id = company_id
        self.calls: List[Dict[str, Any]] = []
        _SchedulingSpy.instances.append(self)

    def run_scheduling(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        return {"scheduled_count": 0}


@pytest.fixture
def scheduling_spy(monkeypatch):
    _SchedulingSpy.instances = []
    monkeypatch.setattr(work_orders_endpoint, "SchedulingService", _SchedulingSpy)
    return _SchedulingSpy


def _draft_with_ops(db: Session, *, company_id: int = COMPANY_A, work_centers=None) -> WorkOrder:
    """A DRAFT work order with one PENDING operation per given work center (a fresh one by default)."""
    part = make_part(db, company_id=company_id)
    wo = make_wo(db, part, company_id=company_id, status_=WorkOrderStatus.DRAFT)
    for index, wc in enumerate(work_centers or [make_work_center(db, company_id=company_id)]):
        make_op(db, wo, wc, company_id=company_id, sequence=(index + 1) * 10, status_=OperationStatus.PENDING)
    return wo


def _committed_release_audits(db: Session, work_order_id: int) -> List[AuditLog]:
    """STATUS_CHANGE rows that were COMMITTED (rollback first discards a flushed-only row)."""
    db.rollback()
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.resource_type == "work_order",
            AuditLog.resource_id == work_order_id,
            AuditLog.action == "STATUS_CHANGE",
        )
        .all()
    )


def _events(db: Session, work_order_id: int, event_type: str) -> List[OperationalEvent]:
    return (
        db.query(OperationalEvent)
        .filter(OperationalEvent.work_order_id == work_order_id, OperationalEvent.event_type == event_type)
        .all()
    )


def _first_op(db: Session, work_order_id: int) -> WorkOrderOperation:
    return (
        db.query(WorkOrderOperation)
        .filter(WorkOrderOperation.work_order_id == work_order_id)
        .order_by(WorkOrderOperation.sequence)
        .first()
    )


# --------------------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------------------


def test_bulk_release_releases_every_draft_in_one_commit(
    client: TestClient, db_session: Session, scheduling_spy, monkeypatch
):
    manager = make_user(db_session, role=UserRole.MANAGER)
    first, second, third = (_draft_with_ops(db_session) for _ in range(3))

    commits: List[int] = []
    real_commit = db_session.commit

    def _counting_commit():
        commits.append(1)
        return real_commit()

    monkeypatch.setattr(db_session, "commit", _counting_commit)

    # Deliberately NOT ascending: the response follows the request order.
    requested = [second.id, third.id, first.id]
    resp = client.post(URL, json={"work_order_ids": requested}, headers=headers_for(manager))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()

    assert set(body) == {"results", "released_count", "error_count"}
    assert body["released_count"] == 3 and body["error_count"] == 0
    assert [item["work_order_id"] for item in body["results"]] == requested
    numbers = {wo.id: wo.work_order_number for wo in (first, second, third)}
    for item in body["results"]:
        assert set(item) == {"work_order_id", "work_order_number", "status", "detail"}
        assert item["status"] == "released"
        assert item["detail"] is None
        assert item["work_order_number"] == numbers[item["work_order_id"]]

    # ONE commit carried every release (the scheduling spy never commits).
    assert len(commits) == 1

    for wo in (first, second, third):
        db_session.expire_all()
        stored = db_session.get(WorkOrder, wo.id)
        assert stored.status == WorkOrderStatus.RELEASED
        assert stored.released_by == manager.id
        assert stored.released_at is not None
        assert _first_op(db_session, wo.id).status == OperationStatus.READY

        released_events = _events(db_session, wo.id, "work_order_released")
        assert len(released_events) == 1
        assert released_events[0].event_payload["old_status"] == "draft"
        assert released_events[0].event_payload["new_status"] == "released"
        assert released_events[0].user_id == manager.id
        assert len(_events(db_session, wo.id, "operation_ready")) == 1

        rows = _committed_release_audits(db_session, wo.id)
        assert len(rows) == 1, "exactly one committed STATUS_CHANGE row per released work order"
        assert rows[0].old_values == {"status": "draft"}
        assert rows[0].new_values == {"status": "released"}
        assert rows[0].resource_identifier == wo.work_order_number
        assert rows[0].user_id == manager.id
        assert rows[0].company_id == COMPANY_A


def test_bulk_audit_row_matches_the_single_route_row(client: TestClient, db_session: Session, scheduling_spy):
    """The bulk door writes the single route's audit row, field for field."""
    admin = make_user(db_session, role=UserRole.ADMIN)
    via_single = _draft_with_ops(db_session)
    via_bulk = _draft_with_ops(db_session)

    assert client.post(f"/api/v1/work-orders/{via_single.id}/release", headers=headers_for(admin)).status_code == 200
    assert client.post(URL, json={"work_order_ids": [via_bulk.id]}, headers=headers_for(admin)).status_code == 200

    (single_row,) = _committed_release_audits(db_session, via_single.id)
    (bulk_row,) = _committed_release_audits(db_session, via_bulk.id)

    def _shape(row: AuditLog, number: str) -> Dict[str, Any]:
        return {
            "action": row.action,
            "resource_type": row.resource_type,
            "description": row.description.replace(number, "<WO>"),
            "old_values": row.old_values,
            "new_values": row.new_values,
            "extra_data": row.extra_data,
            "user_id": row.user_id,
            "company_id": row.company_id,
            "success": row.success,
        }

    assert _shape(bulk_row, via_bulk.work_order_number) == _shape(single_row, via_single.work_order_number)


# --------------------------------------------------------------------------------------
# Per-item refusals, tenant isolation and the existence oracle
# --------------------------------------------------------------------------------------


def test_bulk_release_reports_each_refusal_and_hides_existence(client: TestClient, db_session: Session, scheduling_spy):
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    good = _draft_with_ops(db_session)

    part = make_part(db_session)
    running = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS)
    make_op(db_session, running, make_work_center(db_session), status_=OperationStatus.IN_PROGRESS)
    no_operations = make_wo(db_session, part, status_=WorkOrderStatus.DRAFT)

    foreign = _draft_with_ops(db_session, company_id=COMPANY_B)

    deleted = _draft_with_ops(db_session)
    deleted.soft_delete(supervisor.id)
    db_session.commit()

    absent_id = max(wo.id for wo in (good, running, no_operations, foreign, deleted)) + 1000

    requested = [foreign.id, good.id, running.id, absent_id, no_operations.id, deleted.id]
    resp = client.post(URL, json={"work_order_ids": requested}, headers=headers_for(supervisor))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["released_count"] == 1 and body["error_count"] == 5

    results = {item["work_order_id"]: item for item in body["results"]}
    assert [item["work_order_id"] for item in body["results"]] == requested

    assert results[good.id]["status"] == "released"
    assert results[running.id] == {
        "work_order_id": running.id,
        "work_order_number": running.work_order_number,
        "status": "error",
        "detail": "Only draft work orders can be released",
    }
    assert results[no_operations.id] == {
        "work_order_id": no_operations.id,
        "work_order_number": no_operations.work_order_number,
        "status": "error",
        "detail": "Work order must have at least one operation",
    }
    # Foreign, soft-deleted and absent: one answer, no number -- no existence oracle.
    for hidden_id in (foreign.id, deleted.id, absent_id):
        assert results[hidden_id] == {
            "work_order_id": hidden_id,
            "work_order_number": None,
            "status": "error",
            "detail": "Work order not found",
        }

    db_session.expire_all()
    assert db_session.get(WorkOrder, good.id).status == WorkOrderStatus.RELEASED
    assert db_session.get(WorkOrder, running.id).status == WorkOrderStatus.IN_PROGRESS
    for untouched in (no_operations, foreign, deleted):
        stored = db_session.get(WorkOrder, untouched.id)
        assert stored.status == WorkOrderStatus.DRAFT
        assert stored.released_by is None and stored.released_at is None
    # The other company's operation was never promoted.
    assert _first_op(db_session, foreign.id).status == OperationStatus.PENDING

    # No audit row and no event for any failed item.
    for failed in (running, no_operations, foreign, deleted):
        assert _committed_release_audits(db_session, failed.id) == []
        assert _events(db_session, failed.id, "work_order_released") == []
        assert _events(db_session, failed.id, "operation_ready") == []
    assert len(_committed_release_audits(db_session, good.id)) == 1


def test_bulk_release_answers_200_when_every_item_errors(client: TestClient, db_session: Session, scheduling_spy):
    manager = make_user(db_session, role=UserRole.MANAGER)
    part = make_part(db_session)
    released_already = make_wo(db_session, part, status_=WorkOrderStatus.RELEASED)

    resp = client.post(URL, json={"work_order_ids": [released_already.id, 987654]}, headers=headers_for(manager))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["released_count"] == 0 and body["error_count"] == 2
    assert [item["status"] for item in body["results"]] == ["error", "error"]
    # Nothing was released, so nothing is scheduled.
    assert scheduling_spy.instances == []


def test_bulk_release_dedupes_keeping_first_occurrence(client: TestClient, db_session: Session, scheduling_spy):
    manager = make_user(db_session, role=UserRole.MANAGER)
    a, b, c = (_draft_with_ops(db_session) for _ in range(3))

    resp = client.post(URL, json={"work_order_ids": [b.id, a.id, b.id, a.id, c.id, b.id]}, headers=headers_for(manager))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert [item["work_order_id"] for item in body["results"]] == [b.id, a.id, c.id]
    assert body["released_count"] == 3 and body["error_count"] == 0
    for wo in (a, b, c):
        assert len(_committed_release_audits(db_session, wo.id)) == 1
        assert len(_events(db_session, wo.id, "work_order_released")) == 1


# --------------------------------------------------------------------------------------
# Validation and roles
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"work_order_ids": []},
        {"work_order_ids": list(range(1, 52))},  # 51 ids
        {"work_order_ids": [1, 0]},
        {"work_order_ids": [-3]},
        {"work_order_ids": [1], "release_all": True},  # extra="forbid"
        {"ids": [1]},
        {},
    ],
)
def test_bulk_release_rejects_an_invalid_body(client: TestClient, db_session: Session, body):
    manager = make_user(db_session, role=UserRole.MANAGER)
    resp = client.post(URL, json=body, headers=headers_for(manager))
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, resp.text


def test_bulk_release_accepts_exactly_fifty_ids(client: TestClient, db_session: Session, scheduling_spy):
    manager = make_user(db_session, role=UserRole.MANAGER)
    wo = _draft_with_ops(db_session)
    ids = [wo.id] + [900000 + n for n in range(49)]
    resp = client.post(URL, json={"work_order_ids": ids}, headers=headers_for(manager))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["released_count"] == 1 and resp.json()["error_count"] == 49


@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.VIEWER])
def test_bulk_release_refuses_floor_and_read_roles(client: TestClient, db_session: Session, role):
    user = make_user(db_session, role=role)
    wo = _draft_with_ops(db_session)
    resp = client.post(URL, json={"work_order_ids": [wo.id]}, headers=headers_for(user))
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    db_session.expire_all()
    assert db_session.get(WorkOrder, wo.id).status == WorkOrderStatus.DRAFT


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])
def test_bulk_release_allows_the_single_routes_roles(client: TestClient, db_session: Session, scheduling_spy, role):
    user = make_user(db_session, role=role)
    wo = _draft_with_ops(db_session)
    resp = client.post(URL, json={"work_order_ids": [wo.id]}, headers=headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["released_count"] == 1


# --------------------------------------------------------------------------------------
# Per-item savepoints
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error, expected_detail",
    [
        (SQLAlchemyError("SELECT secret FROM somewhere -- driver text"), release_service.BULK_RELEASE_RETRY_DETAIL),
        (StaleDataError("work_orders row changed"), STALE_WRITE_CONFLICT_DETAIL),
    ],
)
def test_a_database_failure_rolls_back_only_that_item(
    client: TestClient, db_session: Session, scheduling_spy, monkeypatch, error, expected_detail
):
    manager = make_user(db_session, role=UserRole.MANAGER)
    before, failing, after = (_draft_with_ops(db_session) for _ in range(3))

    real_promote = release_service.release_first_ready_operation

    def _promote(work_order, db=None, user_id=None):
        if work_order.id == failing.id:
            raise error
        return real_promote(work_order, db=db, user_id=user_id)

    monkeypatch.setattr(release_service, "release_first_ready_operation", _promote)

    resp = client.post(URL, json={"work_order_ids": [failing.id, before.id, after.id]}, headers=headers_for(manager))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["released_count"] == 2 and body["error_count"] == 1
    failed = body["results"][0]
    assert failed == {
        "work_order_id": failing.id,
        "work_order_number": failing.work_order_number,
        "status": "error",
        "detail": expected_detail,
    }
    assert "SELECT" not in resp.text and "driver text" not in resp.text

    db_session.expire_all()
    stored = db_session.get(WorkOrder, failing.id)
    assert stored.status == WorkOrderStatus.DRAFT
    assert stored.released_by is None and stored.released_at is None
    assert _first_op(db_session, failing.id).status == OperationStatus.PENDING
    assert _committed_release_audits(db_session, failing.id) == []
    assert _events(db_session, failing.id, "work_order_released") == []

    for ok in (before, after):
        db_session.expire_all()
        assert db_session.get(WorkOrder, ok.id).status == WorkOrderStatus.RELEASED
        assert len(_committed_release_audits(db_session, ok.id)) == 1

    # Only the released work orders were scheduled.
    (spy,) = scheduling_spy.instances
    assert spy.calls[0]["work_order_ids"] == sorted([before.id, after.id])


def test_an_event_emitted_inside_a_failed_item_is_never_enqueued(
    client: TestClient, db_session: Session, scheduling_spy, monkeypatch
):
    """The outbox enqueues only at the outermost commit; a rolled-back savepoint drops its ids.

    The failing item is the HIGHEST id, so it is processed last and no later event insert
    can reuse its rolled-back SQLite rowid.
    """
    manager = make_user(db_session, role=UserRole.MANAGER)
    ok = _draft_with_ops(db_session)
    failing = _draft_with_ops(db_session)
    assert failing.id > ok.id

    enqueued: List[int] = []
    monkeypatch.setattr(notification_outbox, "_enqueue_dispatch", enqueued.append)

    dropped: List[int] = []
    real_emit = release_service.emit_work_order_lifecycle_event

    def _emit_then_fail(db, **kwargs):
        real_emit(db, **kwargs)
        if kwargs["work_order"].id == failing.id:
            dropped.append(db.info["pending_notification_event_ids"][-1])
            raise SQLAlchemyError("boom after the event was flushed")

    monkeypatch.setattr(release_service, "emit_work_order_lifecycle_event", _emit_then_fail)

    resp = client.post(URL, json={"work_order_ids": [ok.id, failing.id]}, headers=headers_for(manager))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["released_count"] == 1

    assert dropped, "the failing item's work_order_released event was flushed before the failure"
    assert dropped[0] not in enqueued
    (ok_event,) = _events(db_session, ok.id, "work_order_released")
    assert ok_event.id in enqueued
    assert _events(db_session, failing.id, "work_order_released") == []


# --------------------------------------------------------------------------------------
# Lock order and audit buffering
# --------------------------------------------------------------------------------------


def test_each_item_locks_its_own_rows_in_its_savepoint_and_every_audit_comes_last(
    client: TestClient, db_session: Session, scheduling_spy, monkeypatch
):
    """Per item, inside its savepoint: operations, then the work order, then the release; audits after all."""
    manager = make_user(db_session, role=UserRole.MANAGER)
    wos = [_draft_with_ops(db_session) for _ in range(3)]
    trail: List[tuple] = []

    real_op_lock = release_service.operation_lock_query
    real_wo_lock = release_service.work_order_lock_query
    real_apply = release_service.apply_work_order_release
    real_log = AuditService.log

    def _op_lock(db, *, work_order_id, company_id):
        trail.append(("lock_operations", work_order_id, db.in_nested_transaction()))
        return real_op_lock(db, work_order_id=work_order_id, company_id=company_id)

    def _wo_lock(db, *, work_order_id, company_id):
        trail.append(("lock_work_order", work_order_id, db.in_nested_transaction()))
        return real_wo_lock(db, work_order_id=work_order_id, company_id=company_id)

    def _apply(db, *, work_order, **kwargs):
        trail.append(("release", work_order.id, db.in_nested_transaction()))
        return real_apply(db, work_order=work_order, **kwargs)

    def _log(self, action, resource_type, resource_id=None, *args, **kwargs):
        trail.append(("audit", resource_type, resource_id))
        return real_log(self, action, resource_type, resource_id, *args, **kwargs)

    monkeypatch.setattr(release_service, "operation_lock_query", _op_lock)
    monkeypatch.setattr(release_service, "work_order_lock_query", _wo_lock)
    monkeypatch.setattr(release_service, "apply_work_order_release", _apply)
    monkeypatch.setattr(AuditService, "log", _log)

    requested = [wos[2].id, wos[0].id, wos[1].id]
    resp = client.post(URL, json={"work_order_ids": requested}, headers=headers_for(manager))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["released_count"] == 3

    ascending = sorted(wo.id for wo in wos)
    expected: List[tuple] = []
    for wo_id in ascending:
        # Each item: its operations, then its work order, then its release -- all inside its own
        # savepoint, so a lost lock race on either lock rolls back with that item alone.
        expected += [("lock_operations", wo_id, True), ("lock_work_order", wo_id, True), ("release", wo_id, True)]
    # Every audit row -- of any kind -- after the last item: the global audit-chain lock is the last
    # lock the request takes. (Not asserted outside a savepoint: ``AuditService.log`` leaves its own
    # INSERT savepoint open on success, so from the second row on the session is nested anyway.)
    expected += [("audit", "work_order", wo_id) for wo_id in ascending]
    assert trail == expected


def test_lock_statements_are_no_key_update_ordered_tenant_scoped_and_join_free(db_session: Session):
    """Dialect-compiled for Postgres (SQLite renders no row-lock clause)."""
    operation_sql = str(
        release_service.operation_lock_query(db_session, work_order_id=5, company_id=COMPANY_A).statement.compile(
            dialect=postgresql.dialect()
        )
    )
    work_order_sql = str(
        release_service.work_order_lock_query(db_session, work_order_id=5, company_id=COMPANY_A).statement.compile(
            dialect=postgresql.dialect()
        )
    )

    for sql in (operation_sql, work_order_sql):
        # The strength the release's own UPDATEs take (no key column changes): it blocks every other
        # writer but not a concurrent INSERT's foreign-key KEY SHARE, which FOR UPDATE would queue on.
        assert sql.rstrip().endswith("FOR NO KEY UPDATE"), sql
        assert " JOIN " not in sql.upper(), sql  # a row lock on an outer join's nullable side fails on Postgres
        assert "is_deleted" in sql, sql
        assert "company_id" in sql, sql

    assert "ORDER BY work_order_operations.id ASC" in operation_sql
    assert operation_sql.startswith("SELECT work_order_operations.")
    assert work_order_sql.startswith("SELECT work_orders.")


class _LostRaceDriverError(Exception):
    """Stands in for the psycopg2 error Postgres raises on the side it rolls back to settle a lock race."""

    def __init__(self, pgcode: str) -> None:
        super().__init__("deadlock detected DETAIL: Process 4242 waits for ShareLock on transaction 99")
        self.pgcode = pgcode


@pytest.mark.parametrize(
    "losing_lock, pgcode",
    [
        # The reviewed cycle: the item holds the job's operations and loses waiting for its work order
        # to a work-order-first writer (the single release route, PUT, force-complete, scheduling).
        ("work_order_lock_query", "40P01"),
        ("operation_lock_query", "40P01"),
        ("work_order_lock_query", "40001"),
    ],
)
def test_a_lost_lock_race_while_locking_one_item_fails_only_that_item(
    client: TestClient, db_session: Session, scheduling_spy, monkeypatch, losing_lock, pgcode
):
    manager = make_user(db_session, role=UserRole.MANAGER)
    first, losing, third = (_draft_with_ops(db_session) for _ in range(3))
    assert first.id < losing.id < third.id, "the losing item is processed between the other two"
    numbers = {wo.id: wo.work_order_number for wo in (first, losing, third)}

    commits: List[int] = []
    real_commit = db_session.commit

    def _counting_commit():
        commits.append(1)
        return real_commit()

    monkeypatch.setattr(db_session, "commit", _counting_commit)

    reported: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        release_service,
        "capture_exception_to_sentry",
        lambda exc, *, context=None, fingerprint=None: reported.append({"context": context, "fp": fingerprint}),
    )

    raised: List[OperationalError] = []
    real_lock = getattr(release_service, losing_lock)

    def _lock(db, *, work_order_id, company_id):
        if work_order_id == losing.id:
            assert db.in_nested_transaction(), "the item's locks must be taken inside its own savepoint"
            exc = OperationalError("SELECT ... FOR NO KEY UPDATE", {}, _LostRaceDriverError(pgcode))
            raised.append(exc)
            raise exc
        return real_lock(db, work_order_id=work_order_id, company_id=company_id)

    monkeypatch.setattr(release_service, losing_lock, _lock)

    requested = [third.id, losing.id, first.id]
    resp = client.post(URL, json={"work_order_ids": requested}, headers=headers_for(manager))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert raised, "the simulated lost race was raised"
    body = resp.json()
    assert body["released_count"] == 2 and body["error_count"] == 1
    assert body["results"] == [
        {"work_order_id": third.id, "work_order_number": numbers[third.id], "status": "released", "detail": None},
        {
            "work_order_id": losing.id,
            "work_order_number": numbers[losing.id],
            "status": "error",
            "detail": STALE_WRITE_CONFLICT_DETAIL,
        },
        {"work_order_id": first.id, "work_order_number": numbers[first.id], "status": "released", "detail": None},
    ]
    # Never the driver's text.
    for leaked in ("deadlock", "Process 4242", "ShareLock", "FOR NO KEY UPDATE", pgcode):
        assert leaked not in resp.text
    # Reported like the app-wide lost-race 409: a lost race is a lock-order symptom, not noise.
    assert reported == [
        {
            "context": {
                "lost_lock_race": True,
                "pgcode": pgcode,
                "work_order_id": losing.id,
                "company_id": COMPANY_A,
            },
            "fp": ["lost-lock-race", "bulk-release-item", pgcode],
        }
    ]

    # ONE commit carried both survivors.
    assert len(commits) == 1

    # The losing item: nothing of it was saved.
    db_session.expire_all()
    stored = db_session.get(WorkOrder, losing.id)
    assert stored.status == WorkOrderStatus.DRAFT
    assert stored.released_by is None and stored.released_at is None
    assert _first_op(db_session, losing.id).status == OperationStatus.PENDING
    assert _events(db_session, losing.id, "work_order_released") == []
    assert _events(db_session, losing.id, "operation_ready") == []
    assert _committed_release_audits(db_session, losing.id) == []

    # The items before AND after it: released, committed, audited.
    for ok in (first, third):
        db_session.expire_all()
        stored = db_session.get(WorkOrder, ok.id)
        assert stored.status == WorkOrderStatus.RELEASED
        assert stored.released_by == manager.id
        assert _first_op(db_session, ok.id).status == OperationStatus.READY
        assert len(_events(db_session, ok.id, "work_order_released")) == 1
        rows = _committed_release_audits(db_session, ok.id)
        assert len(rows) == 1
        assert rows[0].old_values == {"status": "draft"} and rows[0].new_values == {"status": "released"}

    (spy,) = scheduling_spy.instances
    assert spy.calls[0]["work_order_ids"] == [first.id, third.id]


def test_a_target_deleted_before_its_lock_is_not_found_without_a_number(
    client: TestClient, db_session: Session, scheduling_spy, monkeypatch
):
    """The unlocked pre-read saw it live; the locked re-read under the item's savepoint does not."""
    manager = make_user(db_session, role=UserRole.MANAGER)
    ok, vanishing = (_draft_with_ops(db_session) for _ in range(2))

    real_wo_lock = release_service.work_order_lock_query

    def _wo_lock(db, *, work_order_id, company_id):
        if work_order_id == vanishing.id:
            # A concurrent soft delete landing between the unlocked read and this item's lock.
            db.execute(update(WorkOrder).where(WorkOrder.id == work_order_id).values(is_deleted=True))
        return real_wo_lock(db, work_order_id=work_order_id, company_id=company_id)

    monkeypatch.setattr(release_service, "work_order_lock_query", _wo_lock)

    resp = client.post(URL, json={"work_order_ids": [vanishing.id, ok.id]}, headers=headers_for(manager))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["released_count"] == 1 and body["error_count"] == 1
    # The same answer as any other foreign / deleted / absent id: no number, no existence oracle.
    assert body["results"][0] == {
        "work_order_id": vanishing.id,
        "work_order_number": None,
        "status": "error",
        "detail": "Work order not found",
    }
    assert body["results"][1]["status"] == "released"

    db_session.expire_all()
    stored = db_session.get(WorkOrder, vanishing.id)
    assert stored.status == WorkOrderStatus.DRAFT and stored.released_at is None
    assert _committed_release_audits(db_session, vanishing.id) == []
    assert _events(db_session, vanishing.id, "work_order_released") == []
    assert len(_committed_release_audits(db_session, ok.id)) == 1


# --------------------------------------------------------------------------------------
# Scheduling and broadcasts after the commit
# --------------------------------------------------------------------------------------


def test_one_tenant_scoped_scheduling_run_over_the_union(
    client: TestClient, db_session: Session, scheduling_spy, monkeypatch
):
    manager = make_user(db_session, role=UserRole.MANAGER)
    shared = make_work_center(db_session)
    own_a = make_work_center(db_session)
    own_b = make_work_center(db_session)
    wo_a = _draft_with_ops(db_session, work_centers=[shared, own_a])
    wo_b = _draft_with_ops(db_session, work_centers=[shared, own_b])
    refused = make_wo(db_session, make_part(db_session), status_=WorkOrderStatus.DRAFT)  # no operation

    commits_at_scheduling: List[int] = []
    commits: List[int] = []
    real_commit = db_session.commit

    def _counting_commit():
        commits.append(1)
        return real_commit()

    monkeypatch.setattr(db_session, "commit", _counting_commit)
    real_run = _SchedulingSpy.run_scheduling

    def _run(self, **kwargs):
        commits_at_scheduling.append(len(commits))
        return real_run(self, **kwargs)

    monkeypatch.setattr(_SchedulingSpy, "run_scheduling", _run)

    broadcasts: List[tuple] = []

    def _broadcast(coro, *args, **kwargs):
        broadcasts.append((coro.__name__, args, kwargs))

    monkeypatch.setattr(work_orders_endpoint, "safe_broadcast", _broadcast)

    resp = client.post(URL, json={"work_order_ids": [wo_b.id, refused.id, wo_a.id]}, headers=headers_for(manager))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    (spy,) = scheduling_spy.instances
    assert spy.company_id == COMPANY_A
    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call["work_order_ids"] == sorted([wo_a.id, wo_b.id])
    assert sorted(call["work_center_ids"]) == sorted({shared.id, own_a.id, own_b.id})
    assert call["horizon_days"] == 90 and call["optimize_setup"] is False
    # Scheduling ran AFTER the one commit.
    assert commits_at_scheduling == [1]

    by_kind: Dict[str, List[tuple]] = {}
    for name, args, kwargs in broadcasts:
        assert kwargs == {"company_id": COMPANY_A}
        by_kind.setdefault(name, []).append(args)
    assert sorted(args[0] for args in by_kind["broadcast_work_order_update"]) == sorted([wo_a.id, wo_b.id])
    for args in by_kind["broadcast_work_order_update"]:
        assert args[1] == {"event": "work_order_released", "status": "released"}
    assert sorted(args[0]["work_order_id"] for args in by_kind["broadcast_dashboard_update"]) == sorted(
        [wo_a.id, wo_b.id]
    )
    # One shop-floor push per DISTINCT work center, naming the released jobs there.
    shop_floor = {args[0]: args[1] for args in by_kind["broadcast_shop_floor_update"]}
    assert len(by_kind["broadcast_shop_floor_update"]) == 3
    assert shop_floor[shared.id]["work_order_ids"] == sorted([wo_a.id, wo_b.id])
    assert shop_floor[own_a.id]["work_order_ids"] == [wo_a.id]
    assert shop_floor[own_b.id]["work_order_ids"] == [wo_b.id]


def test_a_scheduling_failure_after_the_commit_still_answers_200(client: TestClient, db_session: Session, monkeypatch):
    """The releases are durable once committed; a scheduling crash must not report them as failed."""
    manager = make_user(db_session, role=UserRole.MANAGER)
    wo = _draft_with_ops(db_session)

    class _BrokenScheduling:
        def __init__(self, db, company_id=None):
            pass

        def run_scheduling(self, **kwargs):
            raise RuntimeError("scheduler down")

    monkeypatch.setattr(work_orders_endpoint, "SchedulingService", _BrokenScheduling)

    resp = client.post(URL, json={"work_order_ids": [wo.id]}, headers=headers_for(manager))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["released_count"] == 1
    db_session.expire_all()
    assert db_session.get(WorkOrder, wo.id).status == WorkOrderStatus.RELEASED
    assert len(_committed_release_audits(db_session, wo.id)) == 1


# --------------------------------------------------------------------------------------
# The single route, through the shared service
# --------------------------------------------------------------------------------------


def test_single_route_keeps_its_texts_and_codes(client: TestClient, db_session: Session):
    manager = make_user(db_session, role=UserRole.MANAGER)
    foreign = _draft_with_ops(db_session, company_id=COMPANY_B)
    deleted = _draft_with_ops(db_session)
    deleted.soft_delete(manager.id)
    db_session.commit()
    running = make_wo(db_session, make_part(db_session), status_=WorkOrderStatus.IN_PROGRESS)
    empty = make_wo(db_session, make_part(db_session), status_=WorkOrderStatus.DRAFT)

    for hidden in (foreign.id, deleted.id, 987654):
        resp = client.post(f"/api/v1/work-orders/{hidden}/release", headers=headers_for(manager))
        assert resp.status_code == status.HTTP_404_NOT_FOUND
        assert resp.json()["detail"] == "Work order not found"

    resp = client.post(f"/api/v1/work-orders/{running.id}/release", headers=headers_for(manager))
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json()["detail"] == "Only draft work orders can be released"

    resp = client.post(f"/api/v1/work-orders/{empty.id}/release", headers=headers_for(manager))
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json()["detail"] == "Work order must have at least one operation"
