"""Second-round review fixes for routing changes on a released work order (2026-11-25).

1. MCP: skip / unskip / resequence are not tools (a person's judgment, with their reason as
   required audit evidence); ``add_operation`` refuses a work order that is not a DRAFT and
   sends no reason; the generated operation update takes no ``change_reason``.
2. A skip that would leave an ON_HOLD work order with no unfinished step is refused 409, so a
   skip can never complete a held job.
3. / 7. Every routing verb locks the work order's operation rows (ascending id) and THEN the
   work order row -- the operation-then-work-order order the completion verbs use -- and
   clock-in and both start verbs row-lock their operation before they read the predecessor
   gate. SQLite cannot race two transactions, so the mechanism is pinned: the order of the
   locked statements, and that each lock really compiles to FOR UPDATE on Postgres.
4. Adding a step refuses another company's (or a deleted) ``component_part_id``.
5. An open tie on a SKIPPED step no longer cancels the BOM backflush: completion and the
   preview agree with readiness.
6. Every line item skipped with only component steps left: the all-complete header and the
   force-complete bounds agree.
"""

from typing import Any, Dict, Optional

import pytest
from sqlalchemy import event
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session
from sqlalchemy.orm import Session as OrmSession

from app.main import app
from app.mcp.catalog import EXCLUDED_OPERATIONS, ROUTING_CHANGE_OPERATIONS, WITHHELD_BODY_FIELDS, build_catalog
from app.mcp.convenience import ADD_OPERATION_DRAFT_ONLY, CONVENIENCE_TOOL_NAMES, SHADOWED_OPERATIONS
from app.mcp.executor import InProcessExecutor
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services.operation_action_gates import MSG_OPERATION_SKIPPED
from app.services.routing_change_service import (
    SKIP_WOULD_COMPLETE_HELD_WORK_ORDER,
    operation_rows_lock_query,
    work_order_lock_query,
)
from app.services.work_order_state_service import (
    active_work_order_operations,
    finished_quantity_at_all_complete,
    work_order_completion_bounds,
)
from tests.api.kiosk_test_helpers import COMPANY_A, make_user, make_work_center, user_headers
from tests.api.short_count_helpers import fresh, make_part, make_routing, set_op

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_B = 2
PENDING, READY, IN_PROGRESS, COMPLETE, SKIPPED = (
    OperationStatus.PENDING,
    OperationStatus.READY,
    OperationStatus.IN_PROGRESS,
    OperationStatus.COMPLETE,
    OperationStatus.SKIPPED,
)
UPDATE_OPERATION_KEY = ("PUT", "/api/v1/work-orders/operations/{operation_id}")


def _version(db: Session, op) -> int:
    return fresh(db, WorkOrderOperation, op.id).version


def _skip(client, db, op, headers, reason="customer dropped the step"):
    return client.post(
        f"/api/v1/work-orders/operations/{op.id}/skip",
        headers=headers,
        json={"version": _version(db, op), "reason": reason},
    )


def _unskip(client, db, op, headers, reason="customer wants it back"):
    return client.post(
        f"/api/v1/work-orders/operations/{op.id}/unskip",
        headers=headers,
        json={"version": _version(db, op), "reason": reason},
    )


def _op_count(db: Session, wo) -> int:
    db.expire_all()
    return db.query(WorkOrderOperation).filter(WorkOrderOperation.work_order_id == wo.id).count()


@pytest.fixture
def supervisor(db_session: Session):
    return make_user(db_session, role=UserRole.SUPERVISOR)


@pytest.fixture
def three_step(db_session: Session):
    """A released, SEQUENCED routing across three machines: 10 READY, 20 / 30 PENDING."""
    return make_routing(db_session, work_centers=[make_work_center(db_session) for _ in range(3)], sequential=True)


# --------------------------------------------------------------------------- 1. MCP


@pytest.fixture(scope="module")
def catalog():
    return build_catalog(app.openapi(), shadowed=SHADOWED_OPERATIONS, reserved_names=CONVENIENCE_TOOL_NAMES)


