"""Job Wall (owner feedback 2026-07-15): WORK-ORDER tiles on the TV wallboard.

The main wall renders open work orders with their CURRENT operation instead of
machine tiles, and the trailing-30d kpi_strip is gone. Locks:

  * population — company WOs, is_deleted == False, status in (RELEASED,
    IN_PROGRESS, ON_HOLD); DRAFT and terminal statuses off the wall. ON_HOLD
    joined the wall 2026-08-19 by owner decision (a count on the quality rail
    is not a tile: a held WO used to vanish from the only surface that says
    WHICH job stopped) and sorts to the BACK so it can never take a top slot,
  * current-op precedence — lowest-sequence IN_PROGRESS, else lowest READY,
    else lowest PENDING, else lowest ON_HOLD (strictly last), None only when
    all complete,
  * tile facts — order qty (the WO header on a conventional routing; the SUM of
    the per-item operation targets on a POOL WO — see the pool tests at the
    bottom), promise/is_late/days_late via the shared
    promise precedence (must_ship_by || due_date vs Central today), blocked /
    down / running flags, ops_completed "n of N", crew on the current op,
  * deterministic priority sort — ACTIVE work first (blocked/down, then late
    worst-first, then running, then promise asc (nulls last), wo_number
    tie-break), HELD work strictly last. The alarm classes are a CONTIGUOUS
    PREFIX of that order, which is what lets the TV pin jobs[0:4] as an anchor
    row while the rest of the grid rotates,
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
from app.models.bom import BOM
from app.models.downtime import DowntimeCategory, DowntimeEvent
from app.models.laser_nest import LaserNest, LaserNestPackage
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
    PENDING, else lowest ON_HOLD; None only when everything is complete."""
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

    # Nothing workable left, only ON_HOLD ops: the HELD op is the current op.
    # It is strictly last in the chain (an actually-runnable op always wins —
    # wo_active/wo_ready/wo_pending above all carry no hold), but it IS in the
    # chain: returning None here made the tile read ALL OPS COMPLETE beside a
    # HELD chip and a partial progress bar, and dropped the job off every
    # ?dept= board (dept membership is derived from the current op).
    wo_held_op = make_wo(db_session, part)
    make_op(db_session, wo_held_op, wc, sequence=20, status_=OperationStatus.ON_HOLD)
    make_op(db_session, wo_held_op, wc, sequence=10, status_=OperationStatus.COMPLETE)

    # A runnable op still outranks a lower-sequence held one.
    wo_held_and_ready = make_wo(db_session, part)
    make_op(db_session, wo_held_and_ready, wc, sequence=10, status_=OperationStatus.ON_HOLD)
    make_op(db_session, wo_held_and_ready, wc, sequence=20, status_=OperationStatus.PENDING)

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

    held_op = _job(payload, wo_held_op)["current_op"]
    assert held_op is not None
    assert held_op["sequence"] == 20
    assert held_op["status"] == "on_hold"
    assert held_op["work_center_code"] == wc.code

    assert _job(payload, wo_held_and_ready)["current_op"]["sequence"] == 20


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
    """One fully-dressed tile: header qty (no per-item line ops here, so the
    header IS the total), op-level qty via
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


def test_population_includes_hold_and_excludes_draft_terminal_and_deleted(client: TestClient, db_session: Session):
    """The wall is RELEASED + IN_PROGRESS + ON_HOLD; DRAFT, terminal statuses,
    and soft-deleted WOs never tile.

    ON_HOLD joined the population 2026-08-19 (owner decision). The tile carries
    the hold on the EXISTING ``status`` field — no new wire key — and the held
    job sorts BEHIND every active one even though nothing else distinguishes
    them here, so a hold can never take a top-of-board slot from actionable
    work.
    """
    viewer = make_user(db_session)
    part = make_part(db_session)

    on_wall_released = make_wo(db_session, part, status_=WorkOrderStatus.RELEASED)
    on_wall_in_progress = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS)
    # Sorts first on wo_number alone, so its LAST position proves the held bucket.
    on_wall_held = make_wo(db_session, part, status_=WorkOrderStatus.ON_HOLD)
    on_wall_held.work_order_number = "WO-AAA-HELD"
    for status_ in (
        WorkOrderStatus.DRAFT,
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
        on_wall_held.work_order_number,
    }
    assert payload["jobs_total"] == 3
    assert {job["status"] for job in payload["jobs"]} == {"released", "in_progress", "on_hold"}
    # Held to the back: the alarm classes stay a contiguous prefix of the order.
    assert payload["jobs"][-1]["wo_number"] == on_wall_held.work_order_number
    assert sorted(job["wo_number"] for job in payload["jobs"][:2]) == sorted(
        [on_wall_released.work_order_number, on_wall_in_progress.work_order_number]
    )


def test_held_wo_whose_only_open_op_is_held_still_names_an_op_and_stays_dept_attributable(
    client: TestClient, db_session: Session
):
    """The canonical shape of the newly added population, and it used to fail twice.

    The usual route to a WO status of ON_HOLD is a blocker that ALSO held the
    operation (the kiosk hold verb, ``work_order_blocker_service``,
    ``laser_nest_service`` all write OperationStatus.ON_HOLD), so "held WO whose
    only non-complete op is held" is not a corner case here — it is the shape.
    With ON_HOLD off the current-op chain such a WO reported ``current_op:
    null``, and the card renders ``ALL OPS COMPLETE`` from exactly that — beside
    a HELD chip and a partial progress bar, three contradicting statements on
    the board the floor reads to know what is parked. Worse, dept membership is
    derived from the CURRENT op's work center, so the job dropped off every
    ``?dept=`` board and out of that board's ``jobs_total``: precisely the
    disappearance the population change was made to fix.
    """
    viewer = make_user(db_session)
    part = make_part(db_session)
    weld = make_work_center(db_session, work_center_type="welding")

    held = make_wo(db_session, part, status_=WorkOrderStatus.ON_HOLD)
    make_op(db_session, held, weld, sequence=10, status_=OperationStatus.COMPLETE)
    make_op(db_session, held, weld, sequence=20, status_=OperationStatus.ON_HOLD)
    db_session.commit()

    tile = _job(_payload(client, headers_for(viewer)), held)
    assert tile["status"] == "on_hold"
    # A real op, not None -> the card cannot claim ALL OPS COMPLETE at 1 of 2.
    assert tile["current_op"] is not None
    assert tile["current_op"]["sequence"] == 20
    assert tile["current_op"]["status"] == "on_hold"
    assert tile["current_op"]["work_center_code"] == weld.code
    assert (tile["ops_completed"], tile["ops_total"]) == (1, 2)
    # Still no reason text: the hold rides on ``status`` and nothing else.
    assert not any("reason" in key or "ncr" in key or "blocker" in key for key in tile)

    welding = _payload(client, headers_for(viewer), dept="welding")
    assert {job["wo_number"] for job in welding["jobs"]} == {held.work_order_number}
    assert welding["jobs_total"] == 1


def test_held_tile_carries_the_hold_on_status_and_nothing_else(client: TestClient, db_session: Session):
    """A held WO tiles with every fact intact, and says only ``"on_hold"``.

    Two separate locks, both about the 2026-08-19 population change:

    * **The server suppresses nothing.** A held WO that is ALSO blocked, down,
      running and late still reports every one of those flags, because the rails
      and HUD chips keep counting it and the three surfaces must agree on the
      facts. Deciding that HELD *displays* ahead of DOWN/BLOCKED/LATE/RUNNING is
      the CARD's job (``classifyJob``'s precedence), not the server's — if the
      server started zeroing flags for held jobs, the tile and the ``BLOCKED``
      chip beside it would disagree about the same work order.
    * **The tile's KEY SET is exactly the pre-hold key set.** The hold rides on
      the EXISTING ``status`` field: no new wire key, so an old TV bundle is
      unaffected — and, the part that matters, NO hold reason rides along. The Z3
      ON HOLD panel is counts-and-ages only precisely because a blocker title or
      an NCR description can name a customer or a supplier, and this payload is
      served to a scoped DISPLAY TOKEN — no user session — on a screen anybody
      can photograph. A ``hold_reason``/``blocker_title``/``ncr_*`` field here would
      turn a POPULATION change into a DISCLOSURE-CATEGORY change. That is the
      one thing this assertion exists to refuse.
    """
    viewer = make_user(db_session)
    part = make_part(db_session)
    down_wc = make_work_center(db_session)
    central_today = _central_today()

    held = make_wo(
        db_session,
        part,
        status_=WorkOrderStatus.ON_HOLD,
        due_date=central_today - timedelta(days=3),
    )
    op = make_op(db_session, held, down_wc, sequence=10, status_=OperationStatus.READY)
    _add_blocker(db_session, held, operation_id=op.id)
    db_session.add(
        DowntimeEvent(
            work_center_id=down_wc.id,
            start_time=datetime.utcnow() - timedelta(minutes=20),
            category=DowntimeCategory.MECHANICAL,
            reported_by=viewer.id,
            company_id=1,
        )
    )
    operator = make_user(db_session, role=UserRole.OPERATOR, first_name="Hilda", last_name="Holder")
    make_entry(db_session, operator, held, op, down_wc, open_entry=True)
    db_session.commit()

    tile = _job(_payload(client, headers_for(viewer)), held)

    assert tile["status"] == "on_hold"  # THE carrier of the hold — see WallboardJob.status
    # ...and every other fact survives the trip untouched.
    assert tile["blocked"] is True
    assert tile["down"] is True
    assert tile["running"] is True
    assert tile["is_late"] is True
    assert tile["days_late"] == 3
    assert tile["current_op"]["work_center_code"] == down_wc.code
    assert tile["current_op"]["crew_count"] == 1

    assert set(tile) == {
        "wo_number",
        "unit_number",
        "part_number",
        "customer_name",
        "status",
        "qty_complete",
        "qty_ordered",
        "promise_date",
        "is_late",
        "days_late",
        "blocked",
        "down",
        "running",
        "current_op",
        "ops_completed",
        "ops_total",
    }
    assert set(tile["current_op"]) == {
        "sequence",
        "name",
        "work_center_code",
        "work_center_name",
        "status",
        "qty_done",
        "qty_target",
        "crew",
        "crew_count",
        "elapsed_minutes",
    }
    # Redundant against the exact sets above, and deliberately so: it states the
    # RULE rather than the current membership, so a reviewer adding a field sees
    # which categories are refused and why, not just that a set changed.
    forbidden = ("reason", "blocker", "ncr", "note", "comment", "description")
    for key in list(tile) + list(tile["current_op"]):
        assert not any(word in key for word in forbidden), f"free-text disclosure on a public TV tile: {key}"


def test_holds_sort_last_and_the_alarm_classes_stay_a_contiguous_prefix(client: TestClient, db_session: Session):
    """THE property the TV's anchor row is built on, asserted as a property.

    The wallboard pins ``jobs[0:4]`` as a never-rotating anchor row and cycles
    the rest of the grid. That is only sound if severity is monotonically
    non-increasing from index 0 — i.e. the alarm classes are a CONTIGUOUS PREFIX
    of the server's order and the client never re-sorts. The held bucket
    STRENGTHENS that: held work is pushed strictly behind every active job, so no
    number of holds can push an actionable alarm out of the anchor row.

    Stated precisely, because the "contiguous prefix" phrase alone is ambiguous
    once holds exist:
      * held jobs occupy a contiguous SUFFIX,
      * within the active prefix, blocked/down jobs occupy positions 0..m-1,
      * so ``jobs[0:4]`` is the four most severe ACTIVE jobs.

    The corollary is the one thing about this design most likely to be filed as a
    bug, so it is pinned here on purpose: a HELD-and-BLOCKED WO still counts in
    ``blocked_total`` (the HUD chip) and still rides the Z3 blocked rail, while
    its TILE sits at the very back of the board reading HELD. A viewer can read
    "BLOCKED 2" and find one orange card. That is the intended precedence — a
    held job is deliberately stopped and someone already knows — and the rail is
    the disclosure.
    """
    viewer = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)
    down_wc = make_work_center(db_session)
    central_today = _central_today()

    def open_wo(status_, promise_days=None, work_center=wc):
        due = central_today + timedelta(days=promise_days) if promise_days is not None else None
        wo = make_wo(db_session, part, status_=status_, due_date=due)
        make_op(db_session, wo, work_center, sequence=10, status_=OperationStatus.READY)
        return wo

    active_down = open_wo(WorkOrderStatus.IN_PROGRESS, promise_days=2, work_center=down_wc)
    active_blocked = open_wo(WorkOrderStatus.IN_PROGRESS, promise_days=3)
    active_late = open_wo(WorkOrderStatus.IN_PROGRESS, promise_days=-4)
    active_running = open_wo(WorkOrderStatus.IN_PROGRESS, promise_days=5)
    active_quiet = open_wo(WorkOrderStatus.RELEASED, promise_days=6)
    # The adversarial case: a held WO that outranks every active job on EVERY
    # existing sort term (blocked, and later than the latest active job).
    held_blocked_late = open_wo(WorkOrderStatus.ON_HOLD, promise_days=-9)
    held_quiet = open_wo(WorkOrderStatus.ON_HOLD, promise_days=1)

    _add_blocker(db_session, active_blocked)
    _add_blocker(db_session, held_blocked_late)
    db_session.add(
        DowntimeEvent(
            work_center_id=down_wc.id,
            start_time=datetime.utcnow() - timedelta(minutes=30),
            category=DowntimeCategory.MECHANICAL,
            reported_by=viewer.id,
            company_id=1,
        )
    )
    operator = make_user(db_session, role=UserRole.OPERATOR, first_name="Runa", last_name="Runner")
    make_entry(db_session, operator, active_running, active_running.operations[0], wc, open_entry=True)
    db_session.commit()

    payload = _payload(client, headers_for(viewer))
    jobs = payload["jobs"]
    assert payload["jobs_total"] == 7
    assert len(jobs) == 7

    def severity_rank(job: dict) -> int:
        """The wall's class order, read back off the wire (never a re-sort)."""
        if job["status"] == "on_hold":
            return 9
        if job["blocked"] or job["down"]:
            return 0
        if job["is_late"]:
            return 1
        if job["running"]:
            return 2
        return 3

    ranks = [severity_rank(job) for job in jobs]
    assert ranks == sorted(ranks), f"severity is not monotonic across the wall: {ranks}"

    held_positions = [i for i, job in enumerate(jobs) if job["status"] == "on_hold"]
    assert held_positions == [len(jobs) - 2, len(jobs) - 1]  # contiguous suffix, and it IS the tail
    assert {jobs[i]["wo_number"] for i in held_positions} == {
        held_blocked_late.work_order_number,
        held_quiet.work_order_number,
    }

    active = [job for job in jobs if job["status"] != "on_hold"]
    alarm_positions = [i for i, job in enumerate(active) if job["blocked"] or job["down"]]
    assert alarm_positions == list(range(len(alarm_positions)))  # 0..m-1, no calm job wedged in
    assert alarm_positions == [0, 1]

    # The anchor row: four ACTIVE jobs, no hold, however bad the hold looks.
    assert {job["wo_number"] for job in jobs[:4]} == {
        active_down.work_order_number,
        active_blocked.work_order_number,
        active_late.work_order_number,
        active_running.work_order_number,
    }
    assert active_quiet.work_order_number == jobs[4]["wo_number"]  # last active, still ahead of both holds

    # The cross-zone reading, pinned rather than discovered: the HUD counts the
    # held blocker, the card at the back of the board says HELD.
    assert payload["blocked_total"] == 2
    assert {row["wo_number"] for row in payload["blocked_wos"]} == {
        active_blocked.work_order_number,
        held_blocked_late.work_order_number,
    }
    assert jobs[-2]["wo_number"] == held_blocked_late.work_order_number
    assert jobs[-2]["blocked"] is True


