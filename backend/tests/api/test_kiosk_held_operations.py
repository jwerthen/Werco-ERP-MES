"""Held work is VISIBLE to the kiosk -- and still is not queued work.

An operator put an operation ON HOLD by accident on a live nest job and it
vanished from every screen the shop floor can see: the kiosk can place a hold
but has no resume, and ``ShopFloorSimple`` (a desktop page) is the app's only
caller of resume. ``GET /shop-floor/work-center-queue/{id}`` therefore grew a
SEPARATE ``held`` list.

What this file pins, in two halves.

VISIBILITY -- held operations reach the kiosk with the same identity a queued
one has, flagged ``status: "on_hold"`` / ``startable: false``, carrying why
(the still-open blocker) and who/when (blocker or ``operation_hold`` event).

CONTAINMENT -- and nothing else moved. ``dispatch_service.QUEUE_OPERATION_STATUSES``
is still exactly ``(READY, IN_PROGRESS)``; ``queue`` still carries only those;
a held operation takes no ``run_order`` and cannot shift the gap-free RUN chip
numbers of the jobs around it. Widening the queue constant instead of adding
this list is the change these assertions exist to fail.

DISCLOSURE -- and an unattended tablet is not an identified caller. The blocker's
free text (``note`` / ``title``) is NOT SENT to a station principal; category,
severity and the attribution are. A user session gets everything. The rule is
``wallboard_service``'s, applied to the same class of screen.

Plus the two traps: a CANCELLED nest (soft-deleted, whose operation is parked in
ON_HOLD as a tombstone because ``OperationStatus`` has no operation-level
CANCELLED) must NOT be offered back to the floor as resumable work -- pinned on
the READ here and on the WRITE in ``test_kiosk_resume_fence.py`` -- and the whole
read stays tenant-scoped to the station's own company.
"""

import json
from datetime import datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.laser_nest import LaserNest, LaserNestPackage
from app.models.operational_event import OperationalEvent
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_blocker import (
    WorkOrderBlocker,
    WorkOrderBlockerCategory,
    WorkOrderBlockerSeverity,
    WorkOrderBlockerStatus,
)
from app.services import dispatch_service
from tests.api.kiosk_test_helpers import (
    COMPANY_A,
    COMPANY_B,
    bearer,
    kiosk_token_for,
    make_kiosk_station,
    make_user,
    make_wo_with_operation,
    make_work_center,
    queue_url,
    user_headers,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]


def _hold_event(
    db: Session,
    *,
    operation: WorkOrderOperation,
    user_id: int,
    company_id: int = COMPANY_A,
    occurred_at: datetime = None,
) -> OperationalEvent:
    """The record a BARE hold leaves (no note, category OTHER) -- the accident case."""
    event = OperationalEvent(
        company_id=company_id,
        event_type="operation_hold",
        source_module="shop_floor",
        entity_type="work_order_operation",
        entity_id=operation.id,
        work_order_id=operation.work_order_id,
        operation_id=operation.id,
        user_id=user_id,
        severity="medium",
        occurred_at=occurred_at or datetime.utcnow(),
        event_payload={},
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def _blocker(
    db: Session,
    *,
    operation: WorkOrderOperation,
    reported_by: int,
    company_id: int = COMPANY_A,
    category: str = WorkOrderBlockerCategory.MATERIAL_MISSING.value,
    severity: str = WorkOrderBlockerSeverity.HIGH.value,
    blocker_status: str = WorkOrderBlockerStatus.OPEN.value,
    note: str = "Wrong sheet on the rack",
    reported_at: datetime = None,
) -> WorkOrderBlocker:
    blocker = WorkOrderBlocker(
        company_id=company_id,
        work_order_id=operation.work_order_id,
        operation_id=operation.id,
        category=category,
        severity=severity,
        status=blocker_status,
        title="Material Missing: nest 3",
        note=note,
        reported_by=reported_by,
        reported_at=reported_at or datetime.utcnow(),
    )
    db.add(blocker)
    db.commit()
    db.refresh(blocker)
    return blocker


def _nest_for(db: Session, operation: WorkOrderOperation, *, is_deleted: bool) -> LaserNest:
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
        planned_runs=4,
        material="0.250 A36",
        is_deleted=is_deleted,
    )
    db.add(nest)
    db.commit()
    db.refresh(nest)
    return nest


