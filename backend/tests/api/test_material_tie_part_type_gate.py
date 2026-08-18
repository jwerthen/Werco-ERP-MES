"""The part-type gate on the material tie: a produced part may never BE material.

WHY THIS FILE EXISTS. A ``work_order_material_allocations`` row is standing demand the
consumption engine draws against (invariant 6): completing the work order — or one of its
operations — posts an ISSUE for the tied part, FIFO-picks its lots and writes them onto
the as-built record. Nothing downstream distinguishes that from a legitimate material
draw, and consumption **never auto-reverses**, so a tie pointing at a MANUFACTURED part or
an ASSEMBLY makes a job quietly eat finished goods to build itself, remediable only by a
reasoned compensating transaction against stock that should never have moved.

``services/material_tie_part_gate.assert_part_is_tieable_material`` refuses that at tie
time — the last moment an actor with intent is present. This file covers the MANUAL door
over HTTP; ``tests/services/test_material_tie_part_gate.py`` is the predicate's own truth
table (every part type, including the inputs no endpoint can produce, plus the assertion
that both endpoint modules bind the identical callable), and
``tests/api/test_material_tie_nest_import.py`` covers the two laser-nest doors. Three
properties are pinned here, in the order they matter:

1. **A refusal is not a state change.** No allocation row, no audit row — the gate runs
   immediately after the part resolve and BEFORE the first mutation, so a refused request
   is byte-identical to one that never arrived (invariant 2: the tamper-evident chain must
   not carry a row describing something that did not happen).

2. **It refuses a ROLE, not an existence.** 422, not 404 — the part is this tenant's own
   row and the caller is entitled to see it. That is only safe because the gate runs
   strictly AFTER the tenant-scoped resolve, so a cross-tenant id still 404s and the
   status code can never be used to probe another company's catalog (invariant 1). §1
   asserts that ordering directly, on both the cross-tenant and the soft-deleted miss.

3. **All FOUR material/supply types still tie.** The gate refuses PRODUCED parts, not
   "everything that is not raw stock" — ``purchased`` / ``hardware`` / ``consumable`` are
   bought and are genuinely consumed by jobs (hardware into an assembly, weld wire at the
   machine). §1 asserts each one, because a gate that quietly narrowed to ``raw_material``
   would break real ties while looking stricter. Narrowing the PICKERS toward raw stock is
   the frontend's default-with-an-escape-hatch; narrowing what may be WRITTEN to what
   cannot be nonsense is this gate's.

§2 covers the OTHER half of the same hole, and it is a distinct rule rather than a
restatement: ``PUT /parts/{id}`` resolves any part in the company and then forces
``part_type`` into ``ENGINEERING_PART_TYPES``, so a ``raw_material`` part already carrying
live ties could be CONVERTED into a produced part — arriving at the state §1 forbids by
doing the same two steps in the other order, and past a gate that never runs again once a
tie exists. ``assert_part_type_change_allowed`` refuses that **409**, keyed on the CLASS
CHANGE and on standing LIVE demand, not on which door was used.

"Live" is two conditions and §2 covers both. The tie must be OPEN, **and its work order
must not have finished**. The second half is not a refinement — it is what makes the 409's
own promise ("succeeds once those ties are untied") true. Nothing in ``app/`` ever writes
``AllocationStatus.CLOSED``, and completing a work order neither closes nor cancels its
ties, so **every tie a part has carried stays OPEN forever once its job ships**. Counting
those would block the ordinary "we used to buy this, now we make it" reclassification
permanently on any consumed part, while naming a remedy nobody can perform: ``DELETE`` on
the tie 409s once the ledger shows material issued, and ``return_and_untie`` would credit a
shipped job's material back into stock, falsifying its as-built record (invariants 5 and
6b) for a catalog edit. It would also contradict this module's own neighbour, which already
refuses a NEW tie on a terminal work order because a tie that can never consume is a lie.

The narrowing that is deliberately NOT how this is done — 404-ing every material row out of
``PUT /parts/{id}`` — is pinned as rejected in
``test_a_tied_material_part_is_still_editable_in_every_other_way`` and
``test_the_parts_router_still_resolves_a_material_row_on_every_handler``: BOM component
drill-throughs edit ``purchased`` / ``raw_material`` rows from the parts page, and pushing
them to the Materials screen — which force-sends ``revision`` and ``is_critical`` on every
save — would reset revisions and clear critical-characteristic flags (invariant 5).

§3 covers ``MaterialAllocationResponse.part_type``, which exists so the tie table can
FLAG a legacy tie the gate would refuse today. Note the field is a plain lowercase string
and may legitimately read ``"manufactured"`` — that is the whole point of it, so a test
asserting it is always a material type would be asserting a bug.

§4 covers the THIRD order of the same two steps, which walked past both §1 and §2 with no
code change at all: soft-DELETE the work order (which CANCELs its ties, so §2's live-demand
count drops to zero), reclassify the part, then RESTORE — and the restore re-opened the tie
against a part that had become MANUFACTURED. The refusal now lives at the restore, which is
the moment the demand becomes live again and the last one with an actor present; §4 asserts
the whole sequence, the response envelope that names the refused tie, its audit row, and the
control case where an ordinary raw-stock tie still comes back OPEN.
"""

from datetime import date, timedelta
from typing import Optional

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.orm import Session

import app.api.endpoints.work_order_materials as wo_materials_endpoint
from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.part import Part, PartType, normalize_part_type_value
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_material import AllocationStatus, WorkOrderMaterialAllocation

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _ensure_company(db: Session, company_id: int = COMPANY_A) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True)
        db.add(company)
        db.commit()
    return company