def test_late_rail_and_total_are_independent_of_the_wall_population(client: TestClient, db_session: Session):
    """Adding ON_HOLD to the WALL moved no LATE number — a regression test for a
    non-change, because the non-change is the surprising part.

    ``_late_wo_filters`` selects on ``status.not_in(_TERMINAL_WO_STATUSES)``, NOT
    on ``_JOB_WALL_WO_STATUSES``. So ON_HOLD WOs were ALREADY in ``late_total``,
    the LATE rail and the per-job ``is_late`` flag before they ever tiled — and a
    DRAFT WO past its promise is counted too while never appearing on the wall.
    That decoupling is what makes "the population change moves no HUD number" a
    checked claim: if anyone ever "tidies" the late predicate to reuse the wall's
    status list, ``late_total`` silently drops the DRAFT row and this fails.
    """
    viewer = make_user(db_session)
    part = make_part(db_session)
    central_today = _central_today()

    def late_wo(status_, days):
        return make_wo(db_session, part, status_=status_, due_date=central_today - timedelta(days=days))

    released_late = late_wo(WorkOrderStatus.RELEASED, 3)
    held_late = late_wo(WorkOrderStatus.ON_HOLD, 5)
    draft_late = late_wo(WorkOrderStatus.DRAFT, 2)  # counted as late, never tiles
    late_wo(WorkOrderStatus.COMPLETE, 9)  # terminal: off every late surface
    db_session.commit()

    payload = _payload(client, headers_for(viewer))

    assert payload["late_total"] == 3  # released + HELD + DRAFT; the completed one is terminal
    assert [row["wo_number"] for row in payload["late_wos"]] == [
        held_late.work_order_number,  # worst promise first
        released_late.work_order_number,
        draft_late.work_order_number,
    ]
    assert payload["late_wos"][0]["status"] == "on_hold"  # the rail always carried holds
    assert payload["late_wos"][0]["days_late"] == 5

    # The WALL is the narrower population: DRAFT never tiles, ON_HOLD now does.
    assert {job["wo_number"] for job in payload["jobs"]} == {
        released_late.work_order_number,
        held_late.work_order_number,
    }
    assert payload["jobs_total"] == 2

    held_tile = _job(payload, held_late)
    assert held_tile["is_late"] is True
    assert held_tile["days_late"] == 5
    # ...and it is nonetheless LAST on the board: the LATE rail names the worst
    # job first while its tile sits at the back. Same deliberate split as the
    # BLOCKED chip above — the rail is the disclosure, the wall is the queue.
    assert payload["jobs"][-1]["wo_number"] == held_late.work_order_number


