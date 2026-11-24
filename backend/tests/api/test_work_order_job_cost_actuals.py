"""P1.3: a work order's cost by element on the single-work-order read, and the SUBCONTRACT entry type.

What is pinned:

* ``job_cost_actuals`` on ``GET /work-orders/{id}`` and ``/by-number/{n}``: null without a
  JobCost; with one, material / labor / burden (= ``actual_overhead_cost``, OVERHEAD + OTHER) /
  subcontract / total, status and ``updated_at`` (UTC ``Z``).
* SUBCONTRACT has its own branch in ``recalculate_totals``: it never lands in overhead/burden,
  and it does reach the total. ``add_entry`` accepts ``'subcontract'``; ``JobCostResponse``
  and the variance report carry it.
* ``issued_material_cost``: net ISSUE minus RETURN from the inventory ledger -- present with
  no JobCost, a different basis from JobCost material.
* Invariant 1: a legacy foreign-tenant JobCost holding this work order's id on the GLOBAL
  unique key, and foreign ledger rows, are never disclosed.
* by-number parity: same body as by id; a soft-deleted work order is 404 on both.
* Every other ``WorkOrderResponse`` route still validates, with both fields null.
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.company import Company
from app.models.inventory import InventoryTransaction, TransactionType
from app.models.job_costing import CostEntry, CostEntryType, JobCost, JobCostStatus
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services.job_costing_service import recalculate_totals
from tests.api.export_control_helpers import could_occur_in_iso_timestamp

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> None:
    if db.get(Company, company_id) is None:
        db.add(Company(id=company_id, name=f"Cost Co {company_id}", slug=f"cost-co-{company_id}", is_active=True))
        db.commit()


def make_user(db: Session, *, role: UserRole = UserRole.ADMIN, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"p13-{n}@co{company_id}.test",
        employee_id=f"P13-{n:05d}",
        first_name="P13",
        last_name="User",
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=True,
        is_superuser=False,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def make_wo(db: Session, *, company_id: int = COMPANY_A, status_: WorkOrderStatus = WorkOrderStatus.IN_PROGRESS):
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"P13-P-{n}",
        name=f"Part {n}",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    wc = WorkCenter(
        name=f"P13-WC-{n}",
        code=f"P13-WC-{n}",
        work_center_type="welding",
        hourly_rate=80.0,
        is_active=True,
        company_id=company_id,
    )
    db.add_all([part, wc])
    db.flush()
    wo = WorkOrder(
        work_order_number=f"P13-WO-{n:05d}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=10,
        status=status_,
        priority=5,
        due_date=date.today() + timedelta(days=30),
        company_id=company_id,
    )
    db.add(wo)
    db.flush()
    db.add(
        WorkOrderOperation(
            work_order_id=wo.id,
            work_center_id=wc.id,
            sequence=10,
            operation_number="10",
            name="Weld",
            status=OperationStatus.IN_PROGRESS,
            quantity_complete=0,
            company_id=company_id,
        )
    )
    db.commit()
    db.refresh(wo)
    return wo, part


def add_entry(client, user, job_cost_id: int, entry_type: str, *, quantity: float = 1, unit_cost: float):
    resp = client.post(
        f"/api/v1/job-costs/{job_cost_id}/entries",
        json={
            "entry_type": entry_type,
            "description": f"{entry_type} line",
            "quantity": quantity,
            "unit_cost": unit_cost,
            "entry_date": "2026-11-02",
        },
        headers=headers_for(user),
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()


def ledger_row(db: Session, wo: WorkOrder, part: Part, kind: TransactionType, cost: float, *, company_id=COMPANY_A):
    actor = db.query(User).filter(User.company_id == company_id).first()
    db.add(
        InventoryTransaction(
            created_by=actor.id,
            part_id=part.id,
            transaction_type=kind,
            quantity=-1.0 if kind == TransactionType.ISSUE else 1.0,
            # The reconciled-component shape: N rows per (work order, part) are legitimate
            # there, unlike the one-row ``work_order`` ISSUE index.
            reference_type="work_order_backflush",
            reference_id=wo.id,
            unit_cost=cost,
            total_cost=-cost if kind == TransactionType.ISSUE else cost,
            company_id=company_id,
        )
    )
    db.commit()


def get_by_id(client, user, wo_id: int):
    resp = client.get(f"/api/v1/work-orders/{wo_id}", headers=headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# job_cost_actuals
# ---------------------------------------------------------------------------


def test_a_work_order_without_a_job_cost_reads_null_actuals_and_a_zero_ledger_figure(client, db_session):
    admin = make_user(db_session)
    wo, _ = make_wo(db_session)
    body = get_by_id(client, admin, wo.id)
    assert "job_cost_actuals" in body and body["job_cost_actuals"] is None
    assert body["issued_material_cost"] == 0.0


def test_every_element_is_reported_and_subcontract_is_never_burden(client, db_session):
    admin = make_user(db_session)
    wo, _ = make_wo(db_session)
    created = client.post("/api/v1/job-costs/", json={"work_order_id": wo.id}, headers=headers_for(admin))
    assert created.status_code == status.HTTP_200_OK, created.text
    job_cost_id = created.json()["id"]
    assert created.json()["actual_subcontract_cost"] == 0.0

    add_entry(client, admin, job_cost_id, "material", quantity=2, unit_cost=5)  # 10
    add_entry(client, admin, job_cost_id, "labor", unit_cost=40)
    add_entry(client, admin, job_cost_id, "overhead", unit_cost=7)
    add_entry(client, admin, job_cost_id, "other", unit_cost=3)
    subcontract = add_entry(client, admin, job_cost_id, "subcontract", unit_cost=25)
    assert subcontract["entry_type"] == "subcontract"

    job_cost = client.get(f"/api/v1/job-costs/{job_cost_id}", headers=headers_for(admin)).json()
    assert job_cost["actual_material_cost"] == 10.0
    assert job_cost["actual_labor_cost"] == 40.0
    assert job_cost["actual_overhead_cost"] == 10.0, "OVERHEAD + OTHER, never SUBCONTRACT"
    assert job_cost["actual_subcontract_cost"] == 25.0
    assert job_cost["actual_total_cost"] == 85.0
    assert job_cost["total_variance"] == 85.0

    report = client.get(f"/api/v1/job-costs/{job_cost_id}/variance-report", headers=headers_for(admin)).json()
    lines = report["variance_summary"]
    assert lines["subcontract"] == {"estimated": 0.0, "actual": 25.0, "variance": 25.0, "variance_percent": 0}
    assert sum(lines[key]["actual"] for key in ("material", "labor", "overhead", "subcontract")) == 85.0
    assert [row["description"] for row in report["entries_by_type"]["subcontract"]] == ["subcontract line"]

    body = get_by_id(client, admin, wo.id)
    actuals = body["job_cost_actuals"]
    assert {key: actuals[key] for key in actuals if key != "updated_at"} == {
        "job_cost_id": job_cost_id,
        "status": "in_progress",
        "actual_material_cost": 10.0,
        "actual_labor_cost": 40.0,
        "actual_burden_cost": 10.0,
        "actual_subcontract_cost": 25.0,
        "actual_total_cost": 85.0,
    }
    assert actuals["updated_at"].endswith("Z")

    # Removing the subcontract entry takes it out of the element and the total -- and
    # leaves burden exactly where it was.
    removed = client.delete(f"/api/v1/job-costs/{job_cost_id}/entries/{subcontract['id']}", headers=headers_for(admin))
    assert removed.status_code == status.HTTP_200_OK, removed.text
    actuals = get_by_id(client, admin, wo.id)["job_cost_actuals"]
    assert actuals["actual_subcontract_cost"] == 0.0
    assert actuals["actual_burden_cost"] == 10.0
    assert actuals["actual_total_cost"] == 60.0


def test_recalculate_totals_keeps_subcontract_out_of_overhead():
    job_cost = JobCost(
        estimated_material_cost=0.0,
        estimated_labor_cost=0.0,
        estimated_overhead_cost=0.0,
        revenue=200.0,
    )
    job_cost.entries = [
        CostEntry(entry_type=CostEntryType.SUBCONTRACT, total_cost=30.0),
        CostEntry(entry_type=CostEntryType.OTHER, total_cost=4.0),
        CostEntry(entry_type=CostEntryType.OVERHEAD, total_cost=6.0),
    ]
    recalculate_totals(job_cost)
    assert job_cost.actual_subcontract_cost == 30.0
    assert job_cost.actual_overhead_cost == 10.0
    assert job_cost.actual_total_cost == 40.0
    assert job_cost.margin_amount == 160.0


def test_a_negative_manual_entry_still_validates(client, db_session):
    """Plain floats, not Money (Decimal ge=0): a negative manual entry is reachable."""
    admin = make_user(db_session)
    wo, _ = make_wo(db_session)
    job_cost_id = client.post("/api/v1/job-costs/", json={"work_order_id": wo.id}, headers=headers_for(admin)).json()[
        "id"
    ]
    add_entry(client, admin, job_cost_id, "subcontract", unit_cost=-12.5)
    actuals = get_by_id(client, admin, wo.id)["job_cost_actuals"]
    assert actuals["actual_subcontract_cost"] == -12.5
    assert actuals["actual_total_cost"] == -12.5


def test_status_follows_the_job_cost(client, db_session):
    admin = make_user(db_session)
    wo, _ = make_wo(db_session)
    db_session.add(JobCost(company_id=COMPANY_A, work_order_id=wo.id, status=JobCostStatus.REVIEWED))
    db_session.commit()
    assert get_by_id(client, admin, wo.id)["job_cost_actuals"]["status"] == "reviewed"


# Foreign-tenant amounts: an integer part of five or more digits before the "." means no
# server-clock instant in the response can contain them by chance (see could_occur_in_iso_timestamp).
FOREIGN_COST = 98765.4321
FOREIGN_TOTAL = 197530.8642


def foreign_amounts_in(payload) -> list:
    """Every number anywhere in ``payload`` that equals a foreign-tenant amount."""
    found, stack = [], [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
        elif isinstance(current, (int, float)) and not isinstance(current, bool):
            if any(abs(current - amount) < 0.01 for amount in (FOREIGN_COST, FOREIGN_TOTAL)):
                found.append(current)
    return found


def test_a_foreign_tenant_job_cost_on_this_work_orders_id_is_never_disclosed(client, db_session):
    """``job_costs.work_order_id`` is unique GLOBALLY, so a legacy row owned by another
    company can occupy this work order's key. The read is scoped on company_id (never the
    unscoped ``WorkOrder.job_cost`` backref) and reports no JobCost at all."""
    admin = make_user(db_session)
    make_user(db_session, company_id=COMPANY_B)
    wo, part = make_wo(db_session)
    foreign = JobCost(
        company_id=COMPANY_B,
        work_order_id=wo.id,
        actual_material_cost=FOREIGN_COST,
        actual_subcontract_cost=FOREIGN_COST,
        actual_total_cost=FOREIGN_TOTAL,
    )
    db_session.add(foreign)
    db_session.commit()
    db_session.add(
        CostEntry(
            company_id=COMPANY_B,
            job_cost_id=foreign.id,
            entry_type=CostEntryType.SUBCONTRACT,
            description="FOREIGN",
            total_cost=FOREIGN_COST,
            entry_date=date(2026, 11, 2),
        )
    )
    # Foreign ledger rows naming this work order's id do not count either.
    ledger_row(db_session, wo, part, TransactionType.ISSUE, 500.0, company_id=COMPANY_B)
    db_session.commit()

    body = get_by_id(client, admin, wo.id)
    assert body["job_cost_actuals"] is None
    assert body["issued_material_cost"] == 0.0
    assert not foreign_amounts_in(body)
    by_number = client.get(f"/api/v1/work-orders/by-number/{wo.work_order_number}", headers=headers_for(admin))
    assert by_number.json()["job_cost_actuals"] is None
    assert not foreign_amounts_in(by_number.json())
    # Raw text too: catches an amount serialized inside a string. The needles were "999" until a
    # microsecond field ("...58.499912Z") matched it about one run in 84 with nothing leaked.
    needles = ("98765.", "197530.")
    assert not [n for n in needles if could_occur_in_iso_timestamp(n.encode())], "needle fits in a timestamp"
    assert not [n for n in needles if n in by_number.text] and "FOREIGN" not in by_number.text


def test_another_company_cannot_read_this_work_orders_cost(client, db_session):
    admin = make_user(db_session)
    outsider = make_user(db_session, company_id=COMPANY_B)
    wo, _ = make_wo(db_session)
    db_session.add(JobCost(company_id=COMPANY_A, work_order_id=wo.id, actual_total_cost=50.0))
    db_session.commit()
    assert get_by_id(client, admin, wo.id)["job_cost_actuals"]["actual_total_cost"] == 50.0
    for path in (f"/api/v1/work-orders/{wo.id}", f"/api/v1/work-orders/by-number/{wo.work_order_number}"):
        assert client.get(path, headers=headers_for(outsider)).status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# issued_material_cost
# ---------------------------------------------------------------------------


def test_issued_material_cost_is_net_issue_minus_return_with_or_without_a_job_cost(client, db_session):
    admin = make_user(db_session)
    wo, part = make_wo(db_session)
    ledger_row(db_session, wo, part, TransactionType.ISSUE, 30.0)
    ledger_row(db_session, wo, part, TransactionType.ISSUE, 15.0)
    ledger_row(db_session, wo, part, TransactionType.RETURN, 10.0)

    body = get_by_id(client, admin, wo.id)
    assert body["issued_material_cost"] == 35.0
    assert body["job_cost_actuals"] is None, "the ledger figure needs no JobCost"

    # A JobCost's material is its MATERIAL entries only -- a different basis, reported apart.
    db_session.add(JobCost(company_id=COMPANY_A, work_order_id=wo.id, actual_material_cost=0.0))
    db_session.commit()
    body = get_by_id(client, admin, wo.id)
    assert body["issued_material_cost"] == 35.0
    assert body["job_cost_actuals"]["actual_material_cost"] == 0.0


# ---------------------------------------------------------------------------
# by-number parity
# ---------------------------------------------------------------------------


def test_by_number_returns_the_same_body_as_by_id(client, db_session):
    admin = make_user(db_session)
    wo, part = make_wo(db_session)
    db_session.add(JobCost(company_id=COMPANY_A, work_order_id=wo.id, actual_labor_cost=12.0, actual_total_cost=12.0))
    db_session.commit()
    ledger_row(db_session, wo, part, TransactionType.ISSUE, 8.0)

    by_id = get_by_id(client, admin, wo.id)
    by_number = client.get(f"/api/v1/work-orders/by-number/{wo.work_order_number}", headers=headers_for(admin))
    assert by_number.status_code == status.HTTP_200_OK, by_number.text
    assert by_number.json() == by_id
    # Fields the by-number route used to leave at their defaults are now computed.
    assert by_id["operation_count"] == 1
    assert by_id["operations"][0]["hours_source"] == "none"
    assert by_id["operations"][0]["has_labor_evidence"] is False
    assert by_id["issued_material_cost"] == 8.0
    assert by_id["job_cost_actuals"]["actual_labor_cost"] == 12.0


def test_a_soft_deleted_work_order_is_404_by_number_as_by_id(client, db_session):
    admin = make_user(db_session)
    wo, _ = make_wo(db_session)
    wo.soft_delete(admin.id)
    db_session.commit()
    for path in (f"/api/v1/work-orders/{wo.id}", f"/api/v1/work-orders/by-number/{wo.work_order_number}"):
        resp = client.get(path, headers=headers_for(admin))
        assert resp.status_code == status.HTTP_404_NOT_FOUND, path


def test_by_number_writes_nothing(client, db_session):
    """No reconcile on this read: the work order's version and updated_at are untouched."""
    admin = make_user(db_session)
    wo, _ = make_wo(db_session)
    before = (wo.version, wo.updated_at)
    resp = client.get(f"/api/v1/work-orders/by-number/{wo.work_order_number}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    db_session.expire_all()
    row = db_session.get(WorkOrder, wo.id)
    assert (row.version, row.updated_at) == before


# ---------------------------------------------------------------------------
# Every other WorkOrderResponse route still validates (fields null there)
# ---------------------------------------------------------------------------


def test_write_routes_still_validate_with_the_cost_fields_null(client, db_session):
    admin = make_user(db_session)
    n = _next()
    part = Part(part_number=f"P13-NEW-{n}", name="New", part_type="manufactured", unit_of_measure="each", company_id=1)
    wc = WorkCenter(name=f"P13-NEW-WC-{n}", code=f"P13-NEW-WC-{n}", work_center_type="welding", company_id=1)
    db_session.add_all([part, wc])
    db_session.commit()

    created = client.post(
        "/api/v1/work-orders/",
        json={"part_id": part.id, "quantity_ordered": 5, "customer_name": "Acme", "priority": 3},
        headers=headers_for(admin),
    )
    assert created.status_code == status.HTTP_201_CREATED, created.text
    assert created.json()["job_cost_actuals"] is None
    assert created.json()["issued_material_cost"] is None

    updated = client.put(
        f"/api/v1/work-orders/{created.json()['id']}",
        json={"notes": "edited", "version": created.json()["version"]},
        headers=headers_for(admin),
    )
    assert updated.status_code == status.HTTP_200_OK, updated.text
    assert updated.json()["job_cost_actuals"] is None

    added = client.post(
        f"/api/v1/work-orders/{created.json()['id']}/operations",
        json={"name": "Weld", "work_center_id": wc.id, "sequence": 10},
        headers=headers_for(admin),
    )
    assert added.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED), added.text

    released = client.post(f"/api/v1/work-orders/{created.json()['id']}/release", headers=headers_for(admin))
    assert released.status_code == status.HTTP_200_OK, released.text
    assert released.json()["job_cost_actuals"] is None

    duplicated = client.post(
        f"/api/v1/work-orders/{created.json()['id']}/duplicate",
        json={"quantity_ordered": 2, "due_date": (datetime.utcnow().date() + timedelta(days=9)).isoformat()},
        headers=headers_for(admin),
    )
    assert duplicated.status_code == status.HTTP_201_CREATED, duplicated.text
    assert duplicated.json()["work_order"]["job_cost_actuals"] is None