def make_user(db: Session, *, role: UserRole = UserRole.ADMIN, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"mtg-{n}@co{company_id}.test",
        employee_id=f"MTG-{n:05d}",
        first_name="Tie",
        last_name=f"Gate{n}",
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


def make_part(
    db: Session,
    *,
    part_type: str = "raw_material",
    uom: str = "each",
    is_deleted: bool = False,
    company_id: int = COMPANY_A,
) -> Part:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"MTG-P-{n:05d}",
        name=f"Part {n}",
        description="material-tie part-type gate fixture",
        part_type=part_type,
        unit_of_measure=uom,
        standard_cost=3.0,
        is_active=True,
        is_deleted=is_deleted,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_work_center(db: Session, *, company_id: int = COMPANY_A) -> WorkCenter:
    _ensure_company(db, company_id)
    n = _next()
    wc = WorkCenter(
        name=f"MTG-WC-{n}",
        code=f"MTG-WC-{n}",
        work_center_type="laser",
        hourly_rate=100,
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def make_wo(
    db: Session,
    part: Part,
    *,
    company_id: int = COMPANY_A,
    wo_status: WorkOrderStatus = WorkOrderStatus.IN_PROGRESS,
) -> WorkOrder:
    n = _next()
    wo = WorkOrder(
        work_order_number=f"MTG-WO-{n:05d}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=10,
        status=wo_status,
        priority=5,
        due_date=date.today() + timedelta(days=30),
        company_id=company_id,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def make_op(db: Session, wo: WorkOrder, wc: WorkCenter, *, company_id: int = COMPANY_A) -> WorkOrderOperation:
    n = _next()
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id,
        sequence=10,
        operation_number="10",
        name=f"Op {n}",
        status=OperationStatus.IN_PROGRESS,
        quantity_complete=0,
        quantity_scrapped=0,
        company_id=company_id,
    )
    db.add(op)
    db.commit()
    db.refresh(op)
    return op


def tie_url(wo_id: int, allocation_id: int = None) -> str:
    base = f"/api/v1/work-orders/{wo_id}/material-allocations"
    return f"{base}/{allocation_id}" if allocation_id else base


def allocations(db: Session, *, company_id: int = COMPANY_A) -> list:
    return (
        db.query(WorkOrderMaterialAllocation)
        .filter(WorkOrderMaterialAllocation.company_id == company_id)
        .order_by(WorkOrderMaterialAllocation.id)
        .all()
    )


def audit_rows(db: Session, *, company_id: int = COMPANY_A) -> list[AuditLog]:
    return db.query(AuditLog).filter(AuditLog.company_id == company_id).order_by(AuditLog.id).all()


def post_tie(client: TestClient, user: User, wo: WorkOrder, part: Part, **extra):
    body = {"part_id": part.id, "source": "manual", "qty_planned": 5.0}
    body.update(extra)
    return client.post(tie_url(wo.id), json=body, headers=headers_for(user))


# =========================================================================== #
# 1. THE MANUAL DOOR — POST /work-orders/{id}/material-allocations
# =========================================================================== #


@pytest.mark.parametrize("part_type", ["manufactured", "assembly"])
def test_tying_a_produced_part_is_422(client: TestClient, db_session: Session, part_type: str):
    admin = make_user(db_session)
    fg = make_part(db_session, part_type="manufactured")
    wo = make_wo(db_session, fg)
    produced = make_part(db_session, part_type=part_type)

    response = post_tie(client, admin, wo, produced)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, str), "a plain string, so the UI renders it verbatim"
    assert produced.part_number in detail
    assert "not stock material" in detail


@pytest.mark.parametrize("part_type", ["raw_material", "purchased", "hardware", "consumable"])
def test_tying_any_material_supply_part_still_succeeds(client: TestClient, db_session: Session, part_type: str):
    """The negative control for the test above, one case per admitted type.

    Without this, a gate that had simply broken tie creation outright would satisfy the
    refusal assertions just as well.
    """
    admin = make_user(db_session)
    fg = make_part(db_session, part_type="manufactured")
    wo = make_wo(db_session, fg)
    material = make_part(db_session, part_type=part_type, uom="sheets")

    response = post_tie(client, admin, wo, material)

    assert response.status_code == status.HTTP_201_CREATED, response.text
    assert response.json()["part_id"] == material.id
    [allocation] = allocations(db_session)
    assert allocation.part_id == material.id
    assert allocation.status == AllocationStatus.OPEN


def test_a_refused_tie_leaves_no_allocation_row_and_no_audit_row(client: TestClient, db_session: Session):
    """A refusal is not a state change — invariant 2.

    The gate runs immediately after the part resolve and BEFORE the first mutation, so a
    refused request must be indistinguishable from one that never arrived. The audit half
    matters as much as the allocation half: a chain row describing a tie that was refused
    would be a row describing something that did not happen, on a log whose whole value is
    that it only carries things that did.
    """
    admin = make_user(db_session)
    fg = make_part(db_session, part_type="manufactured")
    wo = make_wo(db_session, fg)
    op = make_op(db_session, wo, make_work_center(db_session))
    produced = make_part(db_session, part_type="assembly")

    audit_before = [row.id for row in audit_rows(db_session)]

    response = post_tie(client, admin, wo, produced, work_order_operation_id=op.id, qty_per_run=2.0)
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, response.text

    db_session.expire_all()
    assert allocations(db_session) == [], "a refused tie must not persist an allocation"
    assert [row.id for row in audit_rows(db_session)] == audit_before, "a refusal must not extend the audit chain"


def test_a_cross_tenant_produced_part_is_still_404_never_422(client: TestClient, db_session: Session):
    """422 is only safe because the gate runs AFTER the tenant-scoped resolve (invariant 1).

    If the type check ran first — or if the resolve were widened so the gate could see a
    part the caller may not read — the two status codes would become an existence oracle:
    404 for "no such id anywhere", 422 for "a produced part exists under that id in some
    company". Company B's part must stay a flat 404 regardless of its type.
    """
    admin_a = make_user(db_session, company_id=COMPANY_A)
    fg = make_part(db_session, part_type="manufactured", company_id=COMPANY_A)
    wo = make_wo(db_session, fg, company_id=COMPANY_A)
    foreign_produced = make_part(db_session, part_type="manufactured", company_id=COMPANY_B)

    response = post_tie(client, admin_a, wo, foreign_produced)

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert response.json()["detail"] == "Material part not found"
    assert allocations(db_session, company_id=COMPANY_A) == []


def test_a_soft_deleted_produced_part_is_404_not_422(client: TestClient, db_session: Session):
    """Same ordering argument one step further in: the soft-delete filter is part of the
    RESOLVE, so a tombstoned part is 404 (it is gone) rather than 422 (it is the wrong
    kind). Invariant 3 — the tie doors already excluded soft-deleted parts, and the new
    gate must not have moved that decision after itself.
    """
    admin = make_user(db_session)
    fg = make_part(db_session, part_type="manufactured")
    wo = make_wo(db_session, fg)
    deleted = make_part(db_session, part_type="manufactured", is_deleted=True)

    response = post_tie(client, admin, wo, deleted)

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert allocations(db_session) == []


def test_a_work_order_may_still_tie_material_that_is_its_OWN_finished_part_type_family(
    client: TestClient, db_session: Session
):
    """The gate keys on the TIED part, never on a relationship to the work order.

    Tying a ``purchased`` component of the very part being built is a normal, correct tie
    (that is what a bought sub-component IS). The refusal is about the tied part being
    something the shop produces, and nothing else — pinned so nobody "improves" the gate
    into a same-part or same-BOM comparison.
    """
    admin = make_user(db_session)
    fg = make_part(db_session, part_type="assembly")
    wo = make_wo(db_session, fg)
    bought_component = make_part(db_session, part_type="purchased")

    response = post_tie(client, admin, wo, bought_component)
    assert response.status_code == status.HTTP_201_CREATED, response.text


# =========================================================================== #
# 2. THE CONVERSION DOOR — PUT /parts/{id} vs PUT /materials/{id}
# =========================================================================== #


def put_part(client: TestClient, user: User, part_id: int, **fields):
    return client.put(f"/api/v1/parts/{part_id}", headers=headers_for(user), json={"version": 0, **fields})


def put_material(client: TestClient, user: User, part_id: int, **fields):
    return client.put(f"/api/v1/materials/{part_id}", headers=headers_for(user), json={"version": 0, **fields})


def make_tie(
    db: Session,
    wo: WorkOrder,
    part: Part,
    *,
    status_value: AllocationStatus = AllocationStatus.OPEN,
    operation: Optional[WorkOrderOperation] = None,
    company_id: int = COMPANY_A,
    qty_consumed: float = 0.0,
) -> WorkOrderMaterialAllocation:
    """A tie written straight to the table, so a test can set up a state (or a tombstone)
    the API would not currently mint."""
    allocation = WorkOrderMaterialAllocation(
        company_id=company_id,
        work_order_id=wo.id,
        work_order_operation_id=operation.id if operation is not None else None,
        part_id=part.id,
        source="manual",
        status=status_value,
        qty_per_run=1.0 if operation is not None else None,
        qty_planned=5.0,
        unit_of_measure="each",
        qty_consumed=qty_consumed,
    )
    db.add(allocation)
    db.commit()
    db.refresh(allocation)
    return allocation


@pytest.mark.parametrize("new_type", ["manufactured", "assembly"])
def test_reclassifying_a_TIED_material_part_into_a_produced_one_is_409(
    client: TestClient, db_session: Session, new_type: str
):
    """THE conversion door, gated — the second half of the same rule as §1.

    §1 refuses tying a produced part. This refuses the other order of the same two steps:
    tie sheet stock legitimately, then reclassify the sheet as something the shop
    PRODUCES. The end state is identical — an OPEN tie whose part is manufactured or an
    assembly, depleting finished goods at completion — and nothing re-checks a tie after
    it is made, so the reclassification is where it has to be caught.

    **409, not 422 and not 404.** The part type asked for is a valid value and the payload
    is well formed; what refuses it is state that already exists, and the identical request
    succeeds once the ties are untied. 404 would be the wrong answer twice over: it is not
    true (the part is right there, and the parts page is where BOM components are edited),
    and it would send the planner hunting for a missing record instead of untying a job.
    """
    admin = make_user(db_session)
    fg = make_part(db_session, part_type="manufactured")
    wo = make_wo(db_session, fg)
    sheet = make_part(db_session, part_type="raw_material", uom="sheets")

    assert post_tie(client, admin, wo, sheet).status_code == status.HTTP_201_CREATED
    [allocation] = allocations(db_session)

    response = put_part(client, admin, sheet.id, part_type=new_type)

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, str), "a plain string, so the UI renders it verbatim"
    assert sheet.part_number in detail
    assert (
        "1 unfinished work order still ties" in detail
    ), "the count is of WORK ORDERS, the unit of the remedy — and only of unfinished ones"
    assert "Untie" in detail, "the sentence names the remedy, not just the refusal"

    db_session.expire_all()
    assert db_session.get(Part, sheet.id).part_type == PartType.RAW_MATERIAL, "a refusal leaves the class untouched"
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).status == AllocationStatus.OPEN


