"""``POST /work-orders/{id}/duplicate`` — the plan carries, the production record does not.

The feature copies a job's PLAN (header, operations + instructions, laser nests, open
material ties) onto a fresh DRAFT work order so a 40-nest laser package can be re-run
without re-uploading it. What this file exists to hold down is the OTHER half of that
sentence: the production record must NOT come along.

That asymmetry is the whole compliance argument. A duplicate that carried
``quantity_complete``, ``actual_hours``, ``released_by`` or a ``lot_number`` would be a
work order asserting — on an AS9100D-auditable record — that work happened which never
happened. So :class:`TestTheProductionRecordIsLeftBehind` asserts every one of those
fields individually rather than spot-checking a few: a regression that re-adds one line
to ``_copy_header`` fabricates history silently, and only a per-field assertion names
which line.

The other things pinned here, each because guessing differently is plausible:

* **Process-sheet step snapshots are RE-SNAPSHOTTED, and the gate they feed still
  bites.** This is the highest-consequence item in the file.
  ``process_sheet_service.missing_required_steps`` returns ``[]`` — complete freely —
  for an operation with zero snapshot steps, so a duplicate carrying none silently
  disarms the operation-completion gate on a job whose entire premise is "same plan as
  last time": no measurement, no SPC point, no gauge attribution, no OOT->NCR path,
  nothing for the AS9102 FAI, and — unlike force-complete, which stamps what it
  bypasses onto its audit row — no record that it happened.
  ``TestProcessSheetStepsAreReSnapshotted`` therefore ends by completing the copy and
  the source through the same endpoint and asserting they refuse identically.

* **Quantity-derived plan numbers are SCALED.** ``run_time_hours`` is stored
  pre-multiplied by the ordered quantity, so an unscaled copy of a 100-piece job run at
  10 still claims the 100-piece hours — to scheduling, to the dispatch board, and to
  completion costing, which reads that column first.

* **Skips are visible, refusals are total.** A retired produced part or a sheet family
  with no released revision fails the whole call with 409 and writes nothing. Anything
  else that cannot be copied faithfully is skipped, recorded on the chain AND returned
  in the response envelope — a skip only the audit chain knows about is a job the
  planner releases believing it carries material demand it does not have.

* **A nest-bearing work order's quantity is DERIVED, not chosen.**
  ``laser_nest_service._recompute_child_quantity_ordered`` defines a laser WO's
  ``quantity_ordered`` as the sum of its non-deleted ``planned_runs``, and every nest
  path re-asserts it. So the duplicate must end at that sum whatever the request asked
  for — otherwise the duplicate is the one laser WO in the system where the definition
  is false, until the next nest edit "corrects" it out from under the planner. The
  overruled request is recorded on the chain as ``requested_quantity``. A WO with NO
  nests has no such definition and must honour the request exactly.

* **``qty_planned`` on a copied nest tie is RECOMPUTED, and must agree with the path
  that creates nest ties in the first place.** The test asserts that equivalence against
  a tie built by ``laser_nest_service.create_nest_material_allocation`` itself rather
  than re-stating ``qty_per_run × planned_runs`` — restating the formula would let both
  sides drift together and still pass.

* **``unit_of_measure`` is RE-SNAPSHOTTED from the part, not carried.** The column is
  documented as a snapshot at tie time, and this tie's time is now. A part restocked in
  a different unit since the source job would otherwise hand the new tie a label its own
  quantity is no longer expressed in.

* **Atomicity.** A header without its nests is a plan nobody approved. The whole copy is
  one unit of work, and a constraint fault must surface as 409 with nothing persisted —
  not a 500 off a poisoned session and not a half-built job.

Tenancy, audit, soft-delete and the role gate get their own classes (invariants 1, 2, 3).
"""

from datetime import date, datetime

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.document import Document, DocumentType
from app.models.inventory import InventoryItem
from app.models.laser_nest import LaserNest, LaserNestPackage
from app.models.part import Part
from app.models.process_sheet import ProcessSheet, ProcessSheetStep, WOOperationStep
from app.models.scrap_reason import ScrapReasonCode
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_material import AllocationSource, AllocationStatus, WorkOrderMaterialAllocation
from app.schemas.work_order import WorkOrderDuplicateSkippedAllocation, WorkOrderDuplicateSkippedOperation
from app.services.audit_service import AuditService
from app.services.laser_nest_service import create_nest_material_allocation
from app.services.work_order_duplicate_service import (
    _copy_laser_nests,
    _recomputed_qty_planned,
    _scale_quantity_derived_plan,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def _ensure_company(db: Session, company_id: int) -> None:
    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True))
        db.commit()


