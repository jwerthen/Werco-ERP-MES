from datetime import datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.ai_learning import AICorrection, AIInteractionEvent, AIRecommendation
from app.models.company import Company
from app.services.ai_learning_service import AILearningService


def make_recommendation(db_session: Session, **overrides) -> AIRecommendation:
    defaults = dict(
        company_id=1,
        source_module="quoting",
        recommendation_type="correction_pattern",
        status="pending",
        priority="medium",
        title="Test recommendation",
        summary="Test summary.",
        confidence_score=0.5,
    )
    defaults.update(overrides)
    recommendation = AIRecommendation(**defaults)
    db_session.add(recommendation)
    db_session.commit()
    db_session.refresh(recommendation)
    return recommendation


@pytest.mark.api
@pytest.mark.requires_db
class TestAILearningAPI:
    def test_record_event_redacts_payload_and_creates_corrections(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        payload = {
            "event_type": "edited",
            "source_module": "routing",
            "ai_feature": "drawing_routing_generation",
            "entity_type": "routing",
            "entity_id": 10,
            "context_summary": "Operator changed generated routing",
            "event_payload": {
                "token": "secret-token",
                "drawing_text": "raw drawing text should not persist",
                "safe": "kept",
            },
            "confidence_score": 0.6,
            "corrections": [
                {
                    "field_path": "operations.10.work_center_id",
                    "proposed_value": 1,
                    "final_value": 2,
                    "correction_reason": "Preferred press brake cell",
                }
            ],
        }

        response = client.post("/api/v1/ai/events", json=payload, headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["event_payload"]["token"] == "[redacted]"
        assert data["event_payload"]["drawing_text"] == "[redacted]"
        assert data["event_payload"]["safe"] == "kept"
        assert data["corrections"][0]["field_path"] == "operations.10.work_center_id"

        event = db_session.query(AIInteractionEvent).first()
        correction = db_session.query(AICorrection).first()
        assert event.company_id == 1
        assert correction.company_id == 1
        assert correction.event_id == event.id

    def test_recommendation_accept_is_suggest_only_and_records_event(
        self, client: TestClient, admin_headers: dict, db_session: Session
    ):
        recommendation = AIRecommendation(
            company_id=1,
            source_module="mrp",
            recommendation_type="buyer_override",
            title="Review repeated vendor override",
            summary="Buyers keep changing the suggested vendor.",
            suggested_action={"type": "review_vendor_preference"},
            evidence=[{"overrides": 4}],
            impact={"expected": "fewer buyer edits"},
            confidence_score=0.8,
            expires_at=datetime.utcnow() + timedelta(days=7),
        )
        db_session.add(recommendation)
        db_session.commit()

        response = client.post(
            f"/api/v1/ai/recommendations/{recommendation.id}/accept",
            json={"reason": "Looks useful"},
            headers=admin_headers,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "accepted"
        assert data["accepted_by"] is not None

        event = (
            db_session.query(AIInteractionEvent)
            .filter(AIInteractionEvent.recommendation_id == recommendation.id)
            .first()
        )
        assert event is not None
        assert event.event_type == "accepted"
        assert event.event_payload["note"].startswith("Suggest-only")

    def test_recommendations_are_company_scoped(self, client: TestClient, admin_headers: dict, db_session: Session):
        other_company = Company(name="Other Co", slug="other-co", is_active=True)
        db_session.add(other_company)
        db_session.flush()
        recommendation = AIRecommendation(
            company_id=other_company.id,
            source_module="quality",
            recommendation_type="root_cause",
            title="Other tenant suggestion",
            summary="This must not be visible across companies.",
            confidence_score=0.7,
        )
        db_session.add(recommendation)
        db_session.commit()

        list_response = client.get("/api/v1/ai/recommendations", headers=admin_headers)
        assert list_response.status_code == status.HTTP_200_OK
        assert list_response.json() == []

        accept_response = client.post(f"/api/v1/ai/recommendations/{recommendation.id}/accept", headers=admin_headers)
        assert accept_response.status_code == status.HTTP_404_NOT_FOUND

    def test_aggregate_creates_recommendation_from_repeated_corrections(
        self, client: TestClient, admin_headers: dict, db_session: Session
    ):
        for index in range(3):
            event = AIInteractionEvent(
                company_id=1,
                event_type="edited",
                source_module="quoting",
                entity_type="quote",
                entity_id=index + 1,
                event_payload={},
            )
            db_session.add(event)
            db_session.flush()
            db_session.add(
                AICorrection(
                    company_id=1,
                    event_id=event.id,
                    source_module="quoting",
                    entity_type="quote",
                    entity_id=index + 1,
                    field_path="lead_time_days",
                    proposed_value=10,
                    final_value=14,
                )
            )
        db_session.commit()

        response = client.post("/api/v1/ai/aggregate", headers=admin_headers)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["recommendations_created"] >= 1

        recommendations = client.get("/api/v1/ai/recommendations", headers=admin_headers).json()
        titles = {item["title"] for item in recommendations}
        assert "Teach AI the preferred value for lead_time_days" in titles


@pytest.mark.api
@pytest.mark.requires_db
class TestAIRecommendationScoring:
    def test_list_returns_score_and_sorts_by_it(self, client: TestClient, admin_headers: dict, db_session: Session):
        # Inserted deliberately out of score order.
        low = make_recommendation(db_session, title="Low priority", priority="low", confidence_score=0.9)
        medium = make_recommendation(db_session, title="Medium priority", priority="medium", confidence_score=0.9)
        high = make_recommendation(db_session, title="High priority", priority="high", confidence_score=0.6)

        response = client.get("/api/v1/ai/recommendations", headers=admin_headers)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        titles = [item["title"] for item in data]
        assert titles == ["High priority", "Medium priority", "Low priority"]
        scores = {item["id"]: item["score"] for item in data}
        # high: 1.0 x 0.6, medium: 0.6 x 0.9, low: 0.35 x 0.9 (fresh, no expiry, default impact)
        assert scores[high.id] == pytest.approx(0.6, abs=1e-3)
        assert scores[medium.id] == pytest.approx(0.54, abs=1e-3)
        assert scores[low.id] == pytest.approx(0.315, abs=1e-3)
        assert scores[high.id] > scores[medium.id] > scores[low.id]

    def test_impact_magnitude_can_outrank_priority(self, client: TestClient, admin_headers: dict, db_session: Session):
        make_recommendation(db_session, title="Plain low", priority="low", confidence_score=0.9)
        make_recommendation(
            db_session,
            title="Boosted info",
            priority="info",
            confidence_score=1.0,
            impact={"magnitude": 1000},
        )

        data = client.get("/api/v1/ai/recommendations", headers=admin_headers).json()
        titles = [item["title"] for item in data]
        # info 0.2 x 1.0 conf x 2.0 impact = 0.4 beats low 0.35 x 0.9 = 0.315
        assert titles == ["Boosted info", "Plain low"]

    def test_age_decay_lowers_items_near_expiry(self, client: TestClient, admin_headers: dict, db_session: Session):
        now = datetime.utcnow()
        make_recommendation(
            db_session,
            title="Nearly expired",
            priority="high",
            confidence_score=0.9,
            created_at=now - timedelta(days=30),
            expires_at=now + timedelta(minutes=10),
        )
        make_recommendation(db_session, title="Fresh medium", priority="medium", confidence_score=0.9)

        data = client.get("/api/v1/ai/recommendations", headers=admin_headers).json()
        titles = [item["title"] for item in data]
        # high 1.0 x 0.9 x ~0.2 decay = ~0.18 loses to medium 0.6 x 0.9 x 1.0 = 0.54
        assert titles == ["Fresh medium", "Nearly expired"]


@pytest.mark.api
@pytest.mark.requires_db
class TestAIRecommendationSnooze:
    def test_snooze_flow(self, client: TestClient, admin_headers: dict, db_session: Session):
        recommendation = make_recommendation(db_session, title="Snoozable")

        response = client.post(
            f"/api/v1/ai/recommendations/{recommendation.id}/snooze",
            json={"days": 3, "reason": "Busy week"},
            headers=admin_headers,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "snoozed"
        assert "Snoozed until" in data["status_reason"]

        event = (
            db_session.query(AIInteractionEvent)
            .filter(AIInteractionEvent.recommendation_id == recommendation.id)
            .first()
        )
        assert event is not None
        assert event.event_type == "ignored"
        assert event.event_payload["status"] == "snoozed"
        assert event.event_payload["snooze_days"] == 3
        assert "snoozed_until" in event.event_payload

        pending = client.get("/api/v1/ai/recommendations", headers=admin_headers).json()
        assert all(item["id"] != recommendation.id for item in pending)

        snoozed = client.get("/api/v1/ai/recommendations?status=snoozed", headers=admin_headers).json()
        assert [item["id"] for item in snoozed] == [recommendation.id]

    def test_snoozing_a_non_pending_recommendation_conflicts(
        self, client: TestClient, admin_headers: dict, db_session: Session
    ):
        recommendation = make_recommendation(db_session, status="accepted")

        response = client.post(
            f"/api/v1/ai/recommendations/{recommendation.id}/snooze",
            json={"days": 1},
            headers=admin_headers,
        )

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_snooze_validates_days_and_tenant(self, client: TestClient, admin_headers: dict, db_session: Session):
        recommendation = make_recommendation(db_session)

        bad_days = client.post(
            f"/api/v1/ai/recommendations/{recommendation.id}/snooze",
            json={"days": 0},
            headers=admin_headers,
        )
        assert bad_days.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

        other_company = Company(name="Snooze Other", slug="snooze-other", is_active=True)
        db_session.add(other_company)
        db_session.flush()
        foreign = make_recommendation(db_session, company_id=other_company.id)

        cross_tenant = client.post(
            f"/api/v1/ai/recommendations/{foreign.id}/snooze",
            json={"days": 1},
            headers=admin_headers,
        )
        assert cross_tenant.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.api
@pytest.mark.requires_db
class TestAILearningSweep:
    def test_expiry_sweep_is_tenant_scoped(self, db_session: Session):
        now = datetime.utcnow()
        mine = make_recommendation(db_session, title="Mine expired", expires_at=now - timedelta(hours=1))
        other_company = Company(name="Sweep Other", slug="sweep-other", is_active=True)
        db_session.add(other_company)
        db_session.flush()
        theirs = make_recommendation(
            db_session, title="Theirs expired", company_id=other_company.id, expires_at=now - timedelta(hours=1)
        )

        summary = AILearningService(db_session).aggregate_learning_signals(company_ids=[1])
        db_session.commit()

        assert summary["stale_recommendations"] == 1
        db_session.refresh(mine)
        db_session.refresh(theirs)
        assert mine.status == "stale"
        assert theirs.status == "pending"

    def test_sweep_wakes_elapsed_snoozes_only(self, db_session: Session, admin_user):
        service = AILearningService(db_session)
        elapsed = make_recommendation(db_session, title="Elapsed snooze")
        active = make_recommendation(db_session, title="Active snooze")
        service.snooze_recommendation(recommendation_id=elapsed.id, company_id=1, user=admin_user, days=1)
        service.snooze_recommendation(recommendation_id=active.id, company_id=1, user=admin_user, days=7)
        db_session.commit()

        # Rewind the elapsed snooze's wake-up time into the past.
        event = db_session.query(AIInteractionEvent).filter(AIInteractionEvent.recommendation_id == elapsed.id).first()
        event.event_payload = {
            **event.event_payload,
            "snoozed_until": (datetime.utcnow() - timedelta(hours=1)).isoformat(),
        }
        db_session.commit()

        summary = service.aggregate_learning_signals(company_ids=[1])
        db_session.commit()

        assert summary["snoozed_recommendations_woken"] == 1
        db_session.refresh(elapsed)
        db_session.refresh(active)
        assert elapsed.status == "pending"
        assert elapsed.status_reason is None
        assert active.status == "snoozed"

    def test_aggregation_does_not_recreate_a_snoozed_generated_recommendation(self, db_session: Session, admin_user):
        """Snooze must survive the nightly aggregation: the dedupe check treats snoozed as open."""
        # Seed repeated corrections so aggregation generates a correction_pattern recommendation
        # through the same path the nightly job uses.
        for index in range(3):
            event = AIInteractionEvent(
                company_id=1,
                event_type="edited",
                source_module="quoting",
                entity_type="quote",
                entity_id=index + 1,
                event_payload={},
            )
            db_session.add(event)
            db_session.flush()
            db_session.add(
                AICorrection(
                    company_id=1,
                    event_id=event.id,
                    source_module="quoting",
                    entity_type="quote",
                    entity_id=index + 1,
                    field_path="lead_time_days",
                    proposed_value=10,
                    final_value=14,
                )
            )
        db_session.commit()

        service = AILearningService(db_session)
        service.aggregate_learning_signals(company_ids=[1])
        db_session.commit()

        generated_query = db_session.query(AIRecommendation).filter(
            AIRecommendation.company_id == 1,
            AIRecommendation.recommendation_type == "correction_pattern",
        )
        generated = generated_query.all()
        assert len(generated) == 1

        service.snooze_recommendation(recommendation_id=generated[0].id, company_id=1, user=admin_user, days=7)
        db_session.commit()

        # Re-running aggregation while the recommendation sleeps must not create a duplicate
        # (and must not wake it early — the 7-day window has not elapsed).
        service.aggregate_learning_signals(company_ids=[1])
        db_session.commit()

        survivors = generated_query.all()
        assert len(survivors) == 1
        assert survivors[0].id == generated[0].id
        assert survivors[0].status == "snoozed"

    def test_snoozed_recommendation_past_expiry_goes_stale_not_pending(self, db_session: Session, admin_user):
        service = AILearningService(db_session)
        recommendation = make_recommendation(
            db_session, title="Snoozed then expired", expires_at=datetime.utcnow() + timedelta(minutes=30)
        )
        service.snooze_recommendation(recommendation_id=recommendation.id, company_id=1, user=admin_user, days=1)
        db_session.commit()

        recommendation.expires_at = datetime.utcnow() - timedelta(minutes=5)
        db_session.commit()

        summary = service.aggregate_learning_signals(company_ids=[1])
        db_session.commit()

        assert summary["stale_recommendations"] == 1
        assert summary["snoozed_recommendations_woken"] == 0
        db_session.refresh(recommendation)
        assert recommendation.status == "stale"