def test_a_refused_conversion_writes_nothing_at_all(client: TestClient, db_session: Session):
    """Before the first ``setattr`` — so a refused PUT is byte-identical to one never sent.

    The gate is called alongside ``assert_backflush_change_allowed`` for exactly this
    reason. The audit half matters as much as the column half: a chain row describing an
    edit the server refused would be a row describing something that did not happen.
    Asserted with a SECOND field in the payload, because a gate that ran mid-loop would
    leave that one written while the class stayed put.
    """
    admin = make_user(db_session)
    fg = make_part(db_session, part_type="manufactured")
    wo = make_wo(db_session, fg)
    sheet = make_part(db_session, part_type="raw_material", uom="sheets")
    make_tie(db_session, wo, sheet)
    original_description = sheet.description

    audit_before = [row.id for row in audit_rows(db_session)]

    response = put_part(client, admin, sheet.id, part_type="assembly", description="should not be written either")
    assert response.status_code == status.HTTP_409_CONFLICT, response.text

    db_session.expire_all()
    reread = db_session.get(Part, sheet.id)
    assert reread.part_type == PartType.RAW_MATERIAL
    assert reread.description == original_description, "the gate runs BEFORE the first setattr"
    assert [row.id for row in audit_rows(db_session)] == audit_before, "a refusal must not extend the audit chain"


@pytest.mark.parametrize("part_type", ["raw_material", "purchased", "hardware", "consumable"])
def test_an_UNTIED_material_part_still_converts_freely(client: TestClient, db_session: Session, part_type: str):
    """The gate protects TIES, not classes — the negative control for the test above.

    A shop reclassifying a mis-typed catalog row is ordinary work, and there is no hazard
    without standing demand. Parametrised over all four material/supply types so a gate
    that had quietly become "material rows may never be promoted" fails here.
    """
    admin = make_user(db_session)
    material = make_part(db_session, part_type=part_type)

    response = put_part(client, admin, material.id, part_type="assembly")

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["part_type"] == "assembly"


