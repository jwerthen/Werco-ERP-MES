"""``operation_number`` stores an IDENTIFIER, never a display label.

``WorkOrderOperation.operation_number`` / ``RoutingOperation.operation_number`` are
``String(20)`` columns naming an operation. Every mint site used to bake a display
prefix into them (``f"Op {sequence}"``), and every UI then prefixed the stored value
again at render time — which is how the kiosk read "Op Op 10" on WO-20260807-006.
PR #227 fixed the DISPLAY half (``frontend/src/utils/operationLabel.ts``); this file
holds down the WRITE half: the stored value is the bare sequence, ``"10"``.

What each class pins, and why guessing differently is plausible:

* **Every mint site agrees.** A routing operation and the work-order operation derived
  from it must end up with the SAME identifier — the WO copy is the routing value when
  there is one, and the bare sequence when there is not. Six mints exist (three in
  ``work_orders.py``, three in ``routing.py``/``routing_import_service.py``); a single
  one left minting ``"Op 10"`` reintroduces the doubled label on whichever screen reads
  that path, so each is asserted separately rather than through one representative.

* **The mint is a FALLBACK, not a rewrite.** A caller that supplies its own
  ``operation_number`` — the office typing ``OP-10A``, an AI-approved routing carrying a
  customer's numbering — is stored verbatim. Only the reorder endpoint re-derives the
  value, because re-deriving it from the new sequence is that endpoint's job.

* **Legacy values are NOT rewritten, on purpose.** This change is forward-only: no
  backfill migration, and the duplicate path keeps copying a pre-existing ``"Op 10"``
  byte-for-byte. Rendering is where the two forms converge, and a duplicate that
  silently "corrected" a legacy identifier would be rewriting a plan nobody edited.

* **``Nest N`` is a different label and stays.** ``laser_nest_service`` mints
  ``f"Nest {n}"`` into the same column for laser nests. That is the correct name for a
  nest — it is not an operation sequence, and the display normalizer leaves it alone —
  so the sweep that removed the ``Op`` prefixes must not have touched it.
"""

from datetime import date, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.bom import BOM, BOMItem, BOMItemType
from app.models.part import Part
from app.models.routing import Routing, RoutingOperation
from app.models.work_center import WorkCenter
from app.models.work_order import (
    OperationStatus,
    WorkOrder,
    WorkOrderOperation,
    WorkOrderStatus,
    WorkOrderType,
)
from app.services.laser_nest_service import create_manual_laser_nest

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def _work_center(db: Session, *, work_center_type: str = "machining") -> WorkCenter:
    wc = WorkCenter(
        code=f"WC-OPNUM-{_next()}",
        name=f"Op Number WC {_next()}",
        work_center_type=work_center_type,
        is_active=True,
        company_id=COMPANY_A,
    )
    db.add(wc)
    db.flush()
    return wc


def _part(db: Session, *, part_type: str = "manufactured") -> Part:
    part = Part(
        part_number=f"OPNUM-{_next()}",
        name=f"Op Number Part {_next()}",
        part_type=part_type,
        unit_of_measure="each",
        is_active=True,
        company_id=COMPANY_A,
    )
    db.add(part)
    db.flush()
    return part


def _routing(db: Session, part: Part, *, status_value: str = "released") -> Routing:
    routing = Routing(
        part_id=part.id,
        revision="A",
        status=status_value,
        is_active=True,
        company_id=COMPANY_A,
    )
    db.add(routing)
    db.flush()
    return routing


def _routing_operation(
    db: Session,
    routing: Routing,
    work_center: WorkCenter,
    *,
    sequence: int,
    operation_number=None,
    name: str = "Machine",
) -> RoutingOperation:
    operation = RoutingOperation(
        routing_id=routing.id,
        sequence=sequence,
        operation_number=operation_number,
        name=name,
        work_center_id=work_center.id,
        setup_hours=0.0,
        run_hours_per_unit=0.1,
        is_active=True,
        company_id=COMPANY_A,
    )
    db.add(operation)
    db.flush()
    return operation


def _wo_operations(db: Session, work_order_id: int) -> list[WorkOrderOperation]:
    return (
        db.query(WorkOrderOperation)
        .filter(
            WorkOrderOperation.work_order_id == work_order_id,
            WorkOrderOperation.company_id == COMPANY_A,
        )
        .order_by(WorkOrderOperation.sequence.asc(), WorkOrderOperation.id.asc())
        .all()
    )


