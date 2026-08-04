"""``PredictionService._calculate_wo_demand`` was the one BOM-line reader in the backend
that did not reach its lines through their ``BOM`` header.

    self.db.query(BOMItem).filter(BOMItem.component_part_id == part_id).all()

No join, no ``company_id``, no ``is_deleted`` -- and then, per line, an equally unscoped
work-order query. Three defects rode on that:

* **It crashed.** The loop ends ``demand += remaining_qty * item.quantity_per`` and
  ``BOMItem`` has no ``quantity_per`` column; it is ``quantity`` (``quantity_per`` is the
  name the BOM *response schema* gives it, and ``quantity_per_assembly`` is an RFQ-quote
  column). Reaching that line raised ``AttributeError``, which ``app/main.py``'s handler
  turns into a **500** on ``GET /analytics/predict/inventory-demand``, for any tenant with
  a purchased or raw-material part appearing on any BOM. (Under ``TestClient`` the
  exception propagates instead of being rendered, so
  ``test_the_stockout_endpoint_no_longer_500s`` fails by raising -- same defect, and it is
  the user-visible half of this file either way.)
* **Invariant 1.** ``BOMItem`` carries ``TenantMixin``. Every tenant's lines, and every
  tenant's open work orders, summed into one number.
* **Invariant 3.** ``delete_bom`` is now a SOFT delete that deliberately RETAINS the lines,
  on the stated premise that nothing reads a deleted BOM's lines. This reader broke that
  premise -- where the old hard delete removed the lines, retention would have left them
  generating demand permanently.

**A note on what "fails against pre-fix code" means here.** The ``quantity_per``
``AttributeError`` fires before any of the scoping tests can observe a wrong *number*, so
against genuinely-pre-fix code every test below errors rather than asserting. Each scoping
test was therefore re-run against the pre-fix body with that ONE typo corrected in place
and nothing else changed, and each one failed on the number:

===================================================  ========  ========
case                                                 pre-fix   correct
===================================================  ========  ========
soft-deleted BOM's retained lines                        80.0      50.0
soft-deleted work order                                 440.0      40.0
another tenant's mis-parented BOM line                   80.0      50.0
another tenant's work order on the same parent         2020.0      20.0
===================================================  ========  ========

So all four are real defects in their own right, not artifacts implied by the crash.

The return value is CURRENTLY DISCARDED by ``predict_inventory_demand`` (the stockout model
runs off historical daily usage), so none of this changes a number a user sees today. It is
tested at the unit level for exactly that reason: the endpoint cannot observe the value, so
only a direct call can pin it before someone wires it up.
"""

from datetime import date, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.bom import BOM, BOMItem
from app.models.company import Company
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.services.prediction_service import PredictionService

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True)
        db.add(company)
        db.commit()
    return company