@pytest.mark.parametrize("closed_status", [AllocationStatus.CANCELLED, AllocationStatus.CLOSED])
def test_a_tie_that_is_no_longer_OPEN_does_not_block_the_conversion(
    client: TestClient, db_session: Session, closed_status: AllocationStatus
):
    """``status`` IS the tombstone on this table (there is no ``SoftDeleteMixin``).

    A CANCELLED row is an untied tie and a CLOSED row is a finished one; neither is
    standing demand, and counting either would refuse a conversion that nothing would act
    on — a refusal a planner could not clear, because there is nothing left to untie.
    """
    admin = make_user(db_session)
    fg = make_part(db_session, part_type="manufactured")
    wo = make_wo(db_session, fg)
    sheet = make_part(db_session, part_type="raw_material", uom="sheets")
    make_tie(db_session, wo, sheet, status_value=closed_status)

    response = put_part(client, admin, sheet.id, part_type="manufactured")

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["part_type"] == "manufactured"


@pytest.mark.parametrize(
    "finished_status",
    [WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED],
)
def test_a_tie_on_a_FINISHED_work_order_does_not_block_the_conversion(
    client: TestClient, db_session: Session, finished_status: WorkOrderStatus
):
    """The tie is still OPEN, it has CONSUMED, and it must not refuse anything.

    THIS IS THE CASE A STATUS-ONLY COUNT GETS WRONG, and it is not an edge case — it is
    every part the shop has ever consumed. Nothing in ``app/`` ever writes
    ``AllocationStatus.CLOSED`` (the model's own docstring says so), and work-order
    completion neither closes nor cancels ties: only WO delete, nest re-import detach and
    ``return_and_untie`` do. So a tie stays ``OPEN`` **forever** once its job ships, which
    is exactly the shape this test builds — OPEN, and with material already drawn against
    it.

    A count keyed on the tie status alone would refuse the ordinary "we used to buy this
    bracket, now we make it" reclassification permanently, under a sentence claiming the
    edit "would leave standing demand that consumes finished goods" — untrue here, because
    nothing will ever drive a completion against a terminal work order (the completion
    effects fire only for work orders that reach a completion transition, and every path
    there refuses a terminal one). And the remedy that sentence names is unreachable:
    ``DELETE …/material-allocations/{id}`` 409s once the ledger shows material issued, and
    ``return_and_untie`` would credit a shipped job's material back into stock, falsifying
    its as-built record (invariants 5 and 6b) to satisfy a catalog edit.

    CANCELLED is parametrised alongside COMPLETE/CLOSED deliberately: it is the value
    guards in this codebase have historically forgotten when they spell the terminal set
    out by hand, and a cancelled job's tie is the most clearly dead demand there is.
    """
    admin = make_user(db_session)
    fg = make_part(db_session, part_type="manufactured")
    shipped = make_wo(db_session, fg, wo_status=finished_status)
    sheet = make_part(db_session, part_type="raw_material", uom="sheets")
    tie = make_tie(db_session, shipped, sheet, qty_consumed=4.0)
    assert tie.status == AllocationStatus.OPEN, "the premise: completion does not close a tie"

    response = put_part(client, admin, sheet.id, part_type="manufactured")

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["part_type"] == "manufactured"
    db_session.expire_all()
    assert db_session.get(Part, sheet.id).part_type == PartType.MANUFACTURED
    assert (
        db_session.get(WorkOrderMaterialAllocation, tie.id).status == AllocationStatus.OPEN
    ), "the historical tie is left exactly as it was — nothing is rewritten to permit the edit"


@pytest.mark.parametrize(
    "live_status",
    [
        WorkOrderStatus.DRAFT,
        WorkOrderStatus.RELEASED,
        WorkOrderStatus.IN_PROGRESS,
        WorkOrderStatus.ON_HOLD,
    ],
)
def test_a_tie_on_a_LIVE_work_order_still_blocks_the_conversion(
    client: TestClient, db_session: Session, live_status: WorkOrderStatus
):
    """The negative control for the test above — the hazard itself is unchanged.

    Every non-terminal status is covered, not just IN_PROGRESS: a DRAFT or ON_HOLD job is
    demand that has not been drawn *yet*, which is precisely the state the gate exists to
    protect. Without this, narrowing the count to non-terminal work orders could have been
    narrowed to nothing at all and the suite would not have noticed.
    """
    admin = make_user(db_session)
    fg = make_part(db_session, part_type="manufactured")
    wo = make_wo(db_session, fg, wo_status=live_status)
    sheet = make_part(db_session, part_type="raw_material", uom="sheets")
    make_tie(db_session, wo, sheet)

    response = put_part(client, admin, sheet.id, part_type="assembly")

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert "1 unfinished work order still ties" in response.json()["detail"]
    db_session.expire_all()
    assert db_session.get(Part, sheet.id).part_type == PartType.RAW_MATERIAL


def test_only_the_UNFINISHED_work_orders_are_counted_when_a_part_has_both(client: TestClient, db_session: Session):
    """The count in the sentence has to be a number the planner can act on.

    A sheet that has run on a dozen finished jobs and one live one has ONE thing standing
    in the way. Naming thirteen would send them through twelve shipped work orders looking
    for a tie they must not touch — the remedy would be worse than the refusal.
    """
    admin = make_user(db_session)
    fg = make_part(db_session, part_type="manufactured")
    sheet = make_part(db_session, part_type="raw_material", uom="sheets")

    for finished in (WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED):
        make_tie(db_session, make_wo(db_session, fg, wo_status=finished), sheet, qty_consumed=2.0)
    make_tie(db_session, make_wo(db_session, fg, wo_status=WorkOrderStatus.RELEASED), sheet)

    response = put_part(client, admin, sheet.id, part_type="assembly")

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert "1 unfinished work order still ties" in response.json()["detail"]
    assert "Untie that work order first" in response.json()["detail"]


