"""AI cards about a blocker or a work order never outlive their target (owner ruling 3 follow-through).

A blocker triage / stale-blocker card has no ``expires_at``, so before this change a closed
blocker left an Accept & apply card pending forever. Closing a blocker now retires its open
cards in the same transaction, and the nightly sweep retires any card whose blocker closed or
whose work order finished or was deleted. "Stale", never "dismissed": nobody rejected them.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.ai_learning import AIInteractionEvent, AIRecommendation
from app.models.part import Part
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlocker
from app.services.ai_learning_service import AILearningService

pytestmark = [pytest.mark.requires_db]


def _work_order(db: Session, number: str, *, status_=WorkOrderStatus.RELEASED, priority: int = 5) -> WorkOrder:
    part = Part(
        part_number=f"P-{number}",
        name=f"Part {number}",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=1,
    )
    db.add(part)
    db.flush()
    work_order = WorkOrder(
        work_order_number=number,
        part_id=part.id,
        quantity_ordered=1,
        status=status_,
        priority=priority,
        company_id=1,
    )
    db.add(work_order)
    db.commit()
    db.refresh(work_order)
    return work_order


def _blocker(db: Session, work_order: WorkOrder, *, status_: str = "open") -> WorkOrderBlocker:
    blocker = WorkOrderBlocker(company_id=1, work_order_id=work_order.id, title="Waiting on steel", status=status_)
    db.add(blocker)
    db.commit()
    db.refresh(blocker)
    return blocker


def _card(db: Session, target_type: str, target_id: int, *, rtype: str, status_: str = "pending") -> AIRecommendation:
    rec = AIRecommendation(
        company_id=1,
        source_module="shop_floor",
        recommendation_type=rtype,
        status=status_,
        title=f"{rtype} card",
        summary="Card",
        target_entity_type=target_type,
        target_entity_id=target_id,
        suggested_action={"type": "escalate_blocker", "autonomy": "apply_on_accept"},
        confidence_score=0.8,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


def _create_blocker_via_api(client: TestClient, headers: dict, work_order: WorkOrder) -> dict:
    response = client.post(
        f"/api/v1/work-order-blockers/work-orders/{work_order.id}",
        headers=headers,
        json={"category": "material_missing", "severity": "medium", "note": "No sheet stock."},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json()


def _rejected_events(db: Session, rec_ids) -> int:
    return (
        db.query(AIInteractionEvent)
        .filter(AIInteractionEvent.recommendation_id.in_(list(rec_ids)), AIInteractionEvent.event_type == "rejected")
        .count()
    )


@pytest.mark.api
class TestClosingABlockerRetiresItsCards:
    @pytest.mark.parametrize("close", ["resolve", "dismiss"])
    def test_resolving_blocker_stales_its_recs(
        self, client: TestClient, supervisor_headers: dict, db_session: Session, close: str
    ):
        work_order = _work_order(db_session, f"WO-CLOSE-{close}")
        blocker_id = _create_blocker_via_api(client, supervisor_headers, work_order)["id"]
        triage = (
            db_session.query(AIRecommendation)
            .filter(
                AIRecommendation.target_entity_type == "work_order_blocker",
                AIRecommendation.target_entity_id == blocker_id,
            )
            .one()
        )
        stale_card = _card(db_session, "work_order_blocker", blocker_id, rtype="stale_blocker_escalation")
        decided = _card(db_session, "work_order_blocker", blocker_id, rtype="old_card", status_="accepted")

        if close == "resolve":
            response = client.post(
                f"/api/v1/work-order-blockers/{blocker_id}/resolve",
                json={"resolution_note": "Steel arrived"},
                headers=supervisor_headers,
            )
            expected_status = "resolved"
        else:
            response = client.put(
                f"/api/v1/work-order-blockers/{blocker_id}", json={"status": "dismissed"}, headers=supervisor_headers
            )
            expected_status = "dismissed"
        assert response.status_code == status.HTTP_200_OK, response.text

        for rec in (triage, stale_card):
            db_session.refresh(rec)
            assert rec.status == "stale"
            assert rec.status_reason == f"Blocker {expected_status}; nothing left to escalate"
        db_session.refresh(decided)
        assert decided.status == "accepted"  # a person's decision is never overwritten
        assert _rejected_events(db_session, [triage.id, stale_card.id]) == 0

        listed = client.get("/api/v1/ai/recommendations", headers=supervisor_headers).json()
        assert {triage.id, stale_card.id}.isdisjoint({row["id"] for row in listed})

    def test_triage_rec_title_and_autonomy(self, client: TestClient, supervisor_headers: dict, db_session: Session):
        work_order = _work_order(db_session, "WO-TRIAGE")
        blocker_id = _create_blocker_via_api(client, supervisor_headers, work_order)["id"]
        triage = (
            db_session.query(AIRecommendation)
            .filter(
                AIRecommendation.target_entity_type == "work_order_blocker",
                AIRecommendation.target_entity_id == blocker_id,
            )
            .one()
        )
        assert triage.title == "Triage blocker on WO-TRIAGE"
        assert triage.suggested_action["autonomy"] == "apply_on_accept"
        assert triage.suggested_action["type"] == "escalate_blocker"


@pytest.mark.unit
class TestNightlyClosedTargetSweep:
    def test_sweep_stales_recs_for_closed_or_terminal_targets(self, db_session: Session, admin_user):
        live_wo = _work_order(db_session, "WO-SWEEP-LIVE")
        done_wo = _work_order(db_session, "WO-SWEEP-DONE", status_=WorkOrderStatus.COMPLETE)
        gone_wo = _work_order(db_session, "WO-SWEEP-GONE")
        gone_wo.soft_delete(admin_user.id)
        db_session.commit()

        resolved_blocker = _blocker(db_session, live_wo, status_="resolved")
        live_blocker = _blocker(db_session, live_wo)
        blocker_on_done_wo = _blocker(db_session, done_wo)

        expected_stale = {
            _card(db_session, "work_order_blocker", resolved_blocker.id, rtype="a").id: (
                "Target closed: blocker resolved"
            ),
            _card(db_session, "work_order_blocker", blocker_on_done_wo.id, rtype="b").id: (
                "Target closed: work order WO-SWEEP-DONE is complete"
            ),
            _card(db_session, "work_order_blocker", 999_999, rtype="c").id: "Target closed: blocker no longer exists",
            _card(db_session, "work_order", done_wo.id, rtype="d").id: (
                "Target closed: work order WO-SWEEP-DONE is complete"
            ),
            _card(db_session, "work_order", gone_wo.id, rtype="e", status_="snoozed").id: (
                "Target closed: work order WO-SWEEP-GONE was deleted"
            ),
        }
        stays_open = [
            _card(db_session, "work_order_blocker", live_blocker.id, rtype="f"),
            _card(db_session, "work_order", live_wo.id, rtype="g"),
        ]
        decided = _card(db_session, "work_order_blocker", resolved_blocker.id, rtype="h", status_="dismissed")

        summary = AILearningService(db_session).aggregate_learning_signals(company_ids=[1])
        db_session.commit()

        assert summary["closed_target_recommendations_staled"] == len(expected_stale)
        for rec_id, reason in expected_stale.items():
            rec = db_session.query(AIRecommendation).filter(AIRecommendation.id == rec_id).one()
            assert (rec.status, rec.status_reason) == ("stale", reason)
        for rec in stays_open:
            db_session.refresh(rec)
            assert rec.status == "pending"
        db_session.refresh(decided)
        assert decided.status == "dismissed"
        assert _rejected_events(db_session, expected_stale) == 0
        # A second run finds nothing left to retire (idempotent).
        again = AILearningService(db_session).aggregate_learning_signals(company_ids=[1])
        assert again["closed_target_recommendations_staled"] == 0
