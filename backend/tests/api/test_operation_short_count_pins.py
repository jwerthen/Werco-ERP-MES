"""Short count (owner ruling 1) -- pins added by the mutation pass over the PR's own tests.

Every test here kills a mutation of a guarded line that the rest of the short-count suite
(``test_operation_short_count*.py``) let survive, or closes a gap the design's test list
names and the earlier stages skipped:

* the floor's declared scrap is a DELTA on top of scrap the floor already recorded, not a
  total (with 0 stored scrap, ``stored + delta`` and ``delta`` are indistinguishable);
* a crew-station BADGE token is a person and may close short -- only a standing API token
  is refused (``is_api_token_credential``, never "any non-interactive credential");
* a full-count close the ruling does not touch writes the same ledger / FG / consumption /
  entry / header rows it wrote before (the values below were captured from the gates tip);
* a stale kiosk clock-out after a close changes nothing (entry already closed -> 400;
  office full-count close left the entry open -> 409 labor-only rule);
* the office twin's serialized-short refusal, and each OR-branch of the office and force
  REQUIRED-audit conditions on its own (a branch only ever exercised together with another
  is not pinned);
* force-complete on a laser pool is bounded by the SUM of what each nest recorded, every
  force refusal writes nothing, and its labor evidence read is tenant-scoped (invariant 1);
* the reopen really clears a declared short (the existing test pre-cleared it);
* a READY operation on a held job refused by the gates' rung writes nothing.

Refusal checks look at ``db.dirty`` / ``db.new`` BEFORE re-reading: a fresh read starts
with ``expire_all()``, which silently discards an unflushed change a refusal left behind
(the test client shares the session and never rolls back), so a re-read alone cannot
prove "refused before any write".
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.operational_event import OperationalEvent
from app.models.process_sheet import ProcessSheet, WOOperationStep
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus, WorkOrderType
from app.services.audit_service import AuditService
from app.services.operation_action_gates import (
    API_TOKEN_SCRAP,
    API_TOKEN_SHORT_CLOSE,
    MSG_SCRAP_REASON_OR_CODE_REQUIRED,
    MSG_WORK_ORDER_ON_HOLD,
    completed_operation_clock_out_message,
    force_complete_exceeds_physical_message,
    force_complete_exceeds_recorded_message,
    force_complete_unaccounted_message,
)
from tests.api.kiosk_test_helpers import (
    COMPANY_B,
    bearer,
    kiosk_token_for,
    make_kiosk_station,
    make_user,
    make_work_center,
    mint_badge_token,
    user_headers,
)
from tests.api.short_count_helpers import (
    CLOCK_OUT_URL,
    COMPLETE_URL,
    add_entry,
    fresh,
    ledger_rows,
    make_routing,
    op_audit_rows,
    set_op,
    tie_material,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

OFFICE_COMPLETE_URL = "/api/v1/work-orders/operations/{op}/complete"
FORCE_COMPLETE_URL = "/api/v1/work-orders/{wo}/complete"
OFFICE_REDUCE_URL = "/api/v1/work-orders/operations/{op}/reduce-production"
API_TOKENS_URL = "/api/v1/api-tokens/"
AUDIT_UNAVAILABLE = "The completion audit could not be saved; nothing was changed. Please retry."

DONE = OperationStatus.COMPLETE
OPEN = OperationStatus.IN_PROGRESS
READY = OperationStatus.READY


@pytest.fixture(autouse=True)
def _no_completion_signal_enqueue(monkeypatch):
    """Skip the post-commit ARQ enqueue (it retries a missing Redis for seconds)."""
    monkeypatch.setattr("app.api.endpoints.work_orders.enqueue_work_order_completion_signals", lambda **_: None)
    monkeypatch.setattr("app.api.endpoints.shop_floor.enqueue_work_order_completion_signals", lambda **_: None)


@pytest.fixture
def supervisor(db_session: Session):
    return make_user(db_session, role=UserRole.SUPERVISOR, first_name="Sue", last_name="Visor")


# --------------------------------------------------------------------------- #
# Shared read-backs
# --------------------------------------------------------------------------- #
def _assert_session_clean(db: Session) -> None:
    """A refusal must leave nothing pending in the shared session (see module docstring).

    ``Session.dirty`` is optimistic -- it lists any object that saw an attribute SET event,
    including a collection re-assigned to the same members (force-complete re-binds
    ``work_order.operations`` under its lock) -- so the check is ``is_modified``: a net
    change to a column or a collection's membership.
    """
    modified = [obj for obj in db.dirty if db.is_modified(obj)]
    assert not modified, f"refusal left unflushed changes: {modified}"
    assert not db.new, f"refusal left pending rows: {db.new}"


def _ops_state(db: Session, work_order_id: int):
    db.expire_all()
    rows = (
        db.query(WorkOrderOperation)
        .filter(WorkOrderOperation.work_order_id == work_order_id)
        .order_by(WorkOrderOperation.sequence)
        .all()
    )
    return [
        (
            op.id,
            op.status,
            float(op.quantity_complete or 0),
            float(op.quantity_scrapped or 0),
            op.scrap_reason,
            op.quantity_short_declared,
            op.actual_start,
            op.actual_end,
            op.completed_by,
            op.version,
        )
        for op in rows
    ]


def _wo_state(db: Session, work_order_id: int):
    db.expire_all()
    wo = db.query(WorkOrder).filter(WorkOrder.id == work_order_id).one()
    return (wo.status, float(wo.quantity_complete or 0), float(wo.quantity_scrapped or 0), wo.actual_end, wo.version)


def _entries_state(db: Session, work_order_id: int):
    db.expire_all()
    return [
        (e.id, e.clock_out, float(e.quantity_produced or 0), float(e.quantity_scrapped or 0), e.scrap_reason)
        for e in db.query(TimeEntry).filter(TimeEntry.work_order_id == work_order_id).order_by(TimeEntry.id).all()
    ]


def _audit_rows_for(db: Session, wo: WorkOrder) -> list[tuple]:
    op_ids = [op.id for op in db.query(WorkOrderOperation).filter(WorkOrderOperation.work_order_id == wo.id).all()]
    rows = (
        db.query(AuditLog)
        .filter(
            ((AuditLog.resource_type == "work_order") & (AuditLog.resource_id == wo.id))
            | ((AuditLog.resource_type == "work_order_operation") & (AuditLog.resource_id.in_(op_ids)))
        )
        .order_by(AuditLog.id)
        .all()
    )
    return [(r.id, r.action, r.resource_type, r.resource_id) for r in rows]


def _events(db: Session) -> list[tuple]:
    return [(e.id, e.event_type) for e in db.query(OperationalEvent).order_by(OperationalEvent.id).all()]


def _world(db: Session, wo: WorkOrder) -> dict:
    """Everything a completion verb could write for this work order."""
    return {
        "ops": _ops_state(db, wo.id),
        "wo": _wo_state(db, wo.id),
        "entries": _entries_state(db, wo.id),
        "audit": _audit_rows_for(db, wo),
        "events": _events(db),
        "ledger": [(t.id, t.transaction_type, float(t.quantity or 0)) for t in ledger_rows(db)],
    }


def _assert_nothing_written(db: Session, wo: WorkOrder, before: dict) -> None:
    _assert_session_clean(db)
    assert _world(db, wo) == before


def _token_headers(client: TestClient, admin_headers: dict, user) -> dict:
    issued = client.post(API_TOKENS_URL, json={"user_id": user.id, "label": "short pin"}, headers=admin_headers)
    assert issued.status_code == 201, issued.text
    return {"Authorization": f"Bearer {issued.json()['token']}", "X-Requested-With": "XMLHttpRequest"}


def _two_stage(db: Session, *, op20_good: float, op20_scrap: float = 0.0):
    """Op 10 COMPLETE at the full 10; Op 20 open with ``op20_good`` (+ scrap) recorded."""
    ann = make_user(db)
    wc10, wc20 = make_work_center(db), make_work_center(db)
    wo, (op10, op20) = make_routing(
        db, work_centers=[wc10, wc20], statuses=[DONE, OPEN], wo_status=WorkOrderStatus.IN_PROGRESS
    )
    add_entry(db, ann, op10, produced=10)
    set_op(db, op10, quantity_complete=10)
    add_entry(db, ann, op20, produced=op20_good, scrapped=op20_scrap)
    set_op(
        db,
        op20,
        quantity_complete=op20_good,
        quantity_scrapped=op20_scrap,
        scrap_reason="crack" if op20_scrap else None,
    )
    return wo, op10, op20


# --------------------------------------------------------------------------- #
# Floor close
# --------------------------------------------------------------------------- #
class TestFloorDeclaredScrapIsADelta:
    def test_declared_scrap_adds_to_what_the_floor_already_scrapped(self, client, db_session):
        """Kills ``operation.quantity_scrapped = scrap_delta`` (a total, not a delta)."""
        ann = make_user(db_session)
        wc = make_work_center(db_session)
        wo, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        # Clock-out already recorded 7 good + 1 scrapped; 2 never reported.
        add_entry(db_session, ann, op, produced=7, scrapped=1)
        set_op(db_session, op, quantity_complete=7, quantity_scrapped=1, scrap_reason="burr")

        resp = client.post(
            COMPLETE_URL.format(op=op.id),
            json={"quantity_scrapped": 2, "scrap_reason": "dropped"},
            headers=user_headers(ann),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["operation"]["quantity_scrapped"] == 3
        op_row = fresh(db_session, WorkOrderOperation, op.id)
        assert op_row.status == DONE
        assert (op_row.quantity_complete, op_row.quantity_scrapped, op_row.quantity_short_declared) == (7, 3, 3)
        (row,) = [r for r in op_audit_rows(db_session, op.id) if r.action == "COMPLETE_OPERATION"]
        assert (row.old_values["quantity_scrapped"], row.new_values["quantity_scrapped"]) == (1, 3)
        assert row.extra_data["scrap_declared_at_close"] == 2


class TestACrewBadgeIsAPerson:
    def test_a_crew_station_badge_token_may_close_short(self, client, db_session, admin_headers):
        """Kills widening the 403 to every non-interactive credential (a kiosk badge)."""
        wc = make_work_center(db_session)
        station = make_kiosk_station(db_session, work_center=wc)
        lead = make_user(db_session, first_name="Lena", last_name="Lead")
        minted = mint_badge_token(client, kiosk_token_for(station), lead.employee_id)
        assert minted.status_code == 200, minted.text
        badge = bearer(minted.json()["access_token"])

        ann = make_user(db_session)
        wo, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        add_entry(db_session, ann, op, produced=8)
        set_op(db_session, op, quantity_complete=8)

        resp = client.post(
            COMPLETE_URL.format(op=op.id), json={"quantity_scrapped": 2, "scrap_reason": "crack"}, headers=badge
        )
        assert resp.status_code == 200, resp.text
        op_row = fresh(db_session, WorkOrderOperation, op.id)
        assert (op_row.status, op_row.quantity_short_declared, op_row.completed_by) == (DONE, 2, lead.id)

        # The same person behind a standing API token is refused (the contrast this pins).
        _, (op_b,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        add_entry(db_session, ann, op_b, produced=8)
        set_op(db_session, op_b, quantity_complete=8)
        refused = client.post(
            COMPLETE_URL.format(op=op_b.id),
            json={"quantity_scrapped": 2, "scrap_reason": "crack"},
            headers=_token_headers(client, admin_headers, lead),
        )
        assert refused.status_code == 403, refused.text
        # Owner decision 2026-11-07: a token may not declare the scrap at all.
        assert refused.json()["detail"] == API_TOKEN_SCRAP


class TestAFullCountCloseIsUnchanged:
    """The untouched path: same rows as the gates tip wrote for the same scenario.

    Captured by running this scenario against ``origin/fix/floor-gates-draft-on-hold``
    (where ``quantity_complete`` was a required body field, hence it is sent here).
    """

    def test_floor_close_at_a_full_count_writes_the_same_rows(self, client, db_session):
        ann = make_user(db_session)
        wc = make_work_center(db_session)
        wo, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        sheet = tie_material(db_session, wo, op)
        entry = add_entry(db_session, ann, op, open_entry=True)
        report = client.post(
            f"/api/v1/shop-floor/operations/{op.id}/production",
            json={"quantity_complete_delta": 10},
            headers=user_headers(ann),
        )
        assert report.status_code == 200, report.text

        resp = client.post(
            COMPLETE_URL.format(op=op.id), json={"quantity_complete": 10, "source": "kiosk"}, headers=user_headers(ann)
        )
        assert resp.status_code == 200, resp.text

        db_session.expire_all()
        ledger = [
            (
                t.transaction_type,
                float(t.quantity),
                t.part_id,
                t.reference_type,
                t.reference_id,
                t.reference_number,
                t.allocation_id is not None,
                t.lot_number,
                t.from_location,
                t.to_location,
                float(t.unit_cost or 0),
            )
            for t in db_session.query(InventoryTransaction).order_by(InventoryTransaction.id).all()
        ]
        wo_number = wo.work_order_number
        assert ledger == [
            (
                TransactionType.ISSUE,
                -10.0,
                sheet.id,
                "work_order_operation",
                op.id,
                wo_number,
                True,
                ledger[0][7],
                "RAW-A",
                None,
                50.0,
            ),
            (
                TransactionType.RECEIVE,
                10.0,
                wo.part_id,
                "work_order",
                wo.id,
                wo_number,
                False,
                f"LOT-{wo_number}",
                None,
                "FINISHED-GOODS",
                5.0,
            ),
        ]
        assert ledger[0][7].startswith("SC-LOT-"), "the issue draws the tied sheet's own lot"
        (sheet_stock,) = db_session.query(InventoryItem).filter(InventoryItem.part_id == sheet.id).all()
        assert float(sheet_stock.quantity_on_hand) == 90
        op_row = fresh(db_session, WorkOrderOperation, op.id)
        assert (op_row.status, op_row.quantity_complete, op_row.quantity_scrapped, op_row.scrap_reason) == (
            DONE,
            10,
            0,
            None,
        )
        assert op_row.quantity_short_declared is None
        wo_row = fresh(db_session, WorkOrder, wo.id)
        assert (wo_row.status, wo_row.quantity_complete, float(wo_row.quantity_scrapped or 0)) == (
            WorkOrderStatus.COMPLETE,
            10,
            0,
        )
        entry_row = fresh(db_session, TimeEntry, entry.id)
        assert entry_row.clock_out is not None
        assert (float(entry_row.quantity_produced), float(entry_row.quantity_scrapped or 0)) == (10, 0)
        (row,) = [r for r in op_audit_rows(db_session, op.id) if r.action == "COMPLETE_OPERATION"]
        assert row.extra_data["closed_short"] is False
        assert row.extra_data["scrap_declared_at_close"] == 0
        assert row.extra_data["acknowledged_open_entries"] == []
        assert row.old_values["status"] == "in_progress" and row.new_values["status"] == "complete"


# --------------------------------------------------------------------------- #
# A stale kiosk clock-out after somebody closed the operation
# --------------------------------------------------------------------------- #
class TestAStaleClockOutAfterAClose:
    def test_after_an_acknowledged_office_short_close_the_entry_is_already_closed(self, client, db_session, supervisor):
        bob = make_user(db_session, first_name="Bob", last_name="Turner")
        ann = make_user(db_session)
        wc10, wc20 = make_work_center(db_session), make_work_center(db_session)
        wo, (op10, op20) = make_routing(db_session, work_centers=[wc10, wc20], statuses=[OPEN, OperationStatus.PENDING])
        add_entry(db_session, ann, op10, produced=8)
        bob_entry = add_entry(db_session, bob, op10, open_entry=True)
        set_op(db_session, op10, quantity_complete=8)
        closed = client.post(
            OFFICE_COMPLETE_URL.format(op=op10.id),
            params={
                "quantity_complete": 8,
                "quantity_scrapped": 2,
                "scrap_reason": "crack",
                "acknowledge_open_entries": True,
            },
            headers=user_headers(supervisor),
        )
        assert closed.status_code == 200, closed.text
        before = _world(db_session, wo)
        assert before["ops"][0][5] == 2, "declared 2"

        stale = client.post(
            CLOCK_OUT_URL.format(entry=bob_entry.id), json={"quantity_produced": 2}, headers=user_headers(bob)
        )
        assert stale.status_code == 400, stale.text
        assert stale.json()["detail"] == "Already clocked out"
        _assert_nothing_written(db_session, wo, before)
        # Downstream accounting is still the declared one: Op 20 can receive 8.
        figures = client.get(f"/api/v1/work-orders/{wo.id}", headers=user_headers(supervisor)).json()
        (op20_row,) = [o for o in figures["operations"] if o["id"] == op20.id]
        assert (op20_row["upstream_quantity_short"], op20_row["quantity_good_ceiling"]) == (2, 8)

    def test_after_a_full_count_office_close_pieces_are_refused_and_nothing_moves(self, client, db_session, supervisor):
        """SF4 through the real verbs (the other file sets the COMPLETE state directly)."""
        bob = make_user(db_session)
        wc10, wc20 = make_work_center(db_session), make_work_center(db_session)
        # Two stages, so closing Op 10 leaves the work order open (a terminal work order
        # takes any clock-out labor-only through G6-A, which would hide this rule).
        wo, (op, _) = make_routing(db_session, work_centers=[wc10, wc20], statuses=[OPEN, OperationStatus.PENDING])
        tie_material(db_session, wo, op)
        bob_entry = add_entry(db_session, bob, op, produced=10, open_entry=True)
        set_op(db_session, op, quantity_complete=10)
        closed = client.post(
            OFFICE_COMPLETE_URL.format(op=op.id), params={"quantity_complete": 10}, headers=user_headers(supervisor)
        )
        assert closed.status_code == 200, closed.text
        assert closed.json()["closed_time_entries"] == []
        before = _world(db_session, wo)

        stale = client.post(
            CLOCK_OUT_URL.format(entry=bob_entry.id), json={"quantity_produced": 2}, headers=user_headers(bob)
        )
        assert stale.status_code == 409, stale.text
        assert stale.json()["detail"] == completed_operation_clock_out_message(op)
        _assert_nothing_written(db_session, wo, before)


# --------------------------------------------------------------------------- #
# Office twin
# --------------------------------------------------------------------------- #
def _serialized_op(db: Session, *, recorded: float = 1):
    ann = make_user(db)
    wc = make_work_center(db)
    wo, (op,) = make_routing(
        db, work_centers=[wc], statuses=[OPEN], quantity_ordered=2, serial_numbers='["SN-1", "SN-2"]'
    )
    add_entry(db, ann, op, produced=recorded)
    set_op(db, op, quantity_complete=recorded)
    sheet = ProcessSheet(sheet_number=f"PS-PIN-{op.id}", title="Torque", revision="A", status="released", company_id=1)
    db.add(sheet)
    db.flush()
    db.add(
        WOOperationStep(
            company_id=1,
            work_order_operation_id=op.id,
            source_sheet_id=sheet.id,
            source_sheet_revision="A",
            sequence=1,
            label="Torque verified",
            step_type="checkbox",
            is_required=True,
        )
    )
    db.commit()
    return wo, op


class TestOfficeSerializedShort:
    def test_the_office_short_close_is_refused_explicitly_and_writes_nothing(self, client, db_session, supervisor):
        wo, op = _serialized_op(db_session)
        before = _world(db_session, wo)
        resp = client.post(
            OFFICE_COMPLETE_URL.format(op=op.id),
            params={"quantity_complete": 1, "quantity_scrapped": 1, "scrap_reason": "crack"},
            headers=user_headers(supervisor),
        )
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert isinstance(detail, str), "not the generic STEPS_INCOMPLETE payload"
        assert detail.startswith("Op 10 is 1 short on a serialized work order")
        assert "SN-1, SN-2" in detail
        _assert_nothing_written(db_session, wo, before)


class TestOfficeScrapTotalNeverLowers:
    def test_a_lower_scrap_total_still_counts_the_floors_scrap_in_the_decision(self, client, db_session, supervisor):
        """Kills ``new_scrap = quantity_scrapped`` (the write guard alone hides it elsewhere).

        The pre-PR client sent ``quantity_scrapped=0`` on every office close. With the floor's
        8 good + 2 scrapped recorded, that call must CLOSE the operation on the recorded
        scrap -- not read the 0 as "2 unaccounted" and answer "No change".
        """
        ann = make_user(db_session)
        wc = make_work_center(db_session)
        _, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        add_entry(db_session, ann, op, produced=8, scrapped=2)
        set_op(db_session, op, quantity_complete=8, quantity_scrapped=2, scrap_reason="burr")

        resp = client.post(
            OFFICE_COMPLETE_URL.format(op=op.id),
            params={"quantity_complete": 8, "quantity_scrapped": 0},
            headers=user_headers(supervisor),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "Operation completed"
        op_row = fresh(db_session, WorkOrderOperation, op.id)
        assert (op_row.status, op_row.quantity_scrapped, op_row.scrap_reason, op_row.quantity_short_declared) == (
            DONE,
            2,
            "burr",
            2,
        )


class TestEachRequiredAuditBranch:
    """Each OR-branch of the REQUIRED-evidence condition, alone, must fail closed."""

    def test_office_good_above_the_floor_count_alone(self, client, db_session, supervisor, monkeypatch):
        ann = make_user(db_session)
        wc = make_work_center(db_session)
        wo, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        add_entry(db_session, ann, op, produced=8)
        set_op(db_session, op, quantity_complete=8)
        before = _world(db_session, wo)
        monkeypatch.setattr(AuditService, "log", lambda self, *a, **k: None)

        resp = client.post(
            OFFICE_COMPLETE_URL.format(op=op.id),
            params={"quantity_complete": 10, "quantity_override_reason": "forgot to report 2"},
            headers=user_headers(supervisor),
        )
        assert resp.status_code == 503, resp.text
        assert resp.json()["detail"] == AUDIT_UNAVAILABLE
        _assert_nothing_written(db_session, wo, before)

    def test_office_scrap_rise_on_a_partial_alone(self, client, db_session, supervisor, monkeypatch):
        wc = make_work_center(db_session)
        wo, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        set_op(db_session, op, quantity_complete=4)
        before = _world(db_session, wo)
        monkeypatch.setattr(AuditService, "log", lambda self, *a, **k: None)

        resp = client.post(
            OFFICE_COMPLETE_URL.format(op=op.id),
            params={"quantity_complete": 4, "quantity_scrapped": 2, "scrap_reason": "crack"},
            headers=user_headers(supervisor),
        )
        assert resp.status_code == 503, resp.text
        _assert_nothing_written(db_session, wo, before)

    def test_office_partial_without_scrap_stays_best_effort(self, client, db_session, supervisor, monkeypatch):
        """The contrast: a plain progress save never needed required evidence."""
        wc = make_work_center(db_session)
        _, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        set_op(db_session, op, quantity_complete=4)
        monkeypatch.setattr(AuditService, "log", lambda self, *a, **k: None)
        resp = client.post(
            OFFICE_COMPLETE_URL.format(op=op.id), params={"quantity_complete": 6}, headers=user_headers(supervisor)
        )
        assert resp.status_code == 200, resp.text
        assert fresh(db_session, WorkOrderOperation, op.id).quantity_complete == 6

    def test_force_complete_above_the_floor_count_alone(self, client, db_session, supervisor, monkeypatch):
        wo, _, _ = _two_stage(db_session, op20_good=8)
        before = _world(db_session, wo)
        monkeypatch.setattr(AuditService, "log", lambda self, *a, **k: None)

        resp = client.post(
            FORCE_COMPLETE_URL.format(wo=wo.id),
            params={"quantity_complete": 10, "quantity_override_reason": "counted at pack-out"},
            headers=user_headers(supervisor),
        )
        assert resp.status_code == 503, resp.text
        assert resp.json()["detail"] == AUDIT_UNAVAILABLE
        _assert_nothing_written(db_session, wo, before)


# --------------------------------------------------------------------------- #
# Force-complete
# --------------------------------------------------------------------------- #
class TestForceComplete:
    def test_a_laser_pool_is_bounded_by_the_sum_of_what_each_nest_recorded(self, client, db_session, supervisor):
        ann = make_user(db_session)
        wc = make_work_center(db_session)
        wo, (nest1, nest2) = make_routing(
            db_session,
            work_centers=[wc, wc],
            quantity_ordered=5,
            sequential=False,
            statuses=[DONE, OPEN],
            wo_status=WorkOrderStatus.IN_PROGRESS,
            component_quantities=[2, 3],
        )
        set_op(db_session, fresh(db_session, WorkOrder, wo.id), work_order_type=WorkOrderType.LASER_CUTTING.value)
        add_entry(db_session, ann, nest1, produced=2)
        set_op(db_session, nest1, quantity_complete=2)
        add_entry(db_session, ann, nest2, produced=1)
        set_op(db_session, nest2, quantity_complete=1)
        before = _world(db_session, wo)

        refused = client.post(
            FORCE_COMPLETE_URL.format(wo=wo.id), params={"quantity_complete": 5}, headers=user_headers(supervisor)
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"].startswith("quantity_complete (5) exceeds the 3 the floor recorded")
        _assert_nothing_written(db_session, wo, before)

        resp = client.post(
            FORCE_COMPLETE_URL.format(wo=wo.id),
            params={"quantity_complete": 3, "quantity_scrapped": 2, "scrap_reason": "sheet warped"},
            headers=user_headers(supervisor),
        )
        assert resp.status_code == 200, resp.text
        wo_row = fresh(db_session, WorkOrder, wo.id)
        assert (wo_row.status, wo_row.quantity_complete, wo_row.quantity_scrapped) == (WorkOrderStatus.COMPLETE, 3, 2)

    @pytest.mark.parametrize("kind", ["above-recorded", "unaccounted"])
    def test_every_refusal_writes_nothing(self, client, db_session, supervisor, kind):
        wo, _, op20 = _two_stage(db_session, op20_good=8)
        before = _world(db_session, wo)
        quantity = 10 if kind == "above-recorded" else 8
        resp = client.post(
            FORCE_COMPLETE_URL.format(wo=wo.id),
            params={"quantity_complete": quantity},
            headers=user_headers(supervisor),
        )
        assert resp.status_code == 409, resp.text
        expected = (
            force_complete_exceeds_recorded_message(10, 8, op20)
            if kind == "above-recorded"
            else force_complete_unaccounted_message(2, 10, 8, 0)
        )
        assert resp.json()["detail"] == expected
        _assert_nothing_written(db_session, wo, before)

    def test_the_physical_refusal_writes_nothing(self, client, db_session, supervisor):
        ann = make_user(db_session)
        wc10, wc20 = make_work_center(db_session), make_work_center(db_session)
        wo, (op10, _) = make_routing(
            db_session, work_centers=[wc10, wc20], statuses=[DONE, READY], wo_status=WorkOrderStatus.IN_PROGRESS
        )
        add_entry(db_session, ann, op10, produced=8, scrapped=2)
        set_op(db_session, op10, quantity_complete=8, quantity_scrapped=2, quantity_short_declared=2)
        before = _world(db_session, wo)
        resp = client.post(
            FORCE_COMPLETE_URL.format(wo=wo.id),
            params={"quantity_complete": 10, "quantity_override_reason": "desk"},
            headers=user_headers(supervisor),
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == force_complete_exceeds_physical_message(10, 8, 2)
        _assert_nothing_written(db_session, wo, before)

    def test_the_api_token_refusal_writes_nothing(self, client, db_session, supervisor, admin_headers):
        wc = make_work_center(db_session)
        wo, _ = make_routing(db_session, work_centers=[wc, make_work_center(db_session)])
        token_headers = _token_headers(client, admin_headers, supervisor)
        before = _world(db_session, wo)
        resp = client.post(
            FORCE_COMPLETE_URL.format(wo=wo.id),
            params={"quantity_complete": 8, "quantity_scrapped": 2, "scrap_reason": "lost"},
            headers=token_headers,
        )
        assert resp.status_code == 403, resp.text
        # Owner decision 2026-11-07: the scrap rise is refused first, with its own text.
        assert resp.json()["detail"] == API_TOKEN_SCRAP
        _assert_nothing_written(db_session, wo, before)

    def test_the_labor_evidence_read_is_tenant_scoped(self, client, db_session, supervisor):
        """Invariant 1: a foreign-company entry on this operation id never raises the bound."""
        wo, _, op20 = _two_stage(db_session, op20_good=8)
        foreign = make_user(db_session, company_id=COMPANY_B)
        db_session.add(
            TimeEntry(
                user_id=foreign.id,
                work_order_id=wo.id,
                operation_id=op20.id,
                work_center_id=op20.work_center_id,
                entry_type=TimeEntryType.RUN,
                clock_in=datetime.utcnow() - timedelta(hours=2),
                clock_out=datetime.utcnow() - timedelta(hours=1),
                quantity_produced=10,
                quantity_scrapped=0,
                company_id=COMPANY_B,
            )
        )
        db_session.commit()
        before = _world(db_session, wo)

        resp = client.post(
            FORCE_COMPLETE_URL.format(wo=wo.id), params={"quantity_complete": 10}, headers=user_headers(supervisor)
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == force_complete_exceeds_recorded_message(10, 8, op20)
        _assert_nothing_written(db_session, wo, before)


class TestWhatTheCloseHandsOn:
    def test_an_acknowledged_office_short_close_rolls_up_the_hours_it_clocked_out(
        self, client, db_session, supervisor, monkeypatch
    ):
        """COST-3 still sees the entries the acknowledgement closed (nothing credited, hours kept)."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "LABOR_COST_ROLLUP_ENABLED", True)
        bob = make_user(db_session, first_name="Bob", last_name="Turner")
        ann = make_user(db_session)
        wc10, wc20 = make_work_center(db_session), make_work_center(db_session)
        wo, (op10, _) = make_routing(db_session, work_centers=[wc10, wc20], statuses=[OPEN, OperationStatus.PENDING])
        add_entry(db_session, ann, op10, produced=8)
        bob_entry = add_entry(db_session, bob, op10, open_entry=True)  # clocked in an hour ago
        set_op(db_session, op10, quantity_complete=8)
        hours_before = float(fresh(db_session, WorkOrderOperation, op10.id).actual_run_hours or 0)
        wo_hours_before = float(fresh(db_session, WorkOrder, wo.id).actual_hours or 0)

        resp = client.post(
            OFFICE_COMPLETE_URL.format(op=op10.id),
            params={
                "quantity_complete": 8,
                "quantity_scrapped": 2,
                "scrap_reason": "crack",
                "acknowledge_open_entries": True,
            },
            headers=user_headers(supervisor),
        )
        assert resp.status_code == 200, resp.text
        bob_row = fresh(db_session, TimeEntry, bob_entry.id)
        assert float(bob_row.quantity_produced or 0) == 0, "nothing credited to Bob"
        assert bob_row.duration_hours == pytest.approx(1.0, abs=0.05)
        op_hours = float(fresh(db_session, WorkOrderOperation, op10.id).actual_run_hours or 0)
        assert op_hours - hours_before == pytest.approx(bob_row.duration_hours)
        wo_hours = float(fresh(db_session, WorkOrder, wo.id).actual_hours or 0)
        assert wo_hours - wo_hours_before == pytest.approx(bob_row.duration_hours)

    def test_a_short_close_keeps_the_backflush_basis_at_every_unit_consumed(self, client, db_session, supervisor):
        """INT1's third leg: header 8 + 2 declared scrap -> the BOM basis is still 10."""
        from app.services.completion_inventory_service import _backflush_basis

        wo, _, _ = _two_stage(db_session, op20_good=8, op20_scrap=2)
        resp = client.post(
            FORCE_COMPLETE_URL.format(wo=wo.id), params={"quantity_complete": 8}, headers=user_headers(supervisor)
        )
        assert resp.status_code == 200, resp.text
        db_session.expire_all()
        wo_row = db_session.query(WorkOrder).filter(WorkOrder.id == wo.id).one()
        assert (wo_row.quantity_complete, _backflush_basis(wo_row)) == (8, 10)

        ann = make_user(db_session)
        wc = make_work_center(db_session)
        single, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        add_entry(db_session, ann, op, produced=8)
        set_op(db_session, op, quantity_complete=8)
        closed = client.post(
            COMPLETE_URL.format(op=op.id),
            json={"quantity_scrapped": 2, "scrap_reason": "crack"},
            headers=user_headers(ann),
        )
        assert closed.status_code == 200, closed.text
        db_session.expire_all()
        single_row = db_session.query(WorkOrder).filter(WorkOrder.id == single.id).one()
        assert (single_row.quantity_complete, _backflush_basis(single_row)) == (8, 10)