def test_untying_the_LIVE_tie_lets_the_identical_request_through(client: TestClient, db_session: Session):
    """The 409's own promise, executed: "succeeds once those ties are untied".

    That promise is what makes 409 the right status rather than a wall — retry after
    changing state, not after changing the payload — so it is asserted end to end rather
    than argued for in a docstring. The finished job's consumed tie stays exactly where it
    is throughout: the planner clears live demand, never history.
    """
    admin = make_user(db_session)
    fg = make_part(db_session, part_type="manufactured")
    sheet = make_part(db_session, part_type="raw_material", uom="sheets")
    shipped_tie = make_tie(
        db_session, make_wo(db_session, fg, wo_status=WorkOrderStatus.COMPLETE), sheet, qty_consumed=3.0
    )
    live_wo = make_wo(db_session, fg, wo_status=WorkOrderStatus.RELEASED)
    live_tie = make_tie(db_session, live_wo, sheet)

    refused = put_part(client, admin, sheet.id, part_type="assembly")
    assert refused.status_code == status.HTTP_409_CONFLICT, refused.text

    untied = client.delete(tie_url(live_wo.id, live_tie.id), headers=headers_for(admin))
    assert untied.status_code == status.HTTP_200_OK, untied.text

    retried = put_part(client, admin, sheet.id, part_type="assembly")

    assert retried.status_code == status.HTTP_200_OK, retried.text
    assert retried.json()["part_type"] == "assembly"
    db_session.expire_all()
    assert (
        db_session.get(WorkOrderMaterialAllocation, shipped_tie.id).status == AllocationStatus.OPEN
    ), "nothing about the shipped job had to change for the catalog edit to go through"


def test_another_companys_open_tie_cannot_block_this_companys_conversion(client: TestClient, db_session: Session):
    """Invariant 1 on the gate's own query, in the direction that actually leaks.

    The tie count is read with ``tenant_query``. An unscoped count would let company B's
    ties refuse company A's edit — a cross-tenant denial of service AND an existence
    oracle, since the refusal sentence names how many work orders tie the part.
    """
    admin_a = make_user(db_session, company_id=COMPANY_A)
    sheet_a = make_part(db_session, part_type="raw_material", company_id=COMPANY_A)

    fg_b = make_part(db_session, part_type="manufactured", company_id=COMPANY_B)
    wo_b = make_wo(db_session, fg_b, company_id=COMPANY_B)
    make_tie(db_session, wo_b, sheet_a, company_id=COMPANY_B)

    response = put_part(client, admin_a, sheet_a.id, part_type="assembly")

    assert response.status_code == status.HTTP_200_OK, response.text


def test_the_count_is_of_WORK_ORDERS_not_of_allocation_rows(client: TestClient, db_session: Session):
    """One job holding two operation-scoped ties is ONE thing to go and untie.

    A laser work order carries a tie per nest operation, so counting rows would tell a
    planner to go and fix twelve things when there is one job. The remedy sentence has to
    be true, or it sends someone looking for eleven jobs that are not there.
    """
    admin = make_user(db_session)
    fg = make_part(db_session, part_type="manufactured")
    wo = make_wo(db_session, fg)
    wc = make_work_center(db_session)
    sheet = make_part(db_session, part_type="raw_material", uom="sheets")
    make_tie(db_session, wo, sheet, operation=make_op(db_session, wo, wc))
    make_tie(db_session, wo, sheet, operation=make_op(db_session, wo, wc))

    other_wo = make_wo(db_session, fg)
    make_tie(db_session, other_wo, sheet)

    response = put_part(client, admin, sheet.id, part_type="assembly")

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert "2 unfinished work orders still tie" in response.json()["detail"]


def test_the_reverse_direction_and_an_engineering_to_engineering_edit_are_not_gated(
    client: TestClient, db_session: Session
):
    """Only material -> produced is refused, and only that.

    A produced part that somehow carries ties may still be edited (including toward a
    material type, which this endpoint's own 400 refuses for its own unrelated reason), and
    manufactured <-> assembly is an ordinary engineering edit. Pinned so nobody widens the
    gate into "a tied part's type is frozen", which would strand exactly the legacy rows a
    planner needs to correct.
    """
    admin = make_user(db_session)
    fg = make_part(db_session, part_type="manufactured")
    wo = make_wo(db_session, fg)
    legacy_produced = make_part(db_session, part_type="manufactured")
    make_tie(db_session, wo, legacy_produced)

    swapped = put_part(client, admin, legacy_produced.id, part_type="assembly")
    assert swapped.status_code == status.HTTP_200_OK, swapped.text
    assert swapped.json()["part_type"] == "assembly"

    demoted = put_part(client, admin, legacy_produced.id, part_type="raw_material")
    assert demoted.status_code == status.HTTP_400_BAD_REQUEST, demoted.text


def test_a_tied_material_part_is_still_editable_in_every_other_way(client: TestClient, db_session: Session):
    """The blast radius the reviewers rejected, pinned as NOT happening.

    An earlier version of this change narrowed ``PUT /parts/{id}`` to engineering types
    outright. That 404'd every save from the parts page a BOM component drill-through
    lands on, and left the Materials screen — which force-sends ``revision`` and
    ``is_critical`` on every save — as the only editor for a material row, quietly
    resetting revisions and clearing critical-characteristic flags (invariant 5). The gate
    keys on the CLASS CHANGE instead, so a request that does not carry ``part_type`` is
    never even asked about ties.
    """
    admin = make_user(db_session)
    fg = make_part(db_session, part_type="manufactured")
    wo = make_wo(db_session, fg)
    sheet = make_part(db_session, part_type="raw_material", uom="sheets")
    sheet.revision = "C"
    sheet.is_critical = True
    db_session.commit()
    make_tie(db_session, wo, sheet)

    response = put_part(client, admin, sheet.id, description="edited from the parts page")

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["description"] == "edited from the parts page"
    db_session.expire_all()
    reread = db_session.get(Part, sheet.id)
    assert reread.revision == "C", "an unrelated edit must not touch the revision (invariant 5)"
    assert reread.is_critical is True, "nor clear the critical-characteristic flag"


@pytest.mark.parametrize("part_type", ["manufactured", "assembly"])
def test_put_parts_still_edits_an_engineering_part(client: TestClient, db_session: Session, part_type: str):
    """The broad negative control: the engineering door is unchanged for engineering rows."""
    admin = make_user(db_session)
    part = make_part(db_session, part_type=part_type)

    response = put_part(client, admin, part.id, description="edited through the engineering door")

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["description"] == "edited through the engineering door"
    assert response.json()["part_type"] == part_type


def test_put_parts_can_still_swap_between_the_two_engineering_types(client: TestClient, db_session: Session):
    """A manufactured part becoming an assembly is an ordinary engineering edit and stays
    allowed — the gate is about the material -> produced DIRECTION on a tied part, never
    about freezing the column.
    """
    admin = make_user(db_session)
    part = make_part(db_session, part_type="manufactured")

    response = put_part(client, admin, part.id, part_type="assembly")

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["part_type"] == "assembly"