class TestRoutingChangesAreNotAgentTools:
    def test_skip_unskip_and_resequence_are_excluded(self, catalog):
        assert ROUTING_CHANGE_OPERATIONS == {
            ("POST", "/api/v1/work-orders/operations/{operation_id}/skip"),
            ("POST", "/api/v1/work-orders/operations/{operation_id}/unskip"),
            ("POST", "/api/v1/work-orders/{work_order_id}/operations/resequence"),
        }
        assert ROUTING_CHANGE_OPERATIONS <= set(EXCLUDED_OPERATIONS)
        keys = {tool.key for tool in catalog}
        names = {tool.name for tool in catalog}
        assert keys.isdisjoint(ROUTING_CHANGE_OPERATIONS)
        assert names.isdisjoint(
            {"work_orders_skip_operation", "work_orders_unskip_operation", "work_orders_resequence_operations"}
        )

    def test_the_operation_update_tool_takes_no_change_reason(self, catalog):
        assert WITHHELD_BODY_FIELDS[UPDATE_OPERATION_KEY] == {"change_reason"}
        update = next(tool for tool in catalog if tool.key == UPDATE_OPERATION_KEY)
        assert "change_reason" not in update.input_schema["properties"]
        assert "change_reason" not in update.body_properties
        assert "change_reason" not in (update.input_schema.get("required") or [])
        # The reason-free uses stay: a work-center move (the dispatch board's door).
        assert "work_center_id" in update.body_properties
        assert update.input_schema["additionalProperties"] is False


async def _call_tool(headers: Dict[str, str], name: str, arguments: Dict[str, Any], catalog):
    from tests.test_mcp_server import RecordingExecutor, call, make_server

    executor = RecordingExecutor(InProcessExecutor(app, version=app.version))
    server, _ = make_server(catalog, headers=headers, executor=executor)
    return await call(server, name, arguments), executor


class TestAddOperationToolIsDraftOnly:
    async def test_a_released_work_order_is_refused_before_the_post(
        self, client, db_session, supervisor, three_step, catalog
    ):
        wo, _ = three_step
        result, executor = await _call_tool(
            user_headers(supervisor),
            "add_operation",
            {"work_order_id": wo.id, "name": "Deburr", "work_center": make_work_center(db_session).id},
            catalog,
        )
        assert result.is_error and result.structured_content["status"] == 409
        assert result.structured_content["detail"] == ADD_OPERATION_DRAFT_ONLY.format(status="RELEASED")
        assert ("POST", f"/api/v1/work-orders/{wo.id}/operations") not in executor.calls
        assert _op_count(db_session, wo) == 3

    async def test_the_tool_takes_no_change_reason(self, client, db_session, supervisor, three_step, catalog):
        wo, _ = three_step
        result, executor = await _call_tool(
            user_headers(supervisor),
            "add_operation",
            {
                "work_order_id": wo.id,
                "name": "Deburr",
                "work_center": make_work_center(db_session).id,
                "change_reason": "the agent's own words",
            },
            catalog,
        )
        assert result.is_error and result.structured_content["status"] == 422
        assert executor.calls == []
        assert _op_count(db_session, wo) == 3

    async def test_a_draft_still_gets_its_step(self, client, db_session, supervisor, catalog):
        wo, _ = make_routing(
            db_session, work_centers=[make_work_center(db_session)], statuses=[PENDING], wo_status=WorkOrderStatus.DRAFT
        )
        result, _ = await _call_tool(
            user_headers(supervisor),
            "add_operation",
            {"work_order_id": wo.id, "name": "Deburr", "work_center": make_work_center(db_session).id},
            catalog,
        )
        assert not result.is_error, result.content
        assert _op_count(db_session, wo) == 2

    async def test_a_content_edit_on_a_released_job_through_the_update_tool_is_refused(
        self, client, db_session, supervisor, three_step, catalog
    ):
        _, (_, op20, _) = three_step
        set_op(db_session, op20, run_instructions="torque to spec")
        base = {"operation_id": op20.id, "version": _version(db_session, op20), "run_instructions": "new spec"}
        # The agent cannot supply the person's reason ...
        with_reason, executor = await _call_tool(
            user_headers(supervisor), "work_orders_update_operation", {**base, "change_reason": "ECO 41"}, catalog
        )
        assert with_reason.is_error and with_reason.structured_content["status"] == 422
        assert executor.calls == []
        # ... and without it the route refuses the content edit on a released job.
        without, _ = await _call_tool(user_headers(supervisor), "work_orders_update_operation", base, catalog)
        assert without.is_error and without.structured_content["status"] == 422
        assert fresh(db_session, WorkOrderOperation, op20.id).run_instructions == "torque to spec"