# --------------------------------------------------------------------------- #
# Reopen
# --------------------------------------------------------------------------- #
def test_a_reopen_clears_a_declared_short(client, db_session, supervisor):
    """The other reopen test pre-clears the declaration, so it cannot see this."""
    ann = make_user(db_session)
    wc10, wc20 = make_work_center(db_session), make_work_center(db_session)
    wo, (op10, op20) = make_routing(
        db_session, work_centers=[wc10, wc20], statuses=[DONE, READY], wo_status=WorkOrderStatus.IN_PROGRESS
    )
    add_entry(db_session, ann, op10, produced=9, scrapped=1)
    set_op(db_session, op10, quantity_complete=9, quantity_scrapped=1, scrap_reason="crack", quantity_short_declared=1)

    resp = client.post(
        OFFICE_REDUCE_URL.format(op=op10.id),
        json={"quantity_delta": 1, "reason": "counted one twice", "reopen_operation": True},
        headers=user_headers(supervisor),
    )
    assert resp.status_code == 200, resp.text
    op10_row = fresh(db_session, WorkOrderOperation, op10.id)
    assert (op10_row.status, op10_row.quantity_complete, op10_row.quantity_scrapped) == (OPEN, 8, 1)
    assert op10_row.quantity_short_declared is None, "the next explicit close declares again"
    assert fresh(db_session, WorkOrderOperation, op20.id).status == OperationStatus.PENDING