def test_blocked_rail_covers_every_blocked_tile_on_the_wall(client: TestClient, db_session: Session):
    """The blocked rail's SQL limit is the JOB WALL's cap, not the ticker's.

    ``WoCard`` joins a tile's BLOCKED age and stop reason out of ``blocked_wos``
    by ``wo_number``. Once the grid rotates it shows ranks 13-24 too, and those
    (being the least severe) are systematically the rows a 12-row cap dropped —
    so later field pages would render more blank reason cells than page 0, and a
    page of blanks reads as broken. Unlike the job-wall cap this is a REAL SQL
    limit, so the fix had to be in the query, not a slice.

    The assertion is the JOIN itself: every blocked tile on the wall must resolve
    in the rail. Under the old ``_TICKER_LIMIT`` this shop returns 12 rail rows
    for 20 blocked tiles and the set equality fails.
    """
    from app.services import wallboard_service

    viewer = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)

    blocked_wos = []
    for i in range(20):
        wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS)
        make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.READY)
        _add_blocker(db_session, wo)
        blocked_wos.append(wo)
    db_session.commit()

    payload = _payload(client, headers_for(viewer))

    assert payload["blocked_total"] == 20  # the total was never capped
    assert len(payload["blocked_wos"]) == 20
    assert len(payload["blocked_wos"]) > wallboard_service._TICKER_LIMIT  # the old cap truncated at 12

    on_wall = {job["wo_number"] for job in payload["jobs"] if job["blocked"]}
    assert len(on_wall) == 20
    assert on_wall == {row["wo_number"] for row in payload["blocked_wos"]}  # THE join, complete

    # Structural lock: the join cap must cover the whole wall, whatever the wall's
    # cap becomes. Raising _JOB_WALL_LIMIT alone would re-open the blank-cell gap.
    assert wallboard_service._BLOCKED_JOIN_LIMIT == wallboard_service._JOB_WALL_LIMIT


