from datetime import date, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models.mrp import PlanningAction
from app.models.part import Part
from app.services.mrp_service import MRPService


@pytest.mark.requires_db
def test_mrp_orders_material_supply_shortages_and_manufactures_engineering_parts(db_session: Session):
    parts = [
        Part(part_number="MRP-MFG-001", name="Machined Part", part_type="manufactured", unit_of_measure="each", company_id=1),
        Part(part_number="MRP-ASM-001", name="Assembly", part_type="assembly", unit_of_measure="each", company_id=1),
        Part(part_number="MRP-BUY-001", name="Purchased Item", part_type="purchased", unit_of_measure="each", company_id=1),
        Part(part_number="MRP-RAW-001", name="Raw Sheet", part_type="raw_material", unit_of_measure="sheets", company_id=1),
        Part(part_number="MRP-HW-001", name="Hardware", part_type="hardware", unit_of_measure="each", company_id=1),
        Part(part_number="MRP-CON-001", name="Consumable", part_type="consumable", unit_of_measure="gallons", company_id=1),
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

    _, actions = MRPService(db_session).calculate_shortages_and_actions(aggregated, include_safety_stock=False)
    part_numbers_by_id = {part.id: part.part_number for part in parts}
    actions_by_number = {part_numbers_by_id[action.part_id]: action.action_type for action in actions}

    assert actions_by_number["MRP-MFG-001"] == PlanningAction.MANUFACTURE
    assert actions_by_number["MRP-ASM-001"] == PlanningAction.MANUFACTURE
    assert actions_by_number["MRP-BUY-001"] == PlanningAction.ORDER
    assert actions_by_number["MRP-RAW-001"] == PlanningAction.ORDER
    assert actions_by_number["MRP-HW-001"] == PlanningAction.ORDER
    assert actions_by_number["MRP-CON-001"] == PlanningAction.ORDER
