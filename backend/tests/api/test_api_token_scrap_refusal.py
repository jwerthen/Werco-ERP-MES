"""A standing API token may not declare scrap anywhere (owner decision 2026-11-07).

The question the owner answered: may a standing API token -- the Werco Assistant over MCP,
any bot -- close work short by reporting the missing pieces as scrap? The answer was "Refuse
API tokens": only a person (kiosk, crew badge, desk) declares scrap, because a scrap count is
quality evidence someone has to have seen; full-count closes by a token still work.

The rule, as every scrap-writing route reachable by a token implements it: a request
authenticated by an API token (``is_api_token_credential``) that would RAISE a recorded scrap
quantity -- an operation's, a time entry's or a work order header's ``quantity_scrapped`` --
is refused **403** with ``API_TOKEN_SCRAP``, before any write and before any audit row.

For each route, three pins:

1. an API token with scrap > 0 gets 403 and NOTHING is written (rows, ledger, audit,
   events, NCRs, receipts, Hank tasks);
2. the same token with zero scrap still succeeds exactly as before;
3. a crew-station badge token (``_token_scope == "kiosk"``, no ``_api_token_id``) with
   scrap succeeds -- or, where a badge cannot reach the route (``/work-orders``,
   ``/hank``), an interactive session does.

The short-close rule beside it (``API_TOKEN_SHORT_CLOSE``) is unchanged: a token's short
close whose scrap is ALREADY on record (the request declares none) is still refused with
that text, pinned here for each close verb.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.hank import HankTask
from app.models.inventory import InventoryTransaction
from app.models.operational_event import OperationalEvent
from app.models.production_receipt import ProductionReceipt
from app.models.quality import NonConformanceReport
from app.models.time_entry import TimeEntry
from app.models.user import User, UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services.operation_action_gates import (
    API_TOKEN_SCRAP,
    API_TOKEN_SHORT_CLOSE,
    api_token_declares_scrap,
)
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
    PRODUCTION_URL,
    add_entry,
    fresh,
    make_routing,
    set_op,
)
from tests.api.test_hank_tasks import execute, propose

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

API_TOKENS_URL = "/api/v1/api-tokens/"
OFFICE_COMPLETE_URL = "/api/v1/work-orders/operations/{op}/complete"
FORCE_COMPLETE_URL = "/api/v1/work-orders/{wo}/complete"
OPERATION_PUT_URL = "/api/v1/work-orders/operations/{op}"
WORK_ORDER_URL = "/api/v1/work-orders/{wo}"

DONE = OperationStatus.COMPLETE
OPEN = OperationStatus.IN_PROGRESS


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
def _mint_token(client: TestClient, admin_headers: dict, user: User) -> tuple[dict, int]:
    """A standing API token bound to ``user``: (headers, api_tokens row id)."""
    issued = client.post(API_TOKENS_URL, json={"user_id": user.id, "label": "scrap probe"}, headers=admin_headers)
    assert issued.status_code == 201, issued.text
    body = issued.json()
    return {"Authorization": f"Bearer {body['token']}", "X-Requested-With": "XMLHttpRequest"}, body["id"]


def _token(client: TestClient, admin_headers: dict, user: User) -> dict:
    return _mint_token(client, admin_headers, user)[0]


def _badge(client: TestClient, db: Session, work_center, user: User) -> dict:
    """A crew-station badge token for ``user`` at a station bound to ``work_center``."""
    station = make_kiosk_station(db, work_center=work_center)
    minted = mint_badge_token(client, kiosk_token_for(station), user.employee_id)
    assert minted.status_code == 200, minted.text
    return bearer(minted.json()["access_token"])


def _clean(db: Session) -> None:
    """A refusal leaves nothing pending in the shared session (looked at BEFORE a re-read)."""
    modified = [obj for obj in db.dirty if db.is_modified(obj)]
    assert not modified and not db.new, f"pending changes in the session: {modified} {db.new}"


def _world(db: Session) -> dict:
    """Every row a scrap declaration could touch, plus the append-only tables' sizes."""
    _clean(db)
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
                op.last_reported_scrapped,
                op.version,
            )
            for op in db.query(WorkOrderOperation).order_by(WorkOrderOperation.id).all()
        ],
        "entries": [
            (
                e.id,
                e.clock_out is None,
                float(e.quantity_produced or 0),
                float(e.quantity_scrapped or 0),
                e.scrap_reason,
                e.scrap_reason_code_id,
            )
            for e in db.query(TimeEntry).order_by(TimeEntry.id).all()
        ],
        "work_orders": [
            (wo.id, wo.status, float(wo.quantity_complete or 0), float(wo.quantity_scrapped or 0), wo.version)
            for wo in db.query(WorkOrder).order_by(WorkOrder.id).all()
        ],
        "audit_rows": db.query(AuditLog).count(),
        "ledger_rows": db.query(InventoryTransaction).count(),
        "events": db.query(OperationalEvent).count(),
        "ncrs": db.query(NonConformanceReport).count(),
        "receipts": db.query(ProductionReceipt).count(),
        "hank_tasks": [(t.id, t.status, t.version) for t in db.query(HankTask).order_by(HankTask.id).all()],
    }