def make_user(db: Session, *, role: UserRole = UserRole.ADMIN, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"dup-{n}@co{company_id}.test",
        employee_id=f"DUP-{n:05d}",
        first_name="Dup",
        last_name=f"User{n}",
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=True,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def make_work_center(db: Session, *, company_id: int = COMPANY_A, wc_type: str = "laser") -> WorkCenter:
    _ensure_company(db, company_id)
    n = _next()
    wc = WorkCenter(
        name=f"WC {n}",
        code=f"DUP-WC-{n}",
        work_center_type=wc_type,
        hourly_rate=110.0,
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def make_part(
    db: Session,
    *,
    company_id: int = COMPANY_A,
    part_type: str = "manufactured",
    uom: str = "each",
    is_deleted: bool = False,
) -> Part:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"DUP-P-{n:05d}",
        name=f"Part {n}",
        part_type=part_type,
        unit_of_measure=uom,
        is_active=True,
        is_deleted=is_deleted,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_work_order(
    db: Session,
    *,
    company_id: int = COMPANY_A,
    part: Part = None,
    quantity_ordered: float = 10.0,
    status_value: WorkOrderStatus = WorkOrderStatus.COMPLETE,
    work_order_type: str = "production",
    **overrides,
) -> WorkOrder:
    _ensure_company(db, company_id)
    n = _next()
    fields = {"priority": 3}
    fields.update(overrides)
    wo = WorkOrder(
        work_order_number=f"DUP-WO-{n:05d}",
        part_id=part.id if part is not None else None,
        work_order_type=work_order_type,
        quantity_ordered=quantity_ordered,
        status=status_value,
        company_id=company_id,
        **fields,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def add_operation(
    db: Session,
    wo: WorkOrder,
    work_center: WorkCenter,
    *,
    company_id: int = COMPANY_A,
    sequence: int = 10,
    **overrides,
) -> WorkOrderOperation:
    n = _next()
    fields = {"operation_number": f"OP{sequence}", "name": f"Operation {n}"}
    fields.update(overrides)
    operation = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=work_center.id,
        company_id=company_id,
        sequence=sequence,
        **fields,
    )
    db.add(operation)
    db.commit()
    db.refresh(operation)
    return operation


def make_package(db: Session, wo: WorkOrder, *, company_id: int = COMPANY_A, name: str = None) -> LaserNestPackage:
    n = _next()
    package = LaserNestPackage(
        company_id=company_id,
        parent_work_order_id=None,
        child_work_order_id=wo.id,
        package_name=name or f"Package {n}",
        source_path=f"/tmp/laser/{n}",
        import_status="imported",
    )
    db.add(package)
    db.commit()
    db.refresh(package)
    return package


def make_document(db: Session, *, company_id: int = COMPANY_A, work_order_id: int = None) -> Document:
    n = _next()
    document = Document(
        document_number=f"DUP-DOC-{n:05d}",
        revision="A",
        title=f"Nest drawing {n}",
        document_type=DocumentType.DRAWING,
        work_order_id=work_order_id,
        file_name=f"nest-{n}.pdf",
        file_path=f"local://nest-{n}.pdf",
        status="released",
        company_id=company_id,
    )
    db.add(document)
    db.commit()
    db.refresh(document)
    return document


def attach_nest(
    db: Session,
    package: LaserNestPackage,
    operation: WorkOrderOperation,
    *,
    company_id: int = COMPANY_A,
    planned_runs: int = 3,
    completed_runs: float = 0.0,
    is_deleted: bool = False,
    document_id: int = None,
    **overrides,
) -> LaserNest:
    n = _next()
    fields = {
        "nest_name": f"NEST-{n}",
        "cnc_file_name": f"nest-{n}.pdf",
        "cnc_file_path": f"/tmp/laser/{n}/nest-{n}.pdf",
        "cnc_number": f"0{n:04d}",
        "material": "A36",
        "thickness": "0.250",
        "sheet_size": "60x120",
    }
    fields.update(overrides)
    nest = LaserNest(
        company_id=company_id,
        package_id=package.id,
        work_order_operation_id=operation.id,
        planned_runs=planned_runs,
        completed_runs=completed_runs,
        is_deleted=is_deleted,
        document_id=document_id,
        **fields,
    )
    db.add(nest)
    db.commit()
    db.refresh(nest)
    return nest


def make_tie(
    db: Session,
    wo: WorkOrder,
    part: Part,
    *,
    company_id: int = COMPANY_A,
    operation: WorkOrderOperation = None,
    operation_id: int = None,
    qty_per_run: float = None,
    qty_planned: float = 6.0,
    unit_of_measure: str = "each",
    tie_status: AllocationStatus = AllocationStatus.OPEN,
    source: AllocationSource = AllocationSource.NEST,
    **overrides,
) -> WorkOrderMaterialAllocation:
    allocation = WorkOrderMaterialAllocation(
        company_id=company_id,
        work_order_id=wo.id,
        work_order_operation_id=operation.id if operation is not None else operation_id,
        part_id=part.id,
        source=source,
        status=tie_status,
        qty_per_run=qty_per_run,
        qty_planned=qty_planned,
        unit_of_measure=unit_of_measure,
        **overrides,
    )
    db.add(allocation)
    db.commit()
    db.refresh(allocation)
    return allocation


MEASUREMENT_CONFIG = {"lsl": 0.98, "nominal": 1.0, "usl": 1.02, "unit": "in", "decimals": 3}


def make_process_sheet(
    db: Session,
    *,
    company_id: int = COMPANY_A,
    sheet_number: str = None,
    revision: str = "A",
    sheet_status: str = "released",
    steps: list = None,
) -> ProcessSheet:
    """A process sheet REVISION (one row) plus its step definitions.

    The family is ``sheet_number``; revisions are separate rows sharing it, and
    ``resolve_released_revision`` picks the one whose ``status == 'released'``.
    """
    _ensure_company(db, company_id)
    n = _next()
    sheet = ProcessSheet(
        sheet_number=sheet_number or f"DUP-PS-{n:06d}",
        title=f"Traveler {n}",
        revision=revision,
        status=sheet_status,
        is_active=sheet_status != "obsolete",
        company_id=company_id,
    )
    db.add(sheet)
    db.flush()
    for index, step in enumerate(steps or []):
        db.add(
            ProcessSheetStep(
                process_sheet_id=sheet.id,
                company_id=company_id,
                sequence=step.get("sequence", (index + 1) * 10),
                label=step.get("label", f"Step {(index + 1) * 10}"),
                instruction_text=step.get("instruction_text"),
                step_type=step.get("step_type", "checkbox"),
                is_required=step.get("is_required", True),
                config=step.get("config"),
                requires_gauge=step.get("requires_gauge", False),
            )
        )
    db.commit()
    db.refresh(sheet)
    return sheet


def snapshot_onto(
    db: Session,
    operation: WorkOrderOperation,
    sheet: ProcessSheet,
    *,
    company_id: int = COMPANY_A,
) -> list:
    """Mirror ``sheet``'s step definitions onto ``operation`` as snapshot rows.

    This is what ``create_work_order`` leaves behind on the SOURCE, and it is the only
    thing that tells the duplicate which sheet FAMILY the operation belongs to —
    ``WorkOrderOperation`` carries no ``process_sheet_id``.
    """
    definitions = (
        db.query(ProcessSheetStep)
        .filter(ProcessSheetStep.process_sheet_id == sheet.id)
        .order_by(ProcessSheetStep.sequence)
        .all()
    )
    rows = []
    for definition in definitions:
        row = WOOperationStep(
            company_id=company_id,
            work_order_operation_id=operation.id,
            source_sheet_id=sheet.id,
            source_sheet_revision=sheet.revision,
            sequence=definition.sequence,
            label=definition.label,
            instruction_text=definition.instruction_text,
            step_type=definition.step_type,
            is_required=definition.is_required,
            config=definition.config,
            requires_gauge=definition.requires_gauge,
        )
        db.add(row)
        rows.append(row)
    db.commit()
    return rows


def steps_of(db: Session, operation: WorkOrderOperation) -> list:
    return (
        db.query(WOOperationStep)
        .filter(WOOperationStep.work_order_operation_id == operation.id)
        .order_by(WOOperationStep.sequence, WOOperationStep.id)
        .all()
    )


# --------------------------------------------------------------------------- #
# Request + read helpers
# --------------------------------------------------------------------------- #


def duplicate(client: TestClient, headers: dict, wo_id: int, *, quantity: float = 5, due_date=None):
    body = {"quantity_ordered": quantity, "due_date": due_date}
    return client.post(f"/api/v1/work-orders/{wo_id}/duplicate", headers=headers, json=body)


def created_work_order(db: Session, response) -> WorkOrder:
    """The new WO row behind a 201. The body is an ENVELOPE, not a bare work order."""
    db.expire_all()
    return db.query(WorkOrder).filter(WorkOrder.id == response.json()["work_order"]["id"]).one()


def operations_of(db: Session, wo: WorkOrder) -> list:
    return (
        db.query(WorkOrderOperation)
        .filter(WorkOrderOperation.work_order_id == wo.id)
        .order_by(WorkOrderOperation.sequence, WorkOrderOperation.id)
        .all()
    )


def nests_of(db: Session, wo: WorkOrder) -> list:
    return (
        db.query(LaserNest)
        .join(WorkOrderOperation, LaserNest.work_order_operation_id == WorkOrderOperation.id)
        .filter(WorkOrderOperation.work_order_id == wo.id)
        .order_by(LaserNest.id)
        .all()
    )


def ties_of(db: Session, wo: WorkOrder) -> list:
    return (
        db.query(WorkOrderMaterialAllocation)
        .filter(WorkOrderMaterialAllocation.work_order_id == wo.id)
        .order_by(WorkOrderMaterialAllocation.id)
        .all()
    )


def audit_rows(db: Session, resource_type: str, resource_id: int = None) -> list:
    query = db.query(AuditLog).filter(AuditLog.resource_type == resource_type)
    if resource_id is not None:
        query = query.filter(AuditLog.resource_id == resource_id)
    return query.order_by(AuditLog.id).all()


def build_laser_source(
    db: Session,
    *,
    company_id: int = COMPANY_A,
    runs: tuple = (3, 5),
    with_document: bool = False,
) -> tuple:
    """A laser work order whose quantity is (correctly) the sum of its nest runs."""
    wc = make_work_center(db, company_id=company_id, wc_type="laser")
    part = make_part(db, company_id=company_id)
    wo = make_work_order(db, company_id=company_id, part=part, quantity_ordered=float(sum(runs)))
    package = make_package(db, wo, company_id=company_id)
    document = make_document(db, company_id=company_id, work_order_id=wo.id) if with_document else None

    operations, nests = [], []
    for index, planned in enumerate(runs):
        operation = add_operation(db, wo, wc, company_id=company_id, sequence=(index + 1) * 10)
        nest = attach_nest(
            db,
            package,
            operation,
            company_id=company_id,
            planned_runs=planned,
            document_id=document.id if document is not None else None,
        )
        operations.append(operation)
        nests.append(nest)
    return wo, wc, operations, nests, package


# --------------------------------------------------------------------------- #
# What the duplicate carries
# --------------------------------------------------------------------------- #
class TestThePlanCarries:
    def test_header_plan_fields_carry_onto_a_new_draft(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        part = make_part(db_session)
        source = make_work_order(
            db_session,
            part=part,
            quantity_ordered=40.0,
            customer_name="Acme Aero",
            customer_po="PO-4471",
            po_line_item="3",
            po_date=date(2026, 5, 1),
            sales_order_id=88,
            notes="Deburr all edges",
            special_instructions="Blue tape the finished face",
            priority=2,
            estimated_hours=9.5,
            estimated_cost=1250.0,
        )

        # Same quantity as the source, so the quantity-derived plan numbers scale by
        # exactly 1.0 and this test can assert the carried values verbatim. Scaling
        # has its own class below.
        resp = duplicate(client, headers_for(admin), source.id, quantity=40, due_date="2026-09-30")
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        assert new_wo.id != source.id
        assert new_wo.work_order_number != source.work_order_number
        assert new_wo.company_id == COMPANY_A
        # The plan.
        assert new_wo.part_id == part.id
        assert new_wo.work_order_type == source.work_order_type
        assert new_wo.priority == 2
        assert new_wo.customer_name == "Acme Aero"
        assert new_wo.customer_po == "PO-4471"
        assert new_wo.po_line_item == "3"
        assert new_wo.po_date == date(2026, 5, 1)
        assert new_wo.sales_order_id == 88
        assert new_wo.notes == "Deburr all edges"
        assert new_wo.special_instructions == "Blue tape the finished face"
        # Estimates describe what the job is EXPECTED to take, so they are plan, not
        # record — carried verbatim here because the quantity did not change.
        assert new_wo.estimated_hours == 9.5
        assert new_wo.estimated_cost == 1250.0
        # The two things the caller decides.
        assert new_wo.quantity_ordered == 40.0
        assert new_wo.due_date == date(2026, 9, 30)
        # Born DRAFT so a planner reviews before release, and attributed to the caller.
        assert new_wo.status == WorkOrderStatus.DRAFT
        assert new_wo.created_by == admin.id

    def test_operations_carry_their_instructions_times_and_inspection_flags(
        self, client: TestClient, db_session: Session
    ):
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="machining")
        part = make_part(db_session)
        component = make_part(db_session, part_type="purchased")
        source = make_work_order(db_session, part=part, quantity_ordered=10.0)
        add_operation(
            db_session,
            source,
            wc,
            sequence=20,
            operation_number="OP20",
            name="Deburr",
            description="Break all edges .010",
            operation_group="BENCH",
            setup_instructions="Fixture B, soft jaws",
            run_instructions="Two passes, check with pin gauge",
            setup_time_hours=1.25,
            run_time_hours=4.5,
            run_time_per_piece=0.075,
            component_part_id=component.id,
            component_quantity=2.0,
            requires_inspection=True,
            inspection_type="first_article",
            run_order=7,
        )
        add_operation(db_session, source, wc, sequence=10, name="Saw")

        # Same quantity => ratio 1.0 => run_time_hours carries verbatim (see the
        # scaling class below for what happens when it does not).
        resp = duplicate(client, headers_for(admin), source.id, quantity=10)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        new_operations = operations_of(db_session, new_wo)
        assert [op.sequence for op in new_operations] == [10, 20], "sequence order is part of the routing"

        copied = new_operations[1]
        assert copied.id not in {op.id for op in operations_of(db_session, source)}
        assert copied.work_order_id == new_wo.id
        assert copied.company_id == COMPANY_A
        assert copied.work_center_id == wc.id
        assert copied.operation_number == "OP20"
        assert copied.name == "Deburr"
        assert copied.description == "Break all edges .010"
        assert copied.operation_group == "BENCH"
        assert copied.setup_instructions == "Fixture B, soft jaws"
        assert copied.run_instructions == "Two passes, check with pin gauge"
        assert copied.setup_time_hours == 1.25
        assert copied.run_time_hours == 4.5
        assert copied.run_time_per_piece == 0.075
        assert copied.component_part_id == component.id
        assert copied.component_quantity == 2.0
        assert copied.requires_inspection is True
        assert copied.inspection_type == "first_article"

    def test_laser_nests_carry_every_plan_field_and_share_the_source_drawing(
        self, client: TestClient, db_session: Session
    ):
        """``document_id`` is carried by REFERENCE — no second Document row, no second blob.

        The duplicate points at the SOURCE work order's drawing. The nest-document
        endpoint resolves the PDF by ``nest.document_id`` filtered on ``company_id``
        only (never by work order), so the operator preview works on the copy exactly
        as on the original. Minting a second Document would double storage for a
        byte-identical PDF and give one drawing two document numbers.
        """
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="laser")
        part = make_part(db_session)
        source = make_work_order(db_session, part=part, quantity_ordered=6.0)
        package = make_package(db_session, source, name="ERMAKSAN-2026-05")
        document = make_document(db_session, work_order_id=source.id)
        operation = add_operation(db_session, source, wc, sequence=10)
        source_nest = attach_nest(
            db_session,
            package,
            operation,
            planned_runs=6,
            completed_runs=6.0,
            document_id=document.id,
            nest_name="NEST-07",
            cnc_file_name="05749.pdf",
            cnc_file_path="/tmp/laser/pkg/05749.pdf",
            cnc_number="05749",
            material="304 SS",
            thickness="0.125",
            sheet_size="48x96",
        )
        document_count_before = db_session.query(Document).count()

        resp = duplicate(client, headers_for(admin), source.id, quantity=6)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        [copied] = nests_of(db_session, new_wo)
        assert copied.id != source_nest.id
        assert copied.company_id == COMPANY_A
        assert copied.nest_name == "NEST-07"
        assert copied.cnc_file_name == "05749.pdf"
        assert copied.cnc_file_path == "/tmp/laser/pkg/05749.pdf"
        assert copied.cnc_number == "05749"
        assert copied.planned_runs == 6
        assert copied.material == "304 SS"
        assert copied.thickness == "0.125"
        # THE ONE FIELD GROUP THAT IS NOT COPIED BYTE-FOR-BYTE. The sheet
        # descriptors are canonicalized on the way across (`48x96` -> `48 x 96`,
        # via `services/laser_nest_text`), deliberately breaking this test's
        # otherwise-verbatim contract: `material`/`thickness`/`sheet_size` have
        # no `Part` FK, so the STRING is the only grouping key anything has, and
        # a faithful copy of a pre-normalization job would re-inject a legacy
        # spelling into new data and re-fragment that key. The transform is
        # meaning-preserving (case / whitespace / separator only), so the copied
        # plan still describes the same sheet. Every other field above stays
        # verbatim.
        assert copied.sheet_size == "48 x 96"
        # THE reference, not the blob.
        assert copied.document_id == document.id
        assert (
            db_session.query(Document).count() == document_count_before
        ), "the duplicate must SHARE the source's drawing, not re-upload it"

        # One new package for the copy, hanging off the new work order only.
        new_package = db_session.query(LaserNestPackage).filter(LaserNestPackage.id == copied.package_id).one()
        assert new_package.id != package.id
        assert new_package.child_work_order_id == new_wo.id
        assert new_package.parent_work_order_id is None
        assert new_package.package_name == "ERMAKSAN-2026-05"
        # Not imported from a package directory — claiming the source's extract path
        # would be false provenance, and NULL is what lets a later manual nest reuse it.
        assert new_package.source_path is None

    def test_all_source_nests_land_on_one_package_on_the_duplicate(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        source, _wc, _ops, _nests, _package = build_laser_source(db_session, runs=(2, 3, 4))

        resp = duplicate(client, headers_for(admin), source.id, quantity=9)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        copied = nests_of(db_session, new_wo)
        assert len(copied) == 3
        assert len({nest.package_id for nest in copied}) == 1

    def test_open_material_ties_carry_and_cancelled_ones_do_not(self, client: TestClient, db_session: Session):
        """A CANCELLED tie is a tombstone — a tie the planner untied is not part of the plan."""
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="laser")
        part = make_part(db_session)
        sheet = make_part(db_session, part_type="raw_material", uom="sheets")
        dropped = make_part(db_session, part_type="raw_material", uom="sheets")
        source = make_work_order(db_session, part=part, quantity_ordered=4.0)
        package = make_package(db_session, source)
        operation = add_operation(db_session, source, wc, sequence=10)
        attach_nest(db_session, package, operation, planned_runs=4)
        make_tie(
            db_session,
            source,
            sheet,
            operation=operation,
            qty_per_run=2.0,
            qty_planned=8.0,
            notes="Pull from the north rack",
        )
        make_tie(db_session, source, dropped, operation=operation, tie_status=AllocationStatus.CANCELLED)

        resp = duplicate(client, headers_for(admin), source.id, quantity=4)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        copied_ties = ties_of(db_session, new_wo)
        assert [tie.part_id for tie in copied_ties] == [sheet.id]
        [tie] = copied_ties
        assert tie.company_id == COMPANY_A
        assert tie.status == AllocationStatus.OPEN
        assert tie.source == AllocationSource.NEST
        assert tie.qty_per_run == 2.0
        assert tie.notes == "Pull from the north rack"
        assert tie.created_by == admin.id
        # Scope follows the source: an operation-scoped tie lands on the CORRESPONDING
        # new operation, never silently re-scoped to the work order.
        [new_operation] = operations_of(db_session, new_wo)
        assert tie.work_order_operation_id == new_operation.id

    def test_a_work_order_scoped_tie_stays_work_order_scoped(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="machining")
        part = make_part(db_session)
        bar = make_part(db_session, part_type="raw_material", uom="feet")
        source = make_work_order(db_session, part=part, quantity_ordered=10.0)
        add_operation(db_session, source, wc, sequence=10)
        make_tie(db_session, source, bar, qty_planned=25.0, unit_of_measure="feet", source=AllocationSource.MANUAL)

        resp = duplicate(client, headers_for(admin), source.id, quantity=10)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        [tie] = ties_of(db_session, new_wo)
        assert tie.work_order_operation_id is None
        # Same quantity => ratio 1.0 => the source value reproduced bit-for-bit.
        assert tie.qty_planned == 25.0

    def test_the_response_envelopes_the_new_work_order_and_what_the_copy_lost(
        self, client: TestClient, db_session: Session
    ):
        """The client navigates straight to the copy, so the envelope must carry the NEW WO.

        Both skip lists are empty on a clean duplicate — that emptiness IS the "clean
        copy" signal the client keys off, so it is asserted rather than assumed.
        """
        admin = make_user(db_session)
        source, _wc, _ops, _nests, _package = build_laser_source(db_session, runs=(3, 5))

        resp = duplicate(client, headers_for(admin), source.id, quantity=8)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        body = resp.json()
        assert set(body) == {"work_order", "skipped_operations", "skipped_material_allocations"}
        assert body["skipped_operations"] == []
        assert body["skipped_material_allocations"] == []

        payload = body["work_order"]
        assert payload["id"] != source.id
        assert payload["work_order_number"] != source.work_order_number
        assert payload["status"] == "draft"
        assert len(payload["operations"]) == 2
        assert all(operation["status"] == "pending" for operation in payload["operations"])
        assert {operation["laser_nest"]["planned_runs"] for operation in payload["operations"]} == {3, 5}


# --------------------------------------------------------------------------- #
# What the duplicate must NOT carry
# --------------------------------------------------------------------------- #
class TestTheProductionRecordIsLeftBehind:
    """Every field here fabricates history if it regresses, so each is asserted by name."""

    @pytest.fixture
    def ran_and_finished(self, db_session: Session):
        """A source work order carrying a COMPLETE production record on every column."""
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="machining")
        part = make_part(db_session)
        parent = make_work_order(db_session, part=make_part(db_session, part_type="assembly"))
        code = ScrapReasonCode(code=f"SC-{_next()}", name="Burn through", category="material", company_id=COMPANY_A)
        db_session.add(code)
        db_session.commit()
        db_session.refresh(code)

        source = make_work_order(
            db_session,
            part=part,
            quantity_ordered=40.0,
            parent_work_order_id=parent.id,
            quantity_complete=37.0,
            quantity_scrapped=3.0,
            scrap_reason="burn through on the flange",
            scrap_reason_code_id=code.id,
            actual_start=datetime(2026, 5, 4, 13, 0),
            actual_end=datetime(2026, 5, 9, 21, 30),
            scheduled_start=datetime(2026, 5, 4, 6, 0),
            scheduled_end=datetime(2026, 5, 10, 6, 0),
            due_date=date(2026, 5, 12),
            must_ship_by=date(2026, 5, 11),
            actual_hours=61.25,
            actual_cost=7420.0,
            lot_number="LOT-2026-0042",
            serial_numbers='["SN-001", "SN-002"]',
            released_by=admin.id,
            released_at=datetime(2026, 5, 3, 8, 0),
        )
        operation = add_operation(
            db_session,
            source,
            wc,
            sequence=10,
            name="Mill",
            setup_time_hours=2.0,
            run_time_hours=30.0,
            requires_inspection=True,
            inspection_type="final",
            status=OperationStatus.COMPLETE,
            quantity_complete=37.0,
            quantity_scrapped=3.0,
            quantity_reworked=1.0,
            actual_setup_hours=2.75,
            actual_run_hours=44.0,
            actual_start=datetime(2026, 5, 4, 13, 0),
            actual_end=datetime(2026, 5, 9, 21, 30),
            scheduled_start=datetime(2026, 5, 4, 6, 0),
            scheduled_end=datetime(2026, 5, 10, 6, 0),
            started_by=admin.id,
            completed_by=admin.id,
            last_reported_at=datetime(2026, 5, 9, 21, 15),
            last_reported_good=12.0,
            last_reported_scrapped=1.0,
            inspection_complete=True,
            scrap_reason="burn through on the flange",
            scrap_reason_code_id=code.id,
        )
        source.current_operation_id = operation.id
        db_session.add(
            TimeEntry(
                company_id=COMPANY_A,
                user_id=admin.id,
                work_order_id=source.id,
                operation_id=operation.id,
                work_center_id=wc.id,
                entry_type=TimeEntryType.RUN,
                clock_in=datetime(2026, 5, 4, 13, 0),
                clock_out=datetime(2026, 5, 4, 21, 0),
                duration_hours=8.0,
                quantity_produced=12.0,
            )
        )
        db_session.commit()
        return admin, source, operation

    def test_the_header_starts_with_no_production_record(
        self, client: TestClient, db_session: Session, ran_and_finished
    ):
        admin, source, _operation = ran_and_finished

        resp = duplicate(client, headers_for(admin), source.id, quantity=40, due_date="2026-11-02")
        assert resp.status_code == status.HTTP_201_CREATED, resp.text
        new_wo = created_work_order(db_session, resp)

        assert new_wo.status == WorkOrderStatus.DRAFT
        assert (new_wo.quantity_complete or 0) == 0
        assert (new_wo.quantity_scrapped or 0) == 0
        assert new_wo.scrap_reason is None
        assert new_wo.scrap_reason_code_id is None
        assert new_wo.actual_start is None
        assert new_wo.actual_end is None
        assert (new_wo.actual_hours or 0) == 0
        assert (new_wo.actual_cost or 0) == 0
        assert new_wo.lot_number is None
        assert new_wo.serial_numbers is None
        assert new_wo.released_by is None
        assert new_wo.released_at is None
        assert new_wo.current_operation_id is None
        # SchedulingService OUTPUT for the SOURCE's dates — release re-runs scheduling.
        assert new_wo.scheduled_start is None
        assert new_wo.scheduled_end is None
        # The ORIGINAL order's promise date, which outranks due_date in OTD/OTIF.
        # Carrying it would score the copy against a promise nobody made for it.
        assert new_wo.must_ship_by is None
        # An INDEPENDENT job: re-attaching it to the source's assembly parent would put
        # a second child against demand the first child already satisfied.
        assert new_wo.parent_work_order_id is None
        # Labour is the record of what happened, and it happened on the source.
        assert db_session.query(TimeEntry).filter(TimeEntry.work_order_id == new_wo.id).count() == 0
        assert db_session.query(TimeEntry).filter(TimeEntry.work_order_id == source.id).count() == 1

    def test_operations_start_pending_with_no_production_record(
        self, client: TestClient, db_session: Session, ran_and_finished
    ):
        admin, source, source_operation = ran_and_finished

        resp = duplicate(client, headers_for(admin), source.id, quantity=40)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text
        new_wo = created_work_order(db_session, resp)
        [copied] = operations_of(db_session, new_wo)

        # PENDING, not READY, even where the source op was born READY on a laser pool:
        # the copy is DRAFT, and release promotes pending nest ops. Birthing them READY
        # would put un-released work on the kiosk queue.
        assert copied.status == OperationStatus.PENDING
        assert (copied.quantity_complete or 0) == 0
        assert (copied.quantity_scrapped or 0) == 0
        assert (copied.quantity_reworked or 0) == 0
        assert (copied.actual_setup_hours or 0) == 0
        assert (copied.actual_run_hours or 0) == 0
        assert copied.actual_start is None
        assert copied.actual_end is None
        assert copied.scheduled_start is None
        assert copied.scheduled_end is None
        assert copied.started_by is None
        assert copied.completed_by is None
        assert copied.last_reported_at is None
        assert copied.last_reported_good is None
        assert copied.last_reported_scrapped is None
        assert copied.inspection_complete is False
        assert copied.scrap_reason is None
        assert copied.scrap_reason_code_id is None
        # ... while the requirement to inspect (the PLAN) still carries.
        assert copied.requires_inspection is True
        assert copied.inspection_type == "final"
        # The source's own record is untouched by the copy.
        db_session.refresh(source_operation)
        assert source_operation.quantity_complete == 37.0
        assert source_operation.status == OperationStatus.COMPLETE

    def test_the_managers_dispatch_ranking_does_not_come_along(self, client: TestClient, db_session: Session):
        """``run_order`` is scheduling OUTPUT, one layer down from ``scheduled_start``.

        It is the manager's rank for ONE machine's board. A 40-nest duplicate arriving
        pre-ranked would, at release, drop 40 pre-ordered operations into the ordering
        set of a laser that already has queued work and displace the sequence the
        manager set. Advisory rather than gating, so it misplaces work rather than
        blocking it — but it is still the duplicate deciding something only the manager
        gets to decide.
        """
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="machining")
        part = make_part(db_session)
        source = make_work_order(db_session, part=part, quantity_ordered=10.0)
        ranked = add_operation(db_session, source, wc, sequence=10, run_order=7)

        resp = duplicate(client, headers_for(admin), source.id, quantity=10)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        [copied] = operations_of(db_session, new_wo)
        assert copied.run_order is None
        # The source keeps its rank — the board the manager set is untouched.
        db_session.refresh(ranked)
        assert ranked.run_order == 7

    def test_nests_start_with_zero_completed_runs(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="laser")
        part = make_part(db_session)
        source = make_work_order(db_session, part=part, quantity_ordered=6.0)
        package = make_package(db_session, source)
        operation = add_operation(db_session, source, wc, sequence=10)
        attach_nest(db_session, package, operation, planned_runs=6, completed_runs=6.0)

        resp = duplicate(client, headers_for(admin), source.id, quantity=6)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        [copied] = nests_of(db_session, new_wo)
        assert copied.planned_runs == 6
        assert copied.completed_runs == 0
        assert copied.remaining_runs == 6

    def test_ties_start_unconsumed_and_never_carry_a_lot_pin(self, client: TestClient, db_session: Session):
        """The pinned lot the source consumed is the one lot the copy must not point at.

        A pin says "consume from THIS lot", and the source job very likely consumed it.
        Unpinned means FIFO picks at consume time — the right default for work that has
        not started. ``qty_consumed`` resets for the same reason: the ledger is the record.
        """
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="laser")
        part = make_part(db_session)
        sheet = make_part(db_session, part_type="raw_material", uom="sheets")
        source = make_work_order(db_session, part=part, quantity_ordered=4.0)
        package = make_package(db_session, source)
        operation = add_operation(db_session, source, wc, sequence=10)
        attach_nest(db_session, package, operation, planned_runs=4)
        # The very lot the source job drew down — the one the copy must not point at.
        lot = InventoryItem(
            company_id=COMPANY_A,
            part_id=sheet.id,
            location="RACK-N-04",
            lot_number="HEAT-88213",
            quantity_on_hand=0.0,
        )
        db_session.add(lot)
        db_session.commit()
        db_session.refresh(lot)
        make_tie(
            db_session,
            source,
            sheet,
            operation=operation,
            qty_per_run=1.0,
            qty_planned=4.0,
            unit_of_measure="sheets",
            qty_consumed=4.0,
            pinned_inventory_item_id=lot.id,
            pinned_lot_number="HEAT-88213",
        )

        resp = duplicate(client, headers_for(admin), source.id, quantity=4)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        [tie] = ties_of(db_session, new_wo)
        assert tie.qty_consumed == 0.0
        assert tie.status == AllocationStatus.OPEN
        assert tie.pinned_inventory_item_id is None
        assert tie.pinned_lot_number is None