# --------------------------------------------------------------------------- #
# The gates' rung on the floor close
# --------------------------------------------------------------------------- #
def test_a_ready_operation_on_a_held_job_writes_nothing_on_the_floor_close(client, db_session):
    """Combined pin, floor side: the gates' rung outranks short-shaped input -- and writes nothing."""
    ann = make_user(db_session)
    wc = make_work_center(db_session)
    wo, (op,) = make_routing(db_session, work_centers=[wc], statuses=[READY], wo_status=WorkOrderStatus.ON_HOLD)
    add_entry(db_session, ann, op, produced=8)
    before = _world(db_session, wo)

    for body in ({}, {"quantity_scrapped": 2, "scrap_reason": "crack"}, {"quantity_scrapped": -1}):
        resp = client.post(COMPLETE_URL.format(op=op.id), json=body, headers=user_headers(ann))
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == MSG_WORK_ORDER_ON_HOLD
        _assert_nothing_written(db_session, wo, before)
    assert fresh(db_session, WorkOrderOperation, op.id).actual_start is None


def test_scrap_without_a_reason_on_a_held_ready_operation_gets_the_gates_answer(client, db_session):
    """The scrap-reason rule runs in the handler AFTER the gates' work-order rung (the office
    twin's ladder), not as a schema validator that would answer 422 first -- and nothing is
    written either way."""
    ann = make_user(db_session)
    wc = make_work_center(db_session)
    wo, (op,) = make_routing(db_session, work_centers=[wc], statuses=[READY], wo_status=WorkOrderStatus.ON_HOLD)
    add_entry(db_session, ann, op, produced=8)
    before = _world(db_session, wo)
    resp = client.post(COMPLETE_URL.format(op=op.id), json={"quantity_scrapped": 2}, headers=user_headers(ann))
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == MSG_WORK_ORDER_ON_HOLD
    _assert_nothing_written(db_session, wo, before)


def test_scrap_without_a_reason_on_a_running_operation_is_still_422(client, db_session):
    ann = make_user(db_session)
    wc = make_work_center(db_session)
    wo, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OperationStatus.IN_PROGRESS])
    add_entry(db_session, ann, op, produced=8)
    before = _world(db_session, wo)
    resp = client.post(COMPLETE_URL.format(op=op.id), json={"quantity_scrapped": 2}, headers=user_headers(ann))
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == MSG_SCRAP_REASON_OR_CODE_REQUIRED
    _assert_nothing_written(db_session, wo, before)
