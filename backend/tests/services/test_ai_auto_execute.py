"""Claude always-on auto-execute (existing run_llm_task stack)."""

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models.ai_learning import AIRecommendation
from app.models.part import Part
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.services.ai_auto_execute_service import (
    auto_execute_pending_recommendations,
    resolve_system_actor,
)
from app.services.llm_client import LLMEgressDisabledError, LLMTaskResult


def _part(db: Session, number: str = "AE-1") -> Part:
    p = Part(
        part_number=number,
        name=f"Part {number}",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=1,
    )
    db.add(p)
    db.flush()
    return p


def _pending_priority_rec(db: Session, wo: WorkOrder, conf: float = 0.9) -> AIRecommendation:
    rec = AIRecommendation(
        company_id=1,
        source_module="scheduling",
        recommendation_type="at_risk_delivery",
        status="pending",
        priority="high",
        title=f"Late {wo.work_order_number}",
        summary="Late job",
        target_entity_type="work_order",
        target_entity_id=wo.id,
        suggested_action={
            "type": "adjust_work_order_priority",
            "work_order_id": wo.id,
            "priority": 1,
            "autonomy": "auto_execute",
        },
        confidence_score=conf,
    )
    db.add(rec)
    db.flush()
    return rec


@pytest.mark.unit
@pytest.mark.requires_db
class TestAutoExecute:
    def test_resolve_system_actor(self, db_session: Session, admin_user):
        actor = resolve_system_actor(db_session, 1)
        assert actor is not None
        assert actor.id == admin_user.id

    def test_claude_selects_and_executes(self, db_session: Session, admin_user):
        part = _part(db_session)
        wo = WorkOrder(
            work_order_number="WO-AE-1",
            part_id=part.id,
            quantity_ordered=1,
            status=WorkOrderStatus.RELEASED,
            priority=5,
            due_date=date.today() - timedelta(days=1),
            company_id=1,
        )
        db_session.add(wo)
        db_session.flush()
        rec = _pending_priority_rec(db_session, wo)
        db_session.commit()

        fake = LLMTaskResult(
            text=f'{{"execute": [{{"id": {rec.id}, "reason": "late"}}], "skip": []}}',
            model="claude-haiku-4-5",
            tier="fast",
            model_selection_reason="test",
            prompt_version="1.0.0",
            input_tokens=10,
            output_tokens=20,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            estimated_cost_usd=Decimal("0.001"),
            latency_ms=12,
            raw_response=None,
        )
        with patch("app.services.ai_auto_execute_service.run_llm_task", return_value=fake) as mock_llm:
            summary = auto_execute_pending_recommendations(db_session, 1)
            db_session.commit()
            mock_llm.assert_called_once()
            assert mock_llm.call_args.kwargs.get("feature") == "ai_auto_execute"
            ctx = mock_llm.call_args.args[0]
            assert ctx.task == "auto_execute"

        assert summary["executed"] == 1
        assert summary["used_fallback"] == 0
        db_session.refresh(wo)
        db_session.refresh(rec)
        assert wo.priority == 1
        assert rec.status == "accepted"

    def test_fallback_when_egress_disabled(self, db_session: Session, admin_user):
        part = _part(db_session, "AE-2")
        wo = WorkOrder(
            work_order_number="WO-AE-2",
            part_id=part.id,
            quantity_ordered=1,
            status=WorkOrderStatus.IN_PROGRESS,
            priority=6,
            company_id=1,
        )
        db_session.add(wo)
        db_session.flush()
        rec = _pending_priority_rec(db_session, wo, conf=0.9)
        db_session.commit()

        with patch(
            "app.services.ai_auto_execute_service.run_llm_task",
            side_effect=LLMEgressDisabledError(1),
        ):
            summary = auto_execute_pending_recommendations(db_session, 1)
            db_session.commit()

        assert summary["used_fallback"] == 1
        assert summary["executed"] == 1
        db_session.refresh(wo)
        assert wo.priority == 1

    def test_skips_morning_brief(self, db_session: Session, admin_user):
        rec = AIRecommendation(
            company_id=1,
            source_module="operations",
            recommendation_type="morning_brief",
            status="pending",
            priority="high",
            title="Brief",
            summary="Info only",
            suggested_action={
                "type": "adjust_work_order_priority",
                "work_order_id": 1,
                "priority": 1,
                "autonomy": "auto_execute",
            },
            confidence_score=0.99,
        )
        db_session.add(rec)
        db_session.commit()

        with patch("app.services.ai_auto_execute_service.run_llm_task") as mock_llm:
            summary = auto_execute_pending_recommendations(db_session, 1)
            mock_llm.assert_not_called()
        assert summary["candidates"] == 0
