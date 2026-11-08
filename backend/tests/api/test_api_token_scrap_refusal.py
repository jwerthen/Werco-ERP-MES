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

Three more layers sit on top of the per-route pins:

* ``TestNonFiniteScrapFromAToken`` -- NaN passes every ``> 0`` guard unseen, so a token's
  NaN / infinity scrap must reach no scrap column on any route (clock-out had no non-finite
  check of its own; the predicate fails closed for it).
* ``TestThroughTheMcpDoor`` -- every MCP tool that takes scrap returns the route's 403
  verbatim and writes nothing, and still works with no scrap.
* ``TestEveryScrapWriterIsEnumerated`` -- the audit's three enumerations as trip-wires: every
  function assigning a scrap column, every write route whose request names a scrap field,
  and every MCP tool whose input does. A new writer fails CI until it is guarded and pinned,
  or listed as exempt with the reason it records no scrap.
"""

import ast
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app as app_package
from app.main import app
from app.mcp.catalog import build_catalog, resolve_schema
from app.mcp.convenience import CONVENIENCE_TOOL_NAMES, CONVENIENCE_TOOLS, SHADOWED_OPERATIONS
from app.models.audit_log import AuditLog
from app.models.hank import HankTask
from app.models.inventory import InventoryTransaction
from app.models.operational_event import OperationalEvent
from app.models.production_receipt import ProductionReceipt
from app.models.quality import NonConformanceReport
from app.models.time_entry import TimeEntry
from app.models.user import User, UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.schemas.hank_tasks import INPUT_SCHEMAS as HANK_INPUT_SCHEMAS
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
from tests.test_mcp_server import call, make_server

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
        assert api_token_declares_scrap(self.TOKEN, Decimal("1.5")) is True, "the PUT schemas carry Decimal"

    @pytest.mark.parametrize("delta", [1e-12, 5e-10, 1e-9, Decimal("1E-12"), 5e-324])
    def test_a_token_raising_scrap_by_less_than_the_quantity_tolerance_is_refused(self, delta):
        """The writers behind the predicate store scrap, its reason and an NCR at ``> 0``, so the
        predicate tolerates nothing they do not: ``QTY_EPSILON`` (1e-9) is a comparison tolerance
        for counts, never room for a token to write a sub-epsilon scrap (see
        ``TestSubEpsilonScrapFromAToken`` for what each route used to do with one)."""
        assert api_token_declares_scrap(self.TOKEN, delta) is True

    @pytest.mark.parametrize("delta", [0, 0.0, -0.0, None, -1, -1e-12, Decimal("0"), Decimal("-2")])
    def test_a_token_declaring_nothing_new_is_not(self, delta):
        assert api_token_declares_scrap(self.TOKEN, delta) is False

    @pytest.mark.parametrize("delta", [float("nan"), float("inf"), Decimal("NaN"), Decimal("Infinity"), "not a number"])
    def test_a_token_whose_scrap_is_not_a_finite_number_fails_closed(self, delta):
        """NaN passes every ``> 0`` comparison unseen; a value that cannot be shown to add
        nothing is treated as a declaration (clock-out has no non-finite check of its own)."""
        assert api_token_declares_scrap(self.TOKEN, delta) is True

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

    def test_a_token_sending_nan_scrap_at_clock_out_is_refused_writing_nothing(self, client, db_session, admin_headers):
        """NaN slips past every ``< 0`` / ``> 0`` guard. Clock-out now refuses a non-finite
        quantity 400 for EVERYONE before the token rule is asked (the predicate's fail-closed
        branch is the backstop): the entry stays open and nothing is overwritten with NaN."""
        ann = make_user(db_session)
        _, _, op, entry = _running_op(db_session, ann)
        headers = {**_token(client, admin_headers, ann), "Content-Type": "application/json"}
        before = _world(db_session)

        resp = client.post(
            CLOCK_OUT_URL.format(entry=entry.id),
            content=b'{"quantity_produced": 1, "quantity_scrapped": NaN}',
            headers=headers,
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == "Quantity must be a valid number"
        assert _world(db_session) == before

    def test_the_refusal_comes_before_the_completed_operation_refusal(self, client, db_session, admin_headers):
        """Scrap onto an operation already closed is a person's 409; a token is told 403 first."""
        ann = make_user(db_session)
        _, _, op, entry = _running_op(db_session, ann)
        set_op(db_session, op, status=DONE, quantity_complete=10)
        body = {"quantity_scrapped": 1, "scrap_reason": "crack"}
        person = client.post(CLOCK_OUT_URL.format(entry=entry.id), json=body, headers=user_headers(ann))
        assert person.status_code == 409, "the person's refusal this ordering is measured against"
        headers = _token(client, admin_headers, ann)
        before = _world(db_session)

        resp = client.post(CLOCK_OUT_URL.format(entry=entry.id), json=body, headers=headers)
        _assert_refused_writing_nothing(db_session, resp, before)


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


