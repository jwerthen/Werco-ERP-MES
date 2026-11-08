"""Owner ruling 3 (2026-11-05): nothing AI-driven executes without a named person clicking Accept.

The always-on auto-execute agent used to run last inside the learning aggregation -- from the
nightly cron and from ``POST /ai/aggregate`` -- and applied allowlisted actions as a borrowed
admin, with a deterministic fallback when AI egress was off. These tests seed exactly the cards
it used to pick (high confidence, high priority, allowlisted, even tagged ``auto_execute``) and
prove the aggregation now leaves every one of them pending and every record untouched, with
no Claude call, whatever the egress setting.
"""

from unittest.mock import MagicMock

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.ai_learning import AIRecommendation
from app.models.audit_log import AuditLog
from app.models.part import Part
from app.models.purchasing import PurchaseOrder, Vendor
from app.models.quality import NonConformanceReport
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.services import llm_client
from app.services.ai_learning_service import AILearningService

pytestmark = [pytest.mark.requires_db]


def _seed_former_auto_execute_targets(db: Session) -> dict:
    vendor = Vendor(code="V-AUTO", name="Vendor Auto", is_active=True, company_id=1)
    db.add(vendor)
    db.flush()
    part = Part(
        part_number="AUTO-PART",
        name="Auto part",
        part_type="raw_material",
        unit_of_measure="each",
        is_active=True,
        company_id=1,
        primary_supplier_id=vendor.id,
        reorder_quantity=25,
        standard_cost=4.0,
    )
    db.add(part)
    db.flush()
    work_order = WorkOrder(
        work_order_number="WO-AUTO-1",
        part_id=part.id,
        quantity_ordered=1,
        status=WorkOrderStatus.RELEASED,
        priority=5,
        company_id=1,
    )
    db.add(work_order)
    db.flush()

    actions = [
        (
            "scheduling",
            "at_risk_delivery",
            "work_order",
            work_order.id,
            {"type": "adjust_work_order_priority", "work_order_id": work_order.id, "priority": 1},
        ),
        ("inventory", "inventory_risk", "part", part.id, {"type": "create_draft_po", "part_id": part.id}),
        (
            "quality",
            "quality_trend",
            "part",
            part.id,
            {"type": "create_draft_ncr", "part_id": part.id, "title": "Elevated scrap on AUTO-PART"},
        ),
    ]
    recs = []
    for source, rtype, target_type, target_id, action in actions:
        rec = AIRecommendation(
            company_id=1,
            source_module=source,
            recommendation_type=rtype,
            status="pending",
            priority="high",
            title=f"{rtype} card",
            summary="High-confidence allowlisted action",
            target_entity_type=target_type,
            target_entity_id=target_id,
            suggested_action={**action, "autonomy": "apply_on_accept"},
            confidence_score=0.95,
        )
        db.add(rec)
        db.flush()
        # The ORM @validates refuses the old tier, so retag the way legacy rows exist: Core.
        db.execute(
            update(AIRecommendation)
            .where(AIRecommendation.id == rec.id)
            .values(suggested_action={**action, "autonomy": "auto_execute"})
        )
        recs.append(rec.id)
    db.commit()
    db.expire_all()
    return {"work_order_id": work_order.id, "recommendation_ids": recs}


def _assert_nothing_executed(db: Session, seeded: dict, anthropic: MagicMock) -> None:
    db.expire_all()
    anthropic.assert_not_called()
    assert db.query(WorkOrder).filter(WorkOrder.id == seeded["work_order_id"]).one().priority == 5
    assert db.query(PurchaseOrder).count() == 0
    assert db.query(NonConformanceReport).count() == 0
    for rec_id in seeded["recommendation_ids"]:
        rec = db.query(AIRecommendation).filter(AIRecommendation.id == rec_id).one()
        assert rec.status == "pending"
        assert rec.accepted_by is None
    assert db.query(AuditLog).filter(AuditLog.resource_type.in_(["work_order", "purchase_order", "ncr"])).count() == 0


@pytest.fixture
def anthropic(monkeypatch) -> MagicMock:
    # Looked up at call time inside run_llm_task, so this also catches modules that bound
    # ``run_llm_task`` by name.
    client = MagicMock(name="anthropic_client")
    monkeypatch.setattr(llm_client, "get_anthropic_client", client)
    return client


@pytest.mark.unit
def test_aggregation_never_executes_or_calls_claude(db_session: Session, anthropic: MagicMock):
    seeded = _seed_former_auto_execute_targets(db_session)

    for _ in range(2):
        summary = AILearningService(db_session).aggregate_learning_signals(company_ids=[1])
        db_session.commit()
        assert "auto_executed" not in summary
        assert "auto_execute_failed" not in summary

    _assert_nothing_executed(db_session, seeded, anthropic)


@pytest.mark.unit
def test_aggregation_never_executes_with_egress_off(db_session: Session, anthropic: MagicMock, monkeypatch):
    # The old agent's deterministic fallback ran exactly here: egress off -> execute anyway.
    monkeypatch.setattr(llm_client, "_ai_egress_allowed", lambda company_id=None: False)
    seeded = _seed_former_auto_execute_targets(db_session)

    for _ in range(2):
        AILearningService(db_session).aggregate_learning_signals(company_ids=[1])
        db_session.commit()

    _assert_nothing_executed(db_session, seeded, anthropic)


@pytest.mark.api
def test_post_ai_aggregate_never_executes(client, supervisor_headers: dict, db_session: Session, anthropic):
    seeded = _seed_former_auto_execute_targets(db_session)

    response = client.post("/api/v1/ai/aggregate", headers=supervisor_headers)

    assert response.status_code == 200, response.text
    assert "auto_executed" not in response.json()
    assert "closed_target_recommendations_staled" in response.json()
    _assert_nothing_executed(db_session, seeded, anthropic)