def test_late_rail_keeps_the_twelve_row_ticker_cap(client: TestClient, db_session: Session):
    """``late_wos`` deliberately did NOT follow the blocked rail up to 24.

    ``WoCard`` reads lateness off ``job.is_late`` on the tile itself and joins
    nothing from ``late_wos``, so the rotating grid creates no demand for more
    rows here. The two rails now carry different caps on purpose; this pins the
    smaller one so "the blocked cap was raised" cannot quietly become "the caps
    were raised".
    """
    from app.services import wallboard_service

    viewer = make_user(db_session)
    part = make_part(db_session)
    central_today = _central_today()

    for days in range(1, 15):  # 14 late WOs
        make_wo(db_session, part, status_=WorkOrderStatus.RELEASED, due_date=central_today - timedelta(days=days))
    db_session.commit()

    payload = _payload(client, headers_for(viewer))

    assert payload["late_total"] == 14  # uncapped
    assert len(payload["late_wos"]) == wallboard_service._TICKER_LIMIT == 12
    assert len(payload["jobs"]) == 14  # every one of them still tiles


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


def _nest_for(db: Session, operation, *, is_deleted: bool) -> LaserNest:
    """Back an operation with a laser nest — live, or a soft-deleted tombstone."""
    package = LaserNestPackage(
        company_id=operation.company_id,
        child_work_order_id=operation.work_order_id,
        package_name=f"PKG-{operation.id}",
    )
    db.add(package)
    db.flush()
    nest = LaserNest(
        company_id=operation.company_id,
        package_id=package.id,
        work_order_operation_id=operation.id,
        nest_name=f"NEST-{operation.id}",
        planned_runs=int(operation.component_quantity or 0),
        material="0.250 A36",
        is_deleted=is_deleted,
    )
    db.add(nest)
    db.commit()
    return nest