# ---------------------------------------------------------------------------
# CONTAINMENT: the queue constant, and what `queue` carries, did not move.
# ---------------------------------------------------------------------------


def test_queue_status_constant_still_excludes_on_hold():
    """The constant the dispatch board, run-order and wallboard all share.

    Widening it is how a hold would stop meaning stop: held work would become
    capacity, take a run-order rank and count toward queued_count. The held list
    exists precisely so nobody needs to.
    """
    assert dispatch_service.QUEUE_OPERATION_STATUSES == (OperationStatus.READY, OperationStatus.IN_PROGRESS)
    assert OperationStatus.ON_HOLD not in dispatch_service.QUEUE_OPERATION_STATUSES
    assert dispatch_service.HELD_OPERATION_STATUSES == (OperationStatus.ON_HOLD,)


def test_held_operation_is_absent_from_queue_and_present_in_held(client: TestClient, db_session: Session):
    wc = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc)
    _, ready_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.READY)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)

    body = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station))).json()

    assert [row["operation_id"] for row in body["queue"]] == [ready_op.id]
    assert [row["operation_id"] for row in body["held"]] == [held_op.id]
    assert body["held_truncated"] is False


def test_held_row_is_flagged_held_and_not_startable(client: TestClient, db_session: Session):
    wc = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)

    row = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station))).json()["held"][0]

    assert row["status"] == "on_hold"
    assert row["startable"] is False
    # `startable` is a held-list claim only -- a queued row's startability is
    # decided by the server gates at action time, so the read never asserts it.
    assert "startable" not in row or row["startable"] is False


def test_held_row_carries_the_same_identity_a_queued_row_does(client: TestClient, db_session: Session):
    """Same job card, so the kiosk can render a held job with the code it has."""
    wc = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc)
    _, ready_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.READY)
    wo, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)

    body = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station))).json()
    queued_keys = set(body["queue"][0].keys())
    held_row = body["held"][0]

    # The held row is the queue row plus exactly the two hold-only keys.
    assert queued_keys.issubset(set(held_row.keys()))
    assert set(held_row.keys()) - queued_keys == {"startable", "hold"}
    assert held_row["work_order_id"] == wo.id
    assert held_row["work_order_number"] == wo.work_order_number
    assert held_row["operation_number"] == held_op.operation_number
    assert held_row["operation_name"] == held_op.name
    assert held_row["work_center_id"] == wc.id
    assert held_row["material_ties"] == []
    assert held_row["roster"] == []
    assert ready_op.id != held_op.id


def test_held_operation_takes_no_run_order_and_does_not_shift_the_run_chip(client: TestClient, db_session: Session):
    """A held op has no rank, and cannot renumber the jobs that do."""
    wc = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc)
    _, first = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.READY)
    _, second = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.READY)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    first.run_order = 1
    second.run_order = 2
    # A stale rank left on the operation from before it was held must not surface.
    held_op.run_order = 3
    db_session.commit()

    body = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station))).json()

    assert [(r["operation_id"], r["run_order"]) for r in body["queue"]] == [(first.id, 1), (second.id, 2)]
    assert body["held"][0]["run_order"] is None


def test_held_excludes_terminal_and_soft_deleted_work_orders(client: TestClient, db_session: Session):
    """Same live-work-order filters the queue read applies -- a closed job is not held work."""
    wc = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc)
    cancelled_wo, cancelled_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    cancelled_wo.status = WorkOrderStatus.CANCELLED
    deleted_wo, deleted_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    deleted_wo.is_deleted = True
    _, live_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    db_session.commit()

    body = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station))).json()

    held_ids = [row["operation_id"] for row in body["held"]]
    assert held_ids == [live_op.id]
    assert cancelled_op.id not in held_ids
    assert deleted_op.id not in held_ids


