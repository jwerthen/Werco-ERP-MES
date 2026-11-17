"""The two release doors after the extraction into ``work_order_release_service``.

``POST /work-orders/{id}/release`` was rewritten to delegate to the service the bulk door
shares, and the work-order lifecycle emitter moved out of the router. These tests pin what
``test_work_order_bulk_release.py`` leaves open:

* the single route END TO END, as it behaved before the extraction: 200 + the work-order
  body, the first operation READY, one committed STATUS_CHANGE row, the stored
  ``work_order_released`` event column for column (``source_module`` still
  ``"work_orders"`` after the emitter moved), one scheduling run AFTER the one commit
  (constructed ``SchedulingService(db)`` -- no company -- exactly as before), and its three
  broadcasts;
* the bulk door's stored event is the single route's, column for column;
* tenant isolation from the OTHER side: a company-B caller touches only company B, and
  every row it writes (status, audit, event) and the scheduling run say company B;
* a platform admin switched into another company releases THAT company's work orders and
  stamps its audit rows with it -- the active company, never the home one;
* the role matrix over every ``UserRole``, both doors, against the frontend's
  ``userCanReleaseWorkOrders`` gate (ADMIN / MANAGER / SUPERVISOR / PLATFORM_ADMIN, or any
  role with ``is_superuser``);
* repeats count toward the 50-id cap before de-duplication (the documented contract);
* the opt-in estimated-cost rollup still runs per released work order on both doors;
* a standing API token's bulk release attributes every audit row to the credential, and a
  read-only token is refused before anything is released;
* a lost lock race at the DEFERRED audit rows (after every item's savepoint) fails the whole
  request with the app-wide 409 and saves nothing -- judged with an explicit outer BEGIN, because
  on pysqlite a savepoint opened outside a transaction COMMITS on release.
"""

from typing import Any, Dict, List

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

import app.api.endpoints.work_orders as work_orders_endpoint
import app.services.notification_outbox as notification_outbox
import app.services.work_order_release_service as release_service
from app.core.config import settings
from app.core.security import create_access_token
from app.db.mixins import STALE_WRITE_CONFLICT_DETAIL
from app.models.audit_log import AuditLog
from app.models.operational_event import OperationalEvent
from app.models.user import User, UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
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

BULK_URL = "/api/v1/work-orders/bulk-release"
API_TOKENS_URL = "/api/v1/api-tokens/"


def _single_url(work_order_id: int) -> str:
    return f"/api/v1/work-orders/{work_order_id}/release"


class _SchedulingSpy:
    """Stands in for ``SchedulingService`` in the router: records how it was built and run."""

    instances: List["_SchedulingSpy"] = []

    def __init__(self, db: Session, company_id: Any = None) -> None:
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


@pytest.fixture
def broadcasts(monkeypatch) -> List[tuple]:
    """Every ``safe_broadcast`` the router makes, as (broadcaster name, args, kwargs)."""
    recorded: List[tuple] = []

    def _broadcast(coro, *args, **kwargs):
        recorded.append((coro.__name__, args, kwargs))

    monkeypatch.setattr(work_orders_endpoint, "safe_broadcast", _broadcast)
    return recorded


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


def _ops_by_sequence(db: Session, work_order_id: int) -> List[WorkOrderOperation]:
    db.expire_all()
    return (
        db.query(WorkOrderOperation)
        .filter(WorkOrderOperation.work_order_id == work_order_id)
        .order_by(WorkOrderOperation.sequence)
        .all()
    )


def _assert_untouched_draft(db: Session, wo: WorkOrder) -> None:
    db.expire_all()
    stored = db.get(WorkOrder, wo.id)
    assert stored.status == WorkOrderStatus.DRAFT
    assert stored.released_by is None and stored.released_at is None
    assert all(op.status == OperationStatus.PENDING for op in _ops_by_sequence(db, wo.id))
    assert _committed_release_audits(db, wo.id) == []
    assert _events(db, wo.id, "work_order_released") == []
    assert _events(db, wo.id, "operation_ready") == []