def _assert_refused_writing_nothing(db: Session, resp, before: dict) -> None:
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == API_TOKEN_SCRAP
    assert _world(db) == before


def _running_op(db: Session, user: User, *, good: float = 0.0, scrapped: float = 0.0, quantity_ordered: float = 10):
    """One IN_PROGRESS operation with ``user`` clocked in on an open entry."""
    wc = make_work_center(db)
    wo, (op,) = make_routing(db, work_centers=[wc], statuses=[OPEN], quantity_ordered=quantity_ordered)
    if good or scrapped:
        add_entry(db, user, op, produced=good, scrapped=scrapped)
        set_op(db, op, quantity_complete=good, quantity_scrapped=scrapped, scrap_reason="crack" if scrapped else None)
    entry = add_entry(db, user, op, open_entry=True)
    return wc, wo, op, entry


# --------------------------------------------------------------------------- #
# The predicate
# --------------------------------------------------------------------------- #
class TestThePredicate:
    TOKEN = SimpleNamespace(_api_token_id=7, _token_scope="api")
    BADGE = SimpleNamespace(_api_token_id=None, _token_scope="kiosk")
    PERSON = SimpleNamespace(_api_token_id=None, _token_scope=None)

    def test_a_token_raising_scrap_is_refused(self):
        assert api_token_declares_scrap(self.TOKEN, 1) is True
        assert api_token_declares_scrap(self.TOKEN, 0.5) is True

    @pytest.mark.parametrize("delta", [0, 0.0, None, -1, 1e-12, float("nan"), "not a number"])
    def test_a_token_declaring_nothing_new_is_not(self, delta):
        assert api_token_declares_scrap(self.TOKEN, delta) is False

    def test_a_badge_and_a_person_are_never_refused(self):
        assert api_token_declares_scrap(self.BADGE, 5) is False
        assert api_token_declares_scrap(self.PERSON, 5) is False
        # A User built outside a request (worker, fixture) carries neither marker.
        assert api_token_declares_scrap(SimpleNamespace(), 5) is False

    def test_the_message_is_one_plain_string(self):
        assert isinstance(API_TOKEN_SCRAP, str)
        assert "not an API token" in API_TOKEN_SCRAP
        assert API_TOKEN_SCRAP != API_TOKEN_SHORT_CLOSE