def test_cancelled_nest_tombstone_is_not_offered_as_held_work(client: TestClient, db_session: Session):
    """``soft_delete_laser_nest`` parks the operation in ON_HOLD as a tombstone.

    Surfacing it with a Resume control would let the floor resurrect a cancelled
    nest to READY. A LIVE nest on a genuinely held operation still surfaces.
    """
    wc = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc)
    _, cancelled_nest_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    _nest_for(db_session, cancelled_nest_op, is_deleted=True)
    _, live_nest_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    _nest_for(db_session, live_nest_op, is_deleted=False)

    body = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station))).json()

    held_ids = [row["operation_id"] for row in body["held"]]
    assert cancelled_nest_op.id not in held_ids
    assert held_ids == [live_nest_op.id]
    assert body["held"][0]["laser_nest"]["nest_name"] == f"NEST-{live_nest_op.id}"


# ---------------------------------------------------------------------------
# VISIBILITY: why the job is held, who held it, when.
# ---------------------------------------------------------------------------


def test_hold_reason_comes_from_the_open_blocker(client: TestClient, db_session: Session):
    wc = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc)
    reporter = make_user(db_session, company_id=COMPANY_A, first_name="Dana", last_name="Ruiz")
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    blocker = _blocker(db_session, operation=held_op, reported_by=reporter.id)

    hold = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station))).json()["held"][0]["hold"]

    assert hold["blocker"]["id"] == blocker.id
    assert hold["blocker"]["category"] == WorkOrderBlockerCategory.MATERIAL_MISSING.value
    assert hold["blocker"]["severity"] == WorkOrderBlockerSeverity.HIGH.value
    assert hold["blocker"]["status"] == WorkOrderBlockerStatus.OPEN.value
    assert hold["blocker"]["reported_by_name"] == "Dana R."
    # Blocker path emits no operation_hold event, so who/when come from it.
    assert hold["held_by_user_id"] == reporter.id
    assert hold["held_by_name"] == "Dana R."
    assert hold["held_at"] is not None and hold["held_at"].endswith("Z")


# ---------------------------------------------------------------------------
# DISCLOSURE: the blocker's FREE TEXT does not reach an unattended station.
# ---------------------------------------------------------------------------


def test_station_never_receives_the_blockers_free_text(client: TestClient, db_session: Session):
    """A crew station is a public screen, so `note`/`title` are NOT SENT to it.

    A station token authenticates a 10-15s poll on a PIN-unlocked tablet that has
    no operator identity and no idle logout -- anything the board renders is
    readable by whoever walks past. ``wallboard_service`` already wrote the rule
    for that audience ("no customer names, no ship-to addresses, no dollar
    figures, no NCR titles/descriptions"), and its own blocked-work rail holds
    ``title`` and ``note`` and emits only wo_number / category / age_hours.

    The keys are ABSENT, not blanked: a render-side gate would still put the text
    on the device, where a devtools console or a proxy reads it.
    """
    wc = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc)
    reporter = make_user(db_session, company_id=COMPANY_A, first_name="Dana", last_name="Ruiz")
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    _blocker(db_session, operation=held_op, reported_by=reporter.id, note="Wrong sheet on the rack")

    body = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station))).json()
    blocker_payload = body["held"][0]["hold"]["blocker"]

    assert "note" not in blocker_payload
    assert "title" not in blocker_payload
    # Belt and braces: the strings must not survive anywhere else on the payload.
    serialized = json.dumps(body)
    assert "Wrong sheet on the rack" not in serialized
    assert "Material Missing: nest 3" not in serialized
    # The policy is stated, and the EXISTENCE of a note is a boolean -- so the
    # card can say "a note was recorded" instead of implying none was given.
    assert blocker_payload["free_text_withheld"] is True
    assert blocker_payload["has_note"] is True