# --------------------------------------------------------------------------- #
# The one omission that is about IDENTITY rather than production history
# --------------------------------------------------------------------------- #
class TestUnitNumberIsNotCarried:
    """``unit_number`` (083) is dropped by ``_copy_header``, and NOTHING BUT THIS PINS IT.

    Every other omission in this file is about not fabricating history. This one is
    different in kind, and it is the single most consequential property of the whole
    083 feature: a duplicate is the NEXT unit, not the same one. Carrying the value
    would mint two live work orders both claiming to build unit 2410048 and put that
    claim on the kiosk hero, the crew station, the dispatch board and the public TV
    wall simultaneously — with no error anywhere and nothing in the system able to say
    which one the welder is standing at.

    A regression here is ONE line added to ``_copy_header``'s field list, which is the
    most natural-looking edit in that function. The planner types the new unit on the
    duplicate.
    """

    def test_the_copy_starts_with_no_unit_number(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="machining")
        part = make_part(db_session)
        source = make_work_order(
            db_session,
            part=part,
            quantity_ordered=1.0,
            unit_number="2410048",
            customer_name="Miratech",
            customer_po="PO-88213",
        )
        add_operation(db_session, source, wc, sequence=10, name="Weld out")

        resp = duplicate(client, headers_for(admin), source.id, quantity=1)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text
        new_wo = created_work_order(db_session, resp)

        assert new_wo.unit_number is None
        # Non-vacuity, and the contrast that makes the omission a DECISION rather than
        # an oversight: the neighbouring header free text on the same source row DOES
        # carry, so "nothing copied" is not the explanation for the None above.
        assert new_wo.customer_name == "Miratech"
        assert new_wo.customer_po == "PO-88213"
        # The source is untouched — it still names the unit it is actually building.
        db_session.refresh(source)
        assert source.unit_number == "2410048"

    def test_the_api_response_does_not_report_a_unit_number_either(self, client: TestClient, db_session: Session):
        """The row and the response are separate assertions on purpose: the envelope's
        ``work_order`` is what the planner's screen renders the UNIT badge from, so a
        response that echoed the source's unit would show the wrong number even against
        a correct row."""
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="machining")
        source = make_work_order(db_session, part=make_part(db_session), unit_number="2410048")
        add_operation(db_session, source, wc, sequence=10)

        resp = duplicate(client, headers_for(admin), source.id, quantity=1)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text
        body = resp.json()["work_order"]

        assert "unit_number" in body, "the key must still be present, just empty"
        assert body["unit_number"] is None
        assert "2410048" not in resp.text


# --------------------------------------------------------------------------- #
# The derived-quantity rule
# --------------------------------------------------------------------------- #
class TestQuantityIsDerivedForNestBearingWorkOrders:
    @pytest.mark.parametrize("requested", [1, 7, 99])
    def test_a_nest_bearing_duplicate_lands_on_the_sum_of_planned_runs(
        self, client: TestClient, db_session: Session, requested: int
    ):
        """Whatever the request says, the stored quantity is the definition's answer."""
        admin = make_user(db_session)
        source, _wc, _ops, nests, _package = build_laser_source(db_session, runs=(3, 5, 4))
        expected = float(sum(nest.planned_runs for nest in nests))

        resp = duplicate(client, headers_for(admin), source.id, quantity=requested)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        assert new_wo.quantity_ordered == expected
        # The client must never be shown a number the server did not store.
        assert float(resp.json()["work_order"]["quantity_ordered"]) == expected
        assert [nest.planned_runs for nest in nests_of(db_session, new_wo)] == [3, 5, 4]

    def test_the_overruled_request_is_recorded_on_the_chain(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        source, _wc, _ops, _nests, _package = build_laser_source(db_session, runs=(3, 5))

        resp = duplicate(client, headers_for(admin), source.id, quantity=99)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        [row] = audit_rows(db_session, "work_order", new_wo.id)
        assert row.extra_data["quantity"] == 8.0
        assert row.extra_data["requested_quantity"] == 99.0

    def test_requested_quantity_is_absent_when_the_two_agree(self, client: TestClient, db_session: Session):
        """Recorded only when it says something — an echo of the stored value is noise."""
        admin = make_user(db_session)
        source, _wc, _ops, _nests, _package = build_laser_source(db_session, runs=(3, 5))

        resp = duplicate(client, headers_for(admin), source.id, quantity=8)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        [row] = audit_rows(db_session, "work_order", new_wo.id)
        assert row.extra_data["quantity"] == 8.0
        assert "requested_quantity" not in row.extra_data

    def test_a_work_order_with_no_nests_honours_the_requested_quantity_exactly(
        self, client: TestClient, db_session: Session
    ):
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="machining")
        part = make_part(db_session)
        source = make_work_order(db_session, part=part, quantity_ordered=40.0)
        add_operation(db_session, source, wc, sequence=10)

        resp = duplicate(client, headers_for(admin), source.id, quantity=250)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        assert new_wo.quantity_ordered == 250.0
        [row] = audit_rows(db_session, "work_order", new_wo.id)
        assert row.extra_data["quantity"] == 250.0
        assert "requested_quantity" not in row.extra_data

    def test_soft_deleted_nests_neither_copy_nor_count_toward_the_quantity(
        self, client: TestClient, db_session: Session
    ):
        """Invariant 3 — and the rollup definition excludes deleted runs, so both must agree."""
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="laser")
        part = make_part(db_session)
        source = make_work_order(db_session, part=part, quantity_ordered=9.0)
        package = make_package(db_session, source)
        live_op = add_operation(db_session, source, wc, sequence=10)
        dead_op = add_operation(db_session, source, wc, sequence=20)
        attach_nest(db_session, package, live_op, planned_runs=4)
        attach_nest(db_session, package, dead_op, planned_runs=5, is_deleted=True)

        resp = duplicate(client, headers_for(admin), source.id, quantity=9)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        copied_nests = nests_of(db_session, new_wo)
        assert [nest.planned_runs for nest in copied_nests] == [4]
        assert new_wo.quantity_ordered == 4.0
        # And the OPERATION goes with it — see TestOperationsWhoseNestIsDead.
        assert len(operations_of(db_session, new_wo)) == 1