# --------------------------------------------------------------------------- 2. ON_HOLD


class TestASkipNeverCompletesAHeldWorkOrder:
    def test_skipping_the_last_unfinished_step_of_a_held_job_is_refused(self, client, db_session, supervisor):
        wo, (op10, op20, op30) = make_routing(
            db_session,
            work_centers=[make_work_center(db_session) for _ in range(3)],
            statuses=[COMPLETE, COMPLETE, PENDING],
        )
        set_op(db_session, op10, quantity_complete=10)
        set_op(db_session, op20, quantity_complete=10)
        set_op(db_session, wo, status=WorkOrderStatus.ON_HOLD)
        resp = _skip(client, db_session, op30, user_headers(supervisor))
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == SKIP_WOULD_COMPLETE_HELD_WORK_ORDER
        assert resp.json()["detail"].startswith("Resume the work order first")
        assert fresh(db_session, WorkOrderOperation, op30.id).status == PENDING
        held = fresh(db_session, WorkOrder, wo.id)
        assert held.status == WorkOrderStatus.ON_HOLD
        assert float(held.quantity_complete or 0) == 0

    def test_skipping_another_step_of_a_held_job_is_allowed(self, client, db_session, supervisor, three_step):
        wo, (op10, op20, op30) = three_step
        set_op(db_session, op10, status=COMPLETE, quantity_complete=10)
        set_op(db_session, wo, status=WorkOrderStatus.ON_HOLD)
        resp = _skip(client, db_session, op20, user_headers(supervisor))
        assert resp.status_code == 200, resp.text
        assert fresh(db_session, WorkOrderOperation, op20.id).status == SKIPPED
        assert fresh(db_session, WorkOrder, wo.id).status == WorkOrderStatus.ON_HOLD

    def test_control_the_same_skip_completes_a_job_that_is_not_held(self, client, db_session, supervisor):
        wo, (op10, op20, op30) = make_routing(
            db_session,
            work_centers=[make_work_center(db_session) for _ in range(3)],
            statuses=[COMPLETE, COMPLETE, READY],
        )
        set_op(db_session, op10, quantity_complete=10)
        set_op(db_session, op20, quantity_complete=10)
        set_op(db_session, wo, status=WorkOrderStatus.IN_PROGRESS)
        resp = _skip(client, db_session, op30, user_headers(supervisor))
        assert resp.status_code == 200, resp.text
        assert resp.json()["work_order_status"] == "complete"


# --------------------------------------------------------------------------- 3. / 7. locks