def _event_shape(event: OperationalEvent, number: str) -> Dict[str, Any]:
    payload = dict(event.event_payload or {})
    payload["work_order_number"] = payload.get("work_order_number", "").replace(number, "<WO>")
    return {
        "company_id": event.company_id,
        "event_type": event.event_type,
        "source_module": event.source_module,
        "entity_type": event.entity_type,
        "severity": event.severity,
        "user_id": event.user_id,
        "operation_id": event.operation_id,
        "payload": payload,
    }


# --------------------------------------------------------------------------------------
# The single route, end to end
# --------------------------------------------------------------------------------------


def test_single_route_release_is_unchanged_end_to_end(
    client: TestClient, db_session: Session, scheduling_spy, broadcasts, monkeypatch
):
    manager = make_user(db_session, role=UserRole.MANAGER)
    wc_first = make_work_center(db_session)
    wc_second = make_work_center(db_session)
    wo = _draft_with_ops(db_session, work_centers=[wc_first, wc_second])

    commits: List[int] = []
    real_commit = db_session.commit

    def _counting_commit():
        commits.append(1)
        return real_commit()

    monkeypatch.setattr(db_session, "commit", _counting_commit)
    commits_at_scheduling: List[int] = []
    real_run = _SchedulingSpy.run_scheduling

    def _run(self, **kwargs):
        commits_at_scheduling.append(len(commits))
        return real_run(self, **kwargs)

    monkeypatch.setattr(_SchedulingSpy, "run_scheduling", _run)

    resp = client.post(_single_url(wo.id), headers=headers_for(manager))
    assert resp.status_code == status.HTTP_200_OK, resp.text

    # The response is still the work order itself (WorkOrderResponse), not a bulk-style result.
    body = resp.json()
    assert body["id"] == wo.id
    assert body["work_order_number"] == wo.work_order_number
    assert body["status"] == "released"
    assert [op["status"] for op in sorted(body["operations"], key=lambda op: op["sequence"])] == ["ready", "pending"]

    # One commit, and scheduling ran after it.
    assert commits_at_scheduling == [1]
    (spy,) = scheduling_spy.instances
    # Characterization of the PRE-extraction call, which the extraction left alone: the
    # single route builds SchedulingService(db) with no company (the bulk door passes one).
    assert spy.company_id is None
    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call["work_order_ids"] == [wo.id]
    assert sorted(call["work_center_ids"]) == sorted([wc_first.id, wc_second.id])
    assert call["horizon_days"] == 90 and call["optimize_setup"] is False

    # The stored release.
    db_session.expire_all()
    stored = db_session.get(WorkOrder, wo.id)
    assert stored.status == WorkOrderStatus.RELEASED
    assert stored.released_by == manager.id
    assert stored.released_at is not None
    assert [op.status for op in _ops_by_sequence(db_session, wo.id)] == [OperationStatus.READY, OperationStatus.PENDING]

    (row,) = _committed_release_audits(db_session, wo.id)
    assert row.old_values == {"status": "draft"} and row.new_values == {"status": "released"}
    assert row.resource_identifier == wo.work_order_number
    assert row.user_id == manager.id and row.company_id == COMPANY_A
    assert row.success == "true"  # the column is a string flag

    # The event the moved emitter writes, column for column as the router wrote it.
    (event,) = _events(db_session, wo.id, "work_order_released")
    assert event.company_id == COMPANY_A
    assert event.source_module == "work_orders"
    assert event.entity_type == "work_order"
    assert event.entity_id == wo.id
    assert event.user_id == manager.id
    assert event.severity == "info"
    assert event.operation_id is None
    assert event.event_payload == {
        "work_order_number": wo.work_order_number,
        "status": "released",
        "old_status": "draft",
        "new_status": "released",
    }
    assert len(_events(db_session, wo.id, "operation_ready")) == 1

    # Its three broadcasts, company-scoped; the shop-floor push names ONE work order per work center.
    by_kind: Dict[str, List[tuple]] = {}
    for name, args, kwargs in broadcasts:
        assert kwargs == {"company_id": COMPANY_A}
        by_kind.setdefault(name, []).append(args)
    assert by_kind["broadcast_work_order_update"] == [(wo.id, {"event": "work_order_released", "status": "released"})]
    assert by_kind["broadcast_dashboard_update"] == [
        ({"event": "work_order_released", "work_order_id": wo.id, "status": "released"},)
    ]
    assert sorted(by_kind["broadcast_shop_floor_update"]) == sorted(
        [
            (wc_first.id, {"event": "work_order_released", "work_order_id": wo.id}),
            (wc_second.id, {"event": "work_order_released", "work_order_id": wo.id}),
        ]
    )


