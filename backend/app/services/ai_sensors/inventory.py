"""Inventory risk / below-reorder-point sensor."""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.inventory import InventoryItem
from app.models.part import Part
from app.services.ai_learning_service import AILearningService
from app.services.ai_sensors.common import mint_recommendation, recommendation_open

MAX_RECS_PER_RUN = 25


def run_inventory_risk_sensor(db: Session, company_id: int) -> int:
    """Mint recommend-only items for parts at or below reorder point / safety stock."""
    learning = AILearningService(db)

    on_hand_sq = (
        db.query(
            InventoryItem.part_id.label("part_id"),
            func.coalesce(func.sum(InventoryItem.quantity_on_hand), 0.0).label("on_hand"),
        )
        .filter(
            InventoryItem.company_id == company_id,
            InventoryItem.is_active == True,  # noqa: E712
        )
        .group_by(InventoryItem.part_id)
        .subquery()
    )

    rows = (
        db.query(Part, on_hand_sq.c.on_hand)
        .outerjoin(on_hand_sq, on_hand_sq.c.part_id == Part.id)
        .filter(
            Part.company_id == company_id,
            Part.is_active == True,  # noqa: E712
            Part.is_deleted == False,  # noqa: E712
            # Only parts that have an explicit reorder threshold
            (Part.reorder_point > 0) | (Part.safety_stock > 0),
        )
        .order_by(Part.part_number.asc())
        .limit(200)
        .all()
    )

    created = 0
    for part, on_hand in rows:
        if created >= MAX_RECS_PER_RUN:
            break
        stock = float(on_hand or 0.0)
        threshold = float(part.reorder_point or 0.0)
        safety = float(part.safety_stock or 0.0)
        trigger = max(threshold, safety)
        if trigger <= 0 or stock > trigger:
            continue

        if recommendation_open(
            learning,
            company_id=company_id,
            recommendation_type="inventory_risk",
            source_module="inventory",
            target_entity_type="part",
            target_entity_id=part.id,
        ):
            continue

        critical = stock <= safety if safety > 0 else stock <= 0
        priority = "high" if critical or bool(part.is_critical) else "medium"
        shortage = trigger - stock

        mint_recommendation(
            db,
            company_id=company_id,
            source_module="inventory",
            recommendation_type="inventory_risk",
            priority=priority,
            title=f"Low stock: {part.part_number}",
            summary=(
                f"{part.part_number} ({part.name}) is at {stock:g} on hand, "
                f"below reorder threshold {trigger:g} (short {shortage:g}). "
                "Review MRP / create a draft PO."
            ),
            rationale="Deterministic reorder-point sensor (no LLM).",
            target_entity_type="part",
            target_entity_id=part.id,
            suggested_action={
                "type": "create_draft_po",
                "part_id": part.id,
                "part_number": part.part_number,
                "vendor_id": part.primary_supplier_id,
                "href": f"/inventory?part={part.part_number}",
                "suggested_qty": float(part.reorder_quantity or shortage or 0),
                "autonomy": "auto_execute",
                "dedupe_key": f"inventory_risk:part:{part.id}",
            },
            evidence=[
                {
                    "type": "stock_level",
                    "on_hand": stock,
                    "reorder_point": threshold,
                    "safety_stock": safety,
                    "reorder_quantity": float(part.reorder_quantity or 0),
                    "is_critical": bool(part.is_critical),
                }
            ],
            impact={
                "expected": "Avoid material shortages that block open work orders.",
                "magnitude": 1.5 if critical else 1.0,
            },
            confidence_score=0.85 if critical else 0.7,
            expires_days=10,
        )
        created += 1

    return created