def _set_line_target(op, target: float, *, component_part_id: int | None = None):
    """Make an operation a PER-ITEM line: its own qty target, no component part."""
    op.component_quantity = target
    op.component_part_id = component_part_id
    return op


def test_pool_wo_tile_sums_per_item_operation_totals(client: TestClient, db_session: Session):
    """A POOL work order reports PIECES across all its line items, not the header.

    The prod bug (TV photo 2026-08-13): an 18-item press-brake batch WO sat on
    item 2 while its tile read "8/8 — 100%", because the non-pool header rollup
    takes MAX over ops capped at ``quantity_ordered`` (= 8 here, the set count).
    The tile must read the SUM of the per-item targets and progress, exactly as
    the laser-nest cards beside it already do.
    """
    viewer = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)

    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=8)
    wo.quantity_complete = 8  # what the header rollup froze at — the misleading 8/8
    _set_line_target(make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.COMPLETE, quantity_complete=8), 8)
    _set_line_target(
        make_op(db_session, wo, wc, sequence=20, status_=OperationStatus.IN_PROGRESS, quantity_complete=3), 18
    )
    _set_line_target(make_op(db_session, wo, wc, sequence=30, status_=OperationStatus.READY), 4)
    db_session.commit()

    tile = _job(_payload(client, headers_for(viewer)), wo)
    assert tile["qty_ordered"] == 30.0  # 8 + 18 + 4 pieces across the three items
    assert tile["qty_complete"] == 11.0  # 8 + 3 + 0 — NOT the header's 8
    assert tile["ops_completed"] == 1 and tile["ops_total"] == 3  # op chip unchanged