class TestRoutingLocksOperationsThenTheWorkOrder:
    def test_the_operation_lock_compiles_to_an_ordered_for_update_on_postgres(self, db_session):
        sql = str(operation_rows_lock_query(db_session, 1, COMPANY_A).statement.compile(dialect=postgresql.dialect()))
        assert "FROM work_order_operations" in sql
        assert "work_order_operations.company_id" in sql and "work_order_operations.work_order_id" in sql
        assert "ORDER BY work_order_operations.id FOR UPDATE" in sql
        wo_sql = str(work_order_lock_query(db_session, 1, COMPANY_A).statement.compile(dialect=postgresql.dialect()))
        assert "FROM work_orders" in wo_sql and wo_sql.rstrip().endswith("FOR UPDATE")

    def test_the_start_lock_compiles_to_for_update_on_postgres(self, db_session):
        from app.services.operation_action_gates import operation_start_lock_query

        sql = str(operation_start_lock_query(db_session, 1, COMPANY_A).statement.compile(dialect=postgresql.dialect()))
        assert "FROM work_order_operations" in sql and "work_order_operations.company_id" in sql
        assert sql.rstrip().endswith("FOR UPDATE")

    @pytest.fixture
    def locked(self):
        """The entity of every ``SELECT ... FOR UPDATE`` a request issues, in order."""
        seen: list[str] = []

        def record(state):
            if not state.is_select or state.statement._for_update_arg is None:
                return
            descriptions = getattr(state.statement, "column_descriptions", None) or []
            entity = descriptions[0].get("entity") if descriptions else None
            seen.append(getattr(entity, "__name__", str(entity)))

        event.listen(OrmSession, "do_orm_execute", record)
        yield seen
        event.remove(OrmSession, "do_orm_execute", record)

    @staticmethod
    def _operations_then_work_order(locked) -> bool:
        if "WorkOrder" not in locked or "WorkOrderOperation" not in locked:
            return False
        return locked.index("WorkOrderOperation") < locked.index("WorkOrder")

    def test_skip_and_unskip(self, client, db_session, supervisor, three_step, locked):
        _, (_, _, op30) = three_step
        headers = user_headers(supervisor)
        locked.clear()
        assert _skip(client, db_session, op30, headers).status_code == 200
        assert self._operations_then_work_order(locked), locked
        locked.clear()
        assert _unskip(client, db_session, op30, headers).status_code == 200
        assert self._operations_then_work_order(locked), locked

    def test_add_and_resequence(self, client, db_session, supervisor, three_step, locked):
        wo, (_, op20, op30) = three_step
        headers = user_headers(supervisor)
        locked.clear()
        added = client.post(
            f"/api/v1/work-orders/{wo.id}/operations",
            headers=headers,
            json={
                "work_center_id": make_work_center(db_session).id,
                "sequence": 40,
                "name": "QC",
                "change_reason": "x",
            },
        )
        assert added.status_code == 200, added.text
        assert self._operations_then_work_order(locked), locked
        moves = [
            {"id": op.id, "version": _version(db_session, op), "sequence": seq} for op, seq in ((op20, 30), (op30, 20))
        ]
        locked.clear()
        resp = client.post(
            f"/api/v1/work-orders/{wo.id}/operations/resequence",
            headers=headers,
            json={"reason": "x", "operations": moves},
        )
        assert resp.status_code == 200, resp.text
        assert self._operations_then_work_order(locked), locked


class TestStartVerbsLockTheirOperationBeforeTheGate:
    """Clock-in and both start verbs take their operation's row lock BEFORE they read the
    predecessor gate, so a routing change that locked the route serializes with them."""

    @pytest.fixture
    def order(self, monkeypatch):
        from app.api.endpoints import shop_floor, work_orders
        from app.services import operation_action_gates

        calls: list[str] = []
        real_lock = operation_action_gates.lock_operation_for_start
        real_gate = operation_action_gates.operation_blocked_by_predecessors

        def lock(db, operation_id, company_id):
            calls.append("lock")
            return real_lock(db, operation_id, company_id)

        def gate(db, operation):
            calls.append("gate")
            return real_gate(db, operation)

        for module in (shop_floor, work_orders):
            monkeypatch.setattr(module, "lock_operation_for_start", lock)
            monkeypatch.setattr(module, "operation_blocked_by_predecessors", gate)
        return calls

    @staticmethod
    def _locked_first(calls) -> bool:
        return "gate" in calls and calls.index("lock") < calls.index("gate") if "lock" in calls else False

    def test_clock_in(self, client, db_session, supervisor, three_step, order):
        _, (op10, _, _) = three_step
        resp = client.post(
            "/api/v1/shop-floor/clock-in",
            headers=user_headers(supervisor),
            json={"work_order_id": op10.work_order_id, "operation_id": op10.id, "work_center_id": op10.work_center_id},
        )
        assert resp.status_code == 200, resp.text
        assert self._locked_first(order), order

    def test_shop_floor_start(self, client, db_session, supervisor, three_step, order):
        _, (op10, _, _) = three_step
        resp = client.put(f"/api/v1/shop-floor/operations/{op10.id}/start", headers=user_headers(supervisor))
        assert resp.status_code == 200, resp.text
        assert self._locked_first(order), order

    def test_office_start(self, client, db_session, supervisor, three_step, order):
        _, (op10, _, _) = three_step
        resp = client.post(f"/api/v1/work-orders/operations/{op10.id}/start", headers=user_headers(supervisor))
        assert resp.status_code == 200, resp.text
        assert self._locked_first(order), order


