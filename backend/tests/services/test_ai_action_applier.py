"""Phase 2 apply-with-approval tests."""

from datetime import date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.ai_learning import AIRecommendation
from app.models.part import Part
from app.models.purchasing import PurchaseOrder, Vendor
from app.models.quality import NonConformanceReport
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.models.work_order_blocker import (
    WorkOrderBlocker,
    WorkOrderBlockerCategory,
    WorkOrderBlockerSeverity,
    WorkOrderBlockerStatus,
)
from app.services.ai_action_applier import AIActionApplier, AIActionApplyError
from app.services.ai_learners import run_domain_learners
from app.services.ai_learners.cycle_time import run_cycle_time_learner
from app.services.ai_learners.estimate_calibration import run_estimate_calibration_learner
from app.services.ai_sensors.morning_brief import run_morning_brief_sensor
from app.models.work_order import OperationStatus, WorkOrderOperation
from app.models.work_center import WorkCenter


def _part(db: Session, number: str = "AP-1", **kwargs) -> Part:
    defaults = dict(
        part_number=number,
        name=f"Part {number}",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=1,
        standard_cost=10.0,
    )
    defaults.update(kwargs)
    p = Part(**defaults)
    db.add(p)
    db.flush()
    return p


@pytest.mark.unit
@pytest.mark.requires_db
class TestAIActionApplier:
    def test_adjust_priority(self, db_session: Session, admin_user):
        part = _part(db_session)
        wo = WorkOrder(
            work_order_number="WO-AP-1",
            part_id=part.id,
            quantity_ordered=1,
            status=WorkOrderStatus.RELEASED,
            priority=5,
            company_id=1,
        )
        db_session.add(wo)
        db_session.flush()
        rec = AIRecommendation(
            company_id=1,
            source_module="scheduling",
            recommendation_type="at_risk_delivery",
            title="Expedite",
            summary="Late",
            target_entity_type="work_order",
            target_entity_id=wo.id,
            suggested_action={
                "type": "adjust_work_order_priority",
                "work_order_id": wo.id,
                "priority": 1,
                "autonomy": "apply_on_accept",
            },
            confidence_score=0.9,
        )
        db_session.add(rec)
        db_session.commit()

        result = AIActionApplier(db_session, company_id=1, user=admin_user).apply(rec)
        db_session.commit()
        db_session.refresh(wo)
        assert result["new_priority"] == 1
        assert wo.priority == 1

    def test_create_draft_ncr(self, db_session: Session, admin_user):
        part = _part(db_session, "AP-NCR")
        rec = AIRecommendation(
            company_id=1,
            source_module="quality",
            recommendation_type="quality_trend",
            title="Elevated scrap on AP-NCR",
            summary="Scrap high",
            target_entity_type="part",
            target_entity_id=part.id,
            suggested_action={
                "type": "create_draft_ncr",
                "part_id": part.id,
                "title": "Elevated scrap on AP-NCR",
                "description": "AI quality sensor flagged elevated scrap for investigation.",
                "autonomy": "apply_on_accept",
            },
            confidence_score=0.8,
        )
        db_session.add(rec)
        db_session.commit()

        result = AIActionApplier(db_session, company_id=1, user=admin_user).apply(rec)
        db_session.commit()
        assert "ncr_id" in result
        ncr = db_session.query(NonConformanceReport).filter_by(id=result["ncr_id"]).one()
        assert ncr.part_id == part.id
        assert ncr.company_id == 1

    def test_create_draft_po(self, db_session: Session, admin_user):
        vendor = Vendor(code="V-AP", name="Vendor AP", company_id=1, is_active=True)
        db_session.add(vendor)
        db_session.flush()
        part = _part(db_session, "AP-PO", part_type="raw_material", primary_supplier_id=vendor.id, reorder_quantity=25)
        rec = AIRecommendation(
            company_id=1,
            source_module="inventory",
            recommendation_type="inventory_risk",
            title="Low stock",
            summary="Reorder",
            target_entity_type="part",
            target_entity_id=part.id,
            suggested_action={
                "type": "create_draft_po",
                "part_id": part.id,
                "vendor_id": vendor.id,
                "suggested_qty": 25,
                "autonomy": "apply_on_accept",
            },
            confidence_score=0.85,
        )
        db_session.add(rec)
        db_session.commit()

        result = AIActionApplier(db_session, company_id=1, user=admin_user).apply(rec)
        db_session.commit()
        po = db_session.query(PurchaseOrder).filter_by(id=result["purchase_order_id"]).one()
        assert po.status.value == "draft" or str(po.status) == "draft"
        assert po.company_id == 1

    def test_escalate_blocker(self, db_session: Session, admin_user):
        part = _part(db_session, "AP-BLK")
        wo = WorkOrder(
            work_order_number="WO-BLK-1",
            part_id=part.id,
            quantity_ordered=1,
            status=WorkOrderStatus.IN_PROGRESS,
            priority=6,
            company_id=1,
        )
        db_session.add(wo)
        db_session.flush()
        blocker = WorkOrderBlocker(
            company_id=1,
            work_order_id=wo.id,
            category=WorkOrderBlockerCategory.MATERIAL_MISSING.value,
            severity=WorkOrderBlockerSeverity.MEDIUM.value,
            status=WorkOrderBlockerStatus.OPEN.value,
            title="No stock",
            note="waiting",
            reported_at=datetime.utcnow(),
        )
        db_session.add(blocker)
        db_session.flush()
        rec = AIRecommendation(
            company_id=1,
            source_module="shop_floor",
            recommendation_type="stale_blocker_escalation",
            title="Escalate",
            summary="Stale",
            target_entity_type="work_order_blocker",
            target_entity_id=blocker.id,
            suggested_action={
                "type": "escalate_blocker",
                "blocker_id": blocker.id,
                "work_order_id": wo.id,
                "autonomy": "apply_on_accept",
            },
            confidence_score=0.9,
        )
        db_session.add(rec)
        db_session.commit()

        AIActionApplier(db_session, company_id=1, user=admin_user).apply(rec)
        db_session.commit()
        db_session.refresh(blocker)
        db_session.refresh(wo)
        assert blocker.status == WorkOrderBlockerStatus.ACKNOWLEDGED.value
        assert blocker.severity == WorkOrderBlockerSeverity.HIGH.value
        assert wo.priority == 2

    def test_unknown_action_raises(self, db_session: Session, admin_user):
        rec = AIRecommendation(
            company_id=1,
            source_module="test",
            recommendation_type="x",
            title="t",
            summary="s",
            suggested_action={"type": "not_a_real_action", "autonomy": "apply_on_accept"},
            confidence_score=0.5,
        )
        db_session.add(rec)
        db_session.commit()
        with pytest.raises(AIActionApplyError):
            AIActionApplier(db_session, company_id=1, user=admin_user).apply(rec)