def test_issued_material_cost_reads_every_ledger_shape_of_this_work_order_only(client, db_session):
    """The read uses the shared ``work_order_ledger_filter``: an operation-scoped tie ISSUE
    (``reference_id`` = the OPERATION) and a legacy ``work_order`` ISSUE count; another work
    order's row does not. A narrower ``reference_type`` filter would drop the operation row --
    for a laser nest that is the job's whole material leg."""
    admin = make_user(db_session)
    wo, part = make_wo(db_session)
    other_wo, _ = make_wo(db_session)
    op = db_session.query(WorkOrderOperation).filter(WorkOrderOperation.work_order_id == wo.id).one()

    def issue(reference_type, reference_id, cost):
        db_session.add(
            InventoryTransaction(
                created_by=admin.id,
                part_id=part.id,
                transaction_type=TransactionType.ISSUE,
                quantity=-1.0,
                reference_type=reference_type,
                reference_id=reference_id,
                unit_cost=cost,
                total_cost=-cost,
                company_id=COMPANY_A,
            )
        )
        db_session.commit()

    issue("work_order_operation", op.id, 40.0)
    issue("work_order", wo.id, 2.5)
    issue("work_order_backflush", other_wo.id, 1000.0)

    assert get_by_id(client, admin, wo.id)["issued_material_cost"] == 42.5
    assert get_by_id(client, admin, other_wo.id)["issued_material_cost"] == 1000.0