# --------------------------------------------------------------------------- 4. component part tenancy


class TestAddedStepComponentPartIsTenantScoped:
    def _add(self, client, wo, supervisor, db_session, component_part_id: Optional[int]):
        body: Dict[str, Any] = {
            "work_center_id": make_work_center(db_session).id,
            "sequence": 40,
            "name": "Assemble",
            "change_reason": "customer added a bracket",
        }
        if component_part_id is not None:
            body["component_part_id"] = component_part_id
            body["component_quantity"] = 10
        return client.post(f"/api/v1/work-orders/{wo.id}/operations", headers=user_headers(supervisor), json=body)

    def test_another_companys_part_is_404_and_nothing_is_written(self, client, db_session, supervisor, three_step):
        wo, _ = three_step
        foreign = make_part(db_session, company_id=COMPANY_B)
        resp = self._add(client, wo, supervisor, db_session, foreign.id)
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "Component part not found"
        assert _op_count(db_session, wo) == 3

    def test_a_deleted_part_is_404(self, client, db_session, supervisor, three_step):
        wo, _ = three_step
        gone = make_part(db_session)
        set_op(db_session, gone, is_deleted=True)
        resp = self._add(client, wo, supervisor, db_session, gone.id)
        assert resp.status_code == 404, resp.text
        assert _op_count(db_session, wo) == 3

    def test_an_own_live_part_is_accepted(self, client, db_session, supervisor, three_step):
        wo, _ = three_step
        own = make_part(db_session)
        resp = self._add(client, wo, supervisor, db_session, own.id)
        assert resp.status_code == 200, resp.text
        assert resp.json()["component_part_id"] == own.id


# --------------------------------------------------------------------------- 5. tie on a skipped step


class TestATieOnASkippedStepDoesNotCancelTheBomLine:
    """Part P armed for backflush, BOM 2 x X; an OPEN tie on X sits on op 20, which is SKIPPED."""

    def _job(self, db_session):
        from tests.api import test_backflush_breadth as bf

        user = bf.make_user(db_session)
        wc = bf.make_work_center(db_session)
        fg = bf.make_part(db_session, backflush=True)
        x = bf.make_part(db_session, part_type="purchased")
        bf.make_inventory(db_session, x, qty=100)
        bf.add_bom_item(db_session, bf.make_bom(db_session, fg), x, quantity=2.0)
        wo = bf.make_wo(db_session, fg, quantity_ordered=4, quantity_complete=4)
        bf.make_op(db_session, wo, wc, sequence=10, quantity_complete=4)
        skipped = bf.make_op(db_session, wo, wc, sequence=20, status_=SKIPPED)
        bf.tie(db_session, wo, x, operation=skipped, qty_per_run=1.0, qty_planned=4)
        return bf, user, wo, x, skipped

    def test_completion_issues_the_bom_quantity(self, db_session):
        bf, user, wo, x, _ = self._job(db_session)
        bf.run_effects(db_session, wo, user)
        db_session.expire_all()
        assert bf.wo_issues(db_session, wo) == {x.id: -8.0}
        assert bf.on_hand(db_session, x) == 92.0

    def test_the_resolver_does_not_let_the_skipped_tie_suppress(self, db_session):
        from app.services.completion_inventory_service import _drop_allocation_covered_parts

        bf, _, wo, x, skipped = self._job(db_session)
        from app.models.work_order_material import WorkOrderMaterialAllocation

        ties = db_session.query(WorkOrderMaterialAllocation).filter_by(work_order_id=wo.id).all()
        result, dropped = _drop_allocation_covered_parts(db_session, wo, COMPANY_A, {x.id: 8.0}, ties)
        assert result == {x.id: 8.0} and dropped == {}

    def test_the_preview_agrees(self, db_session):
        from app.services.completion_inventory_service import preview_backflush_for_work_order

        _, _, wo, x, _ = self._job(db_session)
        preview = preview_backflush_for_work_order(db_session, wo, company_id=COMPANY_A)
        line = next(line for line in preview.lines if line.component_part_id == x.id)
        assert not line.suppressed
        assert line.required_quantity == pytest.approx(8.0)

    def test_control_a_tie_on_a_live_step_still_suppresses(self, db_session):
        bf, user, wo, x, skipped = self._job(db_session)
        set_op(db_session, skipped, status=COMPLETE, quantity_complete=4)
        bf.run_effects(db_session, wo, user)
        db_session.expire_all()
        # The tie carried the demand (4 sheets at 1 per run), not the BOM line (8).
        assert bf.wo_issues(db_session, wo).get(x.id) is None
        assert bf.on_hand(db_session, x) == 96.0