def test_pool_wo_tile_caps_each_line_at_its_own_target(client: TestClient, db_session: Session):
    """Per-line cap (the ``pooled_quantity_complete`` rule): one over-posted item
    cannot inflate the pool total past what the line actually ordered."""
    viewer = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)

    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=1)
    _set_line_target(make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.COMPLETE, quantity_complete=9), 4)
    _set_line_target(make_op(db_session, wo, wc, sequence=20, status_=OperationStatus.READY), 6)
    db_session.commit()

    tile = _job(_payload(client, headers_for(viewer)), wo)
    assert tile["qty_ordered"] == 10.0
    assert tile["qty_complete"] == 4.0  # 9 posted on a 4-piece line still counts 4
    assert tile["qty_complete"] <= tile["qty_ordered"]  # the bar can never exceed 100%


def test_conventional_routing_tile_keeps_header_totals(client: TestClient, db_session: Session):
    """The fence: a normal routing's ops each process the WHOLE order, so summing
    them would multiply the order by its op count. Header stays the truth."""
    viewer = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)

    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=50)
    wo.quantity_complete = 12
    make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.COMPLETE, quantity_complete=50)
    make_op(db_session, wo, wc, sequence=20, status_=OperationStatus.IN_PROGRESS, quantity_complete=12)
    make_op(db_session, wo, wc, sequence=30, status_=OperationStatus.PENDING)
    db_session.commit()

    tile = _job(_payload(client, headers_for(viewer)), wo)
    assert tile["qty_ordered"] == 50.0  # not 150
    assert tile["qty_complete"] == 12.0  # not 62