# --------------------------------------------------------------------------- #
# A non-finite scrap value from a token never reaches a scrap column
# --------------------------------------------------------------------------- #
def _short_op(db: Session, user: User, *, good: float = 8):
    """An IN_PROGRESS 10-piece operation with ``good`` recorded on a closed labor entry."""
    wc = make_work_center(db)
    wo, (op,) = make_routing(db, work_centers=[wc], statuses=[OPEN])
    add_entry(db, user, op, produced=good)
    set_op(db, op, quantity_complete=good)
    return wc, wo, op


def _put_refused_before_the_handler(client: TestClient, url: str, body: bytes, headers: dict) -> None:
    """The PUT schemas' ``Decimal(ge=0)`` refuses a non-finite number before the handler runs.

    The 422 that follows echoes the offending ``input`` back, and NaN / infinity cannot be
    JSON-encoded, so the error handler itself raises (a 500 in production) -- for everyone,
    not just a token, and with nothing written. That pre-existing response defect is out of
    this file's scope; what is pinned here is that the request never reaches a write.
    """
    try:
        resp = client.put(url, content=body, headers=headers)
    except ValueError as exc:
        assert "not JSON compliant" in str(exc), exc
        return
    assert resp.status_code == 422, resp.text


class TestNonFiniteScrapFromAToken:
    """NaN passes every ``> 0`` / ``< 0`` guard unseen. Whatever each route answers (its own
    400 / 422, or the token refusal), nothing a NaN or an infinity could reach is written."""

    @pytest.mark.parametrize("value", [b"NaN", b"Infinity"])
    def test_clock_out(self, client, db_session, admin_headers, value):
        ann = make_user(db_session)
        _, _, _, entry = _running_op(db_session, ann)
        headers = {**_token(client, admin_headers, ann), "Content-Type": "application/json"}
        before = _world(db_session)
        resp = client.post(
            CLOCK_OUT_URL.format(entry=entry.id),
            content=b'{"quantity_produced": 1, "quantity_scrapped": ' + value + b', "scrap_reason": "x"}',
            headers=headers,
        )
        # The route's own non-finite 400 (for everyone) now answers before the token rule.
        assert resp.status_code == 400, resp.text
        assert _world(db_session) == before

    @pytest.mark.parametrize("value", [b"NaN", b"Infinity"])
    def test_production_report(self, client, db_session, admin_headers, value):
        ann = make_user(db_session)
        _, _, op, _ = _running_op(db_session, ann)
        headers = {**_token(client, admin_headers, ann), "Content-Type": "application/json"}
        before = _world(db_session)
        resp = client.post(
            PRODUCTION_URL.format(op=op.id),
            content=b'{"quantity_scrapped_delta": ' + value + b', "scrap_reason": "x"}',
            headers=headers,
        )
        assert resp.status_code in (400, 403), resp.text
        assert _world(db_session) == before

    @pytest.mark.parametrize("value", [b"NaN", b"Infinity"])
    def test_floor_close(self, client, db_session, admin_headers, value):
        ann = make_user(db_session)
        _, _, op = _short_op(db_session, ann)
        headers = {**_token(client, admin_headers, ann), "Content-Type": "application/json"}
        before = _world(db_session)
        resp = client.post(
            COMPLETE_URL.format(op=op.id),
            content=b'{"quantity_scrapped": ' + value + b', "scrap_reason": "x"}',
            headers=headers,
        )
        assert resp.status_code in (400, 403), resp.text
        assert _world(db_session) == before

    @pytest.mark.parametrize("value", ["nan", "inf"])
    def test_office_close(self, client, db_session, admin_headers, supervisor, value):
        _, _, op = _short_op(db_session, make_user(db_session))
        headers = _token(client, admin_headers, supervisor)
        before = _world(db_session)
        resp = client.post(
            OFFICE_COMPLETE_URL.format(op=op.id),
            params={"quantity_complete": 8, "quantity_scrapped": value, "scrap_reason": "x"},
            headers=headers,
        )
        assert resp.status_code in (400, 403, 422), resp.text
        assert _world(db_session) == before

    @pytest.mark.parametrize("value", ["nan", "inf"])
    def test_force_complete(self, client, db_session, admin_headers, supervisor, value):
        _, wo, _ = _short_op(db_session, make_user(db_session))
        headers = _token(client, admin_headers, supervisor)
        before = _world(db_session)
        resp = client.post(
            FORCE_COMPLETE_URL.format(wo=wo.id),
            params={"quantity_complete": 8, "quantity_scrapped": value, "scrap_reason": "x"},
            headers=headers,
        )
        assert resp.status_code in (400, 403), resp.text
        assert _world(db_session) == before

    @pytest.mark.parametrize("value", [b"NaN", b"Infinity"])
    def test_operation_put(self, client, db_session, admin_headers, supervisor, value):
        _, _, op = _short_op(db_session, make_user(db_session))
        headers = {**_token(client, admin_headers, supervisor), "Content-Type": "application/json"}
        before = _world(db_session)
        body = b'{"version": %d, "quantity_scrapped": %s, "scrap_reason": "x"}' % (op.version, value)
        _put_refused_before_the_handler(client, OPERATION_PUT_URL.format(op=op.id), body, headers)
        assert _world(db_session) == before

    @pytest.mark.parametrize("value", [b"NaN", b"Infinity"])
    def test_work_order_put(self, client, db_session, admin_headers, supervisor, value):
        _, wo, _ = _short_op(db_session, make_user(db_session))
        headers = {**_token(client, admin_headers, supervisor), "Content-Type": "application/json"}
        before = _world(db_session)
        body = b'{"version": %d, "quantity_scrapped": %s, "scrap_reason": "x"}' % (wo.version, value)
        _put_refused_before_the_handler(client, WORK_ORDER_URL.format(wo=wo.id), body, headers)
        assert _world(db_session) == before

    @pytest.mark.parametrize("value", [b"NaN", b"Infinity"])
    def test_hank_production_task(self, client, db_session, admin_headers, value):
        ann = make_user(db_session)
        _, _, op, _ = _running_op(db_session, ann)
        headers = {**_token(client, admin_headers, ann), "Content-Type": "application/json"}
        before = _world(db_session)
        body = (
            b'{"expected_company_id": 1, "request_key": "%s", "kind": "report_production", '
            b'"input": {"operation_id": %d, "quantity_scrapped_delta": %s, "scrap_reason": "x"}}'
            % (str(uuid4()).encode(), op.id, value)
        )
        try:
            resp = client.post("/api/v1/hank/tasks", content=body, headers=headers)
        except ValueError as exc:  # the 422 echo of a non-finite input (see _put_refused_before_the_handler)
            assert "not JSON compliant" in str(exc), exc
        else:
            assert resp.status_code in (403, 422), resp.text
        assert _world(db_session) == before