# --------------------------------------------------------------------------- #
# An operation whose nest is dead is a dead operation
# --------------------------------------------------------------------------- #
class TestOperationsWhoseNestIsDead:
    """``soft_delete_laser_nest`` parks the operation ON_HOLD without cancelling its tie.

    So the source row is inert but still present, and copying it resets it to PENDING —
    which a laser work order promotes to READY for EVERY pending operation at release.
    The duplicate would push a nest task with no nest, no CNC number and no drawing onto
    the kiosk queue, and an operator would have nothing to run and no way to say so.
    """

    def _source_with_a_dead_nest(self, db: Session):
        wc = make_work_center(db, wc_type="laser")
        part = make_part(db)
        source = make_work_order(db, part=part, quantity_ordered=9.0)
        package = make_package(db, source)
        live_op = add_operation(db, source, wc, sequence=10, name="Nest 1")
        dead_op = add_operation(db, source, wc, sequence=20, name="Nest 2", operation_number="OP20")
        attach_nest(db, package, live_op, planned_runs=4)
        attach_nest(db, package, dead_op, planned_runs=5, is_deleted=True)
        return source, live_op, dead_op

    def test_the_operation_is_skipped_not_copied_as_an_empty_nest_task(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        source, live_op, _dead_op = self._source_with_a_dead_nest(db_session)

        resp = duplicate(client, headers_for(admin), source.id, quantity=9)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        copied = operations_of(db_session, new_wo)
        assert [op.name for op in copied] == ["Nest 1"]
        assert copied[0].sequence == live_op.sequence

    def test_the_skip_is_recorded_on_the_chain_and_returned_to_the_planner(
        self, client: TestClient, db_session: Session
    ):
        """A skip nobody sees is the failure mode: the planner releases a short job."""
        admin = make_user(db_session)
        source, _live_op, dead_op = self._source_with_a_dead_nest(db_session)

        resp = duplicate(client, headers_for(admin), source.id, quantity=9)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        expected = {
            "source_operation_id": dead_op.id,
            "operation_number": "OP20",
            "sequence": 20,
            "reason": "laser_nest_deleted",
        }
        assert resp.json()["skipped_operations"] == [expected]

        new_wo = created_work_order(db_session, resp)
        [row] = audit_rows(db_session, "work_order", new_wo.id)
        assert row.extra_data["skipped_operations"] == [expected]
        assert row.extra_data["operation_count"] == 1

    def test_an_operation_with_a_LIVE_nest_is_untouched_by_the_skip(self, client: TestClient, db_session: Session):
        """Only the operations whose ONLY nest is dead are dropped."""
        admin = make_user(db_session)
        source, _wc, _ops, _nests, _package = build_laser_source(db_session, runs=(3, 5))

        resp = duplicate(client, headers_for(admin), source.id, quantity=8)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        assert len(operations_of(db_session, new_wo)) == 2
        assert resp.json()["skipped_operations"] == []

    def test_a_tie_on_the_skipped_operation_is_skipped_too_and_says_which_one(
        self, client: TestClient, db_session: Session
    ):
        """The tie's ``source_work_order_operation_id`` joins it to the operation entry.

        ``soft_delete_laser_nest`` leaves the tie OPEN, so without this the duplicate
        would carry a tie scoped to an operation that does not exist on it.
        """
        admin = make_user(db_session)
        source, _live_op, dead_op = self._source_with_a_dead_nest(db_session)
        sheet = make_part(db_session, part_type="raw_material", uom="sheets")
        stranded = make_tie(db_session, source, sheet, operation=dead_op, qty_per_run=1.0)

        resp = duplicate(client, headers_for(admin), source.id, quantity=9)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        assert resp.json()["skipped_material_allocations"] == [
            {
                "source_allocation_id": stranded.id,
                "part_id": sheet.id,
                "source_work_order_operation_id": dead_op.id,
                "reason": "operation_not_copied",
            }
        ]
        new_wo = created_work_order(db_session, resp)
        assert ties_of(db_session, new_wo) == []


# --------------------------------------------------------------------------- #
# The one omission with no channel to the planner
# --------------------------------------------------------------------------- #
class TestADroppedNestIsAHardFailureNotALogLine:
    """Every other omission this service can produce reaches the planner. This one cannot.

    There is no "skipped nest" list on ``DuplicateResult``, so a live nest whose source
    operation is missing from ``operation_map`` would reach neither the response envelope
    nor the audit ``extra_data`` — the planner would release a laser job silently short a
    nest, and nothing anywhere would say so. So ``_copy_laser_nests`` RAISES instead of
    logging and continuing, which rolls the whole duplicate back through the caller's
    ``atomic_transaction``.

    It is unreachable through the API today (``_copy_operations`` only skips operations
    with no LIVE nest, and the nest query returns live nests only), so the private
    function is the only way in — the same posture as the ``nest_runs_unavailable``
    branch below. The point of pinning it is that if the two sets ever stop lining up,
    the answer must stay "fail loudly", never "drop the nest".
    """

    def test_a_live_nest_whose_operation_was_not_copied_raises_rather_than_being_dropped(self, db_session: Session):
        admin = make_user(db_session)
        source, _wc, _ops, nests, _package = build_laser_source(db_session, runs=(3,))
        destination = make_work_order(
            db_session,
            part=make_part(db_session),
            quantity_ordered=3.0,
            status_value=WorkOrderStatus.DRAFT,
        )

        # An empty map is exactly the condition: the nest is live, and the operation
        # backing it was not copied.
        with pytest.raises(RuntimeError) as raised:
            _copy_laser_nests(
                db_session,
                source=source,
                new_work_order=destination,
                operation_map={},
                company_id=COMPANY_A,
                user_id=admin.id,
                audit=AuditService(db_session, admin),
            )

        message = str(raised.value)
        # It names the nest and the operation, so whoever reads the 500 can act on it.
        assert str(nests[0].id) in message
        assert source.work_order_number in message
        db_session.rollback()

    def test_the_nest_query_that_feeds_it_ignores_soft_deleted_nests(self, client: TestClient, db_session: Session):
        """Invariant 3, and the reason the raise is unreachable: a DEAD nest is never copied.

        If soft-deleted nests reached the copy loop, every operation skipped for having a
        dead nest would immediately trip the RuntimeError above — the guard and the skip
        would contradict each other. They do not, because the two look at different rows.
        """
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="laser")
        part = make_part(db_session)
        source = make_work_order(db_session, part=part, quantity_ordered=4.0)
        package = make_package(db_session, source)
        live_op = add_operation(db_session, source, wc, sequence=10)
        dead_op = add_operation(db_session, source, wc, sequence=20)
        attach_nest(db_session, package, live_op, planned_runs=4)
        attach_nest(db_session, package, dead_op, planned_runs=9, is_deleted=True)

        resp = duplicate(client, headers_for(admin), source.id, quantity=4)

        # A 201, not a 500: the dead nest's operation is skipped and its nest is never
        # looked at, so the "no channel to the planner" guard is never reached.
        assert resp.status_code == status.HTTP_201_CREATED, resp.text
        new_wo = created_work_order(db_session, resp)
        assert [nest.planned_runs for nest in nests_of(db_session, new_wo)] == [4]
        assert [entry["reason"] for entry in resp.json()["skipped_operations"]] == ["laser_nest_deleted"]


# --------------------------------------------------------------------------- #
# The skip shapes themselves
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestASkipCannotBeMalformedAfterTheCommit:
    """The service builds each skip AS ITS RESPONSE SCHEMA, inside the transaction.

    Hand-rolled ``dict``s were only validated when the endpoint constructed
    ``WorkOrderDuplicateResponse`` — which happens AFTER ``atomic_transaction`` has
    committed. A key typo was therefore a 500 on a work order that already existed, and a
    500 carries no skip list: precisely the "the planner never sees the skip" outcome the
    envelope exists to prevent. Constructing the model in the service moves that failure
    inside the transaction, where it rolls the whole duplicate back.
    """

    def test_a_mistyped_required_field_is_refused(self):
        with pytest.raises(ValidationError):
            WorkOrderDuplicateSkippedOperation(
                source_operation_idd=7, operation_number="OP20", sequence=20, reason="laser_nest_deleted"
            )

    def test_a_mistyped_OPTIONAL_field_is_refused_too(self):
        """``extra="forbid"`` is what covers these — a missing-required error cannot.

        Without it the typo'd key would be dropped and the entry would still validate,
        reaching the planner naming no operation at all.
        """
        with pytest.raises(ValidationError):
            WorkOrderDuplicateSkippedOperation(
                source_operation_id=7, operation_num="OP20", sequence=20, reason="laser_nest_deleted"
            )
        with pytest.raises(ValidationError):
            WorkOrderDuplicateSkippedAllocation(
                source_allocation_id=1, part_id=9, source_operation_id=7, reason="operation_not_copied"
            )

    def test_a_null_part_is_refused_because_the_column_is_not_nullable(self):
        """``work_order_material_allocations.part_id`` is NOT NULL, so the schema says so.

        A tie the planner is told about but cannot name is not actionable.
        """
        with pytest.raises(ValidationError):
            WorkOrderDuplicateSkippedAllocation(
                source_allocation_id=1, part_id=None, source_work_order_operation_id=None, reason="part_not_available"
            )

    def test_the_wire_shape_is_unchanged_from_the_hand_rolled_dicts(self):
        """The audit ``extra_data`` rows are ``model_dump()`` of these same objects."""
        assert WorkOrderDuplicateSkippedOperation(
            source_operation_id=7, operation_number="OP20", sequence=20, reason="laser_nest_deleted"
        ).model_dump() == {
            "source_operation_id": 7,
            "operation_number": "OP20",
            "sequence": 20,
            "reason": "laser_nest_deleted",
        }
        assert WorkOrderDuplicateSkippedAllocation(
            source_allocation_id=1, part_id=9, source_work_order_operation_id=7, reason="operation_not_copied"
        ).model_dump() == {
            "source_allocation_id": 1,
            "part_id": 9,
            "source_work_order_operation_id": 7,
            "reason": "operation_not_copied",
        }


