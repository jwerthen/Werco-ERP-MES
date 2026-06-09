"""Behavior locks for the MRPAutoService PurchaseOrderLine column fix
(fix/wo-remediation-followups, FIX 4).

``MRPAutoService._create_po_from_action`` constructed ``PurchaseOrderLine`` with non-existent
columns (``po_id`` / ``quantity`` / ``unit_cost``) -> ``TypeError`` at construction, so an
AUTO_DRAFT MRP run could never actually draft a PO. The fix uses the real columns
``purchase_order_id`` / ``quantity_ordered`` / ``unit_price`` and also sets ``line_total`` and
``company_id``.

This exercises the real service path (``process_actions`` -> ``_create_po_from_action``) for an
ORDER action and asserts the persisted PO line carries the correct columns and that the
construction no longer raises. Tenant scope is asserted too: the line + PO are stamped with the
service's ``company_id``.
"""

from datetime import date, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.mrp import MRPAction, MRPRun, PlanningAction
from app.models.part import Part
from app.models.purchasing import POStatus, PurchaseOrder, PurchaseOrderLine, Vendor
from app.services.mrp_auto_service import MRPAutoMode, MRPAutoService

pytestmark = [pytest.mark.requires_db]

COMPANY_A = 1
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


def _make_part(db: Session, *, company_id: int = COMPANY_A) -> Part:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"MRP-AUTO-P-{n}",
        name=f"Part {n}",
        description="mrp auto fixture part",
        part_type="purchased",
        unit_of_measure="each",
        standard_cost=12.5,
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def _make_vendor(db: Session, *, company_id: int = COMPANY_A) -> Vendor:
    _ensure_company(db, company_id)
    n = _next()
    vendor = Vendor(
        name=f"Vendor {n}",
        code=f"MRP-V-{n}",
        is_active=True,
        company_id=company_id,
    )
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return vendor


def _make_run(db: Session, *, company_id: int = COMPANY_A) -> MRPRun:
    n = _next()
    run = MRPRun(run_number=f"MRP-RUN-{n}", company_id=company_id)
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _make_order_action(
    db: Session, run: MRPRun, part: Part, *, qty: float = 8, company_id: int = COMPANY_A
) -> MRPAction:
    action = MRPAction(
        mrp_run_id=run.id,
        part_id=part.id,
        action_type=PlanningAction.ORDER,
        priority=3,
        quantity=qty,
        required_date=date.today() + timedelta(days=14),
        suggested_order_date=date.today(),
        company_id=company_id,
    )
    db.add(action)
    db.commit()
    db.refresh(action)
    return action


def test_auto_draft_creates_po_line_with_correct_columns(db_session: Session):
    part = _make_part(db_session)
    _make_vendor(db_session)  # _get_preferred_vendor falls back to first active vendor
    run = _make_run(db_session)
    action = _make_order_action(db_session, run, part, qty=8)
    db_session.commit()

    service = MRPAutoService(db_session, company_id=COMPANY_A)
    # The bug was a TypeError at PurchaseOrderLine(...) construction -- this must not raise.
    results = service.process_actions(actions=[action], mode=MRPAutoMode.AUTO_DRAFT, user_id=None)

    assert results["errors"] == 0, f"action errored: {action.error_message}"
    assert results["pos_created"] == 1

    po = db_session.query(PurchaseOrder).filter(PurchaseOrder.company_id == COMPANY_A).first()
    assert po is not None
    assert po.status == POStatus.DRAFT

    line = db_session.query(PurchaseOrderLine).filter(PurchaseOrderLine.purchase_order_id == po.id).first()
    assert line is not None, "PurchaseOrderLine was not persisted"

    # The corrected column names are populated.
    assert line.purchase_order_id == po.id
    assert line.quantity_ordered == 8
    assert line.unit_price == part.standard_cost  # falls back to standard cost
    assert line.line_total == pytest.approx(8 * part.standard_cost)
    assert line.company_id == COMPANY_A
    assert line.line_number == 1
    assert line.part_id == part.id

    # The action was marked processed and linked to the PO.
    db_session.refresh(action)
    assert action.processed is True
    assert action.result_po_id == po.id


def test_purchase_order_line_constructs_with_service_kwargs(db_session: Session):
    """Minimal regression guard at the construction boundary: the exact kwargs the service
    uses must instantiate AND persist (the original bug was a TypeError from po_id/quantity/
    unit_cost). Asserts the corrected column set is accepted by the model."""
    part = _make_part(db_session)
    vendor = _make_vendor(db_session)
    po = PurchaseOrder(
        company_id=COMPANY_A,
        po_number=f"PO-FU-{_next()}",
        vendor_id=vendor.id,
        status=POStatus.DRAFT,
        order_date=date.today(),
    )
    db_session.add(po)
    db_session.flush()

    line = PurchaseOrderLine(
        company_id=COMPANY_A,
        purchase_order_id=po.id,
        part_id=part.id,
        quantity_ordered=5,
        unit_price=3.0,
        line_total=15.0,
        line_number=1,
    )
    db_session.add(line)
    db_session.commit()
    db_session.refresh(line)

    assert line.id is not None
    assert line.purchase_order_id == po.id
    assert line.quantity_ordered == 5
    assert line.unit_price == 3.0
    assert line.line_total == 15.0