# --------------------------------------------------------------------------- 6. header vs bounds


class TestEverySkippedLineWithOnlyComponentStepsLeft:
    def _job(self, db_session, *, declare_short: bool = False):
        bracket = make_part(db_session, part_type="purchased")
        wo, (line_a, line_b, component) = make_routing(
            db_session,
            work_centers=[make_work_center(db_session) for _ in range(3)],
            sequential=False,
            statuses=[SKIPPED, SKIPPED, COMPLETE],
            component_parts=[None, None, bracket],
            component_quantities=[4, 6, 20],
        )
        set_op(db_session, component, quantity_complete=20)
        if declare_short:
            set_op(db_session, component, quantity_complete=18, quantity_scrapped=2, quantity_short_declared=2)
        db_session.refresh(wo)
        return wo, component

    @pytest.mark.parametrize("declare_short", [False, True])
    def test_the_header_and_the_force_complete_bounds_agree(self, db_session, declare_short):
        wo, component = self._job(db_session, declare_short=declare_short)
        active = active_work_order_operations(wo)
        header = finished_quantity_at_all_complete(wo, active, 0.0)
        good = float(component.quantity_complete or 0)
        bounds = work_order_completion_bounds(wo, active, {component.id: good}, {component.id: True})
        assert header == pytest.approx(0.0)
        assert bounds.deliverable == pytest.approx(header)
        assert bounds.physical_max == pytest.approx(header)


# --------------------------------------------------------------------------- inspection sign-off


class TestASkippedStepTakesNoInspectionSignOff:
    def test_signing_off_a_skipped_step_is_refused_and_writes_nothing(self, client, db_session, supervisor, three_step):
        _wo, ops = three_step
        op = ops[1]
        set_op(db_session, op, requires_inspection=True)
        headers = user_headers(supervisor)
        assert _skip(client, db_session, op, headers).status_code == 200

        resp = client.post(f"/api/v1/shop-floor/operations/{op.id}/inspection", headers=headers, json={})

        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == MSG_OPERATION_SKIPPED
        assert fresh(db_session, WorkOrderOperation, op.id).inspection_complete is not True

    def test_control_a_restored_step_can_be_signed_off(self, client, db_session, supervisor, three_step):
        _wo, ops = three_step
        op = ops[1]
        set_op(db_session, op, requires_inspection=True)
        headers = user_headers(supervisor)
        assert _skip(client, db_session, op, headers).status_code == 200
        assert _unskip(client, db_session, op, headers).status_code == 200

        resp = client.post(f"/api/v1/shop-floor/operations/{op.id}/inspection", headers=headers, json={})

        assert resp.status_code == 200, resp.text
        assert fresh(db_session, WorkOrderOperation, op.id).inspection_complete is True