# --------------------------------------------------------------------------- #
# Quantity-derived plan numbers
# --------------------------------------------------------------------------- #
class TestQuantityDerivedPlanNumbersAreScaled:
    """``run_time_hours`` and ``component_quantity`` are stored PRE-MULTIPLIED by the ordered quantity.

    ``create_routing_operations_for_work_order`` writes ``run_hours_per_unit x
    quantity``, so an unscaled copy claims the SOURCE's hours at the duplicate's
    quantity. Duplicate a 100-piece job at 10 and every operation still claims the
    100-piece run hours — which is not cosmetic: scheduling sizes capacity and slots
    from it, the dispatch board shows it, and ``completion_cost_service`` reads
    ``run_time_hours`` FIRST, falling back to ``run_time_per_piece`` only when it is 0.

    ``component_quantity`` is the same class of number one column over —
    ``qty_per_assembly x quantity_ordered``, a WHOLE-JOB total written that way by both
    of its writers — and its readers are live rather than archival, so an unscaled copy
    is wrong on the shop floor, not just in a report.
    """

    def _source(self, db: Session, *, quantity: float = 100.0):
        wc = make_work_center(db, wc_type="machining")
        part = make_part(db)
        source = make_work_order(db, part=part, quantity_ordered=quantity, estimated_hours=50.0, estimated_cost=4000.0)
        operation = add_operation(
            db, source, wc, sequence=10, setup_time_hours=2.0, run_time_hours=20.0, run_time_per_piece=0.0
        )
        return source, operation

    def test_a_smaller_re_run_scales_the_hours_and_the_cost_estimate(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        source, _operation = self._source(db_session, quantity=100.0)

        resp = duplicate(client, headers_for(admin), source.id, quantity=10)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        [copied] = operations_of(db_session, new_wo)
        # ratio 0.1 — reproduces run_hours_per_unit x new_quantity exactly, because
        # that is what the copied value was divided into.
        assert copied.run_time_hours == 2.0
        assert new_wo.estimated_hours == 5.0
        assert new_wo.estimated_cost == 400.0

    def test_a_larger_re_run_scales_up(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        source, _operation = self._source(db_session, quantity=100.0)

        resp = duplicate(client, headers_for(admin), source.id, quantity=250)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        [copied] = operations_of(db_session, new_wo)
        assert copied.run_time_hours == 50.0
        assert new_wo.estimated_hours == 125.0
        assert new_wo.estimated_cost == 10000.0

    def test_the_whole_job_component_quantity_scales_with_the_hours(self, client: TestClient, db_session: Session):
        """A component operation's ``component_quantity`` is a total, not a per-unit rate.

        Both writers store ``qty_per_assembly x work_order.quantity_ordered``
        (``_create_assembly_routing_operations`` and
        ``_reconcile_operation_component_quantities``), and
        ``completion_inventory_service._routing_backflush_demand`` says so outright. An
        unscaled copy is not cosmetic: ``work_order_state_service
        .operation_target_quantity`` returns ``component_quantity`` IN PREFERENCE to the
        work-order quantity, so the kiosk, the dispatch board, the wallboard, the
        completion caps and the op-satisfied rollup would every one of them target the
        source's 100-piece total on a 10-piece re-run.
        """
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="machining")
        part = make_part(db_session)
        component = make_part(db_session, part_type="purchased")
        source = make_work_order(db_session, part=part, quantity_ordered=100.0)
        add_operation(
            db_session,
            source,
            wc,
            sequence=10,
            component_part_id=component.id,
            # 2 per assembly x 100 ordered — the shape both writers produce.
            component_quantity=200.0,
        )

        resp = duplicate(client, headers_for(admin), source.id, quantity=10)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        [copied] = operations_of(db_session, new_wo)
        # 2 per assembly x 10 ordered: the RATE is preserved, the total is restated.
        assert copied.component_quantity == 20.0
        assert copied.component_part_id == component.id

    def test_a_nest_operations_run_count_is_never_scaled_as_if_it_were_a_component_total(
        self, client: TestClient, db_session: Session
    ):
        """On a NEST operation ``component_quantity`` IS ``planned_runs`` — scaling it corrupts it.

        ``laser_nest_service`` writes the run count into that same column (at import, at
        manual create, and on every edit through ``sync_laser_nest_to_operation``), and
        runs carry across verbatim. Safety here is structural, not lucky: one live nest
        makes the ratio exactly 1.0, so nothing on the work order is scaled at all.
        """
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="laser")
        part = make_part(db_session)
        # Header drift on purpose: 999 stored against 8 runs. A ``new / source`` ratio
        # would be 8/999 and would shred a number that is not quantity-derived at all.
        source = make_work_order(db_session, part=part, quantity_ordered=999.0)
        package = make_package(db_session, source)
        for index, runs in enumerate((3, 5)):
            operation = add_operation(db_session, source, wc, sequence=(index + 1) * 10, component_quantity=float(runs))
            attach_nest(db_session, package, operation, planned_runs=runs)

        resp = duplicate(client, headers_for(admin), source.id, quantity=8)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        assert {op.component_quantity for op in operations_of(db_session, new_wo)} == {3.0, 5.0}
        # And it still agrees with the nest rows that define it.
        assert {nest.planned_runs for nest in nests_of(db_session, new_wo)} == {3, 5}

    def test_setup_is_per_job_and_is_never_scaled(self, client: TestClient, db_session: Session):
        """Creation does not multiply setup by quantity either — one setup is one setup."""
        admin = make_user(db_session)
        source, _operation = self._source(db_session, quantity=100.0)

        resp = duplicate(client, headers_for(admin), source.id, quantity=10)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        [copied] = operations_of(db_session, new_wo)
        assert copied.setup_time_hours == 2.0

    def test_the_ratio_used_is_recorded_on_the_chain(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        source, _operation = self._source(db_session, quantity=100.0)

        resp = duplicate(client, headers_for(admin), source.id, quantity=25)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        [row] = audit_rows(db_session, "work_order", new_wo.id)
        assert row.extra_data["quantity_ratio"] == 0.25

    def test_a_nest_bearing_duplicate_is_never_rescaled(self, client: TestClient, db_session: Session):
        """Ratio is exactly 1.0, not ``new / source``.

        The nests carry their runs across VERBATIM, so per-run the plan is unchanged and
        1.0 is the honest factor. Computing ``new / source`` would let drift between the
        source's stored header quantity and its own runs sum leak in as a spurious
        rescale of a plan nothing about has changed.
        """
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="laser")
        part = make_part(db_session)
        # Deliberate drift: the header says 999 while the runs sum to 8.
        source = make_work_order(
            db_session, part=part, quantity_ordered=999.0, estimated_hours=40.0, estimated_cost=3000.0
        )
        package = make_package(db_session, source)
        for index, runs in enumerate((3, 5)):
            operation = add_operation(db_session, source, wc, sequence=(index + 1) * 10, run_time_hours=6.0)
            attach_nest(db_session, package, operation, planned_runs=runs)

        resp = duplicate(client, headers_for(admin), source.id, quantity=8)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        assert new_wo.quantity_ordered == 8.0
        assert new_wo.estimated_hours == 40.0
        assert new_wo.estimated_cost == 3000.0
        assert {op.run_time_hours for op in operations_of(db_session, new_wo)} == {6.0}
        [row] = audit_rows(db_session, "work_order", new_wo.id)
        assert row.extra_data["quantity_ratio"] == 1.0

    @pytest.mark.unit
    def test_a_legacy_zero_source_quantity_copies_the_plan_unscaled(self):
        """Predates the ``> 0`` CHECK. Copy unscaled rather than divide by zero.

        Exercised directly rather than through the API on purpose: this DB carries
        ``chk_work_orders_quantity_ordered_positive``, so a zero source quantity cannot
        be created here at all. The branch is defence for rows that predate the
        constraint, and the pure function is the only place it is reachable.
        """
        work_order = WorkOrder(estimated_hours=8.0, estimated_cost=600.0)
        operation = WorkOrderOperation(run_time_hours=12.0, component_quantity=40.0)

        ratio = _scale_quantity_derived_plan(
            work_order,
            {1: operation},
            source_quantity=0.0,
            new_quantity=25.0,
            quantity_is_derived=False,
        )

        assert ratio == 1.0
        assert work_order.estimated_hours == 8.0
        assert work_order.estimated_cost == 600.0
        assert operation.run_time_hours == 12.0
        assert operation.component_quantity == 40.0

    @pytest.mark.unit
    def test_a_derived_quantity_returns_before_the_first_write(self):
        """The nest guard must return 1.0 BEFORE any ``setattr``, not compute-then-skip.

        That ordering is what makes it structurally impossible for a nest operation's
        ``component_quantity`` — which holds ``planned_runs``, not a quantity-derived
        total — to be rescaled. Asserted on the pure function because the API path can
        only ever exercise it with a consistent header.
        """
        work_order = WorkOrder(estimated_hours=8.0, estimated_cost=600.0)
        operation = WorkOrderOperation(run_time_hours=12.0, component_quantity=3.0)

        ratio = _scale_quantity_derived_plan(
            work_order,
            {1: operation},
            source_quantity=999.0,
            new_quantity=8.0,
            quantity_is_derived=True,
        )

        assert ratio == 1.0
        assert work_order.estimated_hours == 8.0
        assert work_order.estimated_cost == 600.0
        assert operation.run_time_hours == 12.0
        assert operation.component_quantity == 3.0


# --------------------------------------------------------------------------- #
# Refusals — a duplicate must never mint a WO the create path would have rejected
# --------------------------------------------------------------------------- #
class TestARetiredPartIsRefused:
    """One button is not a licence to route around a gate a planner would have hit.

    Creating a work order by hand at least means typing the retired part number in;
    duplicating a completed job puts a retired part back into production with a click.
    It is also the rule the tie half of this service already applies — refusing a
    deleted part on a TIE while blindly copying the PRODUCED part would be the service
    contradicting itself.
    """

    def test_a_soft_deleted_produced_part_fails_the_whole_call_with_409(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="machining")
        part = make_part(db_session)
        source = make_work_order(db_session, part=part, quantity_ordered=10.0)
        add_operation(db_session, source, wc, sequence=10)
        part.is_deleted = True
        db_session.commit()
        wo_count = db_session.query(WorkOrder).count()
        audit_count = db_session.query(AuditLog).count()

        resp = duplicate(client, headers_for(admin), source.id, quantity=10)
        assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
        detail = resp.json()["detail"]
        assert part.part_number in detail
        assert "deleted" in detail

        # A refusal, not a skip: nothing at all was created.
        db_session.expire_all()
        assert db_session.query(WorkOrder).count() == wo_count
        assert db_session.query(WorkOrderOperation).count() == 1
        assert db_session.query(AuditLog).count() == audit_count

    def test_a_part_less_laser_work_order_is_unaffected(self, client: TestClient, db_session: Session):
        """``ck_work_orders_part_required_unless_laser`` makes NULL legal here — no part to retire."""
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="laser")
        source = make_work_order(db_session, part=None, quantity_ordered=4.0, work_order_type="laser_cutting")
        package = make_package(db_session, source)
        operation = add_operation(db_session, source, wc, sequence=10)
        attach_nest(db_session, package, operation, planned_runs=4)

        resp = duplicate(client, headers_for(admin), source.id, quantity=4)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        assert new_wo.part_id is None
        assert new_wo.work_order_type == "laser_cutting"


