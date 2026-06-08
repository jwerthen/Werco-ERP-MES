from datetime import date, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models.bom import BOM, BOMItem
from app.models.company import Company
from app.models.inventory import InventoryItem
from app.models.mrp import MRPAction, MRPRequirement, MRPRun, MRPRunStatus, PlanningAction
from app.models.part import Part
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.services.mrp_service import MRPService


def _seed_company(db: Session, company_id: int, slug: str) -> None:
    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Co {company_id}", slug=slug, is_active=True))
        db.commit()


def _seed_make_wo_with_purchased_component(db: Session, company_id: int, suffix: str) -> Part:
    """Create, for one tenant, a make part with a BOM that needs one purchased
    component, plus a RELEASED work order driving demand for it. Returns the
    purchased component part."""
    assembly = Part(
        part_number=f"ASM-{suffix}",
        name="Assembly",
        part_type="manufactured",
        unit_of_measure="each",
        lead_time_days=0,
        company_id=company_id,
    )
    component = Part(
        part_number=f"BUY-{suffix}",
        name="Purchased Component",
        part_type="purchased",
        unit_of_measure="each",
        lead_time_days=0,
        safety_stock=0,
        company_id=company_id,
    )
    db.add_all([assembly, component])
    db.commit()

    bom = BOM(part_id=assembly.id, is_active=True, status="released", company_id=company_id)
    db.add(bom)
    db.commit()
    db.add(
        BOMItem(
            bom_id=bom.id,
            component_part_id=component.id,
            item_number=10,
            quantity=2.0,
            item_type="buy",
            line_type="component",
            scrap_factor=0.0,
            lead_time_offset=0,
            is_alternate=False,
            company_id=company_id,
        )
    )
    db.add(
        WorkOrder(
            work_order_number=f"WO-{suffix}",
            part_id=assembly.id,
            quantity_ordered=5.0,
            quantity_complete=0.0,
            status=WorkOrderStatus.RELEASED,
            due_date=date.today() + timedelta(days=7),
            company_id=company_id,
        )
    )
    db.commit()
    return component


@pytest.mark.requires_db
def test_run_mrp_persists_company_id_and_isolates_tenants(db_session: Session):
    """MS-1/MS-3/MS-4: run_mrp must stamp company_id on every persisted row
    (no NOT NULL violation) and must only see its own tenant's WOs/inventory."""
    _seed_company(db_session, 1, "co-1")
    _seed_company(db_session, 2, "co-2")

    component_a = _seed_make_wo_with_purchased_component(db_session, company_id=1, suffix="A")
    # Company 2 has identical demand AND on-hand inventory that fully covers it.
    component_b = _seed_make_wo_with_purchased_component(db_session, company_id=2, suffix="B")
    db_session.add(
        InventoryItem(
            part_id=component_b.id,
            location="MAIN",
            quantity_on_hand=10_000.0,
            quantity_allocated=0.0,
            status="available",
            is_active=True,
            company_id=2,
        )
    )
    db_session.commit()

    run = MRPService(db_session, company_id=1).run_mrp(user_id=None, include_safety_stock=False)

    # MS-1: the run completed (no NOT NULL violation) and is stamped to company 1.
    assert run.status == MRPRunStatus.COMPLETE
    assert run.company_id == 1

    persisted_run = db_session.query(MRPRun).filter(MRPRun.id == run.id).first()
    assert persisted_run is not None and persisted_run.company_id == 1

    reqs = db_session.query(MRPRequirement).filter(MRPRequirement.mrp_run_id == run.id).all()
    actions = db_session.query(MRPAction).filter(MRPAction.mrp_run_id == run.id).all()
    assert reqs, "expected at least one requirement"
    assert actions, "expected at least one shortage action (company 1 has no inventory)"

    # MS-1: child rows carry company_id == 1.
    assert all(r.company_id == 1 for r in reqs)
    assert all(a.company_id == 1 for a in actions)

    # MS-3/MS-4: the run only saw company 1's part — never company 2's identical
    # part, and never netted against company 2's covering inventory.
    seen_part_ids = {r.part_id for r in reqs} | {a.part_id for a in actions}
    assert component_a.id in seen_part_ids
    assert component_b.id not in seen_part_ids