# --------------------------------------------------------------------------- #
# A sub-epsilon scrap from a token is still a declaration
# --------------------------------------------------------------------------- #
SUB_EPSILON = 5e-10  # below QTY_EPSILON (1e-9), above zero
PERSONS_REASON = "crack (person saw it)"


def _person_scrapped_op(db: Session, user: User, *, status: OperationStatus = OPEN, good: float = 10):
    """A 10-piece operation whose ``good`` and 1 scrap -- with a PERSON's reason -- are recorded on
    a closed labor entry: the record a token's sub-epsilon "declaration" would relabel."""
    wc = make_work_center(db)
    wo, (op,) = make_routing(db, work_centers=[wc], statuses=[status])
    add_entry(db, user, op, produced=good, scrapped=1)
    set_op(db, op, quantity_complete=good, quantity_scrapped=1, scrap_reason=PERSONS_REASON)
    return wc, wo, op


class TestSubEpsilonScrapFromAToken:
    """The predicate used to answer ``delta > QTY_EPSILON`` while every writer behind it stores
    scrap, overwrites its reason and files an NCR at ``> 0``. A token sending 5e-10 therefore
    passed the refusal and was written: /production filed an NCR in the bound user's name,
    a full-count floor close replaced a person's recorded reason, clock-out raised the entry's and
    the operation's scrap, and the two PUTs and force-complete wrote it through their setattr. Each
    route below now refuses it 403 writing nothing; a request with no scrap rise is unchanged."""

    def test_clock_out(self, client, db_session, admin_headers):
        ann = make_user(db_session)
        _, _, op, entry = _running_op(db_session, ann, good=4, scrapped=1)
        headers = _token(client, admin_headers, ann)
        before = _world(db_session)

        resp = client.post(
            CLOCK_OUT_URL.format(entry=entry.id),
            json={"quantity_produced": 1, "quantity_scrapped": SUB_EPSILON, "scrap_reason": "token relabel"},
            headers=headers,
        )
        _assert_refused_writing_nothing(db_session, resp, before)

    def test_production_report_with_an_ncr(self, client, db_session, admin_headers):
        ann = make_user(db_session)
        _, _, op, _ = _running_op(db_session, ann, good=4, scrapped=1)
        headers = _token(client, admin_headers, ann)
        before = _world(db_session)

        resp = client.post(
            PRODUCTION_URL.format(op=op.id),
            json={
                "quantity_scrapped_delta": SUB_EPSILON,
                "scrap_reason": "token relabel",
                "open_ncr": True,
                "request_id": str(uuid4()),
            },
            headers=headers,
        )
        _assert_refused_writing_nothing(db_session, resp, before)
        assert before["ncrs"] == 0, "no NCR filed in the bound user's name"

    def test_hank_production_task_at_preparation(self, client, db_session, admin_headers, test_user, hank_running):
        op, _ = hank_running
        headers = _token(client, admin_headers, test_user)
        before = _world(db_session)

        resp = propose(
            client,
            headers,
            "report_production",
            {"operation_id": op.id, "quantity_scrapped_delta": SUB_EPSILON, "scrap_reason": "token relabel"},
        )
        _assert_refused_writing_nothing(db_session, resp, before)

    def test_floor_close_keeps_the_persons_reason(self, client, db_session, admin_headers):
        ann = make_user(db_session)
        _, _, op = _person_scrapped_op(db_session, ann)
        headers = _token(client, admin_headers, ann)
        before = _world(db_session)

        resp = client.post(
            COMPLETE_URL.format(op=op.id),
            json={"quantity_scrapped": SUB_EPSILON, "scrap_reason": "token relabel"},
            headers=headers,
        )
        _assert_refused_writing_nothing(db_session, resp, before)
        assert fresh(db_session, WorkOrderOperation, op.id).scrap_reason == PERSONS_REASON

    def test_the_same_floor_close_with_no_scrap_still_works(self, client, db_session, admin_headers):
        ann = make_user(db_session)
        _, _, op = _person_scrapped_op(db_session, ann)

        resp = client.post(COMPLETE_URL.format(op=op.id), json={}, headers=_token(client, admin_headers, ann))
        assert resp.status_code == 200, resp.text
        row = fresh(db_session, WorkOrderOperation, op.id)
        assert (row.status, float(row.quantity_scrapped), row.scrap_reason) == (DONE, 1, PERSONS_REASON)

    def test_office_close(self, client, db_session, admin_headers, supervisor):
        """Here a sub-epsilon rise was below the verb's own write threshold, so it used to close
        silently at full count; it is now the scrap refusal, like every other route."""
        _, _, op = _person_scrapped_op(db_session, make_user(db_session))
        headers = _token(client, admin_headers, supervisor)
        before = _world(db_session)

        resp = client.post(
            OFFICE_COMPLETE_URL.format(op=op.id),
            params={"quantity_complete": 10, "quantity_scrapped": 1 + SUB_EPSILON, "scrap_reason": "token relabel"},
            headers=headers,
        )
        _assert_refused_writing_nothing(db_session, resp, before)

    def test_force_complete(self, client, db_session, admin_headers, supervisor):
        _, wo, _ = _person_scrapped_op(db_session, make_user(db_session))
        headers = _token(client, admin_headers, supervisor)
        before = _world(db_session)

        resp = client.post(
            FORCE_COMPLETE_URL.format(wo=wo.id),
            params={"quantity_complete": 10, "quantity_scrapped": SUB_EPSILON, "scrap_reason": "token relabel"},
            headers=headers,
        )
        _assert_refused_writing_nothing(db_session, resp, before)

    @pytest.mark.parametrize("status", [OPEN, DONE])
    def test_operation_put(self, client, db_session, admin_headers, supervisor, status):
        """The scrap refusal used to run only for a field in ``count_changes``, which compares with
        ``QTY_EPSILON`` -- so a sub-epsilon rise skipped it (and the count-judgment refusal) and the
        setattr loop wrote it."""
        _, _, op = _person_scrapped_op(db_session, make_user(db_session), status=status)
        headers = _token(client, admin_headers, supervisor)
        before = _world(db_session)

        resp = client.put(
            OPERATION_PUT_URL.format(op=op.id),
            json={
                "version": op.version,
                "quantity_scrapped": 1 + SUB_EPSILON,
                "scrap_reason": PERSONS_REASON,
                "name": "Renamed",
            },
            headers=headers,
        )
        _assert_refused_writing_nothing(db_session, resp, before)

    def test_work_order_put(self, client, db_session, admin_headers, supervisor):
        _, wo, _ = _person_scrapped_op(db_session, make_user(db_session))
        headers = _token(client, admin_headers, supervisor)
        before = _world(db_session)

        resp = client.put(
            WORK_ORDER_URL.format(wo=wo.id),
            json={"version": wo.version, "quantity_scrapped": SUB_EPSILON, "scrap_reason": "token relabel"},
            headers=headers,
        )
        _assert_refused_writing_nothing(db_session, resp, before)

    def test_re_sending_a_float_noisy_recorded_scrap_exactly_is_no_rise(
        self, client, db_session, admin_headers, supervisor
    ):
        """The other side of ``> 0``: a stored total carrying float noise (0.1 + 0.2), sent back
        exactly as read, is a zero delta on both PUTs -- not a declaration."""
        noisy = 0.1 + 0.2
        assert noisy != 0.3
        wc = make_work_center(db_session)
        wo, (op,) = make_routing(db_session, work_centers=[wc], statuses=[OPEN])
        set_op(db_session, op, quantity_complete=4, quantity_scrapped=noisy, scrap_reason=PERSONS_REASON)
        wo.quantity_scrapped = noisy
        wo.scrap_reason = PERSONS_REASON
        db_session.commit()
        headers = _token(client, admin_headers, supervisor)

        op_resp = client.put(
            OPERATION_PUT_URL.format(op=op.id),
            json={"version": op.version, "quantity_scrapped": noisy, "scrap_reason": PERSONS_REASON, "name": "Renamed"},
            headers=headers,
        )
        assert op_resp.status_code == 200, op_resp.text
        wo_resp = client.put(
            WORK_ORDER_URL.format(wo=wo.id),
            json={
                "version": fresh(db_session, WorkOrder, wo.id).version,
                "quantity_scrapped": noisy,
                "scrap_reason": PERSONS_REASON,
                "notes": "n",
            },
            headers=headers,
        )
        assert wo_resp.status_code == 200, wo_resp.text
        assert fresh(db_session, WorkOrderOperation, op.id).name == "Renamed"
        assert fresh(db_session, WorkOrder, wo.id).notes == "n"