def test_station_still_gets_category_severity_and_attribution(client: TestClient, db_session: Session):
    """What is withheld is the free text, NOT the ability to read the hold.

    Category + severity + who/when is what separates a deliberate, categorized
    stop from a mis-tap, which is the entire job of the held card. The motivating
    accident is a BARE hold carrying no note at all, so the feature loses nothing.
    """
    wc = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc)
    reporter = make_user(db_session, company_id=COMPANY_A, first_name="Dana", last_name="Ruiz")
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    _blocker(db_session, operation=held_op, reported_by=reporter.id)

    blocker_payload = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station))).json()["held"][0]["hold"][
        "blocker"
    ]

    assert blocker_payload["category"] == WorkOrderBlockerCategory.MATERIAL_MISSING.value
    assert blocker_payload["severity"] == WorkOrderBlockerSeverity.HIGH.value
    assert blocker_payload["status"] == WorkOrderBlockerStatus.OPEN.value
    assert blocker_payload["reported_by_name"] == "Dana R."
    assert blocker_payload["reported_at"] is not None


def test_station_reports_no_free_text_when_the_blocker_has_none(client: TestClient, db_session: Session):
    """``has_note`` describes the RECORD, not the policy.

    Announcing a note that does not exist would send an operator chasing a
    supervisor over a categorized hold nobody wrote anything on. It keys on the
    NOTE alone because ``work_order_blockers.title`` is ``nullable=False`` and
    ``_blocker_default_title`` always composes one -- a title-inclusive flag
    would be constant-true and carry no information.
    """
    wc = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc)
    reporter = make_user(db_session, company_id=COMPANY_A)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    _blocker(db_session, operation=held_op, reported_by=reporter.id, note="")

    blocker_payload = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station))).json()["held"][0]["hold"][
        "blocker"
    ]

    assert blocker_payload["has_note"] is False
    assert blocker_payload["free_text_withheld"] is True


def test_identified_user_session_keeps_the_full_blocker_text(client: TestClient, db_session: Session):
    """The single-operator kiosk and the desktop run on a REAL user session.

    An identified caller is not an unattended screen, so nothing is withheld --
    the split is by audience, not a blanket redaction that would leave the note
    unreadable everywhere.
    """
    wc = make_work_center(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)
    reporter = make_user(db_session, company_id=COMPANY_A, first_name="Dana", last_name="Ruiz")
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    _blocker(db_session, operation=held_op, reported_by=reporter.id, note="Wrong sheet on the rack")

    blocker_payload = client.get(queue_url(wc.id), headers=user_headers(operator)).json()["held"][0]["hold"]["blocker"]

    assert blocker_payload["note"] == "Wrong sheet on the rack"
    assert blocker_payload["title"] == "Material Missing: nest 3"
    assert blocker_payload["free_text_withheld"] is False
    assert blocker_payload["has_note"] is True


def test_bare_hold_reports_who_and_when_from_the_operation_hold_event(client: TestClient, db_session: Session):
    """The accident case: no note, category OTHER -> an event, no blocker.

    This is exactly the incident that motivated the feature, so "who pressed it"
    has to survive the path that records the least.
    """
    wc = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc)
    operator = make_user(db_session, company_id=COMPANY_A, first_name="Milo", last_name="Vance")
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    _hold_event(db_session, operation=held_op, user_id=operator.id)

    hold = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station))).json()["held"][0]["hold"]

    assert hold["blocker"] is None
    assert hold["held_by_user_id"] == operator.id
    assert hold["held_by_name"] == "Milo V."
    assert hold["held_at"] is not None


def test_most_recent_record_wins_but_the_open_blocker_is_still_the_reason(client: TestClient, db_session: Session):
    """A stale blocker plus a fresh bare hold: newest actor, blocker still shown."""
    wc = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc)
    old_reporter = make_user(db_session, company_id=COMPANY_A, first_name="Pat", last_name="Older")
    recent_holder = make_user(db_session, company_id=COMPANY_A, first_name="Sam", last_name="Newer")
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    _blocker(
        db_session,
        operation=held_op,
        reported_by=old_reporter.id,
        reported_at=datetime.utcnow() - timedelta(days=3),
    )
    _hold_event(db_session, operation=held_op, user_id=recent_holder.id)

    hold = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station))).json()["held"][0]["hold"]

    assert hold["held_by_user_id"] == recent_holder.id
    assert hold["blocker"]["reported_by_user_id"] == old_reporter.id