def test_assembly_component_operations_do_not_sum_into_tile_totals(client: TestClient, db_session: Session):
    """BOM-driven component ops carry a ``component_part_id`` AND a
    ``qty_per_assembly x order qty`` target (``_reconcile_operation_component_quantities``).
    Those are components, not units of the order — summing them with the
    assembly's own operations would add unlike things, so the header wins."""
    viewer = make_user(db_session)
    part = make_part(db_session)
    component = make_part(db_session)
    wc = make_work_center(db_session)

    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=10)
    wo.quantity_complete = 4
    _set_line_target(
        make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.COMPLETE, quantity_complete=40),
        40,
        component_part_id=component.id,
    )
    # The assembly's OWN op carries a target too, so only the ``component_part_id`` half
    # of the filter can refuse this WO — without it the card would read 4/50.
    _set_line_target(
        make_op(db_session, wo, wc, sequence=20, status_=OperationStatus.IN_PROGRESS, quantity_complete=4), 10
    )
    db_session.commit()

    tile = _job(_payload(client, headers_for(viewer)), wo)
    assert tile["qty_ordered"] == 10.0
    assert tile["qty_complete"] == 4.0


def test_pool_wo_tile_is_not_capped_by_a_lagging_header_quantity(client: TestClient, db_session: Session):
    """No header cap, deliberately — the divergence from ``pooled_quantity_complete``.

    That helper caps its SUM at ``work_order.quantity_ordered`` before STORING it.
    The tile does not: where a header was hand-edited below the sum of its lines,
    the lines are what the floor still has to make, so the board shows them.
    """
    viewer = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)

    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=4)
    wo.quantity_complete = 4
    _set_line_target(make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.COMPLETE, quantity_complete=2), 2)
    _set_line_target(make_op(db_session, wo, wc, sequence=20, status_=OperationStatus.COMPLETE, quantity_complete=3), 3)
    db_session.commit()

    tile = _job(_payload(client, headers_for(viewer)), wo)
    assert tile["qty_ordered"] == 5.0  # the lines, not the header's 4
    assert tile["qty_complete"] == 5.0


def test_restated_order_quantity_on_every_op_keeps_header_totals(client: TestClient, db_session: Session):
    """Guard 4 in isolation — the impostor whose part no longer has a BOM.

    ``GET /work-orders/preview-operations`` emits the ASSEMBLY's own routing ops with
    ``component_part_id: None`` and ``component_quantity`` = the WHOLE ORDER QUANTITY,
    restated once per op; the New Work Order wizard posts that verbatim and create
    persists it raw. Summing them would render a 10-piece job as ``0/50`` then
    ``50/50``. Guard 1 catches it while the BOM exists — so this WO deliberately has
    NO BOM, leaving "every target equals quantity_ordered" as the only thing standing
    between the card and 50.
    """
    viewer = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)

    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=10)
    wo.quantity_complete = 4
    for index in range(5):
        _set_line_target(
            make_op(
                db_session,
                wo,
                wc,
                sequence=(index + 1) * 10,
                status_=OperationStatus.COMPLETE if index < 2 else OperationStatus.READY,
                quantity_complete=10 if index < 2 else 0,
            ),
            10,  # the restated order quantity, on every op
        )
    db_session.commit()

    tile = _job(_payload(client, headers_for(viewer)), wo)
    assert tile["qty_ordered"] == 10.0  # not 50
    assert tile["qty_complete"] == 4.0  # not 20