# --------------------------------------------------------------------------- #
# Material tie recomputation
# --------------------------------------------------------------------------- #
class TestMaterialTieRecomputation:
    def test_a_nest_backed_tie_plans_exactly_what_the_nest_tie_creator_would_plan(
        self, client: TestClient, db_session: Session
    ):
        """Assert the EQUIVALENCE, not the formula.

        Re-stating ``qty_per_run × planned_runs`` here would let the duplicate path and
        ``laser_nest_service.create_nest_material_allocation`` drift together and still
        pass. So the reference value is produced by that function itself, on the same
        inputs, and the copy is compared against it.
        """
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="laser")
        part = make_part(db_session)
        sheet = make_part(db_session, part_type="raw_material", uom="sheets")
        source = make_work_order(db_session, part=part, quantity_ordered=7.0)
        package = make_package(db_session, source)
        operation = add_operation(db_session, source, wc, sequence=10)
        attach_nest(db_session, package, operation, planned_runs=7)
        # A deliberately WRONG stored qty_planned on the source, so a copy would fail.
        make_tie(
            db_session,
            source,
            sheet,
            operation=operation,
            qty_per_run=2.5,
            qty_planned=1.0,
            unit_of_measure="sheets",
        )

        resp = duplicate(client, headers_for(admin), source.id, quantity=7)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text
        new_wo = created_work_order(db_session, resp)
        [copied] = ties_of(db_session, new_wo)

        # The reference: the real nest-tie creator, same qty_per_run and planned_runs,
        # on a throwaway operation so the partial unique index sees a distinct scope.
        reference_wo = make_work_order(db_session, part=part, quantity_ordered=7.0)
        reference_op = add_operation(db_session, reference_wo, wc, sequence=10)
        reference = create_nest_material_allocation(
            db_session,
            work_order=reference_wo,
            operation=reference_op,
            part=sheet,
            qty_per_run=2.5,
            planned_runs=7,
            company_id=COMPANY_A,
            created_by=admin.id,
            audit=AuditService(db_session, admin),
        )
        db_session.commit()

        assert copied.qty_planned == reference.qty_planned
        assert copied.qty_per_run == reference.qty_per_run
        assert copied.unit_of_measure == reference.unit_of_measure
        assert copied.source == reference.source
        assert copied.status == reference.status
        assert copied.qty_consumed == reference.qty_consumed
        assert copied.pinned_inventory_item_id == reference.pinned_inventory_item_id
        assert copied.pinned_lot_number == reference.pinned_lot_number

    def test_a_null_qty_per_run_is_read_as_one_per_run(self, client: TestClient, db_session: Session):
        """NULL on an operation-scoped tie means "not run-scaled" — COALESCE(qty_per_run, 1.0)."""
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="laser")
        part = make_part(db_session)
        sheet = make_part(db_session, part_type="raw_material", uom="sheets")
        source = make_work_order(db_session, part=part, quantity_ordered=5.0)
        package = make_package(db_session, source)
        operation = add_operation(db_session, source, wc, sequence=10)
        attach_nest(db_session, package, operation, planned_runs=5)
        make_tie(db_session, source, sheet, operation=operation, qty_per_run=None, qty_planned=99.0)

        resp = duplicate(client, headers_for(admin), source.id, quantity=5)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        [copied] = ties_of(db_session, new_wo)

        reference_wo = make_work_order(db_session, part=part, quantity_ordered=5.0)
        reference_op = add_operation(db_session, reference_wo, wc, sequence=10)
        reference = create_nest_material_allocation(
            db_session,
            work_order=reference_wo,
            operation=reference_op,
            part=sheet,
            qty_per_run=None,
            planned_runs=5,
            company_id=COMPANY_A,
            created_by=admin.id,
            audit=AuditService(db_session, admin),
        )
        db_session.commit()
        assert copied.qty_planned == reference.qty_planned

    def test_the_unit_is_resnapshotted_from_the_part_not_carried_from_the_stale_tie(
        self, client: TestClient, db_session: Session
    ):
        """A part restocked in a different unit must not hand the copy a stale label.

        The stored ``unit_of_measure`` is documented as a snapshot of
        ``Part.unit_of_measure`` AT TIE TIME, and this tie's time is now. Carrying the
        old label is the one way a copied tie can quietly mis-state how much material a
        nest draws.
        """
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="laser")
        part = make_part(db_session)
        # The part is stocked in SHEETS today; the source job tied it when it was EACH.
        sheet = make_part(db_session, part_type="raw_material", uom="sheets")
        source = make_work_order(db_session, part=part, quantity_ordered=3.0)
        package = make_package(db_session, source)
        operation = add_operation(db_session, source, wc, sequence=10)
        attach_nest(db_session, package, operation, planned_runs=3)
        stale = make_tie(db_session, source, sheet, operation=operation, qty_per_run=1.0, unit_of_measure="each")

        resp = duplicate(client, headers_for(admin), source.id, quantity=3)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        [copied] = ties_of(db_session, new_wo)
        assert stale.unit_of_measure == "each"
        assert copied.unit_of_measure == "sheets", "the CURRENT unit, not the one the source job stored"

    def test_a_tie_whose_part_was_soft_deleted_is_skipped_and_recorded(self, client: TestClient, db_session: Session):
        """``POST .../material-allocations`` refuses a deleted part, so the copy must too.

        Re-creating one here would mint a tie no planner could have made by hand. The
        omission goes on the chain rather than happening silently.
        """
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="laser")
        part = make_part(db_session)
        live_sheet = make_part(db_session, part_type="raw_material", uom="sheets")
        gone_sheet = make_part(db_session, part_type="raw_material", uom="sheets", is_deleted=True)
        source = make_work_order(db_session, part=part, quantity_ordered=6.0)
        package = make_package(db_session, source)
        operation = add_operation(db_session, source, wc, sequence=10)
        attach_nest(db_session, package, operation, planned_runs=6)
        make_tie(db_session, source, live_sheet, operation=operation, qty_per_run=1.0)
        orphaned = make_tie(db_session, source, gone_sheet, operation=operation, qty_per_run=1.0)

        resp = duplicate(client, headers_for(admin), source.id, quantity=6)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        copied = ties_of(db_session, new_wo)
        assert [tie.part_id for tie in copied] == [live_sheet.id]

        expected = {
            "source_allocation_id": orphaned.id,
            "part_id": gone_sheet.id,
            "source_work_order_operation_id": operation.id,
            "reason": "part_not_available",
        }
        [row] = audit_rows(db_session, "work_order", new_wo.id)
        assert row.extra_data["material_allocation_count"] == 1
        assert row.extra_data["skipped_material_allocations"] == [expected]
        # Returned to the planner too, not only written to the chain.
        assert resp.json()["skipped_material_allocations"] == [expected]

    def test_a_tie_whose_operation_was_not_copied_is_skipped_and_recorded(
        self, client: TestClient, db_session: Session
    ):
        """Never silently re-scoped to the work order — the two scopes are not interchangeable.

        A work-order-scoped tie carrying a ``qty_per_run`` is a 422 on the tie API, and
        the two scopes consume at different moments under different ledger reference
        shapes. So a tie pointing at an operation the copy does not have is dropped,
        with the reason on the chain.
        """
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="machining")
        part = make_part(db_session)
        bar = make_part(db_session, part_type="raw_material", uom="feet")
        source = make_work_order(db_session, part=part, quantity_ordered=10.0)
        add_operation(db_session, source, wc, sequence=10)
        # Drift: the tie belongs to `source` but names an operation on ANOTHER job.
        foreign_wo = make_work_order(db_session, part=part, quantity_ordered=10.0)
        foreign_op = add_operation(db_session, foreign_wo, wc, sequence=10)
        stranded = make_tie(db_session, source, bar, operation=foreign_op, qty_per_run=1.0)

        resp = duplicate(client, headers_for(admin), source.id, quantity=10)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        assert ties_of(db_session, new_wo) == []
        [row] = audit_rows(db_session, "work_order", new_wo.id)
        assert row.extra_data["skipped_material_allocations"] == [
            {
                "source_allocation_id": stranded.id,
                "part_id": bar.id,
                "source_work_order_operation_id": foreign_op.id,
                "reason": "operation_not_copied",
            }
        ]

    def test_a_nest_backed_tie_with_no_run_count_is_REFUSED_not_planned_at_the_wo_quantity(
        self, client: TestClient, db_session: Session
    ):
        """The old fallback inflated one nest's demand by roughly the nest count.

        A nest-backed operation's run basis is ``planned_runs`` — a per-nest number (a
        nest runs 3 sheets). The work order's quantity is the SUM over every nest (40
        nests x 3 = 120). Substituting one for the other does not degrade gracefully; it
        plans ~40x the material and shows that on the shortage and MRP views. A missing
        tie the planner is told about beats a present tie wrong by two orders of magnitude.

        What this pins down is that the OUTCOME holds — nothing plans 3 x 120 sheets for
        a nest that runs 3 — and that the reason the planner is given is
        ``operation_not_copied``, deterministically. ``nest_runs_unavailable`` is NOT
        producible through this route or any other: an operation that is nest-backed with
        no live nest is in ``deleted_only_operation_ids``, so ``_copy_operations`` skips
        it and the tie is refused one branch earlier. That branch is defence kept for the
        day the sets stop lining up, not a reason clients will ever see.
        """
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="laser")
        part = make_part(db_session)
        sheet = make_part(db_session, part_type="raw_material", uom="sheets")
        source = make_work_order(db_session, part=part, quantity_ordered=120.0)
        package = make_package(db_session, source)
        live_op = add_operation(db_session, source, wc, sequence=10)
        attach_nest(db_session, package, live_op, planned_runs=120)
        # A second nest operation whose nest is dead: nest-backed, zero runs carried.
        dead_op = add_operation(db_session, source, wc, sequence=20)
        attach_nest(db_session, package, dead_op, planned_runs=3, is_deleted=True)
        stranded = make_tie(db_session, source, sheet, operation=dead_op, qty_per_run=3.0)

        resp = duplicate(client, headers_for(admin), source.id, quantity=120)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        # Nothing planned 3 x 120 = 360 sheets for a nest that runs 3.
        assert [tie.part_id for tie in ties_of(db_session, new_wo)] == []
        skipped = resp.json()["skipped_material_allocations"]
        assert [entry["source_allocation_id"] for entry in skipped] == [stranded.id]
        # The operation skip fires FIRST and always — see the docstring. Asserted exactly,
        # not as a set membership, because which reason the planner reads is the contract.
        assert skipped[0]["reason"] == "operation_not_copied"

    def test_planned_runs_of_zero_is_honoured_as_zero_not_treated_as_unknown(
        self, client: TestClient, db_session: Session
    ):
        """0 runs is a real plan; only "no run count at all" refuses."""
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="laser")
        part = make_part(db_session)
        sheet = make_part(db_session, part_type="raw_material", uom="sheets")
        source = make_work_order(db_session, part=part, quantity_ordered=5.0)
        package = make_package(db_session, source)
        runs_op = add_operation(db_session, source, wc, sequence=10)
        zero_op = add_operation(db_session, source, wc, sequence=20)
        attach_nest(db_session, package, runs_op, planned_runs=5)
        attach_nest(db_session, package, zero_op, planned_runs=0)
        make_tie(db_session, source, sheet, operation=zero_op, qty_per_run=2.0, qty_planned=99.0)

        resp = duplicate(client, headers_for(admin), source.id, quantity=5)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        [tie] = ties_of(db_session, new_wo)
        assert tie.qty_planned == 0.0
        assert resp.json()["skipped_material_allocations"] == []

    def test_an_operation_scoped_tie_with_no_nest_at_all_scales_the_stored_qty_planned(
        self, client: TestClient, db_session: Session
    ):
        """The re-derivation is scoped to NEST-backed ties; an ordinary production op scales.

        There are no runs to re-derive from, so the source's own ``qty_planned`` is the
        plan and the duplicate's quantity is the only thing that changed.
        """
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="machining")
        part = make_part(db_session)
        bar = make_part(db_session, part_type="raw_material", uom="feet")
        source = make_work_order(db_session, part=part, quantity_ordered=10.0)
        operation = add_operation(db_session, source, wc, sequence=10)
        make_tie(db_session, source, bar, operation=operation, qty_per_run=1.5, qty_planned=15.0)

        resp = duplicate(client, headers_for(admin), source.id, quantity=20)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        [tie] = ties_of(db_session, new_wo)
        assert tie.qty_planned == 30.0

    def test_an_operation_scoped_ties_caller_supplied_qty_planned_is_not_rewritten(
        self, client: TestClient, db_session: Session
    ):
        """``qty_planned`` is caller-supplied and INDEPENDENT of ``qty_per_run``.

        ``MaterialAllocationCreate`` requires ``qty_planned`` and leaves ``qty_per_run``
        optional — the endpoint defaults the rate to 1.0 on an operation-scoped tie and
        stores ``qty_planned`` verbatim — so "500 lb of bar to OP20" is an ordinary tie,
        not a malformed one. Recomputing it as ``qty_per_run x quantity_ordered`` turned
        that 500 into the work-order quantity: a silent rewrite, with no skip, nothing on
        the response and nothing on the chain to say the number had changed.
        """
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="machining")
        part = make_part(db_session)
        bar = make_part(db_session, part_type="raw_material", uom="feet")
        source = make_work_order(db_session, part=part, quantity_ordered=10.0)
        operation = add_operation(db_session, source, wc, sequence=10)
        # Exactly what POST /work-orders/{id}/material-allocations stores for a tie
        # created as "500 lb to OP20" with no qty_per_run supplied.
        make_tie(db_session, source, bar, operation=operation, qty_per_run=1.0, qty_planned=500.0)

        # Same quantity: the promise is bit-for-bit, on EVERY non-nest tie, not just the
        # work-order-scoped ones.
        same = duplicate(client, headers_for(admin), source.id, quantity=10)
        assert same.status_code == status.HTTP_201_CREATED, same.text
        [tie] = ties_of(db_session, created_work_order(db_session, same))
        assert tie.qty_planned == 500.0
        assert tie.work_order_operation_id is not None
        assert same.json()["skipped_material_allocations"] == []

        # Double the job, double the material — the ratio, never the rate x quantity.
        doubled = duplicate(client, headers_for(admin), source.id, quantity=20)
        assert doubled.status_code == status.HTTP_201_CREATED, doubled.text
        [tie] = ties_of(db_session, created_work_order(db_session, doubled))
        assert tie.qty_planned == 1000.0

    def test_a_work_order_scoped_tie_scales_with_the_requested_quantity(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="machining")
        part = make_part(db_session)
        bar = make_part(db_session, part_type="raw_material", uom="feet")
        source = make_work_order(db_session, part=part, quantity_ordered=10.0)
        add_operation(db_session, source, wc, sequence=10)
        make_tie(db_session, source, bar, qty_planned=25.0, unit_of_measure="feet")

        resp = duplicate(client, headers_for(admin), source.id, quantity=20)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        [tie] = ties_of(db_session, new_wo)
        assert tie.qty_planned == 50.0

    @pytest.mark.unit
    def test_the_nest_backed_refusal_is_exercised_directly_because_the_API_cannot_reach_it(self):
        """``nest_runs_unavailable`` is DEFENCE, so the pure function is the only way in.

        Through the API a nest-backed operation with no live nest is skipped by
        ``_copy_operations`` first, and its tie is refused as ``operation_not_copied``.
        The branch is still worth pinning: if the two sets ever stop lining up, the
        answer must stay "refuse", never "substitute the work-order quantity".
        """
        allocation = WorkOrderMaterialAllocation(work_order_operation_id=42, qty_per_run=3.0, qty_planned=99.0)

        assert (
            _recomputed_qty_planned(allocation, nest_backed=True, planned_runs=None, quantity_ratio=1.0) is None
        ), "no run count => refuse, never fall back to the work-order quantity"
        # 0 runs is a real plan, not "unknown" — the caller passes dict.get(), where a
        # truthiness test would have swallowed a legitimate zero.
        assert _recomputed_qty_planned(allocation, nest_backed=True, planned_runs=0, quantity_ratio=1.0) == 0.0
        assert _recomputed_qty_planned(allocation, nest_backed=True, planned_runs=4, quantity_ratio=1.0) == 12.0


# --------------------------------------------------------------------------- #
# The THIRD tie constructor — the part-type gate reaches it too
# --------------------------------------------------------------------------- #
class TestProducedPartTiesAreSkippedRatherThanCopied:
    """A legacy produced-part tie is DROPPED and REPORTED, never propagated.

    There are three constructors of a ``work_order_material_allocations`` row.
    ``POST /work-orders/{id}/material-allocations`` and ``_find_nest_material_part`` (both
    nest doors) refuse **422** a tie whose part is one the shop PRODUCES, because such a tie
    makes a job deplete finished goods to build itself and consumption never auto-reverses
    (invariant 6b) — see ``tests/api/test_material_tie_part_type_gate.py``.

    ``_copy_material_allocations`` is the third, and it used to copy ``part_id`` verbatim
    without ever re-reading the part's class, so a LEGACY tie — one created before the gate
    shipped, which is the whole population the gate cannot retroactively clean — survived
    duplication and landed OPEN on the new DRAFT work order for the completion engine to
    draw against. It now asks the same predicate the other two doors ask
    (``material_tie_part_gate.part_is_tieable_material``) and SKIPS the tie as
    ``part_not_tieable``.

    **Skip, not 409.** A duplicate is not a tie-creation request: refusing the whole copy
    over one legacy row would leave the planner with nothing, where a skip leaves them a
    draft plus a named tie to re-make. That is the same trade the three pre-existing skip
    reasons already make, and the skip rides BOTH channels the module docstring requires —
    the response envelope and the work order's audit ``extra_data``.
    """

    def test_a_produced_part_tie_is_skipped_and_reported(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="machining")
        part = make_part(db_session)
        # A part the shop PRODUCES, tied as material — the state both live tie doors now
        # refuse, written straight to the table because no API can create it.
        produced = make_part(db_session, part_type="assembly", uom="each")
        source = make_work_order(db_session, part=part, quantity_ordered=10.0)
        operation = add_operation(db_session, source, wc, sequence=10)
        tie = make_tie(db_session, source, produced, operation=operation, qty_per_run=1.0, qty_planned=10.0)

        response = duplicate(client, headers_for(admin), source.id, quantity=10)
        assert response.status_code == status.HTTP_201_CREATED, response.text

        copy = created_work_order(db_session, response)
        assert ties_of(db_session, copy) == [], "the produced-part tie must not propagate"

        [skipped] = response.json()["skipped_material_allocations"]
        assert skipped["reason"] == "part_not_tieable"
        assert skipped["source_allocation_id"] == tie.id
        assert skipped["part_id"] == produced.id
        assert skipped["source_work_order_operation_id"] == operation.id

    def test_the_skip_reaches_the_audit_chain_as_well_as_the_response(self, client: TestClient, db_session: Session):
        """Both channels, per the module docstring — a skip on one of them is half a skip.

        The response tells the planner now; the chain is what an auditor reads later, when
        the question is why the duplicate carries less material demand than its source.
        """
        admin = make_user(db_session)
        part = make_part(db_session)
        produced = make_part(db_session, part_type="manufactured", uom="each")
        source = make_work_order(db_session, part=part, quantity_ordered=10.0)
        make_tie(db_session, source, produced, qty_planned=10.0)

        response = duplicate(client, headers_for(admin), source.id, quantity=10)
        assert response.status_code == status.HTTP_201_CREATED, response.text

        copy = created_work_order(db_session, response)
        row = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.resource_type == "work_order",
                AuditLog.resource_id == copy.id,
                AuditLog.action == "CREATE",
            )
            .one()
        )
        reasons = [entry["reason"] for entry in row.extra_data["skipped_material_allocations"]]
        assert reasons == ["part_not_tieable"]

    def test_a_material_tie_alongside_it_still_copies(self, client: TestClient, db_session: Session):
        """The negative control: one bad tie is dropped, the good ones are not.

        Without this, a copier that had simply stopped copying ties at all would satisfy
        the assertions above.
        """
        admin = make_user(db_session)
        part = make_part(db_session)
        produced = make_part(db_session, part_type="assembly", uom="each")
        sheet = make_part(db_session, part_type="raw_material", uom="sheets")
        source = make_work_order(db_session, part=part, quantity_ordered=10.0)
        make_tie(db_session, source, produced, qty_planned=10.0)
        make_tie(db_session, source, sheet, qty_planned=4.0)

        response = duplicate(client, headers_for(admin), source.id, quantity=10)
        assert response.status_code == status.HTTP_201_CREATED, response.text

        copy = created_work_order(db_session, response)
        assert [tie.part_id for tie in ties_of(db_session, copy)] == [sheet.id]
        assert [entry["part_id"] for entry in response.json()["skipped_material_allocations"]] == [produced.id]

    def test_the_manual_door_refuses_the_same_part_the_copier_now_skips(self, client: TestClient, db_session: Session):
        """The two doors agree, in the two shapes they are each allowed to answer in.

        Same part, same company: the tie endpoint refuses 422 and the copier drops-and-
        reports. Neither produces the row, which is the property that matters.
        """
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="machining")
        part = make_part(db_session)
        produced = make_part(db_session, part_type="assembly", uom="each")
        # IN_PROGRESS, not this file's COMPLETE default: the tie endpoint refuses a
        # TERMINAL work order with a 409 before it ever resolves the part, which would
        # make this test pass for the wrong reason.
        source = make_work_order(db_session, part=part, quantity_ordered=10.0, status_value=WorkOrderStatus.IN_PROGRESS)
        add_operation(db_session, source, wc, sequence=10)

        refused = client.post(
            f"/api/v1/work-orders/{source.id}/material-allocations",
            headers=headers_for(admin),
            json={"part_id": produced.id, "source": "manual", "qty_planned": 10.0},
        )
        assert refused.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, refused.text
        assert ties_of(db_session, source) == []