def _routing_operations(db: Session, routing_id: int) -> list[RoutingOperation]:
    return (
        db.query(RoutingOperation)
        .filter(
            RoutingOperation.routing_id == routing_id,
            RoutingOperation.company_id == COMPANY_A,
        )
        .order_by(RoutingOperation.sequence.asc(), RoutingOperation.id.asc())
        .all()
    )


# --------------------------------------------------------------------------- #
# Work-order mint sites
# --------------------------------------------------------------------------- #


class TestWorkOrderOperationsStoreTheBareIdentifier:
    def test_wo_created_from_a_routing_stores_the_bare_sequence(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        """``create_routing_operations_for_work_order`` — the routing has no number of its own."""
        part = _part(db_session)
        wc = _work_center(db_session)
        routing = _routing(db_session, part)
        _routing_operation(db_session, routing, wc, sequence=10, name="Saw")
        _routing_operation(db_session, routing, wc, sequence=20, name="Deburr")
        db_session.commit()

        response = client.post(
            "/api/v1/work-orders/",
            headers=auth_headers,
            json={"part_id": part.id, "quantity_ordered": 3, "priority": 5},
        )

        assert response.status_code == status.HTTP_201_CREATED, response.text
        operations = _wo_operations(db_session, response.json()["id"])
        assert [op.sequence for op in operations] == [10, 20]
        assert [op.operation_number for op in operations] == ["10", "20"]

    def test_wo_created_from_a_routing_copies_an_explicit_number_verbatim(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        """The mint is a fallback: a routing that names its operations wins.

        Including a LEGACY ``"Op 10"`` — the copy must not "fix" a value the office
        already owns, which is the same forward-only rule the duplicate path follows.
        """
        part = _part(db_session)
        wc = _work_center(db_session)
        routing = _routing(db_session, part)
        _routing_operation(db_session, routing, wc, sequence=10, operation_number="OP-10A", name="Saw")
        _routing_operation(db_session, routing, wc, sequence=20, operation_number="Op 20", name="Deburr")
        db_session.commit()

        response = client.post(
            "/api/v1/work-orders/",
            headers=auth_headers,
            json={"part_id": part.id, "quantity_ordered": 3, "priority": 5},
        )

        assert response.status_code == status.HTTP_201_CREATED, response.text
        operations = _wo_operations(db_session, response.json()["id"])
        assert [op.operation_number for op in operations] == ["OP-10A", "Op 20"]

    def test_assembly_component_and_assembly_operations_store_the_bare_sequence(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        """``_create_assembly_routing_operations`` — both of its mints, in one WO.

        The component leg (BOM explosion) and the assembly leg mint independently and
        share a running ``sequence`` counter, so a WO built this way exercises both.
        """
        assembly = _part(db_session, part_type="assembly")
        component = _part(db_session)
        wc = _work_center(db_session)

        component_routing = _routing(db_session, component)
        _routing_operation(db_session, component_routing, wc, sequence=10, name="Cut blank")

        assembly_routing = _routing(db_session, assembly)
        _routing_operation(db_session, assembly_routing, wc, sequence=10, name="Weld")
        _routing_operation(db_session, assembly_routing, wc, sequence=20, name="Final assemble")

        bom = BOM(part_id=assembly.id, revision="A", status="released", is_active=True, company_id=COMPANY_A)
        db_session.add(bom)
        db_session.flush()
        db_session.add(
            BOMItem(
                bom_id=bom.id,
                component_part_id=component.id,
                item_number=10,
                quantity=2,
                item_type=BOMItemType.MAKE,
                company_id=COMPANY_A,
            )
        )
        db_session.commit()

        response = client.post(
            "/api/v1/work-orders/",
            headers=auth_headers,
            json={"part_id": assembly.id, "quantity_ordered": 4, "priority": 5},
        )

        assert response.status_code == status.HTTP_201_CREATED, response.text
        operations = _wo_operations(db_session, response.json()["id"])
        # One component operation then the two assembly operations, resequenced 10/20/30.
        assert [op.sequence for op in operations] == [10, 20, 30]
        assert [op.operation_number for op in operations] == ["10", "20", "30"]
        # The component leg is the one carrying component_part_id — assert it explicitly
        # so a regression in only ONE of the two mints is still named.
        assert operations[0].component_part_id == component.id
        assert operations[0].operation_number == "10"
        assert [op.component_part_id for op in operations[1:]] == [None, None]

    def test_no_work_order_operation_is_born_with_a_doubled_label(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        """The user-visible symptom, asserted end to end.

        The kiosk renders ``formatOperationLabel(operation_number)``, which prefixes
        ``Op ``. Whatever this endpoint stored must not already carry that prefix.
        """
        part = _part(db_session)
        wc = _work_center(db_session)
        routing = _routing(db_session, part)
        _routing_operation(db_session, routing, wc, sequence=10, name="Saw")
        db_session.commit()

        response = client.post(
            "/api/v1/work-orders/",
            headers=auth_headers,
            json={"part_id": part.id, "quantity_ordered": 1, "priority": 5},
        )

        assert response.status_code == status.HTTP_201_CREATED, response.text
        stored = response.json()["operations"][0]["operation_number"]
        assert stored == "10"
        assert f"Op {stored}" == "Op 10", "the display layer's prefix must produce exactly one 'Op'"


# --------------------------------------------------------------------------- #
# Routing mint sites
# --------------------------------------------------------------------------- #


class TestRoutingOperationsStoreTheBareIdentifier:
    def test_create_from_generation_mints_the_bare_sequence(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        """``POST /routing/create-from-generation`` — the AI-approve / bulk create path."""
        part = _part(db_session)
        wc = _work_center(db_session)
        db_session.commit()

        response = client.post(
            "/api/v1/routing/create-from-generation",
            headers=auth_headers,
            json={
                "part_id": part.id,
                "revision": "A",
                "operations": [
                    {"sequence": 10, "name": "Saw", "work_center_id": wc.id},
                    {"sequence": 20, "name": "Deburr", "work_center_id": wc.id},
                ],
            },
        )

        assert response.status_code == status.HTTP_200_OK, response.text
        operations = _routing_operations(db_session, response.json()["id"])
        assert [op.operation_number for op in operations] == ["10", "20"]

    def test_create_from_generation_keeps_a_caller_supplied_number(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        part = _part(db_session)
        wc = _work_center(db_session)
        db_session.commit()

        response = client.post(
            "/api/v1/routing/create-from-generation",
            headers=auth_headers,
            json={
                "part_id": part.id,
                "revision": "A",
                "operations": [
                    {"sequence": 10, "name": "Saw", "operation_number": "OP-10A", "work_center_id": wc.id},
                ],
            },
        )

        assert response.status_code == status.HTTP_200_OK, response.text
        operations = _routing_operations(db_session, response.json()["id"])
        assert [op.operation_number for op in operations] == ["OP-10A"]

    def test_add_operation_mints_the_bare_sequence(self, client: TestClient, auth_headers: dict, db_session: Session):
        """``POST /routing/{id}/operations`` — the single-operation create."""
        part = _part(db_session)
        wc = _work_center(db_session)
        routing = _routing(db_session, part, status_value="draft")
        db_session.commit()

        response = client.post(
            f"/api/v1/routing/{routing.id}/operations",
            headers=auth_headers,
            json={"sequence": 30, "name": "Grind", "work_center_id": wc.id},
        )

        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.json()["operation_number"] == "30"

    def test_add_operation_keeps_a_caller_supplied_number(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        part = _part(db_session)
        wc = _work_center(db_session)
        routing = _routing(db_session, part, status_value="draft")
        db_session.commit()

        response = client.post(
            f"/api/v1/routing/{routing.id}/operations",
            headers=auth_headers,
            json={"sequence": 30, "name": "Grind", "operation_number": "OP-30B", "work_center_id": wc.id},
        )

        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.json()["operation_number"] == "OP-30B"

    def test_reorder_re_derives_the_bare_sequence(self, client: TestClient, auth_headers: dict, db_session: Session):
        """``POST /routing/{id}/operations/reorder`` re-derives the number from the NEW sequence.

        That overwrite is this endpoint's documented job (pre-existing behavior); what
        changed is that the value it writes is the bare identifier.
        """
        part = _part(db_session)
        wc = _work_center(db_session)
        routing = _routing(db_session, part, status_value="draft")
        first = _routing_operation(db_session, routing, wc, sequence=10, operation_number="10", name="Saw")
        second = _routing_operation(db_session, routing, wc, sequence=20, operation_number="20", name="Deburr")
        db_session.commit()
        first_id, second_id = first.id, second.id

        response = client.post(
            f"/api/v1/routing/{routing.id}/operations/reorder",
            headers=auth_headers,
            json=[{"id": first_id, "sequence": 20}, {"id": second_id, "sequence": 10}],
        )

        assert response.status_code == status.HTTP_200_OK, response.text
        db_session.expire_all()
        by_id = {op.id: op for op in _routing_operations(db_session, routing.id)}
        assert by_id[first_id].operation_number == "20"
        assert by_id[second_id].operation_number == "10"

    def test_csv_importer_mints_the_bare_sequence(self, client: TestClient, auth_headers: dict, db_session: Session):
        """``routing_import_service`` — the A0.2 routing CSV/XLSX commit."""
        from io import BytesIO

        part = _part(db_session)
        wc = _work_center(db_session)
        db_session.commit()

        csv_text = (
            "part_number,routing_revision,routing_description,sequence,operation_name,"
            "work_center_code,setup_hours,run_hours_per_unit,description,"
            "is_inspection_point,is_outside_operation\n"
            f"{part.part_number},A,Imported,10,Saw,{wc.code},0.5,0.1,,false,false\n"
            f"{part.part_number},A,Imported,20,Deburr,{wc.code},0.25,0.05,,false,false\n"
        )
        response = client.post(
            "/api/v1/routing/import/commit",
            headers=auth_headers,
            files={"file": ("routings.csv", BytesIO(csv_text.encode("utf-8")), "text/csv")},
        )

        assert response.status_code == status.HTTP_200_OK, response.text
        routing_id = response.json()["results"][0]["routing_id"]
        assert routing_id is not None
        operations = _routing_operations(db_session, routing_id)
        assert [op.operation_number for op in operations] == ["10", "20"]


# --------------------------------------------------------------------------- #
# What the change deliberately does NOT touch
# --------------------------------------------------------------------------- #


class TestLegacyAndNonOperationLabelsAreLeftAlone:
    def test_duplicate_copies_a_legacy_op_prefixed_value_unchanged(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        """Forward-only: the duplicate path copies the source verbatim, legacy value included.

        There is no backfill migration and there must not be one — a duplicate that
        rewrote ``"Op 10"`` to ``"10"`` would be exactly that backfill, applied one job at
        a time and attributed to whoever pressed Duplicate.
        """
        part = _part(db_session)
        wc = _work_center(db_session)
        source = WorkOrder(
            work_order_number=f"WO-OPNUM-{_next()}",
            part_id=part.id,
            quantity_ordered=5,
            status=WorkOrderStatus.DRAFT,
            priority=3,
            due_date=date.today() + timedelta(days=14),
            company_id=COMPANY_A,
        )
        db_session.add(source)
        db_session.flush()
        db_session.add(
            WorkOrderOperation(
                work_order_id=source.id,
                work_center_id=wc.id,
                sequence=10,
                operation_number="Op 10",
                name="Saw",
                status=OperationStatus.PENDING,
                company_id=COMPANY_A,
            )
        )
        db_session.commit()

        response = client.post(
            f"/api/v1/work-orders/{source.id}/duplicate",
            headers=auth_headers,
            json={"quantity_ordered": 5},
        )

        assert response.status_code == status.HTTP_201_CREATED, response.text
        copied = _wo_operations(db_session, response.json()["work_order"]["id"])
        assert [op.operation_number for op in copied] == ["Op 10"]

    def test_manual_laser_nest_still_labels_operations_nest_n(self, db_session: Session):
        """``Nest 1`` is the right name for a nest and is NOT an operation sequence.

        The sweep that removed the ``Op`` prefixes must not have collapsed this one:
        laser nest operations carry no routing sequence a reader could recognize, and
        the display normalizer passes ``Nest 1`` through untouched.
        """
        laser_wc = _work_center(db_session, work_center_type="laser")
        child = WorkOrder(
            work_order_number=f"WO-NEST-{_next()}",
            part_id=None,
            # A part-less standalone nest WO. quantity_ordered is DERIVED from the
            # nests' planned_runs, but chk_work_orders_quantity_ordered_positive
            # refuses a 0 here, so seed 1 and let the service re-derive it.
            quantity_ordered=1,
            status=WorkOrderStatus.DRAFT,
            priority=3,
            work_order_type=WorkOrderType.LASER_CUTTING.value,
            company_id=COMPANY_A,
        )
        db_session.add(child)
        db_session.flush()

        for expected in ("Nest 1", "Nest 2"):
            nest = create_manual_laser_nest(
                db_session,
                parent_work_order=None,
                child_work_order=child,
                laser_work_center=laser_wc,
                data={
                    "cnc_number": f"CNC-{_next()}",
                    "nest_name": f"NEST-{_next()}",
                    "planned_runs": 2,
                    "material": "304 SS",
                    "thickness": "0.125",
                    "sheet_size": "48 x 96",
                },
                company_id=COMPANY_A,
                user_id=None,
            )
            db_session.flush()
            operation = db_session.query(WorkOrderOperation).filter_by(id=nest.work_order_operation_id).one()
            assert operation.operation_number == expected