@pytest.mark.api
@pytest.mark.requires_db
class TestAcceptApplyAPI:
    def test_accept_with_apply_priority(self, client: TestClient, admin_headers: dict, db_session: Session):
        part = _part(db_session, "API-P")
        wo = WorkOrder(
            work_order_number="WO-API-P",
            part_id=part.id,
            quantity_ordered=1,
            status=WorkOrderStatus.RELEASED,
            priority=7,
            company_id=1,
        )
        db_session.add(wo)
        db_session.flush()
        rec = AIRecommendation(
            company_id=1,
            source_module="scheduling",
            recommendation_type="at_risk_delivery",
            title="At risk",
            summary="Due soon",
            target_entity_type="work_order",
            target_entity_id=wo.id,
            suggested_action={
                "type": "adjust_work_order_priority",
                "work_order_id": wo.id,
                "priority": 1,
                "autonomy": "apply_on_accept",
            },
            confidence_score=0.9,
        )
        db_session.add(rec)
        db_session.commit()

        response = client.post(
            f"/api/v1/ai/recommendations/{rec.id}/accept",
            json={"reason": "Do it", "apply": True},
            headers=admin_headers,
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["applied"] is True
        assert body["recommendation"]["status"] == "accepted"
        assert body["apply_result"]["new_priority"] == 1
        db_session.refresh(wo)
        assert wo.priority == 1


@pytest.mark.unit
@pytest.mark.requires_db
class TestPhase3And4:
    def test_morning_brief(self, db_session: Session):
        part = _part(db_session, "MB-1")
        db_session.add(
            WorkOrder(
                work_order_number="WO-MB-LATE",
                part_id=part.id,
                quantity_ordered=1,
                status=WorkOrderStatus.IN_PROGRESS,
                priority=1,
                due_date=date.today() - timedelta(days=2),
                company_id=1,
            )
        )
        db_session.commit()
        assert run_morning_brief_sensor(db_session, 1) == 1
        db_session.commit()
        assert run_morning_brief_sensor(db_session, 1) == 0  # dedupe same day
        rec = (
            db_session.query(AIRecommendation)
            .filter(AIRecommendation.recommendation_type == "morning_brief", AIRecommendation.company_id == 1)
            .one()
        )
        assert "late" in rec.summary.lower() or "Late" in rec.summary

    def test_estimate_calibration_learner(self, db_session: Session):
        part = _part(db_session, "EC-1")
        now = datetime.utcnow()
        for i in range(3):
            db_session.add(
                WorkOrder(
                    work_order_number=f"WO-EC-{i}",
                    part_id=part.id,
                    quantity_ordered=1,
                    status=WorkOrderStatus.COMPLETE,
                    actual_end=now - timedelta(days=i + 1),
                    estimated_cost=100.0,
                    actual_cost=150.0,
                    company_id=1,
                )
            )
        db_session.commit()
        created = run_estimate_calibration_learner(db_session, 1)
        db_session.commit()
        assert created >= 1

    def test_cycle_time_learner(self, db_session: Session):
        wc = WorkCenter(
            code="WC-CT",
            name="Cycle Cell",
            work_center_type="laser",
            is_active=True,
            company_id=1,
        )
        db_session.add(wc)
        db_session.flush()
        part = _part(db_session, "CT-1")
        wo = WorkOrder(
            work_order_number="WO-CT",
            part_id=part.id,
            quantity_ordered=1,
            status=WorkOrderStatus.COMPLETE,
            company_id=1,
        )
        db_session.add(wo)
        db_session.flush()
        now = datetime.utcnow()
        for i in range(5):
            db_session.add(
                WorkOrderOperation(
                    work_order_id=wo.id,
                    work_center_id=wc.id,
                    sequence=10 + i,
                    operation_number=f"Op {10 + i}",
                    name="Cut",
                    status=OperationStatus.COMPLETE,
                    setup_time_hours=0.5,
                    run_time_hours=1.0,
                    actual_setup_hours=1.0,
                    actual_run_hours=2.0,
                    actual_end=now - timedelta(days=i + 1),
                    company_id=1,
                )
            )
        db_session.commit()
        created = run_cycle_time_learner(db_session, 1)
        db_session.commit()
        assert created >= 1

    def test_run_domain_learners_smoke(self, db_session: Session):
        counts = run_domain_learners(db_session, 1)
        assert set(counts.keys()) == {"cycle_time", "estimate_calibration", "correction_preference"}
