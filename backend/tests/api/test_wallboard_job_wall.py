"""Job Wall (owner feedback 2026-07-15): WORK-ORDER tiles on the TV wallboard.

The main wall renders open work orders with their CURRENT operation instead of
machine tiles, and the trailing-30d kpi_strip is gone. Locks:

  * population — company WOs, is_deleted == False, status in (RELEASED,
    IN_PROGRESS); ON_HOLD deliberately EXCLUDED (the quality rail counts
    holds), DRAFT and terminal statuses off the wall,
  * current-op precedence — lowest-sequence IN_PROGRESS, else lowest READY,
    else lowest PENDING, None when all complete,
  * tile facts — WO-level qty, promise/is_late/days_late via the shared
    promise precedence (must_ship_by || due_date vs Central today), blocked /
    down / running flags, ops_completed "n of N", crew on the current op,
  * deterministic priority sort — blocked/down first, then late worst-first,
    then running, then promise asc (nulls last), wo_number tie-break,
  * cap 24 + jobs_total true count,
  * ?dept= scoping via the CURRENT op's work-center type (case-insensitive),
  * kpi_strip is DEPRECATED: the key survives on the wire but is always null,
    and the strip compute + TTL cache machinery is deleted outright.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.time_utils import CENTRAL_TIME_ZONE
from app.models.downtime import DowntimeCategory, DowntimeEvent
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlocker, WorkOrderBlockerCategory, WorkOrderBlockerStatus
from tests.lean_phase1_helpers import (
    headers_for,
    make_entry,
    make_op,
    make_part,
    make_user,
    make_wo,
    make_work_center,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

WALLBOARD_URL = "/api/v1/shop-floor/wallboard"


def _payload(client: TestClient, headers: dict, dept: "str | None" = None) -> dict:
    url = f"{WALLBOARD_URL}?dept={dept}" if dept else WALLBOARD_URL
    response = client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _job(payload: dict, wo: WorkOrder) -> dict:
    return next(job for job in payload["jobs"] if job["wo_number"] == wo.work_order_number)


def _central_today():
    return datetime.now(CENTRAL_TIME_ZONE).date()


def _add_blocker(db: Session, wo: WorkOrder, operation_id: "int | None" = None) -> None:
    db.add(
        WorkOrderBlocker(
            work_order_id=wo.id,
            operation_id=operation_id,
            category=WorkOrderBlockerCategory.MATERIAL_MISSING.value,
            status=WorkOrderBlockerStatus.OPEN.value,
            title=f"Blocker on {wo.work_order_number}",
            reported_at=datetime.utcnow() - timedelta(hours=1),
            company_id=wo.company_id,
        )
    )


def test_current_op_selection_precedence(client: TestClient, db_session: Session):
    """current_op = lowest-sequence IN_PROGRESS, else lowest READY, else lowest
    PENDING; None when everything is complete (or only ON_HOLD ops remain)."""
    viewer = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)

    # IN_PROGRESS wins even with READY/PENDING present.
    wo_active = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS)
    make_op(db_session, wo_active, wc, sequence=10, status_=OperationStatus.COMPLETE)
    make_op(db_session, wo_active, wc, sequence=20, status_=OperationStatus.IN_PROGRESS)
    make_op(db_session, wo_active, wc, sequence=30, status_=OperationStatus.READY)
    make_op(db_session, wo_active, wc, sequence=40, status_=OperationStatus.PENDING)

    # No IN_PROGRESS: the LOWEST-sequence READY wins (not the seq-50 one).
    wo_ready = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS)
    make_op(db_session, wo_ready, wc, sequence=10, status_=OperationStatus.COMPLETE)
    make_op(db_session, wo_ready, wc, sequence=30, status_=OperationStatus.READY)
    make_op(db_session, wo_ready, wc, sequence=40, status_=OperationStatus.PENDING)
    make_op(db_session, wo_ready, wc, sequence=50, status_=OperationStatus.READY)

    # Neither IN_PROGRESS nor READY: lowest PENDING.
    wo_pending = make_wo(db_session, part)
    make_op(db_session, wo_pending, wc, sequence=10, status_=OperationStatus.COMPLETE)
    make_op(db_session, wo_pending, wc, sequence=40, status_=OperationStatus.PENDING)

    # All complete: the tile stays on the wall with current_op = None.
    wo_done_ops = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS)
    make_op(db_session, wo_done_ops, wc, sequence=10, status_=OperationStatus.COMPLETE)
    make_op(db_session, wo_done_ops, wc, sequence=20, status_=OperationStatus.COMPLETE)

    # Only an ON_HOLD op: not a workable "current" op -> None.
    wo_held_op = make_wo(db_session, part)
    make_op(db_session, wo_held_op, wc, sequence=10, status_=OperationStatus.ON_HOLD)

    payload = _payload(client, headers_for(viewer))

    active = _job(payload, wo_active)
    assert active["current_op"]["sequence"] == 20
    assert active["current_op"]["status"] == "in_progress"
    assert active["current_op"]["name"] == "Op 20"
    assert active["current_op"]["work_center_code"] == wc.code
    assert active["current_op"]["work_center_name"] == wc.name
    assert active["ops_completed"] == 1
    assert active["ops_total"] == 4

    ready = _job(payload, wo_ready)
    assert ready["current_op"]["sequence"] == 30
    assert ready["current_op"]["status"] == "ready"

    pending = _job(payload, wo_pending)
    assert pending["current_op"]["sequence"] == 40
    assert pending["current_op"]["status"] == "pending"

    done_ops = _job(payload, wo_done_ops)
    assert done_ops["current_op"] is None
    assert done_ops["ops_completed"] == 2
    assert done_ops["ops_total"] == 2

    assert _job(payload, wo_held_op)["current_op"] is None


def test_current_op_prefers_in_progress_op_with_open_labor(client: TestClient, db_session: Session):
    """Overlapping IN_PROGRESS ops: the op someone is actually clocked into
    wins the tile, even at a higher sequence — otherwise the wall would show
    WAITING with no crew while people are working the WO."""
    viewer = make_user(db_session)
    part = make_part(db_session)
    wc_a = make_work_center(db_session)
    wc_b = make_work_center(db_session)
    operator = make_user(db_session, role=UserRole.OPERATOR, first_name="Ada", last_name="Miller")

    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS)
    make_op(db_session, wo, wc_a, sequence=10, status_=OperationStatus.IN_PROGRESS)  # idle
    worked = make_op(db_session, wo, wc_b, sequence=20, status_=OperationStatus.IN_PROGRESS)
    make_entry(db_session, operator, wo, worked, wc_b, open_entry=True)
    db_session.commit()

    job = _job(_payload(client, headers_for(viewer)), wo)
    assert job["current_op"]["sequence"] == 20  # labor wins over lower sequence
    assert job["running"] is True
    assert job["current_op"]["crew"] == ["Ada M."]


def test_job_tile_facts_crew_flags_and_late_precedence(client: TestClient, db_session: Session):
    """One fully-dressed tile: WO-level qty, op-level qty via
    operation_target_quantity, crew (deduped, First L.) + elapsed on the
    current op, blocked (WO-level blocker) / down / running flags, and
    lateness via must_ship_by || due_date against Central today."""
    viewer = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    central_today = _central_today()
    now = datetime.utcnow()

    # must_ship_by two days past trumps a comfortable due_date: LATE by 2.
    wo = make_wo(
        db_session,
        part,
        status_=WorkOrderStatus.IN_PROGRESS,
        quantity_ordered=50,
        must_ship_by=central_today - timedelta(days=2),
        due_date=central_today + timedelta(days=7),
    )
    wo.quantity_complete = 12
    make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.COMPLETE)
    op = make_op(db_session, wo, wc, sequence=20, status_=OperationStatus.IN_PROGRESS, quantity_complete=5)

    alice = make_user(db_session, role=UserRole.OPERATOR, first_name="Alice", last_name="Anders")
    bob = make_user(db_session, role=UserRole.OPERATOR, first_name="Bob", last_name="Baker")
    make_entry(db_session, alice, wo, op, wc, open_entry=True, clock_in=now - timedelta(minutes=50))
    make_entry(db_session, bob, wo, op, wc, open_entry=True, clock_in=now - timedelta(minutes=30))
    # Duplicate open entry by one operator: one head, not two.
    make_entry(db_session, alice, wo, op, wc, open_entry=True, clock_in=now - timedelta(minutes=5))

    _add_blocker(db_session, wo, operation_id=None)  # WO-level blocker (no op) still flags the tile
    db_session.add(
        DowntimeEvent(
            work_center_id=wc.id,  # the CURRENT op's work center is down
            start_time=now - timedelta(minutes=15),
            category=DowntimeCategory.MECHANICAL,
            reported_by=viewer.id,
            company_id=1,
        )
    )

    # Past due_date but a future must_ship_by: the promise is NOT late.
    saved = make_wo(
        db_session,
        part,
        status_=WorkOrderStatus.IN_PROGRESS,
        due_date=central_today - timedelta(days=4),
        must_ship_by=central_today + timedelta(days=1),
    )
    quiet_wc = make_work_center(db_session)
    make_op(db_session, saved, quiet_wc, sequence=10, status_=OperationStatus.READY)
    db_session.commit()

    payload = _payload(client, headers_for(viewer))

    tile = _job(payload, wo)
    assert tile["status"] == "in_progress"
    assert tile["part_number"] == part.part_number
    assert tile["qty_complete"] == 12.0
    assert tile["qty_ordered"] == 50.0
    assert tile["promise_date"] == (central_today - timedelta(days=2)).isoformat()
    assert tile["is_late"] is True
    assert tile["days_late"] == 2
    assert tile["blocked"] is True
    assert tile["down"] is True
    assert tile["running"] is True
    assert tile["ops_completed"] == 1
    assert tile["ops_total"] == 2

    current = tile["current_op"]
    assert current["sequence"] == 20
    assert current["qty_done"] == 5.0
    assert current["qty_target"] == 50.0  # operation_target_quantity falls back to the WO qty
    assert current["crew"] == ["Alice A.", "Bob B."]  # clock-in order, deduped, First L. only
    assert current["crew_count"] == 2
    assert 49 <= current["elapsed_minutes"] <= 52  # EARLIEST open clock_in drives elapsed

    quiet = _job(payload, saved)
    assert quiet["is_late"] is False  # promise precedence saved it
    assert quiet["days_late"] == 0
    assert quiet["promise_date"] == (central_today + timedelta(days=1)).isoformat()
    assert quiet["blocked"] is False
    assert quiet["down"] is False
    assert quiet["running"] is False
    assert quiet["current_op"]["crew"] == []
    assert quiet["current_op"]["crew_count"] == 0
    assert quiet["current_op"]["elapsed_minutes"] == 0


def test_priority_sort_is_deterministic(client: TestClient, db_session: Session):
    """Server-side order: blocked/down first, then late (days_late desc), then
    running, then the rest by promise asc (nulls last); the client renders
    SERVER order, so this exact sequence is the wall."""
    viewer = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    down_wc = make_work_center(db_session)
    central_today = _central_today()
    now = datetime.utcnow()

    def open_wo(promise_days=None, work_center=wc):
        due = central_today + timedelta(days=promise_days) if promise_days is not None else None
        wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, due_date=due)
        make_op(db_session, wo, work_center, sequence=10, status_=OperationStatus.READY)
        return wo

    wo_down = open_wo(promise_days=2, work_center=down_wc)
    wo_blocked = open_wo(promise_days=3)
    wo_late5 = open_wo(promise_days=-5)
    wo_late2 = open_wo(promise_days=-2)
    wo_running = open_wo(promise_days=4)
    wo_q1 = open_wo(promise_days=1)
    wo_q5 = open_wo(promise_days=5)
    wo_no_promise = open_wo(promise_days=None)

    _add_blocker(db_session, wo_blocked)
    db_session.add(
        DowntimeEvent(
            work_center_id=down_wc.id,
            start_time=now - timedelta(minutes=30),
            category=DowntimeCategory.MECHANICAL,
            reported_by=viewer.id,
            company_id=1,
        )
    )
    operator = make_user(db_session, role=UserRole.OPERATOR, first_name="Runa", last_name="Runner")
    make_entry(db_session, operator, wo_running, wo_running.operations[0], wc, open_entry=True)
    db_session.commit()

    payload = _payload(client, headers_for(viewer))
    assert [job["wo_number"] for job in payload["jobs"]] == [
        wo_down.work_order_number,  # exceptions first, promise asc within the bucket
        wo_blocked.work_order_number,
        wo_late5.work_order_number,  # then late, worst days_late first
        wo_late2.work_order_number,
        wo_running.work_order_number,  # then running
        wo_q1.work_order_number,  # then the rest by promise asc
        wo_q5.work_order_number,
        wo_no_promise.work_order_number,  # nulls last
    ]
    assert payload["jobs_total"] == 8


def test_job_wall_cap_24_and_true_total(client: TestClient, db_session: Session):
    """26 open WOs: the wall carries the 24 highest-priority tiles (here all
    tie, so the wo_number tie-break decides) and jobs_total says 26."""
    viewer = make_user(db_session)
    part = make_part(db_session)
    numbers = sorted(make_wo(db_session, part).work_order_number for _ in range(26))

    payload = _payload(client, headers_for(viewer))
    assert payload["jobs_total"] == 26
    assert len(payload["jobs"]) == 24
    assert [job["wo_number"] for job in payload["jobs"]] == numbers[:24]


def test_dept_scoping_via_current_op(client: TestClient, db_session: Session):
    """?dept= keeps jobs whose CURRENT op's work-center type matches
    (case-insensitive). A WO with a dept op that is NOT current stays off that
    dept's board; a WO with no current op is off every dept board but on the
    unfiltered wall."""
    viewer = make_user(db_session)
    part = make_part(db_session)
    mill = make_work_center(db_session)  # machining
    weld = make_work_center(db_session, work_center_type="welding")

    # Current op on MACHINING; a welding op exists later in the routing.
    wo_mill_now = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS)
    make_op(db_session, wo_mill_now, mill, sequence=10, status_=OperationStatus.READY)
    make_op(db_session, wo_mill_now, weld, sequence=20, status_=OperationStatus.PENDING)

    # Current op on WELDING (the machining op is already complete).
    wo_weld_now = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS)
    make_op(db_session, wo_weld_now, mill, sequence=10, status_=OperationStatus.COMPLETE)
    make_op(db_session, wo_weld_now, weld, sequence=20, status_=OperationStatus.IN_PROGRESS)

    # No current op (all complete): unfiltered wall only.
    wo_done_ops = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS)
    make_op(db_session, wo_done_ops, mill, sequence=10, status_=OperationStatus.COMPLETE)

    unfiltered = _payload(client, headers_for(viewer))
    assert {job["wo_number"] for job in unfiltered["jobs"]} == {
        wo_mill_now.work_order_number,
        wo_weld_now.work_order_number,
        wo_done_ops.work_order_number,
    }
    assert unfiltered["jobs_total"] == 3

    machining = _payload(client, headers_for(viewer), dept="Machining")  # case-insensitive
    assert {job["wo_number"] for job in machining["jobs"]} == {wo_mill_now.work_order_number}
    assert machining["jobs_total"] == 1

    welding = _payload(client, headers_for(viewer), dept="welding")
    assert {job["wo_number"] for job in welding["jobs"]} == {wo_weld_now.work_order_number}
    assert welding["jobs_total"] == 1


def test_population_excludes_hold_draft_terminal_and_deleted(client: TestClient, db_session: Session):
    """The wall is RELEASED + IN_PROGRESS only: ON_HOLD (quality rail counts
    holds), DRAFT, terminal statuses, and soft-deleted WOs never tile."""
    viewer = make_user(db_session)
    part = make_part(db_session)

    on_wall_released = make_wo(db_session, part, status_=WorkOrderStatus.RELEASED)
    on_wall_in_progress = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS)
    for status_ in (
        WorkOrderStatus.DRAFT,
        WorkOrderStatus.ON_HOLD,
        WorkOrderStatus.COMPLETE,
        WorkOrderStatus.CLOSED,
        WorkOrderStatus.CANCELLED,
    ):
        make_wo(db_session, part, status_=status_)
    deleted = make_wo(db_session, part, status_=WorkOrderStatus.RELEASED)
    deleted.soft_delete(viewer.id)
    db_session.commit()

    payload = _payload(client, headers_for(viewer))
    assert {job["wo_number"] for job in payload["jobs"]} == {
        on_wall_released.work_order_number,
        on_wall_in_progress.work_order_number,
    }
    assert payload["jobs_total"] == 2
    assert {job["status"] for job in payload["jobs"]} == {"released", "in_progress"}


def test_kpi_strip_is_deprecated_and_machinery_deleted(client: TestClient, db_session: Session):
    """The 30d strip is off the TV: the wire key survives (old bundles render
    an em-dash panel on null) but is ALWAYS null even with data that used to
    populate it, and the compute + TTL-cache machinery is gone outright."""
    import app.services.wallboard_service as wallboard_service

    viewer = make_user(db_session)
    part = make_part(db_session)
    # Open WIP that the old strip would have counted.
    make_wo(db_session, part, status_=WorkOrderStatus.RELEASED, released_at=datetime.utcnow() - timedelta(days=2))

    payload = _payload(client, headers_for(viewer))
    assert "kpi_strip" in payload  # wire back-compat: the key still rides
    assert payload["kpi_strip"] is None
    assert payload["jobs_total"] == 1  # the wall took the strip's place

    for zombie in ("get_kpi_strip", "_compute_kpi_strip", "reset_kpi_strip_cache", "_kpi_strip_cache"):
        assert not hasattr(wallboard_service, zombie), f"kpi_strip machinery {zombie!r} survived the deletion"