def make_user(db: Session, *, role: UserRole = UserRole.MANAGER, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"pred-{n}@co{company_id}.test",
        employee_id=f"PRED-{n:05d}",
        first_name="Pred",
        last_name="Iction",
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


def make_part(db: Session, *, part_type: str = "manufactured", company_id: int = COMPANY_A) -> Part:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"PRED-P-{n}",
        name=f"Part {n}",
        description="prediction-scoping fixture part",
        part_type=part_type,
        unit_of_measure="each",
        standard_cost=1.0,
        is_active=True,
        is_deleted=False,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_bom_with_line(
    db: Session,
    assembly: Part,
    component: Part,
    *,
    quantity: float,
    company_id: int = COMPANY_A,
    is_deleted: bool = False,
    is_active: bool = True,
) -> BOM:
    bom = BOM(
        part_id=assembly.id,
        revision="A",
        status="released",
        is_active=is_active,
        is_deleted=is_deleted,
        company_id=company_id,
    )
    db.add(bom)
    db.flush()
    db.add(
        BOMItem(
            bom_id=bom.id,
            component_part_id=component.id,
            item_number=10,
            quantity=quantity,
            item_type="buy",
            line_type="component",
            unit_of_measure="each",
            company_id=company_id,
        )
    )
    db.commit()
    db.refresh(bom)
    return bom


def make_open_wo(
    db: Session,
    part: Part,
    *,
    ordered: float,
    complete: float = 0.0,
    company_id: int = COMPANY_A,
    wo_status: WorkOrderStatus = WorkOrderStatus.RELEASED,
) -> WorkOrder:
    n = _next()
    wo = WorkOrder(
        work_order_number=f"PRED-WO-{n:05d}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=ordered,
        quantity_complete=complete,
        status=wo_status,
        priority=5,
        due_date=date.today() + timedelta(days=30),
        company_id=company_id,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def demand_for(db: Session, component: Part) -> float:
    return PredictionService(db)._calculate_wo_demand(component.id, component.company_id)


# ===========================================================================
# The crash
# ===========================================================================


def test_wo_demand_reads_the_bom_quantity_column(db_session: Session):
    """WOULD FAIL AGAINST PRE-FIX CODE (``AttributeError: 'BOMItem' object has no attribute
    'quantity_per'``). The assertion is on the NUMBER, not merely on "it did not raise":
    the whole point of the column being ``quantity`` is that the demand is
    ``remaining x qty-per-assembly``, and a fix that silently defaulted to 1.0 would also
    stop the crash while giving the wrong answer."""
    component = make_part(db_session, part_type="raw_material")
    assembly = make_part(db_session)
    make_bom_with_line(db_session, assembly, component, quantity=3.0)
    make_open_wo(db_session, assembly, ordered=10, complete=2)

    assert demand_for(db_session, component) == pytest.approx(24.0)  # (10 - 2) * 3


def test_the_stockout_endpoint_no_longer_500s(client: TestClient, db_session: Session):
    """The user-visible failure. ``GET /analytics/predict/inventory-demand`` walks every
    active purchased / raw-material part and calls the helper for each, so ONE such part
    appearing on ONE BOM was enough to 500 the whole report."""
    manager = make_user(db_session, role=UserRole.MANAGER)
    component = make_part(db_session, part_type="purchased")
    assembly = make_part(db_session)
    make_bom_with_line(db_session, assembly, component, quantity=2.0)
    make_open_wo(db_session, assembly, ordered=5)

    response = client.get("/api/v1/analytics/predict/inventory-demand", headers=headers_for(manager))

    assert response.status_code == status.HTTP_200_OK, response.text
    assert component.id in [p["part_id"] for p in response.json()["predictions"]]


# ===========================================================================
# Invariant 3 -- a soft-deleted BOM's retained lines are not demand
# ===========================================================================


def test_a_soft_deleted_boms_retained_lines_stop_feeding_demand(db_session: Session):
    """The reason ``delete_bom`` is allowed to keep the lines. Two BOMs consume the same
    component; one is tombstoned. Only the live one may contribute, and the number says
    WHICH -- an assertion of ``> 0`` or ``!= 0`` would pass with both counted."""
    component = make_part(db_session, part_type="raw_material")

    live_assembly = make_part(db_session)
    make_bom_with_line(db_session, live_assembly, component, quantity=5.0)
    make_open_wo(db_session, live_assembly, ordered=10)  # 10 * 5 = 50

    deleted_assembly = make_part(db_session)
    make_bom_with_line(db_session, deleted_assembly, component, quantity=3.0, is_deleted=True, is_active=False)
    make_open_wo(db_session, deleted_assembly, ordered=10)  # 30, must NOT count

    assert demand_for(db_session, component) == pytest.approx(50.0)


def test_a_soft_deleted_work_order_is_not_open_demand(db_session: Session):
    """``WorkOrder`` carries ``SoftDeleteMixin`` too and the work-order half of this helper
    did not filter it either -- a deleted job kept its material on order forever."""
    component = make_part(db_session, part_type="raw_material")
    assembly = make_part(db_session)
    make_bom_with_line(db_session, assembly, component, quantity=4.0)
    live = make_open_wo(db_session, assembly, ordered=10)  # 40
    dead = make_open_wo(db_session, assembly, ordered=100)  # 400, must NOT count
    dead.is_deleted = True
    db_session.commit()
    assert live.is_deleted is False

    assert demand_for(db_session, component) == pytest.approx(40.0)


def test_a_closed_work_order_is_still_excluded(db_session: Session):
    """The pre-existing status filter has to survive the rewrite -- the query was rebuilt
    from a per-line loop into one batched ``IN`` read, and that is exactly the kind of
    change that drops a predicate."""
    component = make_part(db_session, part_type="raw_material")
    assembly = make_part(db_session)
    make_bom_with_line(db_session, assembly, component, quantity=4.0)
    make_open_wo(db_session, assembly, ordered=10)  # 40
    make_open_wo(db_session, assembly, ordered=100, wo_status=WorkOrderStatus.COMPLETE)

    assert demand_for(db_session, component) == pytest.approx(40.0)


# ===========================================================================
# Invariant 1 -- another tenant's rows are not this tenant's demand
# ===========================================================================


def test_another_tenants_bom_line_does_not_contribute(db_session: Session):
    """A mis-parented line: a COMPANY B BOM whose component FK points at a COMPANY A part.
    That is the shape invariant 1 exists for, and it is the shape the unscoped
    ``component_part_id == part_id`` filter matched happily. Company B's demand must not
    show up in company A's forecast (and, symmetrically, must not leak the existence of
    company B's jobs into a number company A reads)."""
    component = make_part(db_session, part_type="raw_material", company_id=COMPANY_A)

    own_assembly = make_part(db_session, company_id=COMPANY_A)
    make_bom_with_line(db_session, own_assembly, component, quantity=5.0, company_id=COMPANY_A)
    make_open_wo(db_session, own_assembly, ordered=10, company_id=COMPANY_A)  # 50

    foreign_assembly = make_part(db_session, company_id=COMPANY_B)
    make_bom_with_line(db_session, foreign_assembly, component, quantity=3.0, company_id=COMPANY_B)
    make_open_wo(db_session, foreign_assembly, ordered=10, company_id=COMPANY_B)  # 30, must NOT count

    assert demand_for(db_session, component) == pytest.approx(50.0)


def test_another_tenants_work_order_on_the_same_parent_does_not_contribute(db_session: Session):
    """The second unscoped read, pinned separately. Scoping only the LINE query would still
    have let a foreign work order against the same parent part id inflate the number."""
    component = make_part(db_session, part_type="raw_material", company_id=COMPANY_A)
    assembly = make_part(db_session, company_id=COMPANY_A)
    make_bom_with_line(db_session, assembly, component, quantity=2.0, company_id=COMPANY_A)
    make_open_wo(db_session, assembly, ordered=10, company_id=COMPANY_A)  # 20

    # Same parent part id, a company B job. Cross-tenant by construction, like the line above.
    make_open_wo(db_session, assembly, ordered=1000, company_id=COMPANY_B)

    assert demand_for(db_session, component) == pytest.approx(20.0)


def test_a_correctly_scoped_tenant_sees_the_same_number_as_before(db_session: Session):
    """The counterweight to both scoping tests: adding ``company_id`` must not change the
    answer for a shop whose data is entirely its own, which is every real shop. If this
    drifts, the scoping predicates are filtering something they should not."""
    component = make_part(db_session, part_type="raw_material")
    first = make_part(db_session)
    second = make_part(db_session)
    make_bom_with_line(db_session, first, component, quantity=2.0)
    make_bom_with_line(db_session, second, component, quantity=7.0)
    make_open_wo(db_session, first, ordered=10, complete=4)  # 6 * 2 = 12
    make_open_wo(db_session, second, ordered=3)  # 3 * 7 = 21

    assert demand_for(db_session, component) == pytest.approx(33.0)


def test_a_component_on_no_bom_has_no_demand(db_session: Session):
    """The empty path returns 0.0 rather than raising or short-circuiting oddly -- it is the
    common case for a newly-created stock item."""
    assert demand_for(db_session, make_part(db_session, part_type="raw_material")) == pytest.approx(0.0)