# --------------------------------------------------------------------------- #
# The traveler — process-sheet step snapshots
# --------------------------------------------------------------------------- #
class TestProcessSheetStepsAreReSnapshotted:
    """The single highest-consequence thing this feature does.

    ``wo_operation_steps`` is the immutable per-operation snapshot of a released process
    sheet, and it is the INPUT to the operation-completion gate
    (``process_sheet_service.missing_required_steps``). That gate returns ``[]`` — complete
    freely — for an operation with ZERO snapshot steps, which is the right answer for work
    that predates process sheets and the WRONG answer for a duplicate.

    A duplicate carrying no steps would complete with no measurement, no SPC point, no
    gauge attribution, no OOT->NCR path and nothing to pre-fill the AS9102 FAI — on a job
    whose entire premise is "same plan as last time". Note that the deliberate escape
    hatch, force-complete, stamps the very steps it bypasses onto its audit row; a
    duplicate with no steps bypassed the same gate leaving no record at all.

    And it is RE-snapshotted, never copied: the duplicate is a future work order, and the
    settled snapshot semantics are that releasing Rev B flows to future work orders.
    """

    def _source_with_a_traveler(self, db: Session, *, steps=None, sheet_number: str = None):
        wc = make_work_center(db, wc_type="machining")
        part = make_part(db)
        source = make_work_order(db, part=part, quantity_ordered=10.0, status_value=WorkOrderStatus.RELEASED)
        operation = add_operation(db, source, wc, sequence=10, status=OperationStatus.READY)
        sheet = make_process_sheet(
            db,
            sheet_number=sheet_number,
            revision="A",
            steps=steps
            or [
                {"label": "Bore dia", "step_type": "measurement", "config": dict(MEASUREMENT_CONFIG)},
                {"label": "Torque verified", "step_type": "checkbox"},
            ],
        )
        snapshot_onto(db, operation, sheet)
        return source, operation, sheet

    def test_the_duplicated_operation_carries_its_own_snapshot_of_every_step(
        self, client: TestClient, db_session: Session
    ):
        admin = make_user(db_session)
        source, source_operation, sheet = self._source_with_a_traveler(db_session)

        resp = duplicate(client, headers_for(admin), source.id, quantity=10)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        [copied_operation] = operations_of(db_session, new_wo)
        copied_steps = steps_of(db_session, copied_operation)

        assert [step.label for step in copied_steps] == ["Bore dia", "Torque verified"]
        assert [step.step_type for step in copied_steps] == ["measurement", "checkbox"]
        assert all(step.is_required for step in copied_steps)
        assert copied_steps[0].config == dict(MEASUREMENT_CONFIG)
        assert [step.sequence for step in copied_steps] == [10, 20]
        assert all(step.source_sheet_id == sheet.id for step in copied_steps)
        assert all(step.source_sheet_revision == "A" for step in copied_steps)
        assert all(step.company_id == COMPANY_A for step in copied_steps)
        # Fresh rows on the DUPLICATE's operation, not the source's shared with it.
        source_step_ids = {step.id for step in steps_of(db_session, source_operation)}
        assert {step.id for step in copied_steps}.isdisjoint(source_step_ids)
        assert all(step.work_order_operation_id == copied_operation.id for step in copied_steps)

    def test_it_snapshots_the_CURRENTLY_released_revision_not_the_source_s(
        self, client: TestClient, db_session: Session
    ):
        """Rev B released since the source ran? The duplicate is a future WO — it gets B.

        Copying the source's rows verbatim would freeze a revision that has since been
        superseded, which is the opposite of what releasing a revision is for.
        """
        admin = make_user(db_session)
        family = f"DUP-PS-FAMILY-{_next()}"
        source, _source_operation, rev_a = self._source_with_a_traveler(
            db_session,
            sheet_number=family,
            steps=[{"label": "Old check", "step_type": "checkbox"}],
        )
        # The shop released Rev B and obsoleted Rev A.
        rev_a.status = "obsolete"
        db_session.commit()
        make_process_sheet(
            db_session,
            sheet_number=family,
            revision="B",
            sheet_status="released",
            steps=[
                {"label": "New check", "step_type": "checkbox"},
                {"label": "Added in B", "step_type": "checkbox", "sequence": 20},
            ],
        )

        resp = duplicate(client, headers_for(admin), source.id, quantity=10)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        [copied_operation] = operations_of(db_session, new_wo)
        copied_steps = steps_of(db_session, copied_operation)
        assert [step.label for step in copied_steps] == ["New check", "Added in B"]
        assert all(step.source_sheet_revision == "B" for step in copied_steps)

    def test_a_family_with_no_released_revision_REFUSES_the_whole_duplicate(
        self, client: TestClient, db_session: Session
    ):
        """409 ``PROCESS_SHEET_UNAVAILABLE`` — byte-identical to what ``create_work_order`` raises.

        A refusal rather than a skip: an operation silently missing its traveler is an
        operation that completes without evidence, which is the failure this whole
        re-snapshot exists to prevent.
        """
        admin = make_user(db_session)
        source, _operation, sheet = self._source_with_a_traveler(
            db_session, steps=[{"label": "Torque verified", "step_type": "checkbox"}]
        )
        sheet.status = "obsolete"
        db_session.commit()
        wo_count = db_session.query(WorkOrder).count()
        step_count = db_session.query(WOOperationStep).count()
        audit_count = db_session.query(AuditLog).count()

        resp = duplicate(client, headers_for(admin), source.id, quantity=10)
        assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
        detail = resp.json()["detail"]
        assert detail["code"] == "PROCESS_SHEET_UNAVAILABLE"
        assert detail["sheet_number"] == sheet.sheet_number
        assert detail["operation"] == "OP10"

        # Nothing survives the refusal — not the header, not the operations.
        db_session.rollback()
        db_session.expire_all()
        assert db_session.query(WorkOrder).count() == wo_count
        assert db_session.query(WOOperationStep).count() == step_count
        assert db_session.query(AuditLog).count() == audit_count

    def test_an_operation_with_no_traveler_gets_none_and_does_not_fail(self, client: TestClient, db_session: Session):
        """Zero steps is correct for work that predates process sheets."""
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="machining")
        part = make_part(db_session)
        source = make_work_order(db_session, part=part, quantity_ordered=10.0)
        add_operation(db_session, source, wc, sequence=10)

        resp = duplicate(client, headers_for(admin), source.id, quantity=10)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        [copied_operation] = operations_of(db_session, new_wo)
        assert steps_of(db_session, copied_operation) == []
        [row] = audit_rows(db_session, "work_order", new_wo.id)
        assert row.extra_data["process_sheet_snapshot"] == []

    def test_the_resolved_revisions_are_recorded_on_the_chain(self, client: TestClient, db_session: Session):
        """Same key and shape ``create_work_order`` stamps — how an auditor sees the
        duplicate's traveler may differ from the source's."""
        admin = make_user(db_session)
        source, _operation, sheet = self._source_with_a_traveler(db_session)

        resp = duplicate(client, headers_for(admin), source.id, quantity=10)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        [row] = audit_rows(db_session, "work_order", new_wo.id)
        [summary] = row.extra_data["process_sheet_snapshot"]
        assert summary["sheet_number"] == sheet.sheet_number
        assert summary["resolved_sheet_id"] == sheet.id
        assert summary["resolved_revision"] == "A"
        assert summary["step_count"] == 2
        assert summary["operation"] == "OP10"

    def test_the_duplicate_refuses_to_complete_without_records_exactly_as_the_source_does(
        self, client: TestClient, db_session: Session
    ):
        """THE test. A duplicated operation must be gated identically to its source.

        Before the re-snapshot landed, a source operation that refused completion without
        a conforming gauged measurement produced a duplicate that completed with nothing
        at all — the gate silently disarmed on a job whose premise is "same plan".
        """
        admin = make_user(db_session)
        source, source_operation, _sheet = self._source_with_a_traveler(
            db_session, steps=[{"label": "Torque verified", "step_type": "checkbox"}]
        )

        # The SOURCE refuses — the behavior the duplicate has to reproduce.
        source_refusal = client.post(
            f"/api/v1/work-orders/operations/{source_operation.id}/complete?quantity_complete=10",
            headers=headers_for(admin),
        )
        assert source_refusal.status_code == status.HTTP_409_CONFLICT, source_refusal.text
        assert source_refusal.json()["detail"]["code"] == "STEPS_INCOMPLETE"

        resp = duplicate(client, headers_for(admin), source.id, quantity=10)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text
        new_wo = created_work_order(db_session, resp)
        [copied_operation] = operations_of(db_session, new_wo)
        [copied_step] = steps_of(db_session, copied_operation)

        # Put the copy in the state a released job's operation is in.
        new_wo.status = WorkOrderStatus.RELEASED
        copied_operation.status = OperationStatus.READY
        db_session.commit()

        refusal = client.post(
            f"/api/v1/work-orders/operations/{copied_operation.id}/complete?quantity_complete=10",
            headers=headers_for(admin),
        )
        assert refusal.status_code == status.HTTP_409_CONFLICT, refusal.text
        detail = refusal.json()["detail"]
        assert detail["code"] == "STEPS_INCOMPLETE"
        # Named against the DUPLICATE's own step row, not the source's.
        assert detail["missing"] == [{"step_id": copied_step.id, "label": "Torque verified", "serials": []}]

        db_session.expire_all()
        assert copied_operation.status == OperationStatus.READY, "the refusal must not mutate the operation"


# --------------------------------------------------------------------------- #
# Invariant 1 — tenant isolation
# --------------------------------------------------------------------------- #
class TestTenantIsolation:
    def test_another_companys_work_order_is_404_and_creates_nothing(self, client: TestClient, db_session: Session):
        """404, never 403 — a 403 would confirm the id exists somewhere."""
        caller = make_user(db_session, company_id=COMPANY_A)
        foreign_source, _wc, _ops, _nests, _package = build_laser_source(db_session, company_id=COMPANY_B)
        wo_count = db_session.query(WorkOrder).count()
        op_count = db_session.query(WorkOrderOperation).count()
        nest_count = db_session.query(LaserNest).count()

        resp = duplicate(client, headers_for(caller), foreign_source.id, quantity=8)
        assert resp.status_code == status.HTTP_404_NOT_FOUND
        assert resp.json()["detail"] == "Work order not found"

        db_session.expire_all()
        assert db_session.query(WorkOrder).count() == wo_count
        assert db_session.query(WorkOrderOperation).count() == op_count
        assert db_session.query(LaserNest).count() == nest_count
        assert db_session.query(LaserNestPackage).filter(LaserNestPackage.company_id == COMPANY_A).count() == 0

    def test_every_row_the_copy_creates_is_tagged_with_the_active_company(
        self, client: TestClient, db_session: Session
    ):
        caller = make_user(db_session, company_id=COMPANY_B)
        wc = make_work_center(db_session, company_id=COMPANY_B, wc_type="laser")
        part = make_part(db_session, company_id=COMPANY_B)
        sheet = make_part(db_session, company_id=COMPANY_B, part_type="raw_material", uom="sheets")
        source = make_work_order(db_session, company_id=COMPANY_B, part=part, quantity_ordered=4.0)
        package = make_package(db_session, source, company_id=COMPANY_B)
        operation = add_operation(db_session, source, wc, company_id=COMPANY_B, sequence=10)
        attach_nest(db_session, package, operation, company_id=COMPANY_B, planned_runs=4)
        make_tie(db_session, source, sheet, company_id=COMPANY_B, operation=operation, qty_per_run=1.0)

        resp = duplicate(client, headers_for(caller), source.id, quantity=4)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        assert new_wo.company_id == COMPANY_B
        assert {op.company_id for op in operations_of(db_session, new_wo)} == {COMPANY_B}
        assert {nest.company_id for nest in nests_of(db_session, new_wo)} == {COMPANY_B}
        assert {tie.company_id for tie in ties_of(db_session, new_wo)} == {COMPANY_B}
        new_package = db_session.query(LaserNestPackage).filter(LaserNestPackage.id != package.id).one()
        assert new_package.company_id == COMPANY_B
        assert all(row.company_id == COMPANY_B for row in audit_rows(db_session, "work_order", new_wo.id))

    def test_a_cross_tenant_operation_on_the_source_is_not_copied(self, client: TestClient, db_session: Session):
        """The service re-scopes every read even though the caller already scoped the source.

        A duplicate that pulled another tenant's operation would be a security defect,
        not a copy bug — so the drift case is pinned rather than assumed unreachable.
        """
        admin = make_user(db_session, company_id=COMPANY_A)
        _ensure_company(db_session, COMPANY_B)
        wc = make_work_center(db_session, wc_type="machining")
        foreign_wc = make_work_center(db_session, company_id=COMPANY_B, wc_type="machining")
        part = make_part(db_session)
        source = make_work_order(db_session, part=part, quantity_ordered=10.0)
        add_operation(db_session, source, wc, sequence=10, name="Ours")
        add_operation(db_session, source, foreign_wc, company_id=COMPANY_B, sequence=20, name="Theirs")

        resp = duplicate(client, headers_for(admin), source.id, quantity=10)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        new_wo = created_work_order(db_session, resp)
        copied = operations_of(db_session, new_wo)
        assert [op.name for op in copied] == ["Ours"]