@pytest.mark.parametrize("part_type", ["raw_material", "purchased", "hardware", "consumable"])
def test_put_materials_is_unchanged_and_still_edits_its_own_rows(
    client: TestClient, db_session: Session, part_type: str
):
    admin = make_user(db_session)
    material = make_part(db_session, part_type=part_type)

    response = put_material(client, admin, material.id, description="edited through the materials door")

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["description"] == "edited through the materials door"
    assert response.json()["part_type"] == part_type


def test_put_materials_still_refuses_the_conversion_with_its_own_400(client: TestClient, db_session: Session):
    """The materials door reaches the row but can NEVER change its class — 400, unconditionally.

    Both doors write the same ``parts`` rows, and each refuses the conversion in its own
    shape: ``/materials`` refuses it outright (``_require_material_type``, no tie check
    needed — this door simply does not do promotions), while ``/parts`` permits it and runs
    the tie gate. That asymmetry is deliberate: there has to be ONE place a mis-typed,
    untied catalog row can be corrected, and the parts door is it. What neither door can do
    is promote a part that ties are still standing against.
    """
    admin = make_user(db_session)
    material = make_part(db_session, part_type="raw_material")

    response = put_material(client, admin, material.id, part_type="manufactured")

    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
    db_session.expire_all()
    assert db_session.get(Part, material.id).part_type == PartType.RAW_MATERIAL


def test_put_materials_still_404s_an_engineering_part(client: TestClient, db_session: Session):
    """The pre-existing half of the symmetry, restated so the pair is visible in one file."""
    admin = make_user(db_session)
    part = make_part(db_session, part_type="manufactured")

    response = put_material(client, admin, part.id, description="through the wrong door")

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert response.json()["detail"] == "Material not found"


def test_the_parts_router_still_resolves_a_material_row_on_every_handler(client: TestClient, db_session: Session):
    """``GET /parts/{id}`` — and its PUT twin — must keep resolving a material row.

    The BOM tab drills through to a component's part page, and BOM components are routinely
    ``purchased`` / ``raw_material``; from that page the overview tab, the edit form and the
    backflush card all call ``PUT /parts/{id}``. Narrowing either verb to engineering types
    would turn every such drill-through and save into a 404 whose message ("Part not found")
    is not true. Pinned here because that narrowing was tried and rejected: the gate keys on
    the class change instead, which is the only thing that was ever unsafe.
    """
    admin = make_user(db_session)
    material = make_part(db_session, part_type="purchased")

    response = client.get(f"/api/v1/parts/{material.id}", headers=headers_for(admin))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["part_type"] == "purchased"


# =========================================================================== #
# 3. THE RESPONSE FIELD — MaterialAllocationResponse.part_type
# =========================================================================== #


def test_the_tie_response_carries_the_lowercase_part_type_value(client: TestClient, db_session: Session):
    """The enum flattened to its VALUE, on both the create response and the list read.

    Pinned as the exact string a client compares against — not ``PartType.RAW_MATERIAL``
    and not ``"RAW_MATERIAL"``. The frontend's ``isProductionPartType`` matches on these
    literals, so a serializer that started emitting the enum NAME would silently stop
    flagging legacy ties while every backend test still passed.
    """
    admin = make_user(db_session)
    fg = make_part(db_session, part_type="manufactured")
    wo = make_wo(db_session, fg)
    sheet = make_part(db_session, part_type="raw_material", uom="sheets")

    created = post_tie(client, admin, wo, sheet)
    assert created.status_code == status.HTTP_201_CREATED, created.text
    assert created.json()["part_type"] == "raw_material"

    listed = client.get(tie_url(wo.id), headers=headers_for(admin))
    assert listed.status_code == status.HTTP_200_OK, listed.text
    assert [row["part_type"] for row in listed.json()] == ["raw_material"]


def test_a_LEGACY_tie_reports_its_produced_part_type_rather_than_hiding_it(client: TestClient, db_session: Session):
    """THE reason the field exists.

    Ties created before the gate shipped can point at a manufactured or assembly part, and
    the tie list is the only place anyone would notice. The field must report
    ``"manufactured"`` verbatim — a serializer that blanked or normalised it would make the
    exact rows a planner has to go fix the ones the UI cannot see. Written directly to the
    table because the API can no longer create this shape, which is the point.
    """
    admin = make_user(db_session)
    fg = make_part(db_session, part_type="manufactured")
    wo = make_wo(db_session, fg)
    produced = make_part(db_session, part_type="manufactured")

    legacy = WorkOrderMaterialAllocation(
        company_id=COMPANY_A,
        work_order_id=wo.id,
        part_id=produced.id,
        source="manual",
        status=AllocationStatus.OPEN,
        qty_planned=4.0,
        unit_of_measure="each",
        qty_consumed=0.0,
    )
    db_session.add(legacy)
    db_session.commit()

    listed = client.get(tie_url(wo.id), headers=headers_for(admin))
    assert listed.status_code == status.HTTP_200_OK, listed.text
    [row] = listed.json()
    assert row["part_type"] == "manufactured", "the legacy tie must be visible AS a produced part"
    assert row["part_number"] == produced.part_number


def test_the_part_type_field_is_null_when_the_part_row_cannot_be_read(client: TestClient, db_session: Session):
    """``None``, not a guess — the same miss that already blanks ``part_number``/``part_name``.

    ``_serialize`` resolves the part company-scoped, so a tie whose part row is gone (or
    belongs to another tenant, which is the same thing through that query) renders every
    part display field NULL rather than inventing one.
    """
    admin = make_user(db_session, company_id=COMPANY_A)
    fg = make_part(db_session, part_type="manufactured", company_id=COMPANY_A)
    wo = make_wo(db_session, fg, company_id=COMPANY_A)
    foreign_part = make_part(db_session, part_type="raw_material", company_id=COMPANY_B)

    orphan = WorkOrderMaterialAllocation(
        company_id=COMPANY_A,
        work_order_id=wo.id,
        part_id=foreign_part.id,
        source="manual",
        status=AllocationStatus.OPEN,
        qty_planned=1.0,
        unit_of_measure="each",
        qty_consumed=0.0,
    )
    db_session.add(orphan)
    db_session.commit()

    listed = client.get(tie_url(wo.id), headers=headers_for(admin))
    assert listed.status_code == status.HTTP_200_OK, listed.text
    [row] = listed.json()
    assert row["part_type"] is None
    assert row["part_number"] is None
    assert row["part_name"] is None