# --------------------------------------------------------------------------- #
# Through the MCP door: every tool that takes scrap carries the route's 403 verbatim
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def mcp_catalog():
    return build_catalog(app.openapi(), shadowed=SHADOWED_OPERATIONS, reserved_names=CONVENIENCE_TOOL_NAMES)


def _door_floor_op(db: Session, user: User, full: bool) -> dict:
    _, wo, op, entry = _running_op(db, user)
    return {"op": op.id, "wo": wo.id, "entry": entry.id, "version": op.version}


def _door_closable(db: Session, user: User, full: bool) -> dict:
    """8 (or, ``full``, 10) of 10 good on a closed labor entry."""
    _, wo, op = _short_op(db, user, good=10 if full else 8)
    return {"op": op.id, "wo": wo.id, "version": op.version, "wo_version": wo.version}


def _good(db: Session, op_id: int) -> float:
    return float(fresh(db, WorkOrderOperation, op_id).quantity_complete or 0)


# tool -> (the token's user role, setup(db, user, full), scrap arguments, zero-scrap arguments,
# check that the zero-scrap call did what it always did). ``prepare_task`` is the Hank door:
# its ``input`` is free-form, so the scrap field lives in the task kind, not the tool schema.
DOOR_CASES = {
    "clock_out": (
        UserRole.OPERATOR,
        _door_floor_op,
        lambda c: {"time_entry_id": c["entry"], "quantity_produced": 2, "quantity_scrapped": 1, "scrap_reason": "x"},
        lambda c: {"time_entry_id": c["entry"], "quantity_produced": 2},
        lambda db, c: fresh(db, TimeEntry, c["entry"]).clock_out is not None and _good(db, c["op"]) == 2,
    ),
    "report_operation_production": (
        UserRole.OPERATOR,
        _door_floor_op,
        lambda c: {"operation_id": c["op"], "quantity_scrapped_delta": 1, "scrap_reason": "x", "open_ncr": True},
        lambda c: {"operation_id": c["op"], "quantity_complete_delta": 3},
        lambda db, c: _good(db, c["op"]) == 3,
    ),
    "shop_floor_complete_operation": (
        UserRole.OPERATOR,
        _door_closable,
        lambda c: {"operation_id": c["op"], "quantity_scrapped": 2, "scrap_reason": "x"},
        lambda c: {"operation_id": c["op"]},
        lambda db, c: fresh(db, WorkOrderOperation, c["op"]).status == DONE,
    ),
    "work_orders_complete_operation": (
        UserRole.SUPERVISOR,
        _door_closable,
        lambda c: {"operation_id": c["op"], "quantity_complete": 8, "quantity_scrapped": 2, "scrap_reason": "x"},
        lambda c: {"operation_id": c["op"], "quantity_complete": 10},
        lambda db, c: fresh(db, WorkOrderOperation, c["op"]).status == DONE,
    ),
    "work_orders_complete_work_order": (
        UserRole.SUPERVISOR,
        _door_closable,
        lambda c: {"work_order_id": c["wo"], "quantity_complete": 8, "quantity_scrapped": 2, "scrap_reason": "x"},
        lambda c: {"work_order_id": c["wo"], "quantity_complete": 10},
        lambda db, c: fresh(db, WorkOrder, c["wo"]).status == WorkOrderStatus.COMPLETE,
    ),
    "work_orders_update_operation": (
        UserRole.SUPERVISOR,
        _door_closable,
        lambda c: {"operation_id": c["op"], "version": c["version"], "quantity_scrapped": 1, "scrap_reason": "x"},
        lambda c: {"operation_id": c["op"], "version": c["version"], "name": "Renamed"},
        lambda db, c: fresh(db, WorkOrderOperation, c["op"]).name == "Renamed",
    ),
    "update_work_order": (
        UserRole.SUPERVISOR,
        _door_closable,
        lambda c: {"work_order_id": c["wo"], "version": c["wo_version"], "quantity_scrapped": 2, "scrap_reason": "x"},
        lambda c: {"work_order_id": c["wo"], "version": c["wo_version"], "notes": "agent note"},
        lambda db, c: fresh(db, WorkOrder, c["wo"]).notes == "agent note",
    ),
    "prepare_task": (
        UserRole.OPERATOR,
        _door_floor_op,
        lambda c: {
            "expected_company_id": 1,
            "request_key": str(uuid4()),
            "kind": "report_production",
            "input": {"operation_id": c["op"], "quantity_scrapped_delta": 1, "scrap_reason": "x"},
        },
        lambda c: {
            "expected_company_id": 1,
            "request_key": str(uuid4()),
            "kind": "report_production",
            "input": {"operation_id": c["op"], "quantity_complete_delta": 2},
        },
        lambda db, c: [t.status for t in db.query(HankTask).all()] == ["awaiting_review"],
    ),
}