# --------------------------------------------------------------------------- #
# Invariant 2 — audit
# --------------------------------------------------------------------------- #
class TestAuditChain:
    def test_the_work_order_row_carries_the_source_lineage(self, client: TestClient, db_session: Session):
        """The audit row is the ONLY place the derivation exists — the copy carries no FK back."""
        admin = make_user(db_session)
        source, _wc, _ops, _nests, _package = build_laser_source(db_session, runs=(3, 5))

        resp = duplicate(client, headers_for(admin), source.id, quantity=8)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text
        new_wo = created_work_order(db_session, resp)

        [row] = audit_rows(db_session, "work_order", new_wo.id)
        assert row.action == "CREATE"
        assert row.user_id == admin.id
        assert row.company_id == COMPANY_A
        assert row.resource_identifier == new_wo.work_order_number
        assert source.work_order_number in row.description
        assert new_wo.work_order_number in row.description
        assert row.extra_data["source"] == "work_order_duplicate"
        assert row.extra_data["source_work_order_id"] == source.id
        assert row.extra_data["source_work_order_number"] == source.work_order_number
        assert row.extra_data["operation_count"] == 2
        assert row.extra_data["laser_nest_count"] == 2
        assert row.extra_data["material_allocation_count"] == 0
        assert row.extra_data["skipped_material_allocations"] == []
        # The duplicate carries no FK to its source, so nothing on the row itself says so.
        assert new_wo.parent_work_order_id is None

    def test_every_nest_and_every_tie_gets_its_own_create_row(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="laser")
        part = make_part(db_session)
        sheet = make_part(db_session, part_type="raw_material", uom="sheets")
        source = make_work_order(db_session, part=part, quantity_ordered=7.0)
        package = make_package(db_session, source)
        op_a = add_operation(db_session, source, wc, sequence=10)
        op_b = add_operation(db_session, source, wc, sequence=20)
        attach_nest(db_session, package, op_a, planned_runs=3)
        attach_nest(db_session, package, op_b, planned_runs=4)
        make_tie(db_session, source, sheet, operation=op_a, qty_per_run=1.0)
        make_tie(db_session, source, sheet, operation=op_b, qty_per_run=2.0)
        nest_rows_before = len(audit_rows(db_session, "laser_nest"))
        tie_rows_before = len(audit_rows(db_session, "work_order_material_allocation"))

        resp = duplicate(client, headers_for(admin), source.id, quantity=7)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text
        new_wo = created_work_order(db_session, resp)

        new_nest_ids = {nest.id for nest in nests_of(db_session, new_wo)}
        new_tie_ids = {tie.id for tie in ties_of(db_session, new_wo)}
        nest_rows = [row for row in audit_rows(db_session, "laser_nest") if row.resource_id in new_nest_ids]
        tie_rows = [
            row for row in audit_rows(db_session, "work_order_material_allocation") if row.resource_id in new_tie_ids
        ]
        assert len(nest_rows) == 2
        assert len(tie_rows) == 2
        assert len(audit_rows(db_session, "laser_nest")) == nest_rows_before + 2
        assert len(audit_rows(db_session, "work_order_material_allocation")) == tie_rows_before + 2

        for row in nest_rows:
            assert row.action == "CREATE"
            assert row.extra_data["source"] == "work_order_duplicate"
            assert row.extra_data["source_work_order_id"] == source.id
            assert row.extra_data["source_work_order_number"] == source.work_order_number
            assert row.extra_data["child_work_order_id"] == new_wo.id
        for row in tie_rows:
            assert row.action == "CREATE"
            assert row.extra_data["work_order_id"] == new_wo.id
            assert row.extra_data["duplicated_from_work_order_id"] == source.id
            assert row.extra_data["duplicated_from_work_order_number"] == source.work_order_number
            assert row.extra_data["pinned_inventory_item_id"] is None

    def test_the_nest_row_records_that_the_drawing_is_shared_not_re_uploaded(
        self, client: TestClient, db_session: Session
    ):
        admin = make_user(db_session)
        source, _wc, _ops, _nests, _package = build_laser_source(db_session, runs=(3,), with_document=True)

        resp = duplicate(client, headers_for(admin), source.id, quantity=3)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text
        new_wo = created_work_order(db_session, resp)

        [nest] = nests_of(db_session, new_wo)
        [row] = audit_rows(db_session, "laser_nest", nest.id)
        assert row.extra_data["document_shared_with_source"] is True
        assert row.new_values["document_id"] == nest.document_id

    def test_every_row_is_written_through_the_hash_chain_never_inserted_directly(
        self, client: TestClient, db_session: Session
    ):
        """A direct INSERT could not produce a sequence number or a chain hash.

        ``AuditService`` is the only writer that allocates ``sequence_number`` and
        computes ``integrity_hash``/``previous_hash``, so a row lacking them did not go
        through it. (The DB-level 008/060 triggers already refuse UPDATE/DELETE; this
        pins the INSERT side for the rows this feature adds.)
        """
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="laser")
        part = make_part(db_session)
        sheet = make_part(db_session, part_type="raw_material", uom="sheets")
        source = make_work_order(db_session, part=part, quantity_ordered=3.0)
        package = make_package(db_session, source)
        operation = add_operation(db_session, source, wc, sequence=10)
        attach_nest(db_session, package, operation, planned_runs=3)
        make_tie(db_session, source, sheet, operation=operation, qty_per_run=1.0)
        before = {row.id for row in db_session.query(AuditLog).all()}

        resp = duplicate(client, headers_for(admin), source.id, quantity=3)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        db_session.expire_all()
        new_rows = [row for row in db_session.query(AuditLog).all() if row.id not in before]
        assert {row.resource_type for row in new_rows} == {
            "work_order",
            "laser_nest",
            "work_order_material_allocation",
        }
        for row in new_rows:
            assert row.sequence_number is not None
            assert row.integrity_hash
            assert len(row.integrity_hash) == 64
            assert row.company_id == COMPANY_A
            assert row.user_id == admin.id


# --------------------------------------------------------------------------- #
# Invariant 3 — soft delete
# --------------------------------------------------------------------------- #
class TestSoftDeletedSourcesAreInvisible:
    def test_a_soft_deleted_work_order_is_404(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        source, _wc, _ops, _nests, _package = build_laser_source(db_session, runs=(3,))
        source.is_deleted = True
        source.deleted_at = datetime.utcnow()
        db_session.commit()
        wo_count = db_session.query(WorkOrder).count()

        resp = duplicate(client, headers_for(admin), source.id, quantity=3)
        assert resp.status_code == status.HTTP_404_NOT_FOUND
        db_session.expire_all()
        assert db_session.query(WorkOrder).count() == wo_count


# --------------------------------------------------------------------------- #
# RBAC
# --------------------------------------------------------------------------- #
class TestRoleGate:
    @pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])
    def test_the_planning_roles_may_duplicate(self, client: TestClient, db_session: Session, role: UserRole):
        user = make_user(db_session, role=role)
        source, _wc, _ops, _nests, _package = build_laser_source(db_session, runs=(3,))

        resp = duplicate(client, headers_for(user), source.id, quantity=3)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

    @pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.VIEWER, UserRole.QUALITY, UserRole.SHIPPING])
    def test_everyone_else_is_refused_and_nothing_is_created(
        self, client: TestClient, db_session: Session, role: UserRole
    ):
        user = make_user(db_session, role=role)
        source, _wc, _ops, _nests, _package = build_laser_source(db_session, runs=(3,))
        wo_count = db_session.query(WorkOrder).count()

        resp = duplicate(client, headers_for(user), source.id, quantity=3)
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        db_session.expire_all()
        assert db_session.query(WorkOrder).count() == wo_count
        assert db_session.query(AuditLog).filter(AuditLog.resource_type == "work_order").count() == 0

    def test_an_unauthenticated_caller_is_refused(self, client: TestClient, db_session: Session):
        make_user(db_session)
        source, _wc, _ops, _nests, _package = build_laser_source(db_session, runs=(3,))

        resp = client.post(f"/api/v1/work-orders/{source.id}/duplicate", json={"quantity_ordered": 3})
        assert resp.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)


# --------------------------------------------------------------------------- #
# Atomicity
# --------------------------------------------------------------------------- #
class TestAtomicity:
    def test_a_constraint_fault_mid_copy_leaves_nothing_behind_and_answers_409(
        self, client: TestClient, db_session: Session
    ):
        """A header without its nests is a plan the planner never approved.

        The fault is provoked the way the app could actually meet it: the duplicate puts
        every source nest on ONE package, so two source nests that lived on DIFFERENT
        packages under the same ``(nest_name, cnc_file_name)`` collide on
        ``uq_laser_nests_package_file`` — after the header and the operations have
        already been flushed. That must surface as a 409 with nothing persisted, not a
        500 off a poisoned session and not a half-built job.
        """
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="laser")
        part = make_part(db_session)
        source = make_work_order(db_session, part=part, quantity_ordered=6.0)
        package_one = make_package(db_session, source, name="Package one")
        package_two = make_package(db_session, source, name="Package two")
        op_a = add_operation(db_session, source, wc, sequence=10)
        op_b = add_operation(db_session, source, wc, sequence=20)
        attach_nest(db_session, package_one, op_a, planned_runs=3, nest_name="NEST-1", cnc_file_name="05749.pdf")
        attach_nest(db_session, package_two, op_b, planned_runs=3, nest_name="NEST-1", cnc_file_name="05749.pdf")

        wo_count = db_session.query(WorkOrder).count()
        op_count = db_session.query(WorkOrderOperation).count()
        nest_count = db_session.query(LaserNest).count()
        package_count = db_session.query(LaserNestPackage).count()
        audit_count = db_session.query(AuditLog).count()

        resp = duplicate(client, headers_for(admin), source.id, quantity=6)
        assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
        assert "conflicts" in resp.json()["detail"]

        db_session.expire_all()
        assert db_session.query(WorkOrder).count() == wo_count, "no partial work order may survive"
        assert db_session.query(WorkOrderOperation).count() == op_count
        assert db_session.query(LaserNest).count() == nest_count
        assert db_session.query(LaserNestPackage).count() == package_count
        assert db_session.query(AuditLog).count() == audit_count, "the chain must not record a job that does not exist"

    def test_the_session_survives_the_refusal(self, client: TestClient, db_session: Session):
        """Nothing was committed, so the very next request must work normally."""
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="laser")
        part = make_part(db_session)
        poisoned = make_work_order(db_session, part=part, quantity_ordered=6.0)
        package_one = make_package(db_session, poisoned, name="Package one")
        package_two = make_package(db_session, poisoned, name="Package two")
        op_a = add_operation(db_session, poisoned, wc, sequence=10)
        op_b = add_operation(db_session, poisoned, wc, sequence=20)
        attach_nest(db_session, package_one, op_a, planned_runs=3, nest_name="NEST-1", cnc_file_name="a.pdf")
        attach_nest(db_session, package_two, op_b, planned_runs=3, nest_name="NEST-1", cnc_file_name="a.pdf")
        healthy, _wc, _ops, _nests, _package = build_laser_source(db_session, runs=(4,))

        assert duplicate(client, headers_for(admin), poisoned.id, quantity=6).status_code == 409

        resp = duplicate(client, headers_for(admin), healthy.id, quantity=4)
        assert resp.status_code == status.HTTP_201_CREATED, resp.text


# --------------------------------------------------------------------------- #
# Repeatability
# --------------------------------------------------------------------------- #
class TestDuplicatingTwice:
    def test_two_duplicates_are_two_independent_jobs(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        wc = make_work_center(db_session, wc_type="laser")
        part = make_part(db_session)
        sheet = make_part(db_session, part_type="raw_material", uom="sheets")
        source = make_work_order(db_session, part=part, quantity_ordered=5.0)
        package = make_package(db_session, source)
        operation = add_operation(db_session, source, wc, sequence=10)
        attach_nest(db_session, package, operation, planned_runs=5)
        make_tie(db_session, source, sheet, operation=operation, qty_per_run=1.0)

        first = duplicate(client, headers_for(admin), source.id, quantity=5)
        second = duplicate(client, headers_for(admin), source.id, quantity=5)
        assert first.status_code == status.HTTP_201_CREATED, first.text
        assert second.status_code == status.HTTP_201_CREATED, second.text

        first_wo = created_work_order(db_session, first)
        second_wo = created_work_order(db_session, second)
        assert first_wo.id != second_wo.id
        assert first_wo.work_order_number != second_wo.work_order_number

        first_nests = nests_of(db_session, first_wo)
        second_nests = nests_of(db_session, second_wo)
        assert len(first_nests) == len(second_nests) == 1
        assert {nest.id for nest in first_nests}.isdisjoint({nest.id for nest in second_nests})
        assert first_nests[0].package_id != second_nests[0].package_id
        # Both point at the SOURCE's drawing rather than each other's.
        assert first_nests[0].document_id == second_nests[0].document_id

        first_ties = ties_of(db_session, first_wo)
        second_ties = ties_of(db_session, second_wo)
        assert len(first_ties) == len(second_ties) == 1
        assert first_ties[0].id != second_ties[0].id
        # The partial unique index tolerates both because their scopes differ.
        assert first_ties[0].work_order_operation_id != second_ties[0].work_order_operation_id

        # The source is untouched by either copy.
        db_session.refresh(source)
        assert source.quantity_ordered == 5.0
        assert len(nests_of(db_session, source)) == 1
