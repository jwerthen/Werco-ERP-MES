"""The floor sees the COMPONENT's live part number, not a snapshot.

Why this exists, and why it ships in the same release as the renumber verb:

On a BOM-exploded assembly work order, an operation builds a COMPONENT, not the
work order's produced part. The kiosk and dispatch payloads carry
``part_number`` from the work order's own part -- the ASSEMBLY -- so the only
place the component's number reached the floor was the prefix baked into
``operation.name`` when the work order was raised ("ABC-1 - Deburr").

That prefix is a SNAPSHOT. Renumbering a part deliberately does NOT rewrite it,
because an operation name on a released work order is part of the released
quality plan (invariant 5). So without a live component number the floor would
keep building to an identifier the system no longer recognizes -- and the
decision not to rewrite those strings is only defensible because this exists.

Deriving it live fixes it for that rename AND every future one, with nothing
mutated anywhere.

The N+1 test is not padding. ``component_part`` is a lazy relationship, and the
kiosk queue is polled every 10-15 seconds per station, shop-wide. Reading it
without the eager load would cost one extra SELECT per card per poll, and that
regression is invisible in any functional assertion.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models.part import Part
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder, WorkOrderOperation

COMPANY_A = 1


def _part(db: Session, *, number: str, name: str, part_type: str = "manufactured") -> Part:
    part = Part(
        part_number=number,
        name=name,
        part_type=part_type,
        unit_of_measure="each",
        is_active=True,
        company_id=COMPANY_A,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def _assembly_job(db: Session, *, component: Part, suffix: str) -> tuple[WorkOrder, WorkOrderOperation, WorkCenter]:
    """An assembly work order whose operation builds `component`.

    The operation carries BOTH the stale baked prefix and a live component FK --
    the exact state after a renumber has drained the links and swapped the number.
    """
    wc = WorkCenter(name=f"Brake {suffix}", code=f"BRK-{suffix}", work_center_type="press_brake", company_id=COMPANY_A)
    db.add(wc)
    assembly = _part(db, number=f"ASSY-{suffix}", name="Assembly", part_type="assembly")
    wo = WorkOrder(
        work_order_number=f"WO-COMP-{suffix}",
        part_id=assembly.id,
        quantity_ordered=10,
        status="released",
        priority=3,
        company_id=COMPANY_A,
    )
    db.add(wo)
    db.flush()
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id,
        sequence=10,
        operation_number="10",
        # The STALE prefix: what the number was when the work order was raised.
        name="OLD-123 - Deburr",
        component_part_id=component.id,
        component_quantity=20,
        status="ready",
        company_id=COMPANY_A,
    )
    db.add(op)
    db.commit()
    db.refresh(op)
    return wo, op, wc


@pytest.mark.api
@pytest.mark.requires_db
class TestKioskQueueCarriesLiveComponentNumber:
    def test_queue_row_reports_the_components_current_number(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        """The assertion that matters: LIVE number, while the baked prefix stays stale.

        Both are asserted in the same test on purpose. Checking only that the live
        number is present would pass against an implementation that also rewrote
        `operation_name` — which is precisely what must NOT happen.
        """
        component = _part(db_session, number="NEW-456", name="Bracket", part_type="purchased")
        _wo, op, wc = _assembly_job(db_session, component=component, suffix="K1")

        response = client.get(
            f"/api/v1/shop-floor/work-center-queue/{wc.id}",
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        rows = [r for r in response.json()["queue"] if r["operation_id"] == op.id]
        assert rows, "the queued operation is missing from the kiosk queue"
        row = rows[0]

        assert row["component_part_number"] == "NEW-456", "the floor must see the CURRENT number"
        assert row["component_part_name"] == "Bracket"
        # The released quality plan is untouched — the prefix still reads the old number.
        assert row["operation_name"] == "OLD-123 - Deburr"
        # And `part_number` remains the ASSEMBLY's, which is why the component line
        # is needed at all.
        assert row["part_number"].startswith("ASSY-")

    def test_component_fields_are_null_when_there_is_no_component(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        """Most operations carry no component; the card must render nothing for them."""
        wc = WorkCenter(name="Lathe", code="LTH-K2", work_center_type="cnc_lathe", company_id=COMPANY_A)
        db_session.add(wc)
        part = _part(db_session, number="PLAIN-1", name="Shaft")
        wo = WorkOrder(
            work_order_number="WO-PLAIN-K2",
            part_id=part.id,
            quantity_ordered=5,
            status="released",
            priority=3,
            company_id=COMPANY_A,
        )
        db_session.add(wo)
        db_session.flush()
        op = WorkOrderOperation(
            work_order_id=wo.id,
            work_center_id=wc.id,
            sequence=10,
            operation_number="10",
            name="Turn OD",
            component_part_id=None,
            status="ready",
            company_id=COMPANY_A,
        )
        db_session.add(op)
        db_session.commit()

        response = client.get(f"/api/v1/shop-floor/work-center-queue/{wc.id}", headers=auth_headers)
        row = [r for r in response.json()["queue"] if r["operation_id"] == op.id][0]
        assert row["component_part_number"] is None
        assert row["component_part_name"] is None

    def test_the_component_part_is_eager_loaded(self, client: TestClient, auth_headers: dict, db_session: Session):
        """No N+1 across the queue.

        The kiosk queue is polled every 10-15s per station, shop-wide. A lazy
        `component_part` would cost one extra SELECT per card per poll, and no
        functional assertion would notice. Counts SELECTs against `parts` while
        serving a queue of three assembly operations: with the eager load the part
        rows come back with the operations, so the count does not scale with the
        number of cards.
        """
        wc = WorkCenter(name="Brake N1", code="BRK-N1", work_center_type="press_brake", company_id=COMPANY_A)
        db_session.add(wc)
        db_session.commit()

        for i in range(3):
            component = _part(db_session, number=f"COMP-N{i}", name=f"Part {i}", part_type="purchased")
            assembly = _part(db_session, number=f"ASSY-N{i}", name="Assembly", part_type="assembly")
            wo = WorkOrder(
                work_order_number=f"WO-N{i}",
                part_id=assembly.id,
                quantity_ordered=10,
                status="released",
                priority=3,
                company_id=COMPANY_A,
            )
            db_session.add(wo)
            db_session.flush()
            db_session.add(
                WorkOrderOperation(
                    work_order_id=wo.id,
                    work_center_id=wc.id,
                    sequence=10,
                    operation_number="10",
                    name=f"COMP-N{i} - Deburr",
                    component_part_id=component.id,
                    status="ready",
                    company_id=COMPANY_A,
                )
            )
        db_session.commit()

        statements: list[str] = []

        def _record(conn, cursor, statement, parameters, context, executemany):
            if "parts" in statement.lower() and statement.strip().lower().startswith("select"):
                statements.append(statement)

        engine = db_session.get_bind()
        event.listen(engine, "before_cursor_execute", _record)
        try:
            response = client.get(f"/api/v1/shop-floor/work-center-queue/{wc.id}", headers=auth_headers)
        finally:
            event.remove(engine, "before_cursor_execute", _record)

        assert response.status_code == status.HTTP_200_OK
        rows = [r for r in response.json()["queue"] if r["component_part_number"]]
        assert len(rows) == 3, f"expected 3 component rows, got {len(rows)}"
        # Generous bound: the point is that it does NOT grow one-per-card. A lazy
        # relationship would add at least three more on top of the base queries.
        # MEASURED, not guessed: 2 with the eager load, 5 without (one extra per
        # card -- the N+1, exactly). The bound sits between them so the assertion
        # actually discriminates. An earlier version used <= 8 and passed happily
        # with the eager load removed, which is worse than having no test at all.
        assert len(statements) <= 3, (
            f"N+1 on component_part: {len(statements)} SELECTs against parts for 3 cards "
            "(expected 2 with the eager load). The kiosk queue is polled every 10-15s "
            "per station, so this multiplies shop-wide."
        )


@pytest.mark.api
@pytest.mark.requires_db
class TestDispatchBoardCarriesLiveComponentNumber:
    def test_board_row_reports_the_components_current_number(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        component = _part(db_session, number="NEW-789", name="Gusset", part_type="purchased")
        _wo, op, _wc = _assembly_job(db_session, component=component, suffix="D1")

        response = client.get("/api/v1/shop-floor/dispatch-board", headers=auth_headers)
        assert response.status_code == status.HTTP_200_OK, response.text

        rows = [
            row
            for column in response.json().get("work_centers", [])
            for row in column.get("queue", [])
            if row["operation_id"] == op.id
        ]
        assert rows, "the operation is missing from the dispatch board"
        row = rows[0]
        assert row["component_part_number"] == "NEW-789"
        assert row["component_part_name"] == "Gusset"
        # The released operation name is untouched.
        assert row["operation_name"] == "OLD-123 - Deburr"