def test_resolved_blocker_is_not_shown_as_the_current_reason(client: TestClient, db_session: Session):
    """Resolve/dismiss is what auto-resumes; a closed blocker is stale narrative."""
    wc = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc)
    reporter = make_user(db_session, company_id=COMPANY_A)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    _blocker(
        db_session,
        operation=held_op,
        reported_by=reporter.id,
        blocker_status=WorkOrderBlockerStatus.RESOLVED.value,
    )

    hold = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station))).json()["held"][0]["hold"]

    assert hold["blocker"] is None


def test_hold_with_no_recorded_reason_reports_nulls_rather_than_inventing_one(client: TestClient, db_session: Session):
    """A hold that predates both records: say nothing, never guess a holder."""
    wc = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)

    hold = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station))).json()["held"][0]["hold"]

    assert hold == {"held_at": None, "held_by_user_id": None, "held_by_name": None, "blocker": None}
    assert held_op.status == OperationStatus.ON_HOLD


# ---------------------------------------------------------------------------
# Tenancy, bounds, and the normal-user caller.
# ---------------------------------------------------------------------------


def test_held_is_tenant_scoped_to_the_stations_own_company(client: TestClient, db_session: Session):
    """Invariant 1: the station's company comes from its DB row, never the client."""
    wc_a = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc_a)
    _, own_held = make_wo_with_operation(
        db_session, company_id=COMPANY_A, work_center=wc_a, op_status=OperationStatus.ON_HOLD
    )
    wc_b = make_work_center(db_session, company_id=COMPANY_B)
    _, foreign_held = make_wo_with_operation(
        db_session, company_id=COMPANY_B, work_center=wc_b, op_status=OperationStatus.ON_HOLD
    )
    foreign_reporter = make_user(db_session, company_id=COMPANY_B)
    _blocker(db_session, operation=foreign_held, reported_by=foreign_reporter.id, company_id=COMPANY_B)

    body = client.get(queue_url(wc_a.id), headers=bearer(kiosk_token_for(station))).json()

    assert [row["operation_id"] for row in body["held"]] == [own_held.id]


def test_foreign_blocker_never_explains_an_operation_in_this_company(client: TestClient, db_session: Session):
    """A cross-tenant blocker row must not leak its note onto our hold card."""
    wc = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    foreign_reporter = make_user(db_session, company_id=COMPANY_B)
    # Same operation id, wrong company_id on the blocker row.
    _blocker(
        db_session,
        operation=held_op,
        reported_by=foreign_reporter.id,
        company_id=COMPANY_B,
        note="Other tenant's secret",
    )

    hold = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station))).json()["held"][0]["hold"]

    assert hold["blocker"] is None
    assert hold["held_by_user_id"] is None


def test_held_list_is_capped_and_reports_truncation(client: TestClient, db_session: Session, monkeypatch):
    wc = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc)
    monkeypatch.setattr(dispatch_service, "MAX_HELD_OPERATIONS", 2)
    for _ in range(3):
        make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)

    body = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station))).json()

    assert len(body["held"]) == 2
    assert body["held_truncated"] is True


def test_normal_user_session_sees_the_same_held_list(client: TestClient, db_session: Session):
    wc = make_work_center(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)

    resp = client.get(queue_url(wc.id), headers=user_headers(operator))

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert [row["operation_id"] for row in resp.json()["held"]] == [held_op.id]


def test_the_read_writes_nothing(client: TestClient, db_session: Session):
    """A poll is not an actor: no audit row, no event, no status change."""
    from app.models.audit_log import AuditLog

    wc = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    audit_before = db_session.query(AuditLog).count()
    events_before = db_session.query(OperationalEvent).count()

    client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station)))

    db_session.expire_all()
    assert db_session.query(AuditLog).count() == audit_before
    assert db_session.query(OperationalEvent).count() == events_before
    assert db_session.query(WorkOrderOperation).get(held_op.id).status == OperationStatus.ON_HOLD
    assert db_session.query(WorkOrder).get(held_op.work_order_id).status == WorkOrderStatus.RELEASED