@pytest.mark.requires_db
def test_run_mrp_nets_against_only_own_inventory(db_session: Session):
    """MS-4 isolation: on-hand from another tenant must not satisfy this tenant's
    demand. With company 1's own inventory covering demand, no shortage arises;
    company 2's stock for the same part number is irrelevant."""
    _seed_company(db_session, 1, "co-1")
    _seed_company(db_session, 2, "co-2")

    component_a = _seed_make_wo_with_purchased_component(db_session, company_id=1, suffix="A")
    _seed_make_wo_with_purchased_component(db_session, company_id=2, suffix="B")

    # Company 1's own on-hand fully covers its demand (5 WO * qty 2 = 10 needed).
    db_session.add(
        InventoryItem(
            part_id=component_a.id,
            location="MAIN",
            quantity_on_hand=1_000.0,
            quantity_allocated=0.0,
            status="available",
            is_active=True,
            company_id=1,
        )
    )
    db_session.commit()

    run = MRPService(db_session, company_id=1).run_mrp(user_id=None, include_safety_stock=False)
    assert run.status == MRPRunStatus.COMPLETE

    actions = db_session.query(MRPAction).filter(MRPAction.mrp_run_id == run.id).all()
    # No shortage actions for the covered component.
    assert all(a.part_id != component_a.id for a in actions)


@pytest.mark.requires_db
def test_mrp_orders_material_supply_shortages_and_manufactures_engineering_parts(db_session: Session):
    parts = [
        Part(
            part_number="MRP-MFG-001",
            name="Machined Part",
            part_type="manufactured",
            unit_of_measure="each",
            company_id=1,
        ),
        Part(part_number="MRP-ASM-001", name="Assembly", part_type="assembly", unit_of_measure="each", company_id=1),
        Part(
            part_number="MRP-BUY-001",
            name="Purchased Item",
            part_type="purchased",
            unit_of_measure="each",
            company_id=1,
        ),
        Part(
            part_number="MRP-RAW-001",
            name="Raw Sheet",
            part_type="raw_material",
            unit_of_measure="sheets",
            company_id=1,
        ),
        Part(part_number="MRP-HW-001", name="Hardware", part_type="hardware", unit_of_measure="each", company_id=1),
        Part(
            part_number="MRP-CON-001",
            name="Consumable",
            part_type="consumable",
            unit_of_measure="gallons",
            company_id=1,
        ),
    ]
    db_session.add_all(parts)
    db_session.commit()

    required_date = date.today() + timedelta(days=7)
    aggregated = {
        part.id: {
            "part_id": part.id,
            "part_number": part.part_number,
            "part_name": part.name,
            "part_type": part.part_type,
            "lead_time_days": 0,
            "by_date": {required_date.isoformat(): 5},
            "total_required": 5,
            "sources": [],
        }
        for part in parts
    }

    _, actions = MRPService(db_session, company_id=1).calculate_shortages_and_actions(
        aggregated, include_safety_stock=False
    )
    part_numbers_by_id = {part.id: part.part_number for part in parts}
    actions_by_number = {part_numbers_by_id[action.part_id]: action.action_type for action in actions}

    assert actions_by_number["MRP-MFG-001"] == PlanningAction.MANUFACTURE
    assert actions_by_number["MRP-ASM-001"] == PlanningAction.MANUFACTURE
    assert actions_by_number["MRP-BUY-001"] == PlanningAction.ORDER
    assert actions_by_number["MRP-RAW-001"] == PlanningAction.ORDER
    assert actions_by_number["MRP-HW-001"] == PlanningAction.ORDER
    assert actions_by_number["MRP-CON-001"] == PlanningAction.ORDER