def test_bulk_event_row_matches_the_single_route_event_row(client: TestClient, db_session: Session, scheduling_spy):
    """Both doors call one emitter; the stored ``work_order_released`` rows must not drift apart."""
    supervisor = make_user(db_session, role=UserRole.SUPERVISOR)
    via_single = _draft_with_ops(db_session)
    via_bulk = _draft_with_ops(db_session)

    assert client.post(_single_url(via_single.id), headers=headers_for(supervisor)).status_code == 200
    assert (
        client.post(BULK_URL, json={"work_order_ids": [via_bulk.id]}, headers=headers_for(supervisor)).status_code
        == 200
    )

    (single_event,) = _events(db_session, via_single.id, "work_order_released")
    (bulk_event,) = _events(db_session, via_bulk.id, "work_order_released")
    assert bulk_event.entity_id == via_bulk.id
    assert _event_shape(bulk_event, via_bulk.work_order_number) == _event_shape(
        single_event, via_single.work_order_number
    )
    assert _event_shape(bulk_event, via_bulk.work_order_number)["source_module"] == "work_orders"


# --------------------------------------------------------------------------------------
# Tenant isolation and the active company
# --------------------------------------------------------------------------------------


def test_company_b_caller_releases_only_company_b_and_stamps_b_everywhere(
    client: TestClient, db_session: Session, scheduling_spy, broadcasts
):
    supervisor_b = make_user(db_session, role=UserRole.SUPERVISOR, company_id=COMPANY_B)
    wc_b = make_work_center(db_session, company_id=COMPANY_B)
    own_1 = _draft_with_ops(db_session, company_id=COMPANY_B, work_centers=[wc_b])
    own_2 = _draft_with_ops(db_session, company_id=COMPANY_B, work_centers=[wc_b])
    wc_a = make_work_center(db_session, company_id=COMPANY_A)
    foreign = _draft_with_ops(db_session, company_id=COMPANY_A, work_centers=[wc_a])

    resp = client.post(
        BULK_URL, json={"work_order_ids": [foreign.id, own_2.id, own_1.id]}, headers=headers_for(supervisor_b)
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["released_count"] == 2 and body["error_count"] == 1
    assert body["results"][0] == {
        "work_order_id": foreign.id,
        "work_order_number": None,
        "status": "error",
        "detail": "Work order not found",
    }
    # The other company's work order number never reaches this caller.
    assert foreign.work_order_number not in resp.text

    _assert_untouched_draft(db_session, foreign)

    for own in (own_1, own_2):
        db_session.expire_all()
        stored = db_session.get(WorkOrder, own.id)
        assert stored.status == WorkOrderStatus.RELEASED and stored.released_by == supervisor_b.id
        (row,) = _committed_release_audits(db_session, own.id)
        assert row.company_id == COMPANY_B
        (event,) = _events(db_session, own.id, "work_order_released")
        assert event.company_id == COMPANY_B
        (ready,) = _events(db_session, own.id, "operation_ready")
        assert ready.company_id == COMPANY_B

    (spy,) = scheduling_spy.instances
    assert spy.company_id == COMPANY_B
    assert spy.calls[0]["work_order_ids"] == sorted([own_1.id, own_2.id])
    assert spy.calls[0]["work_center_ids"] == [wc_b.id]

    by_kind: Dict[str, List[tuple]] = {}
    for name, args, kwargs in broadcasts:
        assert kwargs == {"company_id": COMPANY_B}
        by_kind.setdefault(name, []).append(args)
    # Only company B's released work orders, on company B's channel; nothing for company A's id.
    assert sorted(args[0] for args in by_kind["broadcast_work_order_update"]) == sorted([own_1.id, own_2.id])
    assert sorted(args[0]["work_order_id"] for args in by_kind["broadcast_dashboard_update"]) == sorted(
        [own_1.id, own_2.id]
    )
    assert by_kind["broadcast_shop_floor_update"] == [
        (wc_b.id, {"event": "work_order_released", "work_order_ids": sorted([own_1.id, own_2.id])})
    ]


def test_platform_admin_releases_in_the_switched_company_not_the_home_one(
    client: TestClient, db_session: Session, scheduling_spy
):
    """The scope is ``get_current_company_id`` (the active company), never ``current_user.company_id``."""
    platform_admin = make_user(db_session, role=UserRole.PLATFORM_ADMIN, company_id=COMPANY_A)
    switched = {
        "Authorization": f"Bearer {create_access_token(subject=platform_admin.id, company_id=COMPANY_B)}",
        "X-Requested-With": "XMLHttpRequest",
    }
    home_draft = _draft_with_ops(db_session, company_id=COMPANY_A)
    viewed_draft = _draft_with_ops(db_session, company_id=COMPANY_B)
    viewed_single = _draft_with_ops(db_session, company_id=COMPANY_B)

    resp = client.post(BULK_URL, json={"work_order_ids": [home_draft.id, viewed_draft.id]}, headers=switched)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    results = {item["work_order_id"]: item for item in resp.json()["results"]}
    assert results[viewed_draft.id]["status"] == "released"
    # The admin's HOME company's draft is out of scope while switched away from it.
    assert results[home_draft.id] == {
        "work_order_id": home_draft.id,
        "work_order_number": None,
        "status": "error",
        "detail": "Work order not found",
    }
    _assert_untouched_draft(db_session, home_draft)

    (row,) = _committed_release_audits(db_session, viewed_draft.id)
    assert row.company_id == COMPANY_B and row.user_id == platform_admin.id
    (event,) = _events(db_session, viewed_draft.id, "work_order_released")
    assert event.company_id == COMPANY_B
    (spy,) = scheduling_spy.instances
    assert spy.company_id == COMPANY_B

    # The single door judges the same scope: the home company's draft is a 404 while switched.
    assert client.post(_single_url(home_draft.id), headers=switched).status_code == status.HTTP_404_NOT_FOUND
    _assert_untouched_draft(db_session, home_draft)
    assert client.post(_single_url(viewed_single.id), headers=switched).status_code == status.HTTP_200_OK
    (single_row,) = _committed_release_audits(db_session, viewed_single.id)
    assert single_row.company_id == COMPANY_B and single_row.user_id == platform_admin.id


# --------------------------------------------------------------------------------------
# The role matrix (mirrors frontend/src/utils/permissions.ts::userCanReleaseWorkOrders)
# --------------------------------------------------------------------------------------

_RELEASING_ROLES = {UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.PLATFORM_ADMIN}


def _release_via(client: TestClient, door: str, work_order_id: int, headers: dict):
    if door == "single":
        return client.post(_single_url(work_order_id), headers=headers)
    return client.post(BULK_URL, json={"work_order_ids": [work_order_id]}, headers=headers)


@pytest.mark.parametrize("door", ["single", "bulk"])
@pytest.mark.parametrize("role", list(UserRole), ids=lambda role: role.value)
def test_every_role_on_both_doors_matches_the_frontend_release_gate(
    client: TestClient, db_session: Session, scheduling_spy, door, role
):
    """ADMIN / MANAGER / SUPERVISOR by role, PLATFORM_ADMIN through require_role; every other role 403."""
    user = make_user(db_session, role=role)
    wo = _draft_with_ops(db_session)

    resp = _release_via(client, door, wo.id, headers_for(user))

    if role in _RELEASING_ROLES:
        assert resp.status_code == status.HTTP_200_OK, resp.text
        db_session.expire_all()
        assert db_session.get(WorkOrder, wo.id).status == WorkOrderStatus.RELEASED
    else:
        assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
        _assert_untouched_draft(db_session, wo)


@pytest.mark.parametrize("door", ["single", "bulk"])
@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.VIEWER, UserRole.QUALITY, UserRole.SHIPPING])
def test_a_superuser_releases_whatever_its_role(client: TestClient, db_session: Session, scheduling_spy, door, role):
    """require_role waves ``is_superuser`` through; the frontend gate offers it Release for the same reason."""
    user = make_user(db_session, role=role)
    user.is_superuser = True
    db_session.commit()
    wo = _draft_with_ops(db_session)

    resp = _release_via(client, door, wo.id, headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    db_session.expire_all()
    stored = db_session.get(WorkOrder, wo.id)
    assert stored.status == WorkOrderStatus.RELEASED and stored.released_by == user.id
    (row,) = _committed_release_audits(db_session, wo.id)
    assert row.user_id == user.id and row.company_id == COMPANY_A


# --------------------------------------------------------------------------------------
# Request contract
# --------------------------------------------------------------------------------------


def test_repeats_count_toward_the_fifty_id_cap_before_dedupe(client: TestClient, db_session: Session, scheduling_spy):
    manager = make_user(db_session, role=UserRole.MANAGER)
    wo = _draft_with_ops(db_session)

    too_many = client.post(BULK_URL, json={"work_order_ids": [wo.id] * 51}, headers=headers_for(manager))
    assert too_many.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, too_many.text
    _assert_untouched_draft(db_session, wo)

    at_cap = client.post(BULK_URL, json={"work_order_ids": [wo.id] * 50}, headers=headers_for(manager))
    assert at_cap.status_code == status.HTTP_200_OK, at_cap.text
    body = at_cap.json()
    assert body["released_count"] == 1 and body["error_count"] == 0
    assert [item["work_order_id"] for item in body["results"]] == [wo.id]
    assert len(_committed_release_audits(db_session, wo.id)) == 1


# --------------------------------------------------------------------------------------
# The opt-in estimated-cost rollup rides the shared release
# --------------------------------------------------------------------------------------


def test_estimated_cost_rollup_runs_once_per_released_work_order_on_both_doors(
    client: TestClient, db_session: Session, scheduling_spy, monkeypatch
):
    monkeypatch.setattr(settings, "LABOR_COST_ROLLUP_ENABLED", True)
    computed: List[tuple] = []

    def _compute(db, work_order, company_id):
        computed.append((work_order.id, company_id))

    monkeypatch.setattr(release_service, "compute_and_store_estimated_cost", _compute)

    manager = make_user(db_session, role=UserRole.MANAGER)
    single = _draft_with_ops(db_session)
    bulk_a = _draft_with_ops(db_session)
    bulk_b = _draft_with_ops(db_session)
    refused = make_wo(db_session, make_part(db_session), status_=WorkOrderStatus.DRAFT)  # no operation

    assert client.post(_single_url(single.id), headers=headers_for(manager)).status_code == 200
    assert computed == [(single.id, COMPANY_A)]

    computed.clear()
    resp = client.post(
        BULK_URL, json={"work_order_ids": [bulk_b.id, refused.id, bulk_a.id]}, headers=headers_for(manager)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["released_count"] == 2
    # Ascending processing order; never for the refused item.
    assert computed == [(bulk_a.id, COMPANY_A), (bulk_b.id, COMPANY_A)]


def test_estimated_cost_rollup_stays_off_by_default(
    client: TestClient, db_session: Session, scheduling_spy, monkeypatch
):
    monkeypatch.setattr(settings, "LABOR_COST_ROLLUP_ENABLED", False)
    computed: List[int] = []
    monkeypatch.setattr(
        release_service, "compute_and_store_estimated_cost", lambda db, wo, company_id: computed.append(wo.id)
    )
    manager = make_user(db_session, role=UserRole.MANAGER)
    wo = _draft_with_ops(db_session)
    assert client.post(BULK_URL, json={"work_order_ids": [wo.id]}, headers=headers_for(manager)).status_code == 200
    assert computed == []


# --------------------------------------------------------------------------------------
# Standing API tokens
# --------------------------------------------------------------------------------------


def _mint(client: TestClient, admin_headers: dict, user: User, **extra) -> tuple:
    issued = client.post(
        API_TOKENS_URL, json={"user_id": user.id, "label": "release bot", **extra}, headers=admin_headers
    )
    assert issued.status_code == 201, issued.text
    body = issued.json()
    return {"Authorization": f"Bearer {body['token']}", "X-Requested-With": "XMLHttpRequest"}, body["id"]


def test_api_token_bulk_release_attributes_every_audit_row_to_the_credential(
    client: TestClient, db_session: Session, scheduling_spy, admin_headers
):
    """Audit rows are written after the loop by one AuditService; each still carries the token marker."""
    manager = make_user(db_session, role=UserRole.MANAGER)
    token_headers, token_id = _mint(client, admin_headers, manager)
    first, second = _draft_with_ops(db_session), _draft_with_ops(db_session)

    resp = client.post(BULK_URL, json={"work_order_ids": [first.id, second.id]}, headers=token_headers)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["released_count"] == 2

    for wo in (first, second):
        (row,) = _committed_release_audits(db_session, wo.id)
        assert row.user_id == manager.id
        credential = (row.extra_data or {}).get("credential")
        assert credential is not None, row.extra_data
        assert credential["kind"] == "api_token" and credential["api_token_id"] == token_id


def test_read_only_api_token_cannot_bulk_release(
    client: TestClient, db_session: Session, scheduling_spy, admin_headers
):
    manager = make_user(db_session, role=UserRole.MANAGER)
    token_headers, _token_id = _mint(client, admin_headers, manager, scope="read_only")
    wo = _draft_with_ops(db_session)

    resp = client.post(BULK_URL, json={"work_order_ids": [wo.id]}, headers=token_headers)
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    _assert_untouched_draft(db_session, wo)
    assert scheduling_spy.instances == []


# --------------------------------------------------------------------------------------
# A failure AFTER the per-item phase fails the whole request
# --------------------------------------------------------------------------------------


class _LostRaceDriverError(Exception):
    """Stands in for the psycopg2 error Postgres raises on the side it rolls back to settle a lock race."""

    def __init__(self, pgcode: str) -> None:
        super().__init__("deadlock detected DETAIL: Process 4242 waits for ShareLock on transaction 99")
        self.pgcode = pgcode


@pytest.mark.parametrize("pgcode", ["40P01", "40001"])
def test_a_lost_race_at_the_deferred_audit_rows_fails_the_whole_request_and_saves_nothing(
    client: TestClient, db_session: Session, scheduling_spy, broadcasts, monkeypatch, pgcode
):
    """The audit rows are written after every item, outside any item's savepoint, under the global chain
    lock. A lost race there is not one item's failure: the request answers the app-wide 409, commits
    nothing, schedules nothing, broadcasts nothing and enqueues no notification -- even for the items
    whose release had already been written."""
    manager = make_user(db_session, role=UserRole.MANAGER)
    first, second = _draft_with_ops(db_session), _draft_with_ops(db_session)

    commits: List[int] = []
    real_commit = db_session.commit

    def _counting_commit():
        commits.append(1)
        return real_commit()

    monkeypatch.setattr(db_session, "commit", _counting_commit)
    enqueued: List[int] = []
    monkeypatch.setattr(notification_outbox, "_enqueue_dispatch", enqueued.append)

    audited: List[int] = []
    real_audit = release_service.audit_work_order_release

    def _audit(audit, *, work_order, old_status):
        audited.append(work_order.id)
        if len(audited) == 2:
            raise OperationalError("SELECT pg_advisory_xact_lock(...)", {}, _LostRaceDriverError(pgcode))
        return real_audit(audit, work_order=work_order, old_status=old_status)

    monkeypatch.setattr(release_service, "audit_work_order_release", _audit)

    if db_session.get_bind().dialect.name == "sqlite":
        # sqlite3 legacy mode does not BEGIN on SELECT, so each item's SAVEPOINT would otherwise open
        # its own transaction and its RELEASE would COMMIT that item -- unlike production Postgres,
        # where every item's savepoint nests inside the request's one outer transaction. Open that
        # outer transaction explicitly so "nothing is saved" is judged under production semantics.
        db_session.connection().exec_driver_sql("BEGIN")

    resp = client.post(BULK_URL, json={"work_order_ids": [first.id, second.id]}, headers=headers_for(manager))
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert resp.json()["detail"] == STALE_WRITE_CONFLICT_DETAIL
    for leaked in ("deadlock", "Process 4242", "pg_advisory"):
        assert leaked not in resp.text
    assert audited == sorted([first.id, second.id]), "both items were released before the audit phase failed"

    assert commits == []
    assert scheduling_spy.instances == []
    assert broadcasts == []
    assert enqueued == []

    # The request's session is closed (rolled back) in production; here the shared test session is.
    db_session.rollback()
    for wo in (first, second):
        _assert_untouched_draft(db_session, wo)