class TestThroughTheMcpDoor:
    """The Werco Assistant reaches these routes as MCP tools carrying its API token. The
    route is the boundary (``docs/MCP.md``), so the tool result must be the route's 403 --
    ``is_error``, the exact plain-string detail -- with nothing written behind it."""

    @pytest.mark.parametrize("tool", sorted(DOOR_CASES))
    async def test_a_token_declaring_scrap_through_the_tool_is_refused_writing_nothing(
        self, client, db_session, admin_headers, mcp_catalog, tool
    ):
        role, setup, scrap_args, _, _ = DOOR_CASES[tool]
        user = make_user(db_session, role=role)
        context = setup(db_session, user, False)
        server, _ = make_server(mcp_catalog, headers=_token(client, admin_headers, user))
        before = _world(db_session)

        result = await call(server, tool, scrap_args(context))
        assert result.is_error, result.content
        assert (result.structured_content["status"], result.structured_content["detail"]) == (403, API_TOKEN_SCRAP)
        assert _world(db_session) == before

    @pytest.mark.parametrize("tool", sorted(DOOR_CASES))
    async def test_the_same_token_with_no_scrap_still_works_through_the_tool(
        self, client, db_session, admin_headers, mcp_catalog, tool
    ):
        role, setup, _, zero_args, worked = DOOR_CASES[tool]
        user = make_user(db_session, role=role)
        context = setup(db_session, user, True)
        server, _ = make_server(mcp_catalog, headers=_token(client, admin_headers, user))

        result = await call(server, tool, zero_args(context))
        assert not result.is_error, result.content
        assert worked(db_session, context)


