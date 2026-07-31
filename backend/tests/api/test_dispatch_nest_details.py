"""Laser-nest details on the dispatch board row.

A planner sequences nests by MATERIAL and THICKNESS -- batching like with like
is what avoids sheet swaps, assist-gas changes and nozzle/lens changes. The
board's cards carried only the WO number and the operation name, so ordering
nests was guesswork. ``DispatchQueueRow.laser_nest`` closes that gap.

Invariants pinned here:

1. SHAPE + SEMANTICS MATCH THE KIOSK. The board's block is a subset of the
   kiosk queue's ``laser_nest`` object, field-for-field, with identical values
   (``dispatch_service.dispatch_nest_info`` vs ``shop_floor._laser_nest_payload``).
2. SOFT-DELETED NESTS NEVER SURFACE. Both surfaces decide "which nest is live"
   through ``laser_nest_service.active_laser_nest``.
3. NO N+1. The board renders every work center's queue at once, so the nest
   must be eager-loaded: the query count may not grow with the number of cards.
4. THE BOARD DOES NOT WRITE. Unlike the kiosk, the board does not reconcile
   ``nest.completed_runs`` -- the same row builder also serves the run-order
   PUT, which commits.
"""

from typing import List, Optional, Tuple

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.inventory import InventoryItem, InventoryTransaction
from app.models.laser_nest import LaserNest, LaserNestPackage
from app.models.part import Part
from app.models.user import UserRole
from app.models.work_order import WorkOrder, WorkOrderOperation, WorkOrderType
from app.models.work_order_material import AllocationSource, AllocationStatus, WorkOrderMaterialAllocation
from app.services import dispatch_service
from tests.api.kiosk_test_helpers import (
    COMPANY_A,
    make_user,
    make_wo_with_operation,
    make_work_center,
    queue_url,
    user_headers,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

BOARD_URL = "/api/v1/shop-floor/dispatch-board"

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _column(payload: dict, work_center_id: int) -> dict:
    for column in payload["work_centers"]:
        if column["id"] == work_center_id:
            return column
    raise AssertionError(f"work center {work_center_id} missing from board")


def make_nest_operation(
    db: Session,
    *,
    work_center,
    company_id: int = COMPANY_A,
    material: Optional[str] = "A36",
    thickness: Optional[str] = "0.25in",
    sheet_size: Optional[str] = "48x96",
    cnc_number: Optional[str] = None,
    planned_runs: int = 5,
    completed_runs: float = 0.0,
    quantity_complete: float = 0.0,
    is_deleted: bool = False,
) -> Tuple[WorkOrderOperation, LaserNest]:
    """A laser-cutting WO with ONE nest-backed queued operation.

    Mirrors what ``laser_nest_service.build_laser_nest_child_work_order``
    produces: a LASER_CUTTING work order, one operation per nest, and one
    ``LaserNest`` row under a package pointing back at that operation.
    """
    n = _next()
    work_order, operation = make_wo_with_operation(db, company_id=company_id, work_center=work_center)
    work_order.work_order_type = WorkOrderType.LASER_CUTTING.value
    operation.operation_number = f"Nest {n}"
    operation.name = f"Laser Cut - CNC{n:04d}"
    operation.operation_group = "LASER"
    operation.quantity_complete = quantity_complete
    package = LaserNestPackage(
        company_id=company_id,
        parent_work_order_id=None,
        child_work_order_id=work_order.id,
        package_name=f"Package {n}",
        import_status="imported",
    )
    db.add(package)
    db.flush()
    nest = LaserNest(
        company_id=company_id,
        package_id=package.id,
        work_order_operation_id=operation.id,
        nest_name=f"CNC{n:04d}",
        cnc_number=cnc_number if cnc_number is not None else f"CNC{n:04d}",
        planned_runs=planned_runs,
        completed_runs=completed_runs,
        material=material,
        thickness=thickness,
        sheet_size=sheet_size,
        is_deleted=is_deleted,
    )
    db.add(nest)
    db.commit()
    db.refresh(operation)
    db.refresh(nest)
    return operation, nest


def make_sheet_part(db: Session, *, company_id: int = COMPANY_A, on_hand: float = 100.0) -> Part:
    """A raw-material part with one available lot -- what a nest's tie points at."""
    n = _next()
    part = Part(
        part_number=f"SHEET-{n:05d}",
        name=f"Sheet stock {n}",
        part_type="raw_material",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()
    db.add(
        InventoryItem(
            part_id=part.id,
            location="RAW-A",
            warehouse="MAIN",
            quantity_on_hand=on_hand,
            quantity_allocated=0.0,
            quantity_available=on_hand,
            lot_number=f"LOT-{n:05d}",
            unit_cost=80.0,
            status="available",
            is_active=True,
            company_id=company_id,
        )
    )
    db.commit()
    db.refresh(part)
    return part


def tie_operation(
    db: Session,
    operation: WorkOrderOperation,
    part: Part,
    *,
    qty_per_run: float = 1.0,
    qty_planned: float = 5.0,
    qty_consumed: float = 0.0,
    status: AllocationStatus = AllocationStatus.OPEN,
    company_id: int = COMPANY_A,
) -> WorkOrderMaterialAllocation:
    """An OPERATION-scoped material tie, the shape the nest import creates."""
    allocation = WorkOrderMaterialAllocation(
        company_id=company_id,
        work_order_id=operation.work_order_id,
        work_order_operation_id=operation.id,
        part_id=part.id,
        source=AllocationSource.NEST,
        status=status,
        qty_per_run=qty_per_run,
        qty_planned=qty_planned,
        unit_of_measure="each",
        qty_consumed=qty_consumed,
    )
    db.add(allocation)
    db.commit()
    db.refresh(allocation)
    return allocation


def _count_select_queries(db: Session, fn) -> int:
    """Run ``fn`` and return the number of statements ITS OWN engine executed.

    Listens on ``db.get_bind()`` -- the engine the fixture session is actually
    bound to -- NOT on the module-level ``tests.conftest.engine``.

    That distinction is the whole reason this helper takes a session. ``tests/``
    has no ``__init__.py``, so pytest imports the fixture file as the top-level
    module ``conftest`` while ``from tests.conftest import ...`` imports a SECOND,
    independent copy as ``tests.conftest``. Each copy runs ``create_engine`` at
    import, so there are two distinct ``Engine`` objects over the same SQLite
    file, and ``db_session`` is bound to the pytest one. Listening on the other
    object recorded ZERO statements for every call -- which made every assertion
    in :class:`TestNoNPlusOne` vacuously true (``0 == 0``, ``0 <= 3``): the board's
    N+1 guard had not actually been guarding anything. Keep the bind lookup here.
    """
    statements: List[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    bind = db.get_bind()
    event.listen(bind, "after_cursor_execute", _record)
    try:
        fn()
    finally:
        event.remove(bind, "after_cursor_execute", _record)
    return len(statements)


class TestBoardRowCarriesNestDetails:
    def test_nest_operation_row_carries_the_full_block(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        operation, _ = make_nest_operation(
            db_session,
            work_center=wc,
            material="A36",
            thickness="0.25in",
            sheet_size="48x96",
            cnc_number="CNC-9001",
            planned_runs=5,
            quantity_complete=2,
        )

        resp = client.get(BOARD_URL, headers=user_headers(manager))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        rows = _column(resp.json(), wc.id)["queue"]
        assert [row["operation_id"] for row in rows] == [operation.id]
        assert rows[0]["laser_nest"] == {
            "cnc_number": "CNC-9001",
            "material": "A36",
            "thickness": "0.25in",
            "sheet_size": "48x96",
            "planned_runs": 5,
            "completed_runs": 2.0,
            "remaining_runs": 3.0,
        }

    def test_non_laser_operation_row_carries_null(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        _, plain = make_wo_with_operation(db_session, work_center=wc)

        resp = client.get(BOARD_URL, headers=user_headers(manager))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        rows = _column(resp.json(), wc.id)["queue"]
        assert [row["operation_id"] for row in rows] == [plain.id]
        assert rows[0]["laser_nest"] is None

    def test_partially_keyed_nest_omits_only_the_missing_fields(self, client: TestClient, db_session: Session):
        """A manually-keyed nest may carry only a CNC number; the block still renders."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        make_nest_operation(
            db_session,
            work_center=wc,
            material=None,
            thickness=None,
            sheet_size=None,
            cnc_number="CNC-7",
            planned_runs=2,
        )

        resp = client.get(BOARD_URL, headers=user_headers(manager))
        block = _column(resp.json(), wc.id)["queue"][0]["laser_nest"]
        assert block["cnc_number"] == "CNC-7"
        assert block["material"] is None
        assert block["thickness"] is None
        assert block["sheet_size"] is None
        assert block["planned_runs"] == 2

    def test_remaining_runs_floors_at_zero_on_an_over_count(self, client: TestClient, db_session: Session):
        """Completed quantity is monotonic-up and can exceed the plan; the
        board must never show negative sheets remaining (same formula as
        ``LaserNest.remaining_runs``)."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        make_nest_operation(db_session, work_center=wc, planned_runs=3, quantity_complete=5)

        block = _column(client.get(BOARD_URL, headers=user_headers(manager)).json(), wc.id)["queue"][0]["laser_nest"]
        assert block["completed_runs"] == 5.0
        assert block["remaining_runs"] == 0.0

    def test_run_order_put_response_carries_nest_details_too(self, client: TestClient, db_session: Session):
        """The rewrite echoes a board column back; it must be the SAME row shape,
        or the board would blank every nest line straight after a reorder."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        first, _ = make_nest_operation(db_session, work_center=wc, material="A36")
        second, _ = make_nest_operation(db_session, work_center=wc, material="SS304")

        resp = client.put(
            f"/api/v1/shop-floor/work-centers/{wc.id}/run-order",
            json={"operation_ids": [second.id, first.id]},
            headers=user_headers(manager),
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        rows = resp.json()["queue"]
        assert [row["operation_id"] for row in rows] == [second.id, first.id]
        assert [row["laser_nest"]["material"] for row in rows] == ["SS304", "A36"]


class TestSoftDeletedNestNeverSurfaces:
    """Invariant 3 of CLAUDE.md on the read side: a soft-deleted nest is gone
    from the UI. ``WorkOrderOperation.laser_nest`` happily loads a deleted row,
    so the board routes through ``active_laser_nest`` exactly like the kiosk."""

    def test_soft_deleted_nest_leaves_the_row_null(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        operation, _ = make_nest_operation(db_session, work_center=wc, is_deleted=True)

        resp = client.get(BOARD_URL, headers=user_headers(manager))
        rows = _column(resp.json(), wc.id)["queue"]
        # The operation itself is still queued -- only its nest block is withheld.
        assert [row["operation_id"] for row in rows] == [operation.id]
        assert rows[0]["laser_nest"] is None

    def test_service_projection_agrees(self, db_session: Session):
        wc = make_work_center(db_session)
        live_op, _ = make_nest_operation(db_session, work_center=wc)
        deleted_op, _ = make_nest_operation(db_session, work_center=wc, is_deleted=True)

        assert dispatch_service.dispatch_nest_info(live_op) is not None
        assert dispatch_service.dispatch_nest_info(deleted_op) is None


class TestMatchesTheKioskPayload:
    def test_board_block_is_a_field_for_field_subset_of_the_kiosk_block(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        headers = user_headers(manager)
        wc = make_work_center(db_session)
        operation, _ = make_nest_operation(
            db_session,
            work_center=wc,
            material="AL5052",
            thickness="10ga",
            sheet_size="60x120",
            cnc_number="CNC-4242",
            planned_runs=7,
            quantity_complete=3,
        )

        board_block = _column(client.get(BOARD_URL, headers=headers).json(), wc.id)["queue"][0]["laser_nest"]

        kiosk = client.get(queue_url(wc.id), headers=headers)
        assert kiosk.status_code == status.HTTP_200_OK, kiosk.text
        kiosk_rows = [row for row in kiosk.json()["queue"] if row["operation_id"] == operation.id]
        kiosk_block = kiosk_rows[0]["laser_nest"]

        for field in board_block:
            assert field in kiosk_block, f"{field} is not a kiosk laser_nest field"
            assert board_block[field] == kiosk_block[field], field


class TestBoardDoesNotWrite:
    def test_board_read_does_not_reconcile_completed_runs(self, client: TestClient, db_session: Session):
        """The kiosk's payload builder WRITES ``nest.completed_runs``. The board's
        must not: the same row builder serves ``PUT .../run-order``, which commits,
        so a reorder would silently persist a nest reconcile."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        _, nest = make_nest_operation(
            db_session,
            work_center=wc,
            planned_runs=6,
            completed_runs=0.0,
            quantity_complete=4,
        )
        nest_id = nest.id

        block = _column(client.get(BOARD_URL, headers=user_headers(manager)).json(), wc.id)["queue"][0]["laser_nest"]
        # Served value follows the OPERATION (what the kiosk's sync would have written)...
        assert block["completed_runs"] == 4.0
        assert block["remaining_runs"] == 2.0
        # ...but the stored nest row is untouched.
        db_session.expire_all()
        assert db_session.get(LaserNest, nest_id).completed_runs == 0.0

    def test_board_read_of_an_open_tie_consumes_nothing(self, client: TestClient, db_session: Session):
        """A board poll must never move stock.

        Consumption is a completion-path verb: a read has no actor intent and
        records no reason, and this same projection is reused by
        ``PUT .../run-order``, which COMMITS -- so a write here would be silently
        persisted by a manager's drag-reorder. The operation carries completed
        runs, so a reconcile-on-read would have real work to do and a genuine
        positive delta to post; nothing may.
        """
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        operation, _ = make_nest_operation(db_session, work_center=wc, planned_runs=5, quantity_complete=3)
        sheet = make_sheet_part(db_session, on_hand=50.0)
        allocation = tie_operation(db_session, operation, sheet, qty_per_run=1.0, qty_planned=5.0)

        txns_before = db_session.query(InventoryTransaction).count()

        board = client.get(BOARD_URL, headers=user_headers(manager))
        assert board.status_code == status.HTTP_200_OK, board.text
        # The read genuinely saw the tie -- so "nothing was written" is not a
        # statement about an empty code path.
        assert _column(board.json(), wc.id)["queue"][0]["material_tie"]["allocation_id"] == allocation.id

        # And the run-order PUT, which commits, projects the same column.
        rewrite = client.put(
            f"/api/v1/shop-floor/work-centers/{wc.id}/run-order",
            json={"operation_ids": [operation.id]},
            headers=user_headers(manager),
        )
        assert rewrite.status_code == status.HTTP_200_OK, rewrite.text

        db_session.expire_all()
        # No ledger row, no advance of the consumed cache, no stock movement.
        assert db_session.query(InventoryTransaction).count() == txns_before
        assert db_session.get(WorkOrderMaterialAllocation, allocation.id).qty_consumed == 0.0
        stock = db_session.query(InventoryItem).filter(InventoryItem.part_id == sheet.id).one()
        assert stock.quantity_on_hand == 50.0
        # The only audit rows the PUT may add are its own run-order rewrite -- never
        # an allocation row (consumption, shortage, or held-material).
        assert (
            db_session.query(AuditLog).filter(AuditLog.resource_type == "work_order_material_allocation").count() == 0
        )


class TestNoNPlusOne:
    """The property that matters is FLAT-IN-CARDS, not the absolute number.

    A 24-machine shop renders every queue at once, so a per-row SELECT is
    multiplied by the whole floor. The absolute ratchets below exist only to stop
    a constant cost from quietly becoming a constant LARGE cost; the
    ``five == one`` assertions are the real guard.
    """

    # Untied board: 2 work-center SELECTs (active + inactive-with-work) + 1 queued
    # operations SELECT + 1 material-allocation SELECT that comes back empty and
    # short-circuits ``tie_views_for_operations`` before it reads parts or stock.
    # That fourth query is the documented "cost of an untied work order: exactly
    # one tenant-scoped SELECT".
    UNTIED_BOARD_QUERIES = 4
    # Tied board: the same 4 plus the tie view's other two legs -- one part-label
    # SELECT and one UNION-ALL inventory aggregate. Both are batched for the whole
    # board, which is why this number does not move with the card count.
    TIED_BOARD_QUERIES = 6

    def test_board_query_count_does_not_grow_with_the_number_of_nest_cards(self, db_session: Session):
        """Without ``joinedload(WorkOrderOperation.laser_nest)`` this costs one
        extra SELECT per card -- across every work center on the board."""
        wc = make_work_center(db_session)
        make_nest_operation(db_session, work_center=wc)

        db_session.expire_all()
        one_card = _count_select_queries(
            db_session, lambda: dispatch_service.build_dispatch_board(db_session, COMPANY_A)
        )

        for _ in range(4):
            make_nest_operation(db_session, work_center=wc)

        db_session.expire_all()
        five_cards = _count_select_queries(
            db_session, lambda: dispatch_service.build_dispatch_board(db_session, COMPANY_A)
        )

        assert five_cards == one_card
        # Pin the absolute cost too, so a future eager load can't quietly become
        # "constant but large". Raised from 3 in PR 2: the material-tie read adds
        # exactly ONE SELECT to an untied board (it comes back empty and stops).
        assert one_card == self.UNTIED_BOARD_QUERIES

    def test_board_query_count_does_not_grow_with_the_number_of_TIED_cards(self, db_session: Session):
        """The tie read is batched ONCE for the whole board, not once per card.

        This is the assertion the material-tie feature actually owes: a naive
        per-row ``tie_views_for_operations`` would be correct and would still
        multiply the kiosk/board cost by the queue length on a 24-machine shop.
        Five tied nests must cost exactly what one tied nest costs.
        """
        wc = make_work_center(db_session)
        first, _ = make_nest_operation(db_session, work_center=wc)
        tie_operation(db_session, first, make_sheet_part(db_session))

        db_session.expire_all()
        one_tied_card = _count_select_queries(
            db_session, lambda: dispatch_service.build_dispatch_board(db_session, COMPANY_A)
        )

        for _ in range(4):
            operation, _nest = make_nest_operation(db_session, work_center=wc)
            # A DISTINCT part per tie, so the part-label and inventory legs would
            # both fan out if either were per-row rather than per-board.
            tie_operation(db_session, operation, make_sheet_part(db_session))

        db_session.expire_all()
        five_tied_cards = _count_select_queries(
            db_session, lambda: dispatch_service.build_dispatch_board(db_session, COMPANY_A)
        )

        assert five_tied_cards == one_tied_card
        assert one_tied_card == self.TIED_BOARD_QUERIES

    def test_nest_is_eager_loaded_by_the_shared_load_options(self, db_session: Session):
        """Serializing a board row must not touch the DB at all once the queue
        query has run -- proven by expiring, loading, then counting."""
        wc = make_work_center(db_session)
        make_nest_operation(db_session, work_center=wc)
        db_session.expire_all()

        operations = dispatch_service.queued_operations(
            db_session,
            COMPANY_A,
            [wc.id],
            load_options=dispatch_service.queue_row_load_options(),
        )
        assert len(operations) == 1

        serialize_cost = _count_select_queries(
            db_session, lambda: dispatch_service.dispatch_queue_row(operations[0], 1)
        )
        assert serialize_cost == 0

    def test_row_projection_stays_db_free_when_ties_are_passed_in(self, db_session: Session):
        """``dispatch_queue_row`` takes ALREADY-FETCHED ties and must never look
        one up: a lazy ``allocation.part`` inside the row builder is an N+1
        multiplied by the whole shop's queue."""
        wc = make_work_center(db_session)
        operation, _ = make_nest_operation(db_session, work_center=wc)
        tie_operation(db_session, operation, make_sheet_part(db_session))
        db_session.expire_all()

        operations = dispatch_service.queued_operations(
            db_session,
            COMPANY_A,
            [wc.id],
            load_options=dispatch_service.queue_row_load_options(),
        )
        # NOT expired afterwards: the queue query has already eager-loaded every
        # relationship the row builder touches, and ``MaterialTieView`` is a plain
        # frozen dataclass, so serialization has nothing left to fetch.
        ties = dispatch_service.material_tie_info(db_session, COMPANY_A, operations)

        serialize_cost = _count_select_queries(
            db_session,
            lambda: dispatch_service.dispatch_queue_row(operations[0], 1, ties=ties.get(operations[0].id)),
        )
        assert serialize_cost == 0

    def test_work_order_and_part_stay_eager_loaded(self, db_session: Session):
        """Regression guard for the relationships the shared options already
        covered before the nest joined them."""
        wc = make_work_center(db_session)
        make_wo_with_operation(db_session, work_center=wc)
        db_session.expire_all()

        operations = dispatch_service.queued_operations(
            db_session,
            COMPANY_A,
            [wc.id],
            load_options=dispatch_service.queue_row_load_options(),
        )
        row_cost = _count_select_queries(db_session, lambda: dispatch_service.dispatch_queue_row(operations[0], 1))
        assert row_cost == 0
        assert isinstance(operations[0].work_order, WorkOrder)


class TestBoardRowCarriesTheMaterialTie:
    """``material_tie`` follows ``laser_nest``'s "null unless it applies" rule."""

    def test_tied_operation_row_carries_the_block(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        operation, _ = make_nest_operation(db_session, work_center=wc, planned_runs=5)
        sheet = make_sheet_part(db_session, on_hand=12.0)
        allocation = tie_operation(db_session, operation, sheet, qty_per_run=1.0, qty_planned=5.0, qty_consumed=2.0)

        resp = client.get(BOARD_URL, headers=user_headers(manager))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        block = _column(resp.json(), wc.id)["queue"][0]["material_tie"]

        assert block["allocation_id"] == allocation.id
        assert block["part_id"] == sheet.id
        assert block["part_number"] == sheet.part_number
        assert block["unit_of_measure"] == "each"
        assert block["qty_per_run"] == 1.0
        assert block["qty_planned"] == 5.0
        assert block["qty_consumed"] == 2.0
        # Server-derived: planned - consumed, then the FIFO-eligible stock.
        assert block["qty_remaining"] == 3.0
        assert block["on_hand"] == 12.0
        assert block["short_by"] == 0.0
        assert block["pinned_inventory_item_id"] is None
        assert block["pinned_lot_number"] is None

    def test_untied_operation_row_carries_null(self, client: TestClient, db_session: Session):
        """The rendering contract for an untied operation: NOTHING is drawn."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        make_nest_operation(db_session, work_center=wc)

        resp = client.get(BOARD_URL, headers=user_headers(manager))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert _column(resp.json(), wc.id)["queue"][0]["material_tie"] is None

    def test_cancelled_and_work_order_scoped_ties_never_reach_the_board(self, client: TestClient, db_session: Session):
        """A CANCELLED tie must not paint a chip, and a WORK-ORDER-scoped tie
        would fan across every card of that job and read as N separate ties."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        operation, _ = make_nest_operation(db_session, work_center=wc)
        tie_operation(db_session, operation, make_sheet_part(db_session), status=AllocationStatus.CANCELLED)
        db_session.add(
            WorkOrderMaterialAllocation(
                company_id=COMPANY_A,
                work_order_id=operation.work_order_id,
                work_order_operation_id=None,  # work-order-scoped
                part_id=make_sheet_part(db_session).id,
                source=AllocationSource.MANUAL,
                status=AllocationStatus.OPEN,
                qty_per_run=None,
                qty_planned=4.0,
                unit_of_measure="each",
                qty_consumed=0.0,
            )
        )
        db_session.commit()

        resp = client.get(BOARD_URL, headers=user_headers(manager))
        assert _column(resp.json(), wc.id)["queue"][0]["material_tie"] is None

    def test_run_order_put_response_carries_the_tie_too(self, client: TestClient, db_session: Session):
        """The PUT's response REPLACES the whole column on the manager's board,
        so a drag-reorder that dropped ``material_tie`` would read as "reordering
        untied the shop's material"."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        first, _ = make_nest_operation(db_session, work_center=wc)
        second, _ = make_nest_operation(db_session, work_center=wc)
        first_sheet = make_sheet_part(db_session)
        second_sheet = make_sheet_part(db_session)
        tie_operation(db_session, first, first_sheet)
        tie_operation(db_session, second, second_sheet)

        resp = client.put(
            f"/api/v1/shop-floor/work-centers/{wc.id}/run-order",
            json={"operation_ids": [second.id, first.id]},
            headers=user_headers(manager),
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        rows = resp.json()["queue"]
        assert [row["operation_id"] for row in rows] == [second.id, first.id]
        assert [row["material_tie"]["part_number"] for row in rows] == [
            second_sheet.part_number,
            first_sheet.part_number,
        ]
