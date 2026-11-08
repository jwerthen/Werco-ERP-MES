"""Wave 2 cross-review fixes on the short-count branch (owner ruling 1 + the 2026-11-07 token decision).

Each class pins one finding, against the real route, and each refusal is pinned as writing
NOTHING (rows, ledger, audit chain, events):

* ``TestApiTokenReduceProduction`` (S1) -- a standing API token may not correct counts on
  either reduce-production verb: a reopen (it would erase a person's short close and walk other
  operators' labor down), a re-stamp (``API_TOKEN_SHORT_CLOSE`` -- it re-declares a short) and a
  plain lowering alike. This is THIS PR's extension of the 2026-11-07 decision, which covered
  scrap and short closes only. Refused before the lock (route) and again under it (core).
* ``TestFloorCloseAuditsTheWorkOrder`` (S2) -- the floor close that finishes a job writes the
  work order's STATUS_CHANGE row, as its clock-out and office twins do.
* ``TestCompletedOperationScrapRiseIsBounded`` (S14) -- on a COMPLETE operation the generic PUT
  lets scrap rise only into what is still unaccounted.
* ``TestApiTokenScrapReason`` (S18) -- a token may not write or change a scrap REASON, even with
  no new scrap.
* ``TestClockOutRefusesNonFiniteQuantities`` (S19) -- NaN / infinity are a 400 for everyone.
* ``TestApiTokenWorkOrderHeaderCounts`` (S20) -- a token may not change the header's
  ``quantity_complete`` / ``quantity_scrapped`` in either direction (this PR's extension).
"""