# --------------------------------------------------------------------------- #
# Every scrap writer is enumerated here -- three ways -- so a new one fails CI
# --------------------------------------------------------------------------- #
_SCRAP_COLUMNS = frozenset({"quantity_scrapped", "scrap_reason", "scrap_reason_code_id", "last_reported_scrapped"})
_SCRAP_MODELS = frozenset({"WorkOrder", "WorkOrderOperation", "TimeEntry"})

# (1) Functions that assign a scrap column on a WorkOrder / WorkOrderOperation / TimeEntry.
# Each GUARDED one calls ``api_token_declares_scrap`` itself and is pinned by the class named.
AST_GUARDED_WRITERS = {
    ("app.api.endpoints.shop_floor", "clock_out"): "TestClockOut",
    ("app.api.endpoints.shop_floor", "complete_operation"): "TestFloorClose",
    ("app.services.shop_floor_commands", "report_production_command"): "TestProductionReport",
    ("app.api.endpoints.work_orders", "complete_work_order"): "TestForceComplete",
    ("app.api.endpoints.work_orders", "complete_operation"): "TestOfficeClose",
}
AST_EXEMPT_WRITERS = {
    ("app.api.endpoints.work_orders", "_enrich_work_order_operations"): "NULL -> 0 for serialization on a read",
    ("app.api.endpoints.work_orders", "get_work_order"): "NULL -> 0 for serialization on a read",
    ("app.api.endpoints.work_orders", "_run_laser_nest_import"): (
        "a nest import resets its laser child to 0 -- a standing API token is refused (403 count-judgment) when "
        "any operation the import would delete has recorded work (TestApiTokenNestReimportOverRecordedWork)"
    ),
    ("app.services.work_order_duplicate_service", "_copy_operations"): "a duplicate's operations start at 0",
    ("app.services.material_readiness_service", "material_readiness"): "a SimpleNamespace preview, not a row",
    (
        "app.services.work_order_state_service",
        "reconcile_work_orders_from_completion_evidence",
    ): "the read-path heal copies time-entry scrap a person recorded through a guarded writer up to the operation",
}