def test_part_type_survives_a_column_value_the_enum_does_not_know():
    """Belt-and-braces on the flattener: it reads ``.value`` when there is one and stringifies
    otherwise, so it cannot be the thing that 500s a tie list.

    Driven through the model attribute rather than the database because SQLAlchemy raises
    ``LookupError`` LOADING an unknown enum string — which is itself worth knowing: an
    unrecognised stored value never reaches the gate or the serializer at all, it fails one
    layer earlier. This asserts the serializer helper's own contract directly.
    """
    part = Part(part_number="MTG-FLAT-1", name="Flatten me", part_type="raw_material")
    assert wo_materials_endpoint._part_type_value(part) == "raw_material"

    part.part_type = PartType.HARDWARE
    assert wo_materials_endpoint._part_type_value(part) == "hardware"

    assert wo_materials_endpoint._part_type_value(None) is None

    # The NULL-COLUMN branch, which is a different miss from the NULL-PART one above and
    # is the other half of what ``MaterialAllocationResponse.part_type`` promises its
    # readers ("NULL when the part row could not be read, and NULL for a NULL column").
    # ``Part.part_type`` is ``nullable=False`` (pinned in
    # ``tests/services/test_material_tie_part_gate.py``), so this state is not reachable
    # through the API today and the branch is defence — but the guard is what stops
    # ``normalize_part_type_value(None)`` emitting the STRING ``"none"``, which every
    # consumer would read as a part type it simply does not recognise rather than as an
    # absent one. Asserted here so a "simplify" pass cannot delete the guard and leave the
    # documented contract false.
    part.part_type = None
    assert wo_materials_endpoint._part_type_value(part) is None
    assert normalize_part_type_value(None) == "none", (
        "the guard exists precisely because the normaliser stringifies None; if this ever "
        "changes, the guard's rationale above needs rewriting rather than the guard removing"
    )


def test_no_stray_part_type_column_was_added_to_the_allocation_table(db_session: Session):
    """``part_type`` is a DISPLAY field resolved from ``parts`` on every read, never stored.

    Snapshotting it would create a second copy of a fact the ``parts`` row already owns,
    and the two would disagree the first time a part was reclassified — the tie table would
    then be asserting a part class the catalog contradicts. ``unit_of_measure`` IS
    snapshotted, deliberately and for the opposite reason (it must stay readable as the
    quantity that was planned), so the distinction is easy to erase by accident.
    """
    assert "part_type" not in WorkOrderMaterialAllocation.__table__.c
    # Read back off the LIVE table rather than only the model, via SQLAlchemy inspection
    # so the assertion is dialect-independent (the suite runs SQLite; production is
    # Postgres). ``unit_of_measure`` is the positive control: without it, this would pass
    # just as well against a table that had no columns at all.
    columns = {
        column["name"] for column in inspect(db_session.get_bind()).get_columns("work_order_material_allocations")
    }
    assert "unit_of_measure" in columns, "the table must be readable for this assertion to mean anything"
    assert "part_type" not in columns


# =========================================================================== #
# 4. THE DELETE -> RECLASSIFY -> RESTORE BYPASS, CLOSED AT THE RESTORE SEAM
# =========================================================================== #
#
# §2 refuses reclassifying a part that LIVE ties still hold. "Live" deliberately means an
# OPEN tie on an unfinished work order -- which leaves a door that needs no code change to
# walk through, only three supported ADMIN/MANAGER verbs:
#
#   1. DELETE /work-orders/{id}   -- the soft delete CANCELs every OPEN tie the job holds,
#                                   so `live_tie_work_order_ids` now counts zero.
#   2. PUT /parts/{id}            -- the reclassification is therefore allowed, and that is
#                                   CORRECT: a deleted work order completes nothing, so at
#                                   this moment the hazard genuinely is not live.
#   3. POST /work-orders/{id}/restore
#                                 -- which re-OPENs exactly those ties. The demand comes
#                                   back live, and nothing had re-checked the part.
#
# The end state is the one this whole module exists to prevent: an OPEN tie to a produced
# part on a released work order, which issues finished goods to build the job, never
# auto-reverses (invariant 6b), and is remediable only by a compensating RETURN that
# falsifies the as-built record (invariants 5 and 6b).
#
# The refusal lives at step 3, not step 2. Counting a deleted job's ties in step 2 would
# refuse a conversion while the hazard is dormant AND would still not stop step 3.


def delete_wo(client: TestClient, user: User, wo: WorkOrder):
    return client.delete(f"/api/v1/work-orders/{wo.id}", headers=headers_for(user))


def restore_wo(client: TestClient, user: User, wo: WorkOrder):
    return client.post(f"/api/v1/work-orders/{wo.id}/restore", headers=headers_for(user))


def test_delete_then_reclassify_then_restore_leaves_the_produced_part_tie_CANCELLED(
    client: TestClient, db_session: Session
):
    """The whole three-step sequence, asserted step by step including the middle one.

    Step 2 asserting **200** is not a slack assertion — it pins the deliberate scope of the
    conversion gate. Tightening it to refuse a deleted job's ties would be the wrong repair
    (false while the work order is deleted, and it would not close this anyway), so the
    test records that the reclassification is allowed and that the RESTORE is what refuses.
    """
    admin = make_user(db_session)
    fg = make_part(db_session, part_type="manufactured")
    wo = make_wo(db_session, fg)
    sheet = make_part(db_session, part_type="raw_material", uom="sheets")

    assert post_tie(client, admin, wo, sheet).status_code == status.HTTP_201_CREATED
    [allocation] = allocations(db_session)

    # Step 0 — the conversion gate holds while the demand is live.
    assert put_part(client, admin, sheet.id, part_type="manufactured").status_code == status.HTTP_409_CONFLICT

    # Step 1 — the soft delete cancels the tie, so the gate's count drops to zero.
    assert delete_wo(client, admin, wo).status_code == status.HTTP_204_NO_CONTENT
    db_session.expire_all()
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).status == AllocationStatus.CANCELLED

    # Step 2 — the reclassification now succeeds, and that is the intended behavior.
    converted = put_part(client, admin, sheet.id, part_type="manufactured")
    assert converted.status_code == status.HTTP_200_OK, converted.text
    assert converted.json()["part_type"] == "manufactured"

    # Step 3 — the restore refuses to re-arm the demand, and SAYS SO.
    restored = restore_wo(client, admin, wo)
    assert restored.status_code == status.HTTP_200_OK, restored.text
    body = restored.json()
    assert "restored" in body["message"], "the pre-envelope message field is unchanged"
    assert body["skipped_material_allocations"] == [
        {
            "allocation_id": allocation.id,
            "part_id": sheet.id,
            "work_order_operation_id": None,
            "reason": "part_not_tieable",
        }
    ]

    db_session.expire_all()
    assert (
        db_session.get(WorkOrderMaterialAllocation, allocation.id).status == AllocationStatus.CANCELLED
    ), "the tie must NOT come back OPEN against a part the shop now produces"
    assert db_session.get(WorkOrder, wo.id).is_deleted is False, "the work order itself is still restored"


