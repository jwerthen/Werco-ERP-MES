from datetime import datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.ai_learning import AICorrection, AIInteractionEvent, AIRecommendation
from app.models.company import Company


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

        event = db_session.query(AIInteractionEvent).filter(AIInteractionEvent.recommendation_id == recommendation.id).first()
        assert event is not None
        assert event.event_type == "accepted"
        assert event.event_payload["note"].startswith("Suggest-only")

    def test_recommendations_are_company_scoped(
        self, client: TestClient, admin_headers: dict, db_session: Session
    ):
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