from datetime import datetime

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.inventory import InventoryTransaction
from app.models.operational_event import OperationalEvent
from app.models.time_entry import TimeEntry
from app.models.user import User, UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services.audit_service import AuditService
from app.services.operation_action_gates import (
    API_TOKEN_COUNT_JUDGMENT,
    API_TOKEN_SCRAP,
    API_TOKEN_SHORT_CLOSE,
    api_token_changes_scrap_reason,
    completed_scrap_over_received_message,
)
from app.services.production_reduction_service import api_token_reduction_refusal, perform_production_reduction
from app.services.work_order_state_service import operation_accounting
from tests.api.kiosk_test_helpers import (
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
    make_routing,
    make_scrap_code,
    set_op,
    tie_material,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

API_TOKENS_URL = "/api/v1/api-tokens/"
OFFICE_REDUCE_URL = "/api/v1/work-orders/operations/{op}/reduce-production"
FLOOR_REDUCE_URL = "/api/v1/shop-floor/operations/{op}/reduce-production"
OPERATION_PUT_URL = "/api/v1/work-orders/operations/{op}"
WORK_ORDER_URL = "/api/v1/work-orders/{wo}"
FORCE_COMPLETE_URL = "/api/v1/work-orders/{wo}/complete"

DONE = OperationStatus.COMPLETE
OPEN = OperationStatus.IN_PROGRESS
READY = OperationStatus.READY
PENDING = OperationStatus.PENDING


@pytest.fixture(autouse=True)
def _no_completion_signal_enqueue(monkeypatch):
    """Skip the post-commit ARQ enqueue (it retries a missing Redis for seconds)."""
    monkeypatch.setattr("app.api.endpoints.work_orders.enqueue_work_order_completion_signals", lambda **_: None)
    monkeypatch.setattr("app.api.endpoints.shop_floor.enqueue_work_order_completion_signals", lambda **_: None)


@pytest.fixture
def supervisor(db_session: Session) -> User:
    return make_user(db_session, role=UserRole.SUPERVISOR, first_name="Sue", last_name="Visor")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _token(client: TestClient, admin_headers: dict, user: User) -> dict:
    """A standing API token bound to ``user``."""
    issued = client.post(
        API_TOKENS_URL, json={"user_id": user.id, "label": "cross-review probe"}, headers=admin_headers
    )
    assert issued.status_code == 201, issued.text
    return {"Authorization": f"Bearer {issued.json()['token']}", "X-Requested-With": "XMLHttpRequest"}


def _badge(client: TestClient, db: Session, work_center, user: User) -> dict:
    station = make_kiosk_station(db, work_center=work_center)
    minted = mint_badge_token(client, kiosk_token_for(station), user.employee_id)
    assert minted.status_code == 200, minted.text
    return bearer(minted.json()["access_token"])


def _world(db: Session) -> dict:
    """Every row these verbs could touch, plus the append-only tables' sizes."""
    modified = [obj for obj in db.dirty if db.is_modified(obj)]
    assert not modified and not db.new, f"pending changes in the session: {modified} {db.new}"
    db.expire_all()
    return {
        "operations": [
            (
                op.id,
                op.status,
                float(op.quantity_complete or 0),
                float(op.quantity_scrapped or 0),
                op.scrap_reason,
                op.scrap_reason_code_id,
                op.quantity_short_declared,
                op.actual_end,
                op.version,
            )
            for op in db.query(WorkOrderOperation).order_by(WorkOrderOperation.id).all()
        ],
        "entries": [
            (
                e.id,
                e.clock_out,
                float(e.quantity_produced or 0),
                float(e.quantity_scrapped or 0),
                e.scrap_reason,
                e.scrap_reason_code_id,
                e.version,
            )
            for e in db.query(TimeEntry).order_by(TimeEntry.id).all()
        ],
        "work_orders": [
            (
                wo.id,
                wo.status,
                float(wo.quantity_complete or 0),
                float(wo.quantity_scrapped or 0),
                wo.scrap_reason,
                wo.scrap_reason_code_id,
                wo.version,
            )
            for wo in db.query(WorkOrder).order_by(WorkOrder.id).all()
        ],
        "audit_rows": db.query(AuditLog).count(),
        "ledger_rows": db.query(InventoryTransaction).count(),
        "events": db.query(OperationalEvent).count(),
    }


def _refused(db: Session, resp, before: dict, *, status: int = 403, detail: str) -> None:
    assert resp.status_code == status, resp.text
    assert resp.json()["detail"] == detail
    assert _world(db) == before


def _reduce(client: TestClient, url: str, op, headers: dict, *, delta: float, reopen: bool = False):
    body = {"quantity_delta": delta, "reason": "counted the tray twice"}
    if reopen:
        body["reopen_operation"] = True
    return client.post(url.format(op=op.id), json=body, headers=headers)


def _wo_audit_rows(db: Session, work_order_id: int) -> list:
    db.expire_all()
    return (
        db.query(AuditLog)
        .filter(AuditLog.resource_type == "work_order", AuditLog.resource_id == work_order_id)
        .order_by(AuditLog.id)
        .all()
    )


# --------------------------------------------------------------------------- #
# S1 -- a standing API token may not correct counts on either reduce-production verb
# --------------------------------------------------------------------------- #
class TestApiTokenReduceProduction:
    def _completed_first_stage(self, db: Session, *, op10_scrap: float = 0.0):
        """op10 COMPLETE at 10 good (Ann's closed entry) + ``op10_scrap``; op20 READY."""
        ann = make_user(db, first_name="Ann")
        wo, (op10, op20) = make_routing(
            db,
            work_centers=[make_work_center(db), make_work_center(db)],
            statuses=[DONE, READY],
            wo_status=WorkOrderStatus.IN_PROGRESS,
        )
        add_entry(db, ann, op10, produced=10)
        set_op(
            db,
            op10,
            quantity_complete=10,
            quantity_scrapped=op10_scrap,
            scrap_reason="crack" if op10_scrap else None,
            actual_end=datetime.utcnow(),
        )
        return wo, op10, op20

    def test_a_token_reopen_is_refused_writing_nothing(self, client, db_session, admin_headers, supervisor):
        """The probe: a token reopened op10 (op20 READY -> PENDING, Ann's entry walked down)."""
        _, op10, _ = self._completed_first_stage(db_session)
        headers = _token(client, admin_headers, supervisor)
        before = _world(db_session)

        resp = _reduce(client, OFFICE_REDUCE_URL, op10, headers, delta=1, reopen=True)
        _refused(db_session, resp, before, detail=API_TOKEN_COUNT_JUDGMENT)

    def test_the_refusal_comes_before_the_leave_short_409(self, client, db_session, admin_headers, supervisor):
        _, op10, _ = self._completed_first_stage(db_session)
        headers = _token(client, admin_headers, supervisor)
        before = _world(db_session)

        resp = _reduce(client, OFFICE_REDUCE_URL, op10, headers, delta=1)
        _refused(db_session, resp, before, detail=API_TOKEN_COUNT_JUDGMENT)

    def test_a_token_restamp_is_refused_as_a_short_close(self, client, db_session, admin_headers, supervisor):
        """10 good + 2 scrap on 10: lowering good by 1 keeps it accounted and would RE-DECLARE a
        short of 1 -- a short close by another door, so it gets the short-close text."""
        _, op10, _ = self._completed_first_stage(db_session, op10_scrap=2)
        headers = _token(client, admin_headers, supervisor)
        before = _world(db_session)

        resp = _reduce(client, OFFICE_REDUCE_URL, op10, headers, delta=1)
        _refused(db_session, resp, before, detail=API_TOKEN_SHORT_CLOSE)

    def test_a_token_lowering_an_open_operation_is_refused(self, client, db_session, admin_headers, supervisor):
        ann = make_user(db_session)
        _, (op,) = make_routing(db_session, work_centers=[make_work_center(db_session)], statuses=[OPEN])
        add_entry(db_session, ann, op, produced=4)
        set_op(db_session, op, quantity_complete=4)
        headers = _token(client, admin_headers, supervisor)
        before = _world(db_session)

        resp = _reduce(client, OFFICE_REDUCE_URL, op, headers, delta=1)
        _refused(db_session, resp, before, detail=API_TOKEN_COUNT_JUDGMENT)

    def test_the_refusal_comes_before_the_allowance_400(self, client, db_session, admin_headers, supervisor):
        ann = make_user(db_session)
        _, (op,) = make_routing(db_session, work_centers=[make_work_center(db_session)], statuses=[OPEN])
        add_entry(db_session, ann, op, produced=4)
        set_op(db_session, op, quantity_complete=4)
        person = _reduce(client, OFFICE_REDUCE_URL, op, user_headers(supervisor), delta=9)
        assert person.status_code == 400, "the person's refusal this ordering is measured against"
        headers = _token(client, admin_headers, supervisor)
        before = _world(db_session)

        resp = _reduce(client, OFFICE_REDUCE_URL, op, headers, delta=9)
        _refused(db_session, resp, before, detail=API_TOKEN_COUNT_JUDGMENT)

    def test_a_person_at_the_desk_may_still_reopen(self, client, db_session, supervisor):
        _, op10, op20 = self._completed_first_stage(db_session)
        resp = _reduce(client, OFFICE_REDUCE_URL, op10, user_headers(supervisor), delta=1, reopen=True)
        assert resp.status_code == 200, resp.text
        assert fresh(db_session, WorkOrderOperation, op10.id).status == OPEN
        assert fresh(db_session, WorkOrderOperation, op20.id).status == PENDING

    def test_a_token_on_the_floor_twin_is_refused_writing_nothing(self, client, db_session, admin_headers):
        """The floor verb let a token lower an IN_PROGRESS count (200) -- half the parity gap."""
        ann = make_user(db_session)
        _, (op,) = make_routing(db_session, work_centers=[make_work_center(db_session)], statuses=[OPEN])
        add_entry(db_session, ann, op, produced=4, open_entry=True)
        set_op(db_session, op, quantity_complete=4)
        headers = _token(client, admin_headers, ann)
        before = _world(db_session)

        resp = _reduce(client, FLOOR_REDUCE_URL, op, headers, delta=1)
        _refused(db_session, resp, before, detail=API_TOKEN_COUNT_JUDGMENT)

    def test_a_crew_badge_on_the_floor_twin_is_a_person(self, client, db_session):
        lead = make_user(db_session, first_name="Lena", last_name="Lead")
        wc = make_work_center(db_session)
        _, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        entry = add_entry(db_session, lead, op, produced=4, open_entry=True)
        set_op(db_session, op, quantity_complete=4)

        resp = _reduce(client, FLOOR_REDUCE_URL, op, _badge(client, db_session, wc, lead), delta=1)
        assert resp.status_code == 200, resp.text
        assert float(fresh(db_session, TimeEntry, entry.id).quantity_produced) == 3
        assert float(fresh(db_session, WorkOrderOperation, op.id).quantity_complete) == 3

    def test_the_shared_core_refuses_under_the_lock_too(self, db_session, supervisor):
        """A future caller that forgets the route-level check is still refused by the core,
        before the walk and any write."""
        ann = make_user(db_session)
        _, (op,) = make_routing(db_session, work_centers=[make_work_center(db_session)], statuses=[OPEN])
        entry = add_entry(db_session, ann, op, produced=4)
        set_op(db_session, op, quantity_complete=4)
        supervisor._api_token_id = 4242  # what get_current_user marks for an API-token request
        try:
            before = _world(db_session)
            with pytest.raises(HTTPException) as refused:
                perform_production_reduction(
                    db_session,
                    operation_id=op.id,
                    company_id=op.company_id,
                    actor=supervisor,
                    audit=AuditService(db_session, supervisor),
                    entries=[fresh(db_session, TimeEntry, entry.id)],
                    delta=1,
                    reason="core probe",
                    notes=None,
                    recorded_source=None,
                    notes_entry=None,
                    event_source_module="work_orders",
                    path="office",
                    allow_completed_operation=True,
                )
            assert (refused.value.status_code, refused.value.detail) == (403, API_TOKEN_COUNT_JUDGMENT)
            db_session.rollback()
            assert _world(db_session) == before
        finally:
            supervisor._api_token_id = None

    def test_the_predicate_never_refuses_a_person(self, db_session, supervisor):
        wo, op10, _ = self._completed_first_stage(db_session, op10_scrap=2)
        assert api_token_reduction_refusal(supervisor, wo, op10, 1) is None


# --------------------------------------------------------------------------- #
# S2 -- the floor close that finishes the job audits the work order's transition
# --------------------------------------------------------------------------- #
class TestFloorCloseAuditsTheWorkOrder:
    @pytest.mark.parametrize("wo_status", [WorkOrderStatus.IN_PROGRESS, WorkOrderStatus.ON_HOLD])
    def test_completing_the_last_operation_writes_the_work_order_row(self, client, db_session, wo_status):
        ann = make_user(db_session)
        wo, (op,) = make_routing(
            db_session, work_centers=[make_work_center(db_session)], statuses=[OPEN], wo_status=wo_status
        )
        add_entry(db_session, ann, op, produced=10)
        set_op(db_session, op, quantity_complete=10)
        assert _wo_audit_rows(db_session, wo.id) == []

        resp = client.post(COMPLETE_URL.format(op=op.id), json={}, headers=user_headers(ann))
        assert resp.status_code == 200, resp.text
        assert fresh(db_session, WorkOrder, wo.id).status == WorkOrderStatus.COMPLETE
        rows = [r for r in _wo_audit_rows(db_session, wo.id) if r.action == "STATUS_CHANGE"]
        assert [(r.old_values, r.new_values) for r in rows] == [({"status": wo_status.value}, {"status": "complete"})]
        assert rows[0].user_id == ann.id
        assert "via shop-floor completion" in rows[0].description

    def test_a_close_that_leaves_work_open_writes_no_work_order_row(self, client, db_session):
        ann = make_user(db_session)
        wo, (op10, _) = make_routing(
            db_session,
            work_centers=[make_work_center(db_session), make_work_center(db_session)],
            statuses=[OPEN, PENDING],
            wo_status=WorkOrderStatus.IN_PROGRESS,
        )
        add_entry(db_session, ann, op10, produced=10)
        set_op(db_session, op10, quantity_complete=10)

        resp = client.post(COMPLETE_URL.format(op=op10.id), json={}, headers=user_headers(ann))
        assert resp.status_code == 200, resp.text
        assert fresh(db_session, WorkOrder, wo.id).status != WorkOrderStatus.COMPLETE
        assert [r for r in _wo_audit_rows(db_session, wo.id) if r.action == "STATUS_CHANGE"] == []


# --------------------------------------------------------------------------- #
# S14 -- scrap on a COMPLETE operation may rise only into what is unaccounted
# --------------------------------------------------------------------------- #
class TestCompletedOperationScrapRiseIsBounded:
    def _completed(self, db: Session, *, good: float):
        wo, (op10, op20) = make_routing(
            db,
            work_centers=[make_work_center(db), make_work_center(db)],
            statuses=[DONE, READY],
            wo_status=WorkOrderStatus.IN_PROGRESS,
        )
        tie_material(db, wo, op10)
        set_op(db, op10, quantity_complete=good)
        return wo, op10

    def test_a_rise_past_what_reached_the_operation_is_refused_writing_nothing(self, client, db_session, supervisor):
        """The probe: 10 good on 10, +6 scrap -> 16 accounted of 10 (16 sheets consumed at WO close)."""
        wo, op10 = self._completed(db_session, good=10)
        acct = operation_accounting(wo, op10)
        before = _world(db_session)

        resp = client.put(
            OPERATION_PUT_URL.format(op=op10.id),
            json={"version": op10.version, "quantity_scrapped": 6, "scrap_reason": "found later"},
            headers=user_headers(supervisor),
        )
        _refused(db_session, resp, before, status=409, detail=completed_scrap_over_received_message(op10, acct))
        assert resp.json()["detail"] == (
            "Op 10 is complete with 10 good and 0 scrapped of the 10 that reached it, so only 0 more can be recorded "
            "as scrap. Pieces counted good and later found bad: lower the good count with reduce-production (reopen "
            "the operation), then complete it again with the scrap and a reason."
        )

    def test_a_legacy_short_close_may_have_its_remainder_scrapped(self, client, db_session, supervisor):
        wo, op10 = self._completed(db_session, good=8)
        too_much = client.put(
            OPERATION_PUT_URL.format(op=op10.id),
            json={"version": op10.version, "quantity_scrapped": 3, "scrap_reason": "found later"},
            headers=user_headers(supervisor),
        )
        assert too_much.status_code == 409, too_much.text
        assert fresh(db_session, WorkOrderOperation, op10.id).quantity_scrapped == 0

        resp = client.put(
            OPERATION_PUT_URL.format(op=op10.id),
            json={"version": op10.version, "quantity_scrapped": 2, "scrap_reason": "found later"},
            headers=user_headers(supervisor),
        )
        assert resp.status_code == 200, resp.text
        row = fresh(db_session, WorkOrderOperation, op10.id)
        assert (row.status, float(row.quantity_complete), float(row.quantity_scrapped)) == (DONE, 8, 2)

    def test_a_reason_correction_on_an_accounted_close_still_lands(self, client, db_session, supervisor):
        _, op10 = self._completed(db_session, good=10)
        resp = client.put(
            OPERATION_PUT_URL.format(op=op10.id),
            json={"version": op10.version, "scrap_reason": "worded better"},
            headers=user_headers(supervisor),
        )
        assert resp.status_code == 200, resp.text
        assert fresh(db_session, WorkOrderOperation, op10.id).scrap_reason == "worded better"


# --------------------------------------------------------------------------- #
# S18 -- a token may not write or change a scrap REASON, even with no new scrap
# --------------------------------------------------------------------------- #
class TestApiTokenScrapReason:
    def test_the_predicate(self):
        token, badge, person = (
            type("U", (), {"_api_token_id": 7, "_token_scope": "api"})(),
            type("U", (), {"_api_token_id": None, "_token_scope": "kiosk"})(),
            type("U", (), {})(),
        )
        assert api_token_changes_scrap_reason(token, ("relabel", "crack"))
        assert api_token_changes_scrap_reason(token, (None, "crack")), "erasing a reason is a change"
        assert api_token_changes_scrap_reason(token, ("crack", "crack"), (3, None))
        assert not api_token_changes_scrap_reason(token, ("crack", "crack"), (None, None))
        assert not api_token_changes_scrap_reason(token)
        assert not api_token_changes_scrap_reason(badge, ("relabel", "crack"))
        assert not api_token_changes_scrap_reason(person, ("relabel", "crack"))

    def _running(self, db: Session, user: User, *, entry_reason=None, entry_code_id=None):
        wc = make_work_center(db)
        _, (op,) = make_routing(db, work_centers=[wc], statuses=[OPEN])
        entry = add_entry(db, user, op, open_entry=True)
        if entry_reason or entry_code_id:
            entry.scrap_reason = entry_reason
            entry.scrap_reason_code_id = entry_code_id
            db.commit()
        return wc, op, entry

    @pytest.mark.parametrize("body_key", ["scrap_reason", "scrap_reason_code_id"])
    def test_clock_out_with_zero_scrap_may_not_write_a_reason(self, client, db_session, admin_headers, body_key):
        ann = make_user(db_session)
        _, _, entry = self._running(db_session, ann, entry_reason="crack (person saw it)")
        value = "token relabel" if body_key == "scrap_reason" else make_scrap_code(db_session).id
        headers = _token(client, admin_headers, ann)
        before = _world(db_session)

        resp = client.post(
            CLOCK_OUT_URL.format(entry=entry.id), json={"quantity_produced": 1, body_key: value}, headers=headers
        )
        _refused(db_session, resp, before, detail=API_TOKEN_SCRAP)
        assert fresh(db_session, TimeEntry, entry.id).clock_out is None, "the entry stays open for a retry"

    def test_clock_out_refusal_comes_before_the_code_lookup(self, client, db_session, admin_headers):
        ann = make_user(db_session)
        _, _, entry = self._running(db_session, ann)
        headers = _token(client, admin_headers, ann)
        before = _world(db_session)
        resp = client.post(
            CLOCK_OUT_URL.format(entry=entry.id), json={"scrap_reason_code_id": 987654321}, headers=headers
        )
        _refused(db_session, resp, before, detail=API_TOKEN_SCRAP)

    def test_clock_out_re_sending_the_stored_reason_still_works(self, client, db_session, admin_headers):
        ann = make_user(db_session)
        _, op, entry = self._running(db_session, ann, entry_reason="crack")
        resp = client.post(
            CLOCK_OUT_URL.format(entry=entry.id),
            json={"quantity_produced": 1, "scrap_reason": "crack"},
            headers=_token(client, admin_headers, ann),
        )
        assert resp.status_code == 200, resp.text
        row = fresh(db_session, TimeEntry, entry.id)
        assert (row.clock_out is not None, row.scrap_reason) == (True, "crack")

    def test_a_person_clocking_out_may_still_write_a_reason(self, client, db_session):
        ann = make_user(db_session)
        _, _, entry = self._running(db_session, ann)
        resp = client.post(
            CLOCK_OUT_URL.format(entry=entry.id),
            json={"quantity_produced": 1, "scrap_reason": "noted"},
            headers=user_headers(ann),
        )
        assert resp.status_code == 200, resp.text
        assert fresh(db_session, TimeEntry, entry.id).scrap_reason == "noted"

    def test_a_crew_badge_clocking_out_may_write_a_reason(self, client, db_session):
        lead = make_user(db_session, first_name="Lena", last_name="Lead")
        wc, _, entry = self._running(db_session, lead)
        resp = client.post(
            CLOCK_OUT_URL.format(entry=entry.id),
            json={"quantity_produced": 1, "scrap_reason": "noted"},
            headers=_badge(client, db_session, wc, lead),
        )
        assert resp.status_code == 200, resp.text
        assert fresh(db_session, TimeEntry, entry.id).scrap_reason == "noted"

    def _scrapped_header(self, db: Session):
        wo, (op,) = make_routing(db, work_centers=[make_work_center(db)], statuses=[OPEN])
        wo.quantity_scrapped = 1
        wo.scrap_reason = "crack (person saw it)"
        db.commit()
        db.refresh(wo)
        return wo, op

    @pytest.mark.parametrize("sent", ["token relabel", None])
    def test_work_order_put_may_not_change_the_header_reason(self, client, db_session, admin_headers, supervisor, sent):
        wo, _ = self._scrapped_header(db_session)
        headers = _token(client, admin_headers, supervisor)
        before = _world(db_session)
        resp = client.put(
            WORK_ORDER_URL.format(wo=wo.id), json={"version": wo.version, "scrap_reason": sent}, headers=headers
        )
        _refused(db_session, resp, before, detail=API_TOKEN_SCRAP)

    def test_work_order_put_re_sending_the_stored_reason_still_works(
        self, client, db_session, admin_headers, supervisor
    ):
        wo, _ = self._scrapped_header(db_session)
        resp = client.put(
            WORK_ORDER_URL.format(wo=wo.id),
            json={"version": wo.version, "scrap_reason": "crack (person saw it)", "notes": "agent note"},
            headers=_token(client, admin_headers, supervisor),
        )
        assert resp.status_code == 200, resp.text
        assert fresh(db_session, WorkOrder, wo.id).notes == "agent note"

    def test_a_person_may_correct_the_header_reason(self, client, db_session, supervisor):
        wo, _ = self._scrapped_header(db_session)
        resp = client.put(
            WORK_ORDER_URL.format(wo=wo.id),
            json={"version": wo.version, "scrap_reason": "worded better"},
            headers=user_headers(supervisor),
        )
        assert resp.status_code == 200, resp.text
        assert fresh(db_session, WorkOrder, wo.id).scrap_reason == "worded better"

    def _full_count(self, db: Session):
        ann = make_user(db)
        wo, op = self._scrapped_header(db)
        add_entry(db, ann, op, produced=10)
        set_op(db, op, quantity_complete=10)
        return wo

    @pytest.mark.parametrize("params", [{"scrap_reason": "token relabel"}, {}])
    def test_force_complete_re_sending_the_scrap_may_not_relabel_or_erase_the_reason(
        self, client, db_session, admin_headers, supervisor, params
    ):
        wo = self._full_count(db_session)
        headers = _token(client, admin_headers, supervisor)
        before = _world(db_session)
        resp = client.post(
            FORCE_COMPLETE_URL.format(wo=wo.id),
            params={"quantity_complete": 10, "quantity_scrapped": 1, **params},
            headers=headers,
        )
        _refused(db_session, resp, before, detail=API_TOKEN_SCRAP)

    def test_force_complete_re_sending_the_scrap_and_its_reason_still_works(
        self, client, db_session, admin_headers, supervisor
    ):
        wo = self._full_count(db_session)
        resp = client.post(
            FORCE_COMPLETE_URL.format(wo=wo.id),
            params={"quantity_complete": 10, "quantity_scrapped": 1, "scrap_reason": "crack (person saw it)"},
            headers=_token(client, admin_headers, supervisor),
        )
        assert resp.status_code == 200, resp.text
        row = fresh(db_session, WorkOrder, wo.id)
        assert (row.status, row.scrap_reason) == (WorkOrderStatus.COMPLETE, "crack (person saw it)")

    def test_operation_put_reason_change_gets_the_scrap_text(self, client, db_session, admin_headers, supervisor):
        _, (op,) = make_routing(db_session, work_centers=[make_work_center(db_session)], statuses=[OPEN])
        set_op(db_session, op, quantity_complete=4, quantity_scrapped=1, scrap_reason="crack")
        headers = _token(client, admin_headers, supervisor)
        before = _world(db_session)
        resp = client.put(
            OPERATION_PUT_URL.format(op=op.id), json={"version": op.version, "scrap_reason": "relabel"}, headers=headers
        )
        _refused(db_session, resp, before, detail=API_TOKEN_SCRAP)


# --------------------------------------------------------------------------- #
# S19 -- a clock-out quantity that is not a finite number is a 400 for everyone
# --------------------------------------------------------------------------- #
class TestClockOutRefusesNonFiniteQuantities:
    @pytest.mark.parametrize(
        "body",
        [
            b'{"quantity_produced": NaN}',
            b'{"quantity_produced": Infinity}',
            b'{"quantity_produced": 1, "quantity_scrapped": NaN, "scrap_reason": "x"}',
            b'{"quantity_produced": 1, "quantity_scrapped": Infinity, "scrap_reason": "x"}',
            b'{"quantity_produced": -Infinity}',
        ],
    )
    def test_a_person_s_non_finite_quantity_is_refused_writing_nothing(self, client, db_session, body):
        """It used to close the entry, commit NaN (NULL on SQLite) into the labor record and the
        operation, and then 500."""
        ann = make_user(db_session)
        _, (op,) = make_routing(db_session, work_centers=[make_work_center(db_session)], statuses=[OPEN])
        entry = add_entry(db_session, ann, op, open_entry=True)
        before = _world(db_session)

        resp = client.post(
            CLOCK_OUT_URL.format(entry=entry.id),
            content=body,
            headers={**user_headers(ann), "Content-Type": "application/json"},
        )
        _refused(db_session, resp, before, status=400, detail="Quantity must be a valid number")
        assert fresh(db_session, TimeEntry, entry.id).clock_out is None

    def test_a_finite_clock_out_is_unchanged(self, client, db_session):
        ann = make_user(db_session)
        _, (op,) = make_routing(db_session, work_centers=[make_work_center(db_session)], statuses=[OPEN])
        entry = add_entry(db_session, ann, op, open_entry=True)
        resp = client.post(
            CLOCK_OUT_URL.format(entry=entry.id), json={"quantity_produced": 3}, headers=user_headers(ann)
        )
        assert resp.status_code == 200, resp.text
        assert float(fresh(db_session, WorkOrderOperation, op.id).quantity_complete) == 3


# --------------------------------------------------------------------------- #
# S20 -- a token may not change the work-order header's counts, either direction
# --------------------------------------------------------------------------- #
class TestApiTokenWorkOrderHeaderCounts:
    def _header(self, db: Session):
        wo, _ = make_routing(db, work_centers=[make_work_center(db)], statuses=[OPEN])
        wo.quantity_complete = 4
        wo.quantity_scrapped = 2
        wo.scrap_reason = "crack"
        db.commit()
        db.refresh(wo)
        return wo

    @pytest.mark.parametrize(
        "fields",
        [
            {"quantity_complete": 6},
            {"quantity_complete": 3},
            {"quantity_scrapped": 1, "scrap_reason": "crack"},
            {"quantity_scrapped": 0},
        ],
    )
    def test_a_token_changing_a_header_count_is_refused_writing_nothing(
        self, client, db_session, admin_headers, supervisor, fields
    ):
        wo = self._header(db_session)
        headers = _token(client, admin_headers, supervisor)
        before = _world(db_session)
        resp = client.put(WORK_ORDER_URL.format(wo=wo.id), json={"version": wo.version, **fields}, headers=headers)
        _refused(db_session, resp, before, detail=API_TOKEN_COUNT_JUDGMENT)

    def test_a_token_raising_the_header_scrap_keeps_the_scrap_text(self, client, db_session, admin_headers, supervisor):
        wo = self._header(db_session)
        headers = _token(client, admin_headers, supervisor)
        before = _world(db_session)
        resp = client.put(
            WORK_ORDER_URL.format(wo=wo.id),
            json={"version": wo.version, "quantity_scrapped": 3, "scrap_reason": "crack"},
            headers=headers,
        )
        _refused(db_session, resp, before, detail=API_TOKEN_SCRAP)

    def test_a_token_re_sending_the_recorded_counts_still_works(self, client, db_session, admin_headers, supervisor):
        wo = self._header(db_session)
        resp = client.put(
            WORK_ORDER_URL.format(wo=wo.id),
            json={
                "version": wo.version,
                "quantity_complete": 4,
                "quantity_scrapped": 2,
                "scrap_reason": "crack",
                "notes": "agent note",
            },
            headers=_token(client, admin_headers, supervisor),
        )
        assert resp.status_code == 200, resp.text
        row = fresh(db_session, WorkOrder, wo.id)
        assert (row.notes, float(row.quantity_complete), float(row.quantity_scrapped)) == ("agent note", 4, 2)

    def test_a_person_may_edit_the_header_counts(self, client, db_session, supervisor):
        wo = self._header(db_session)
        resp = client.put(
            WORK_ORDER_URL.format(wo=wo.id),
            json={"version": wo.version, "quantity_complete": 3},
            headers=user_headers(supervisor),
        )
        assert resp.status_code == 200, resp.text
        assert float(fresh(db_session, WorkOrder, wo.id).quantity_complete) == 3