def test_the_same_round_trip_still_reopens_an_ordinary_raw_stock_tie(client: TestClient, db_session: Session):
    """The control. Without it, a restore that had simply stopped re-opening ANY tie would
    satisfy the test above just as well — and that would be a silent regression of the
    delete/restore symmetry (a restored job whose material never depletes, and, once
    ``backflush_components`` is on, a consumed-then-cancelled operation tie that stops
    suppressing the BOM backflush and double-issues the part).

    Same three verbs, same order, one difference: the part is left as raw stock.
    """
    admin = make_user(db_session)
    fg = make_part(db_session, part_type="manufactured")
    wo = make_wo(db_session, fg)
    sheet = make_part(db_session, part_type="raw_material", uom="sheets")

    assert post_tie(client, admin, wo, sheet).status_code == status.HTTP_201_CREATED
    [allocation] = allocations(db_session)

    assert delete_wo(client, admin, wo).status_code == status.HTTP_204_NO_CONTENT

    # The catalog edit really happens — it just does not change the part's CLASS.
    edited = put_part(client, admin, sheet.id, description="still stock, renamed")
    assert edited.status_code == status.HTTP_200_OK, edited.text

    restored = restore_wo(client, admin, wo)
    assert restored.status_code == status.HTTP_200_OK, restored.text
    assert restored.json()["skipped_material_allocations"] == [], "an empty list is the clean-restore signal"

    db_session.expire_all()
    assert db_session.get(WorkOrderMaterialAllocation, allocation.id).status == AllocationStatus.OPEN


def test_a_restore_reopens_the_still_tieable_ties_and_skips_only_the_produced_one(
    client: TestClient, db_session: Session
):
    """A skip is per-tie, not per-restore: one bad tie must not strand the good ones.

    The produced-part tie here is OPERATION-scoped, so the skip entry has to carry the
    operation id — that is what lets a planner find the card they now have to re-tie by
    hand rather than hunting the whole job.
    """
    admin = make_user(db_session)
    fg = make_part(db_session, part_type="manufactured")
    wo = make_wo(db_session, fg)
    op = make_op(db_session, wo, make_work_center(db_session))
    converted_part = make_part(db_session, part_type="raw_material", uom="sheets")
    stays_stock = make_part(db_session, part_type="purchased", uom="each")

    assert (
        post_tie(client, admin, wo, converted_part, work_order_operation_id=op.id, qty_per_run=2.0).status_code
        == status.HTTP_201_CREATED
    )
    assert post_tie(client, admin, wo, stays_stock).status_code == status.HTTP_201_CREATED
    converted_tie, kept_tie = allocations(db_session)

    assert delete_wo(client, admin, wo).status_code == status.HTTP_204_NO_CONTENT
    assert put_part(client, admin, converted_part.id, part_type="assembly").status_code == status.HTTP_200_OK

    restored = restore_wo(client, admin, wo)
    assert restored.status_code == status.HTTP_200_OK, restored.text
    assert restored.json()["skipped_material_allocations"] == [
        {
            "allocation_id": converted_tie.id,
            "part_id": converted_part.id,
            "work_order_operation_id": op.id,
            "reason": "part_not_tieable",
        }
    ]

    db_session.expire_all()
    assert db_session.get(WorkOrderMaterialAllocation, converted_tie.id).status == AllocationStatus.CANCELLED
    assert db_session.get(WorkOrderMaterialAllocation, kept_tie.id).status == AllocationStatus.OPEN


def test_the_skipped_tie_is_on_the_audit_chain_and_gets_no_RESTORE_row_of_its_own(
    client: TestClient, db_session: Session
):
    """Two audit properties, and they pull in opposite directions on purpose.

    The skip must be RECORDED — a dropped tie whose only trace is a `cancelled` row nobody
    reads is the failure the whole skip convention exists to prevent — but it must not be
    recorded as something that happened to the allocation, because nothing did: its status
    is unchanged. So it rides the restore VERB's own `work_order` row (the channel
    `duplicate_work_order` uses for its skips), and the allocation gets no `RESTORE` row.
    """
    admin = make_user(db_session)
    fg = make_part(db_session, part_type="manufactured")
    wo = make_wo(db_session, fg)
    sheet = make_part(db_session, part_type="raw_material", uom="sheets")

    assert post_tie(client, admin, wo, sheet).status_code == status.HTTP_201_CREATED
    [allocation] = allocations(db_session)

    assert delete_wo(client, admin, wo).status_code == status.HTTP_204_NO_CONTENT
    assert put_part(client, admin, sheet.id, part_type="manufactured").status_code == status.HTTP_200_OK
    assert restore_wo(client, admin, wo).status_code == status.HTTP_200_OK

    db_session.expire_all()
    restore_rows = [
        row
        for row in audit_rows(db_session)
        if row.resource_type == "work_order" and row.resource_id == wo.id and row.action == "RESTORE"
    ]
    assert len(restore_rows) == 1
    extra = restore_rows[0].extra_data or {}
    assert extra["reopened_material_allocations"] == [], "nothing was re-opened"
    assert extra["skipped_material_allocations"] == [
        {
            "allocation_id": allocation.id,
            "part_id": sheet.id,
            "work_order_operation_id": None,
            "reason": "part_not_tieable",
        }
    ], "the chain names the same skip the response does, from one model_dump()"

    assert not [
        row
        for row in audit_rows(db_session)
        if row.resource_type == "work_order_material_allocation"
        and row.resource_id == allocation.id
        and row.action == "RESTORE"
    ], "nothing happened to the allocation, so the chain must not claim anything did"