# (2) Write routes whose request (body or query) carries a scrap field. The two blind-setattr
# PUTs appear here, not in (1): they write whatever their schema names.
ROUTE_PINS = {
    ("POST", "/api/v1/shop-floor/clock-out/{time_entry_id}"): "TestClockOut",
    ("POST", "/api/v1/shop-floor/operations/{operation_id}/production"): "TestProductionReport",
    ("POST", "/api/v1/shop-floor/operations/{operation_id}/complete"): "TestFloorClose",
    ("POST", "/api/v1/work-orders/operations/{operation_id}/complete"): "TestOfficeClose",
    ("POST", "/api/v1/work-orders/{work_order_id}/complete"): "TestForceComplete",
    ("PUT", "/api/v1/work-orders/operations/{operation_id}"): "TestOperationPut",
    ("PUT", "/api/v1/work-orders/{work_order_id}"): "TestWorkOrderPut",
}
ROUTE_EXEMPT = {
    ("POST", "/api/v1/bom/"): "scrap_factor is a BOM line's planning allowance, not recorded scrap",
    ("POST", "/api/v1/bom/{bom_id}/items"): "scrap_factor is a BOM line's planning allowance, not recorded scrap",
    ("PUT", "/api/v1/bom/items/{item_id}"): "scrap_factor is a BOM line's planning allowance, not recorded scrap",
    (
        "POST",
        "/api/v1/fabrication-quotes/{quote_id}/actuals",
    ): "a quote-calibration observation (FabricationQuoteActual.observation_json), not a production scrap record",
}
# Hank's task door takes free-form ``input``; its scrap field lives in the task KIND.
HANK_SCRAP_KINDS = {"report_production": "TestHankProductionTask"}

# (3) MCP tools whose input schema carries a scrap field (convenience + generated).
MCP_EXEMPT = {
    "add_bom_item": ROUTE_EXEMPT[("POST", "/api/v1/bom/{bom_id}/items")],
    "create_bom": ROUTE_EXEMPT[("POST", "/api/v1/bom/")],
    "update_bom_item": ROUTE_EXEMPT[("PUT", "/api/v1/bom/items/{item_id}")],
    "record_actuals": ROUTE_EXEMPT[("POST", "/api/v1/fabrication-quotes/{quote_id}/actuals")],
}


def _scrap_property_names(schema) -> set:
    found: set = set()
    if isinstance(schema, dict):
        for name, sub in (schema.get("properties") or {}).items():
            if "scrap" in name.lower():
                found.add(name)
            found |= _scrap_property_names(sub)
        for key, value in schema.items():
            if key != "properties":
                found |= _scrap_property_names(value)
    elif isinstance(schema, list):
        for item in schema:
            found |= _scrap_property_names(item)
    return found


