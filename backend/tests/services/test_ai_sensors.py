"""Unit tests for Phase-1 always-on domain sensors + outcome capture."""

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models.ai_learning import AIOutcome, AIRecommendation
from app.models.inventory import InventoryItem
from app.models.part import Part
from app.models.quote import Quote, QuoteStatus
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.services.ai_learning_service import AILearningService
from app.services.ai_outcome_capture_service import (
    record_quote_status_outcome,
    record_work_order_completion_outcomes,
)
from app.services.ai_sensors import run_domain_sensors
from app.services.ai_sensors.delivery import run_at_risk_delivery_sensor
from app.services.ai_sensors.inventory import run_inventory_risk_sensor
from app.services.ai_sensors.quality import run_quality_trend_sensor
from app.services.completion_signal_service import emit_work_order_completed_event


def _part(db: Session, *, part_number: str, company_id: int = 1, **kwargs) -> Part:
    defaults = dict(
        part_number=part_number,
        name=f"Name {part_number}",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    defaults.update(kwargs)
    part = Part(**defaults)
    db.add(part)
    db.flush()
    return part


@pytest.mark.unit
@pytest.mark.requires_db
class TestDeliverySensor:
    def test_mints_late_and_at_risk_work_orders(self, db_session: Session):
        part = _part(db_session, part_number="DEL-1")
        late = WorkOrder(
            work_order_number="WO-LATE-1",
            part_id=part.id,
            quantity_ordered=10,
            status=WorkOrderStatus.IN_PROGRESS,
            priority=2,
            due_date=date.today() - timedelta(days=2),
            company_id=1,
        )
        at_risk = WorkOrder(
            work_order_number="WO-RISK-1",
            part_id=part.id,
            quantity_ordered=5,
            status=WorkOrderStatus.RELEASED,
            priority=5,
            due_date=date.today() + timedelta(days=1),
            company_id=1,
        )
        future = WorkOrder(
            work_order_number="WO-OK-1",
            part_id=part.id,
            quantity_ordered=5,
            status=WorkOrderStatus.RELEASED,
            priority=5,
            due_date=date.today() + timedelta(days=14),
            company_id=1,
        )
        db_session.add_all([late, at_risk, future])
        db_session.commit()

        created = run_at_risk_delivery_sensor(db_session, 1)
        db_session.commit()

        assert created == 2
        recs = (
            db_session.query(AIRecommendation)
            .filter(AIRecommendation.company_id == 1, AIRecommendation.recommendation_type == "at_risk_delivery")
            .all()
        )
        titles = {r.title for r in recs}
        assert any("Late work order WO-LATE-1" in t for t in titles)
        assert any("At-risk delivery WO-RISK-1" in t for t in titles)
        assert all(r.suggested_action.get("autonomy") == "auto_execute" for r in recs)
        assert all(r.suggested_action.get("type") == "adjust_work_order_priority" for r in recs)

        # Dedupe on second run
        assert run_at_risk_delivery_sensor(db_session, 1) == 0


@pytest.mark.unit
@pytest.mark.requires_db
class TestInventorySensor:
    def test_mints_below_reorder_point(self, db_session: Session):
        low = _part(
            db_session,
            part_number="INV-LOW",
            part_type="raw_material",
            reorder_point=50,
            safety_stock=10,
            reorder_quantity=100,
        )
        ok = _part(
            db_session,
            part_number="INV-OK",
            part_type="raw_material",
            reorder_point=50,
            safety_stock=10,
        )
        db_session.add(
            InventoryItem(
                part_id=low.id,
                location="A-1",
                quantity_on_hand=5,
                is_active=True,
                company_id=1,
            )
        )
        db_session.add(
            InventoryItem(
                part_id=ok.id,
                location="A-2",
                quantity_on_hand=200,
                is_active=True,
                company_id=1,
            )
        )
        db_session.commit()

        created = run_inventory_risk_sensor(db_session, 1)
        db_session.commit()

        assert created == 1
        rec = (
            db_session.query(AIRecommendation)
            .filter(AIRecommendation.recommendation_type == "inventory_risk", AIRecommendation.company_id == 1)
            .one()
        )
        assert rec.target_entity_id == low.id
        assert "INV-LOW" in rec.title
        assert rec.priority == "high"  # below safety stock
        assert run_inventory_risk_sensor(db_session, 1) == 0


@pytest.mark.unit
@pytest.mark.requires_db
class TestQualitySensor:
    def test_mints_elevated_scrap_trend(self, db_session: Session):
        bad = _part(db_session, part_number="Q-BAD")
        good = _part(db_session, part_number="Q-GOOD")
        now = datetime.utcnow()

        for i in range(3):
            db_session.add(
                WorkOrder(
                    work_order_number=f"WO-BAD-{i}",
                    part_id=bad.id,
                    quantity_ordered=100,
                    quantity_complete=80,
                    quantity_scrapped=20,
                    status=WorkOrderStatus.COMPLETE,
                    actual_end=now - timedelta(days=i + 1),
                    company_id=1,
                )
            )
        for i in range(3):
            db_session.add(
                WorkOrder(
                    work_order_number=f"WO-GOOD-{i}",
                    part_id=good.id,
                    quantity_ordered=100,
                    quantity_complete=99,
                    quantity_scrapped=1,
                    status=WorkOrderStatus.COMPLETE,
                    actual_end=now - timedelta(days=i + 1),
                    company_id=1,
                )
            )
        db_session.commit()

        created = run_quality_trend_sensor(db_session, 1)
        db_session.commit()

        assert created == 1
        rec = (
            db_session.query(AIRecommendation)
            .filter(AIRecommendation.recommendation_type == "quality_trend", AIRecommendation.company_id == 1)
            .one()
        )
        assert rec.target_entity_id == bad.id
        assert "Q-BAD" in rec.title


@pytest.mark.unit
@pytest.mark.requires_db
class TestDomainSensorFanout:
    def test_run_domain_sensors_returns_counts(self, db_session: Session):
        part = _part(db_session, part_number="FAN-1", reorder_point=10, safety_stock=5, part_type="raw_material")
        db_session.add(
            WorkOrder(
                work_order_number="WO-FAN-LATE",
                part_id=part.id,
                quantity_ordered=1,
                status=WorkOrderStatus.RELEASED,
                priority=1,
                due_date=date.today() - timedelta(days=1),
                company_id=1,
            )
        )
        db_session.add(InventoryItem(part_id=part.id, location="B-1", quantity_on_hand=0, is_active=True, company_id=1))
        db_session.commit()

        counts = run_domain_sensors(db_session, 1)
        db_session.commit()
        assert counts["at_risk_delivery"] >= 1
        assert counts["inventory_risk"] >= 1
        assert counts["quality_trend"] == 0


@pytest.mark.unit
@pytest.mark.requires_db
class TestOutcomeCapture:
    def test_work_order_completion_records_otd_and_scrap(self, db_session: Session):
        part = _part(db_session, part_number="OUT-1")
        wo = WorkOrder(
            work_order_number="WO-OUT-1",
            part_id=part.id,
            quantity_ordered=100,
            quantity_complete=90,
            quantity_scrapped=10,
            status=WorkOrderStatus.COMPLETE,
            due_date=date.today() - timedelta(days=1),
            actual_end=datetime.utcnow(),
            estimated_cost=1000.0,
            actual_cost=1200.0,
            company_id=1,
        )
        db_session.add(wo)
        db_session.commit()

        record_work_order_completion_outcomes(db_session, company_id=1, work_order=wo, user_id=1)
        db_session.commit()

        outcomes = db_session.query(AIOutcome).filter(AIOutcome.entity_id == wo.id).all()
        types = {o.outcome_type for o in outcomes}
        assert "on_time_delivery" in types
        assert "scrap_rate" in types
        assert "cost_variance" in types

        scrap = next(o for o in outcomes if o.outcome_type == "scrap_rate")
        assert scrap.metric_value == pytest.approx(0.1)
        otd = next(o for o in outcomes if o.outcome_type == "on_time_delivery")
        assert otd.metric_value == 0.0  # late

    def test_emit_work_order_completed_event_captures_outcomes(self, db_session: Session):
        part = _part(db_session, part_number="OUT-2")
        wo = WorkOrder(
            work_order_number="WO-OUT-2",
            part_id=part.id,
            quantity_ordered=10,
            quantity_complete=10,
            quantity_scrapped=0,
            status=WorkOrderStatus.COMPLETE,
            due_date=date.today() + timedelta(days=1),
            actual_end=datetime.utcnow(),
            company_id=1,
        )
        db_session.add(wo)
        db_session.commit()

        emit_work_order_completed_event(
            db_session,
            company_id=1,
            work_order=wo,
            user_id=None,
            source_module="shop_floor",
            source="kiosk",
        )
        db_session.commit()

        assert (
            db_session.query(AIOutcome)
            .filter(AIOutcome.entity_type == "work_order", AIOutcome.entity_id == wo.id)
            .count()
            >= 2
        )

    def test_quote_status_outcome_win_loss(self, db_session: Session):
        quote = Quote(
            quote_number="Q-OUT-1",
            customer_name="Acme",
            status=QuoteStatus.ACCEPTED,
            total=5000.0,
            company_id=1,
        )
        db_session.add(quote)
        db_session.commit()

        record_quote_status_outcome(
            db_session,
            company_id=1,
            quote=quote,
            previous_status=QuoteStatus.SENT.value,
            user_id=1,
        )
        db_session.commit()

        outcome = (
            db_session.query(AIOutcome).filter(AIOutcome.entity_type == "quote", AIOutcome.entity_id == quote.id).one()
        )
        assert outcome.outcome_type == "quote_result"
        assert outcome.metric_value == 1.0


@pytest.mark.unit
@pytest.mark.requires_db
class TestAggregateIncludesSensors:
    def test_aggregate_learning_signals_runs_sensors(self, db_session: Session):
        part = _part(db_session, part_number="AGG-1")
        db_session.add(
            WorkOrder(
                work_order_number="WO-AGG-LATE",
                part_id=part.id,
                quantity_ordered=1,
                status=WorkOrderStatus.ON_HOLD,
                priority=1,
                due_date=date.today() - timedelta(days=3),
                company_id=1,
            )
        )
        db_session.commit()

        summary = AILearningService(db_session).aggregate_learning_signals(company_ids=[1])
        db_session.commit()

        assert summary["sensor_recommendations_created"] >= 1
        assert summary["recommendations_created"] >= 1
        assert (
            db_session.query(AIRecommendation)
            .filter(AIRecommendation.recommendation_type == "at_risk_delivery", AIRecommendation.company_id == 1)
            .count()
            >= 1
        )