def test_bom_backed_part_keeps_header_totals_even_with_mixed_targets(client: TestClient, db_session: Session):
    """Guard 1 alone, with the all-equal signature deliberately broken.

    Editing one previewed op and THEN raising the quantity rescales only the
    still-from-routing ops (``WorkOrderNew.handleQuantityChange``), leaving one stale
    target behind — so "every target equals the header" no longer catches it. The
    part's BOM does: a BOM-backed part's operations are a routing, by construction.
    """
    viewer = make_user(db_session)
    part = make_part(db_session)
    db_session.add(BOM(part_id=part.id, revision="A", is_active=True, company_id=1))
    wc = make_work_center(db_session)

    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=100)
    wo.quantity_complete = 0
    for index, target in enumerate([100, 10, 100, 100, 100]):  # one stale op
        _set_line_target(make_op(db_session, wo, wc, sequence=(index + 1) * 10), target)
    db_session.commit()

    tile = _job(_payload(client, headers_for(viewer)), wo)
    assert tile["qty_ordered"] == 100.0  # not 410
    assert tile["qty_complete"] == 0.0


def test_pool_wo_with_an_untargeted_operation_falls_back_to_the_header(client: TestClient, db_session: Session):
    """All-or-nothing. A batch WO whose lines are only PARTLY targeted must not
    render the targeted subset as the whole order — that reports "100%" with items
    still open, which is the very failure this rule exists to remove. The shortfall
    is unrepairable in-app (``WorkOrderOperationUpdate`` exposes neither field), so
    silently dropping the untargeted lines would be permanent and invisible."""
    viewer = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)

    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=8)
    wo.quantity_complete = 8
    _set_line_target(make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.COMPLETE, quantity_complete=8), 8)
    _set_line_target(make_op(db_session, wo, wc, sequence=20, status_=OperationStatus.COMPLETE, quantity_complete=4), 4)
    make_op(db_session, wo, wc, sequence=30, status_=OperationStatus.READY)  # no target
    db_session.commit()

    tile = _job(_payload(client, headers_for(viewer)), wo)
    assert tile["qty_ordered"] == 8.0  # the header, NOT 12 (which would read 100%)
    assert tile["qty_complete"] == 8.0


def test_cancelled_nest_tombstone_is_excluded_from_pool_totals(client: TestClient, db_session: Session):
    """A soft-deleted nest's operation survives at ON_HOLD with its
    ``component_quantity`` intact (``soft_delete_laser_nest``), while the WO header is
    recomputed over LIVE nests only. Counting it would put sheets on the board that
    nobody will cut."""
    viewer = make_user(db_session)
    part = make_part(db_session)
    wc = make_work_center(db_session)

    wo = make_wo(db_session, part, status_=WorkOrderStatus.IN_PROGRESS, quantity_ordered=9)
    live_a = _set_line_target(
        make_op(db_session, wo, wc, sequence=10, status_=OperationStatus.COMPLETE, quantity_complete=2), 2
    )
    live_b = _set_line_target(
        make_op(db_session, wo, wc, sequence=20, status_=OperationStatus.IN_PROGRESS, quantity_complete=1), 3
    )
    dead = _set_line_target(make_op(db_session, wo, wc, sequence=30, status_=OperationStatus.ON_HOLD), 4)
    db_session.commit()
    _nest_for(db_session, live_a, is_deleted=False)
    _nest_for(db_session, live_b, is_deleted=False)
    _nest_for(db_session, dead, is_deleted=True)

    tile = _job(_payload(client, headers_for(viewer)), wo)
    assert tile["qty_ordered"] == 5.0  # 2 + 3 — the cancelled nest's 4 sheets are gone
    assert tile["qty_complete"] == 3.0