class _ScrapWriteFinder(ast.NodeVisitor):
    def __init__(self, module: str) -> None:
        self.module = module
        self.scope: list = []
        self.writers: dict = {}

    def _scoped(self, node) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

    def _hit(self, node) -> None:
        self.writers.setdefault((self.module, ".".join(self.scope)), []).append(node)

    def _store_targets(self, targets) -> None:
        for target in targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Attribute) and sub.attr in _SCRAP_COLUMNS and isinstance(sub.ctx, ast.Store):
                    self._hit(sub)

    def visit_Assign(self, node) -> None:
        self._store_targets(node.targets)
        self.generic_visit(node)

    def visit_AugAssign(self, node) -> None:
        self._store_targets([node.target])
        self.generic_visit(node)

    def visit_AnnAssign(self, node) -> None:
        self._store_targets([node.target])
        self.generic_visit(node)

    def visit_Call(self, node) -> None:
        name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
        if name in _SCRAP_MODELS and any(kw.arg in _SCRAP_COLUMNS for kw in node.keywords):
            self._hit(node)
        if (
            name == "setattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in _SCRAP_COLUMNS
        ):
            self._hit(node)
        self.generic_visit(node)


def _function_nodes(tree) -> dict:
    """``qualname -> FunctionDef`` for every function in a module."""
    out: dict = {}

    def walk(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualname = scope + (child.name,)
                if not isinstance(child, ast.ClassDef):
                    out[".".join(qualname)] = child
                walk(child, qualname)

    walk(tree, ())
    return out


@pytest.mark.unit
class TestEveryScrapWriterIsEnumerated:
    """The three enumerations the owner decision was audited against. A NEW scrap writer --
    an ORM assignment, a request field, an MCP tool -- fails here until it is guarded and
    pinned, or listed as exempt with the reason it declares no scrap."""

    def test_every_function_that_writes_a_scrap_column_is_guarded_or_exempt(self):
        app_root = Path(app_package.__file__).parent
        writers: dict = {}
        trees: dict = {}
        for path in sorted(app_root.rglob("*.py")):
            module = ".".join(("app",) + path.relative_to(app_root).with_suffix("").parts)
            trees[module] = ast.parse(path.read_text())
            finder = _ScrapWriteFinder(module)
            finder.visit(trees[module])
            writers.update(finder.writers)

        assert set(writers) == set(AST_GUARDED_WRITERS) | set(AST_EXEMPT_WRITERS)
        for (module, qualname), pinning_class in AST_GUARDED_WRITERS.items():
            body = _function_nodes(trees[module])[qualname]
            calls = {
                sub.func.id for sub in ast.walk(body) if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
            }
            assert "api_token_declares_scrap" in calls, f"{module}.{qualname} writes scrap with no token check"
            assert pinning_class in globals(), pinning_class

    def test_every_write_route_taking_scrap_is_pinned_or_exempt(self):
        spec = app.openapi()
        components = spec["components"]["schemas"]
        found = set()
        for path, item in spec["paths"].items():
            for method, operation in item.items():
                if method not in {"post", "put", "patch", "delete"}:
                    continue
                names = {
                    p["name"]
                    for p in (item.get("parameters") or []) + (operation.get("parameters") or [])
                    if "scrap" in p["name"].lower()
                }
                for media in ((operation.get("requestBody") or {}).get("content") or {}).values():
                    names |= _scrap_property_names(resolve_schema(media.get("schema"), components))
                if names:
                    found.add((method.upper(), path))
        assert found == set(ROUTE_PINS) | set(ROUTE_EXEMPT)
        assert all(name in globals() for name in ROUTE_PINS.values())

    def test_the_hank_task_kinds_that_carry_scrap_are_pinned(self):
        carrying = {
            kind for kind, model in HANK_INPUT_SCHEMAS.items() if _scrap_property_names(model.model_json_schema())
        }
        assert carrying == set(HANK_SCRAP_KINDS)
        assert all(name in globals() for name in HANK_SCRAP_KINDS.values())

    def test_every_mcp_tool_taking_scrap_has_a_door_test_or_is_exempt(self, mcp_catalog):
        tools = [*CONVENIENCE_TOOLS, *mcp_catalog]
        carrying = {tool.name for tool in tools if _scrap_property_names(tool.input_schema)}
        door_tested = set(DOOR_CASES) - {"prepare_task"}
        assert carrying == door_tested | set(MCP_EXEMPT)
        # The Hank door's schema is free-form, so it is pinned by name.
        assert {"prepare_task", "execute_task"} <= {tool.name for tool in tools}