# --------------------------------------------------------------------------- #
# POST /shop-floor/clock-out/{id}
# --------------------------------------------------------------------------- #
class TestClockOut:
    def test_a_token_reporting_scrap_at_clock_out_is_refused_writing_nothing(self, client, db_session, admin_headers):
        ann = make_user(db_session)
        _, _, op, entry = _running_op(db_session, ann)
        headers = _token(client, admin_headers, ann)
        before = _world(db_session)

        resp = client.post(
            CLOCK_OUT_URL.format(entry=entry.id),
            json={"quantity_produced": 2, "quantity_scrapped": 1, "scrap_reason": "crack"},
            headers=headers,
        )
        _assert_refused_writing_nothing(db_session, resp, before)
        assert fresh(db_session, TimeEntry, entry.id).clock_out is None, "the entry stays open for a retry"

    def test_the_same_token_clocking_out_with_good_only_still_works(self, client, db_session, admin_headers):
        ann = make_user(db_session)
        _, _, op, entry = _running_op(db_session, ann)
        headers = _token(client, admin_headers, ann)

        resp = client.post(CLOCK_OUT_URL.format(entry=entry.id), json={"quantity_produced": 2}, headers=headers)
        assert resp.status_code == 200, resp.text
        row = fresh(db_session, TimeEntry, entry.id)
        assert (row.clock_out is not None, float(row.quantity_produced), float(row.quantity_scrapped or 0)) == (
            True,
            2,
            0,
        )
        assert float(fresh(db_session, WorkOrderOperation, op.id).quantity_complete) == 2

    def test_a_crew_badge_reporting_scrap_at_clock_out_is_a_person(self, client, db_session):
        lead = make_user(db_session, first_name="Lena", last_name="Lead")
        wc, _, op, entry = _running_op(db_session, lead)
        badge = _badge(client, db_session, wc, lead)

        resp = client.post(
            CLOCK_OUT_URL.format(entry=entry.id),
            json={"quantity_produced": 2, "quantity_scrapped": 1, "scrap_reason": "crack"},
            headers=badge,
        )
        assert resp.status_code == 200, resp.text
        assert float(fresh(db_session, TimeEntry, entry.id).quantity_scrapped) == 1
        assert float(fresh(db_session, WorkOrderOperation, op.id).quantity_scrapped) == 1


# --------------------------------------------------------------------------- #
# POST /shop-floor/operations/{id}/production (and scrap -> NCR)
# --------------------------------------------------------------------------- #
class TestProductionReport:
    @pytest.mark.parametrize("open_ncr", [False, True])
    def test_a_token_reporting_scrap_is_refused_writing_nothing(self, client, db_session, admin_headers, open_ncr):
        ann = make_user(db_session)
        _, _, op, _ = _running_op(db_session, ann)
        headers = _token(client, admin_headers, ann)
        before = _world(db_session)

        resp = client.post(
            PRODUCTION_URL.format(op=op.id),
            json={
                "quantity_complete_delta": 1,
                "quantity_scrapped_delta": 1,
                "scrap_reason": "crack",
                "open_ncr": open_ncr,
                "request_id": str(uuid4()),
            },
            headers=headers,
        )
        _assert_refused_writing_nothing(db_session, resp, before)

    def test_the_same_token_reporting_good_only_still_works(self, client, db_session, admin_headers):
        ann = make_user(db_session)
        _, _, op, entry = _running_op(db_session, ann)
        headers = _token(client, admin_headers, ann)

        resp = client.post(PRODUCTION_URL.format(op=op.id), json={"quantity_complete_delta": 3}, headers=headers)
        assert resp.status_code == 200, resp.text
        assert float(fresh(db_session, WorkOrderOperation, op.id).quantity_complete) == 3
        assert float(fresh(db_session, TimeEntry, entry.id).quantity_produced) == 3

    def test_a_crew_badge_reporting_scrap_with_an_ncr_is_a_person(self, client, db_session):
        lead = make_user(db_session, first_name="Lena", last_name="Lead")
        wc, _, op, entry = _running_op(db_session, lead)
        badge = _badge(client, db_session, wc, lead)

        resp = client.post(
            PRODUCTION_URL.format(op=op.id),
            json={"quantity_scrapped_delta": 1, "scrap_reason": "crack", "open_ncr": True},
            headers=badge,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["ncr"] is not None
        assert float(fresh(db_session, WorkOrderOperation, op.id).quantity_scrapped) == 1
        assert float(fresh(db_session, TimeEntry, entry.id).quantity_scrapped) == 1


# --------------------------------------------------------------------------- #
# POST /hank/tasks + /hank/tasks/{id}/execute (report_production runs the same command)
# --------------------------------------------------------------------------- #
@pytest.fixture
def hank_running(db_session, test_work_order, test_user):
    test_work_order.status = WorkOrderStatus.IN_PROGRESS
    operation = db_session.query(WorkOrderOperation).filter_by(work_order_id=test_work_order.id).first()
    operation.status = OperationStatus.IN_PROGRESS
    entry = TimeEntry(
        company_id=1,
        work_order_id=test_work_order.id,
        operation_id=operation.id,
        user_id=test_user.id,
        clock_in=datetime.utcnow() - timedelta(hours=1),
    )
    db_session.add(entry)
    db_session.commit()
    return operation, entry


def _scrap_input(op) -> dict:
    return {"operation_id": op.id, "quantity_complete_delta": 1, "quantity_scrapped_delta": 1, "scrap_reason": "crack"}


class TestHankProductionTask:
    def test_a_token_preparing_a_scrap_report_is_refused_writing_nothing(
        self, client, db_session, admin_headers, test_user, hank_running
    ):
        op, _ = hank_running
        headers = _token(client, admin_headers, test_user)
        before = _world(db_session)

        resp = propose(client, headers, "report_production", _scrap_input(op))
        _assert_refused_writing_nothing(db_session, resp, before)
        assert before["hank_tasks"] == [], "no task row, so there is nothing a token could later execute"

    def test_a_token_executing_a_scrap_task_it_already_holds_is_refused_writing_nothing(
        self, client, db_session, admin_headers, test_user, hank_running
    ):
        """A task saved before the decision (inserted directly: prepare now refuses it)."""
        op, _ = hank_running
        headers, token_id = _mint_token(client, admin_headers, test_user)
        task = HankTask(
            company_id=1,
            owner_id=test_user.id,
            credential_key=f"api:{token_id}",
            request_key=str(uuid4()),
            request_hash="0" * 64,
            kind="report_production",
            title="Report production",
            status="awaiting_review",
            version=1,
            input_json=_scrap_input(op),
            preview_json={},
            source_versions_json={},
        )
        db_session.add(task)
        db_session.commit()
        before = _world(db_session)

        resp = execute(client, headers, {"id": task.id, "version": 1})
        _assert_refused_writing_nothing(db_session, resp, before)
        assert fresh(db_session, HankTask, task.id).status == "awaiting_review"

    def test_the_same_token_reporting_good_only_still_works(
        self, client, db_session, admin_headers, test_user, hank_running
    ):
        op, entry = hank_running
        headers = _token(client, admin_headers, test_user)
        task = propose(client, headers, "report_production", {"operation_id": op.id, "quantity_complete_delta": 2})
        assert task.status_code == 200, task.text
        done = execute(client, headers, task.json())
        assert done.status_code == 200, done.text
        assert float(fresh(db_session, WorkOrderOperation, op.id).quantity_complete) == 2
        assert float(fresh(db_session, TimeEntry, entry.id).quantity_produced) == 2

    def test_a_person_in_the_app_may_report_scrap_through_a_task(self, client, db_session, auth_headers, hank_running):
        """A badge token cannot reach /hank (path fence), so the person here is a session."""
        op, entry = hank_running
        task = propose(client, auth_headers, "report_production", _scrap_input(op))
        assert task.status_code == 200, task.text
        done = execute(client, auth_headers, task.json())
        assert done.status_code == 200, done.text
        assert float(fresh(db_session, WorkOrderOperation, op.id).quantity_scrapped) == 1
        assert float(fresh(db_session, TimeEntry, entry.id).quantity_scrapped) == 1


# --------------------------------------------------------------------------- #
# POST /shop-floor/operations/{id}/complete
# --------------------------------------------------------------------------- #
class TestFloorClose:
    def _eight_of_ten(self, db: Session, user: User, *, scrapped: float = 0.0):
        wc = make_work_center(db)
        wo, (op,) = make_routing(db, work_centers=[wc], statuses=[OPEN])
        add_entry(db, user, op, produced=8, scrapped=scrapped)
        set_op(db, op, quantity_complete=8, quantity_scrapped=scrapped, scrap_reason="crack" if scrapped else None)
        return wc, wo, op

    def test_a_token_declaring_scrap_at_close_is_refused_writing_nothing(self, client, db_session, admin_headers):
        ann = make_user(db_session)
        _, _, op = self._eight_of_ten(db_session, ann)
        headers = _token(client, admin_headers, ann)
        before = _world(db_session)

        resp = client.post(
            COMPLETE_URL.format(op=op.id), json={"quantity_scrapped": 2, "scrap_reason": "crack"}, headers=headers
        )
        _assert_refused_writing_nothing(db_session, resp, before)

    def test_the_scrap_refusal_comes_before_the_reason_and_quantity_refusals(self, client, db_session, admin_headers):
        """Never told to add a reason (422) or to lower an over-declaration (409) first."""
        ann = make_user(db_session)
        _, _, op = self._eight_of_ten(db_session, ann)
        headers = _token(client, admin_headers, ann)
        for body in ({"quantity_scrapped": 2}, {"quantity_scrapped": 5, "scrap_reason": "crack"}):
            resp = client.post(COMPLETE_URL.format(op=op.id), json=body, headers=headers)
            assert resp.status_code == 403, resp.text
            assert resp.json()["detail"] == API_TOKEN_SCRAP

    def test_a_token_short_close_with_its_scrap_already_on_record_keeps_the_short_close_text(
        self, client, db_session, admin_headers
    ):
        ann = make_user(db_session)
        _, _, op = self._eight_of_ten(db_session, ann, scrapped=2)
        headers = _token(client, admin_headers, ann)
        before = _world(db_session)

        resp = client.post(COMPLETE_URL.format(op=op.id), json={}, headers=headers)
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == API_TOKEN_SHORT_CLOSE
        assert _world(db_session) == before

    def test_the_same_token_closing_a_full_count_still_works(self, client, db_session, admin_headers):
        ann = make_user(db_session)
        _, _, op = self._eight_of_ten(db_session, ann)
        add_entry(db_session, ann, op, produced=2)
        headers = _token(client, admin_headers, ann)

        resp = client.post(COMPLETE_URL.format(op=op.id), json={"quantity_scrapped": 0}, headers=headers)
        assert resp.status_code == 200, resp.text
        row = fresh(db_session, WorkOrderOperation, op.id)
        assert (row.status, float(row.quantity_complete), float(row.quantity_scrapped or 0)) == (DONE, 10, 0)

    def test_a_crew_badge_declaring_scrap_at_close_is_a_person(self, client, db_session):
        lead = make_user(db_session, first_name="Lena", last_name="Lead")
        wc, _, op = self._eight_of_ten(db_session, lead)
        badge = _badge(client, db_session, wc, lead)

        resp = client.post(
            COMPLETE_URL.format(op=op.id), json={"quantity_scrapped": 2, "scrap_reason": "crack"}, headers=badge
        )
        assert resp.status_code == 200, resp.text
        row = fresh(db_session, WorkOrderOperation, op.id)
        assert (row.status, float(row.quantity_scrapped), row.quantity_short_declared) == (DONE, 2, 2)


# --------------------------------------------------------------------------- #
# POST /work-orders/operations/{id}/complete (office)
# --------------------------------------------------------------------------- #
class TestOfficeClose:
    def _op(self, db: Session, *, good: float, scrapped: float = 0.0):
        ann = make_user(db)
        wc = make_work_center(db)
        wo, (op,) = make_routing(db, work_centers=[wc], statuses=[OPEN])
        add_entry(db, ann, op, produced=good, scrapped=scrapped)
        set_op(db, op, quantity_complete=good, quantity_scrapped=scrapped, scrap_reason="crack" if scrapped else None)
        return op

    def test_a_token_raising_scrap_is_refused_writing_nothing(self, client, db_session, admin_headers, supervisor):
        op = self._op(db_session, good=8)
        headers = _token(client, admin_headers, supervisor)
        before = _world(db_session)

        resp = client.post(
            OFFICE_COMPLETE_URL.format(op=op.id),
            params={"quantity_complete": 8, "quantity_scrapped": 2, "scrap_reason": "crack"},
            headers=headers,
        )
        _assert_refused_writing_nothing(db_session, resp, before)

    def test_a_scrap_rise_without_a_reason_is_still_the_token_refusal(
        self, client, db_session, admin_headers, supervisor
    ):
        op = self._op(db_session, good=8)
        resp = client.post(
            OFFICE_COMPLETE_URL.format(op=op.id),
            params={"quantity_complete": 8, "quantity_scrapped": 2},
            headers=_token(client, admin_headers, supervisor),
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == API_TOKEN_SCRAP

    def test_a_token_short_close_with_its_scrap_already_on_record_keeps_the_short_close_text(
        self, client, db_session, admin_headers, supervisor
    ):
        op = self._op(db_session, good=8, scrapped=2)
        headers = _token(client, admin_headers, supervisor)
        before = _world(db_session)
        # Re-sending the recorded scrap total is not a rise.
        resp = client.post(
            OFFICE_COMPLETE_URL.format(op=op.id),
            params={"quantity_complete": 8, "quantity_scrapped": 2},
            headers=headers,
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == API_TOKEN_SHORT_CLOSE
        assert _world(db_session) == before

    def test_the_same_token_closing_a_full_count_still_works(self, client, db_session, admin_headers, supervisor):
        op = self._op(db_session, good=10)
        resp = client.post(
            OFFICE_COMPLETE_URL.format(op=op.id),
            params={"quantity_complete": 10, "quantity_scrapped": 0},
            headers=_token(client, admin_headers, supervisor),
        )
        assert resp.status_code == 200, resp.text
        assert fresh(db_session, WorkOrderOperation, op.id).status == DONE

    def test_a_person_at_the_desk_may_raise_scrap(self, client, db_session, supervisor):
        """A badge token cannot reach /work-orders (path fence), so the person is a session."""
        op = self._op(db_session, good=8)
        resp = client.post(
            OFFICE_COMPLETE_URL.format(op=op.id),
            params={"quantity_complete": 8, "quantity_scrapped": 2, "scrap_reason": "crack"},
            headers=user_headers(supervisor),
        )
        assert resp.status_code == 200, resp.text
        row = fresh(db_session, WorkOrderOperation, op.id)
        assert (row.status, float(row.quantity_scrapped), row.quantity_short_declared) == (DONE, 2, 2)


# --------------------------------------------------------------------------- #
# POST /work-orders/{id}/complete (force-complete; quantity_scrapped overwrites the header)
# --------------------------------------------------------------------------- #
class TestForceComplete:
    def _wo(self, db: Session, *, good: float):
        ann = make_user(db)
        wc = make_work_center(db)
        wo, (op,) = make_routing(db, work_centers=[wc], statuses=[OPEN])
        add_entry(db, ann, op, produced=good)
        set_op(db, op, quantity_complete=good)
        return wo

    def test_a_token_raising_the_header_scrap_is_refused_writing_nothing(
        self, client, db_session, admin_headers, supervisor
    ):
        wo = self._wo(db_session, good=8)
        headers = _token(client, admin_headers, supervisor)
        before = _world(db_session)

        resp = client.post(
            FORCE_COMPLETE_URL.format(wo=wo.id),
            params={"quantity_complete": 8, "quantity_scrapped": 2, "scrap_reason": "lost"},
            headers=headers,
        )
        _assert_refused_writing_nothing(db_session, resp, before)

    def test_the_scrap_refusal_comes_before_the_reason_refusal(self, client, db_session, admin_headers, supervisor):
        wo = self._wo(db_session, good=8)
        resp = client.post(
            FORCE_COMPLETE_URL.format(wo=wo.id),
            params={"quantity_complete": 8, "quantity_scrapped": 2},
            headers=_token(client, admin_headers, supervisor),
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == API_TOKEN_SCRAP

    @pytest.mark.parametrize("params", [{"quantity_complete": 10}, {"quantity_complete": 10, "quantity_scrapped": 0}])
    def test_the_same_token_completing_a_full_count_still_works(
        self, client, db_session, admin_headers, supervisor, params
    ):
        wo = self._wo(db_session, good=10)
        resp = client.post(
            FORCE_COMPLETE_URL.format(wo=wo.id), params=params, headers=_token(client, admin_headers, supervisor)
        )
        assert resp.status_code == 200, resp.text
        row = fresh(db_session, WorkOrder, wo.id)
        assert (row.status, float(row.quantity_complete), float(row.quantity_scrapped or 0)) == (
            WorkOrderStatus.COMPLETE,
            10,
            0,
        )

    def test_a_person_at_the_desk_may_complete_with_scrap(self, client, db_session, supervisor):
        wo = self._wo(db_session, good=8)
        resp = client.post(
            FORCE_COMPLETE_URL.format(wo=wo.id),
            params={"quantity_complete": 8, "quantity_scrapped": 2, "scrap_reason": "lost"},
            headers=user_headers(supervisor),
        )
        assert resp.status_code == 200, resp.text
        row = fresh(db_session, WorkOrder, wo.id)
        assert (row.status, float(row.quantity_scrapped)) == (WorkOrderStatus.COMPLETE, 2)


# --------------------------------------------------------------------------- #
# PUT /work-orders/operations/{id}
# --------------------------------------------------------------------------- #
class TestOperationPut:
    @pytest.mark.parametrize("status", [OPEN, DONE])
    def test_a_token_raising_scrap_is_refused_writing_nothing(
        self, client, db_session, admin_headers, supervisor, status
    ):
        wc = make_work_center(db_session)
        _, (op,) = make_routing(db_session, work_centers=[wc], statuses=[status])
        set_op(db_session, op, quantity_complete=10 if status == DONE else 4)
        headers = _token(client, admin_headers, supervisor)
        before = _world(db_session)

        resp = client.put(
            OPERATION_PUT_URL.format(op=op.id),
            json={"version": op.version, "quantity_scrapped": 1, "scrap_reason": "found later"},
            headers=headers,
        )
        _assert_refused_writing_nothing(db_session, resp, before)

    def test_the_same_token_re_sending_the_recorded_scrap_still_works(
        self, client, db_session, admin_headers, supervisor
    ):
        wc = make_work_center(db_session)
        _, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        set_op(db_session, op, quantity_complete=4, quantity_scrapped=1, scrap_reason="crack")
        resp = client.put(
            OPERATION_PUT_URL.format(op=op.id),
            json={"version": op.version, "quantity_scrapped": 1, "scrap_reason": "crack", "name": "Renamed"},
            headers=_token(client, admin_headers, supervisor),
        )
        assert resp.status_code == 200, resp.text
        row = fresh(db_session, WorkOrderOperation, op.id)
        assert (row.name, float(row.quantity_scrapped)) == ("Renamed", 1)

    def test_a_person_at_the_desk_may_raise_scrap(self, client, db_session, supervisor):
        wc = make_work_center(db_session)
        _, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        resp = client.put(
            OPERATION_PUT_URL.format(op=op.id),
            json={"version": op.version, "quantity_scrapped": 1, "scrap_reason": "crack"},
            headers=user_headers(supervisor),
        )
        assert resp.status_code == 200, resp.text
        assert float(fresh(db_session, WorkOrderOperation, op.id).quantity_scrapped) == 1


# --------------------------------------------------------------------------- #
# PUT /work-orders/{id} (the header's quantity_scrapped is writable through its setattr)
# --------------------------------------------------------------------------- #
class TestWorkOrderPut:
    def test_a_token_raising_the_header_scrap_is_refused_writing_nothing(
        self, client, db_session, admin_headers, supervisor
    ):
        wc = make_work_center(db_session)
        wo, _ = make_routing(db_session, work_centers=[wc])
        headers = _token(client, admin_headers, supervisor)
        before = _world(db_session)

        resp = client.put(
            WORK_ORDER_URL.format(wo=wo.id),
            json={"version": wo.version, "quantity_scrapped": 2, "scrap_reason": "agent", "notes": "x"},
            headers=headers,
        )
        _assert_refused_writing_nothing(db_session, resp, before)

    def test_the_same_token_re_sending_the_recorded_scrap_still_works(
        self, client, db_session, admin_headers, supervisor
    ):
        wc = make_work_center(db_session)
        wo, _ = make_routing(db_session, work_centers=[wc])
        resp = client.put(
            WORK_ORDER_URL.format(wo=wo.id),
            json={"version": wo.version, "quantity_scrapped": 0, "notes": "agent note"},
            headers=_token(client, admin_headers, supervisor),
        )
        assert resp.status_code == 200, resp.text
        row = fresh(db_session, WorkOrder, wo.id)
        assert (row.notes, float(row.quantity_scrapped or 0)) == ("agent note", 0)

    def test_a_person_at_the_desk_may_raise_the_header_scrap(self, client, db_session, supervisor):
        wc = make_work_center(db_session)
        wo, _ = make_routing(db_session, work_centers=[wc])
        resp = client.put(
            WORK_ORDER_URL.format(wo=wo.id),
            json={"version": wo.version, "quantity_scrapped": 2, "scrap_reason": "dropped"},
            headers=user_headers(supervisor),
        )
        assert resp.status_code == 200, resp.text
        assert float(fresh(db_session, WorkOrder, wo.id).quantity_scrapped) == 2
