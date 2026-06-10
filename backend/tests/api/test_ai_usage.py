"""API tests for /api/v1/ai-usage/summary — RBAC + tenant isolation + math."""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.models.ai_usage import AIUsageEvent
from app.models.company import Company

pytestmark = pytest.mark.api

SUMMARY_URL = "/api/v1/ai-usage/summary"


def _seed_events(db: Session):
    other_company = db.query(Company).filter(Company.id == 2).first()
    if not other_company:
        other_company = Company(id=2, name="Other Corp", slug="other-corp", is_active=True)
        db.add(other_company)
        db.commit()

    events = [
        # Company 1: two successful PO extractions on sonnet
        AIUsageEvent(
            company_id=1,
            task="po_extraction",
            model="claude-sonnet-4-6",
            tier="default",
            feature="po_upload",
            prompt_version="1.0.0",
            input_tokens=1000,
            output_tokens=200,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            estimated_cost_usd=Decimal("0.006000"),
            latency_ms=900,
            success=True,
        ),
        AIUsageEvent(
            company_id=1,
            task="po_extraction",
            model="claude-sonnet-4-6",
            tier="default",
            feature="po_upload",
            prompt_version="1.0.0",
            input_tokens=3000,
            output_tokens=400,
            cache_creation_tokens=500,
            cache_read_tokens=1500,
            estimated_cost_usd=Decimal("0.018000"),
            latency_ms=1100,
            success=True,
        ),
        # Company 1: one failed routing generation on an unpriced model (NULL cost)
        AIUsageEvent(
            company_id=1,
            task="routing_generation",
            model="claude-unpriced-model",
            tier="reasoning",
            feature="routing_generation",
            prompt_version="1.1.0",
            input_tokens=0,
            output_tokens=0,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            estimated_cost_usd=None,
            latency_ms=50,
            success=False,
            error_type="APIError",
        ),
        # Company 2: must never appear in company 1's summary
        AIUsageEvent(
            company_id=2,
            task="po_extraction",
            model="claude-opus-4-8",
            tier="reasoning",
            input_tokens=999_999,
            output_tokens=999_999,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            estimated_cost_usd=Decimal("30.000000"),
            latency_ms=5000,
            success=True,
        ),
        # Company 1 but outside the window: excluded by days filter
        AIUsageEvent(
            company_id=1,
            task="po_extraction",
            model="claude-sonnet-4-6",
            tier="default",
            input_tokens=77,
            output_tokens=77,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            estimated_cost_usd=Decimal("1.000000"),
            latency_ms=10,
            success=True,
            created_at=datetime.utcnow() - timedelta(days=90),
        ),
    ]
    db.add_all(events)
    db.commit()


class TestAIUsageSummaryRBAC:
    def test_requires_auth(self, client):
        response = client.get(SUMMARY_URL)
        assert response.status_code == 401

    def test_operator_forbidden(self, client, operator_headers, db_session):
        response = client.get(SUMMARY_URL, headers=operator_headers)
        assert response.status_code == 403

    def test_manager_allowed(self, client, manager_headers, db_session):
        response = client.get(SUMMARY_URL, headers=manager_headers)
        assert response.status_code == 200

    def test_admin_allowed(self, client, admin_headers, db_session):
        response = client.get(SUMMARY_URL, headers=admin_headers)
        assert response.status_code == 200


class TestAIUsageSummaryAggregates:
    def test_tenant_isolation_and_window(self, client, manager_headers, db_session):
        _seed_events(db_session)
        response = client.get(SUMMARY_URL, headers=manager_headers, params={"days": 30})
        assert response.status_code == 200
        body = response.json()

        assert body["window_days"] == 30
        totals = body["totals"]
        # Only the 3 in-window company-1 events: company 2 and the 90-day-old row excluded
        assert totals["calls"] == 3
        assert totals["input_tokens"] == 4000
        assert totals["output_tokens"] == 600
        assert totals["cache_creation_tokens"] == 500
        assert totals["cache_read_tokens"] == 1500
        assert totals["estimated_cost_usd"] == pytest.approx(0.024, abs=1e-9)
        assert totals["error_rate"] == pytest.approx(1 / 3)

        models_seen = {row["model"] for row in body["by_model"]}
        assert "claude-opus-4-8" not in models_seen  # company 2's model never leaks

    def test_per_task_breakdown(self, client, manager_headers, db_session):
        _seed_events(db_session)
        body = client.get(SUMMARY_URL, headers=manager_headers).json()
        by_task = {row["task"]: row for row in body["by_task"]}

        po = by_task["po_extraction"]
        assert po["calls"] == 2
        assert po["error_rate"] == 0.0
        assert po["estimated_cost_usd"] == pytest.approx(0.024, abs=1e-9)
        assert po["avg_latency_ms"] == pytest.approx(1000.0)

        routing = by_task["routing_generation"]
        assert routing["calls"] == 1
        assert routing["error_rate"] == 1.0
        assert routing["estimated_cost_usd"] is None  # unpriced model stays NULL

    def test_per_model_breakdown(self, client, manager_headers, db_session):
        _seed_events(db_session)
        body = client.get(SUMMARY_URL, headers=manager_headers).json()
        by_model = {row["model"]: row for row in body["by_model"]}
        assert set(by_model) == {"claude-sonnet-4-6", "claude-unpriced-model"}
        assert by_model["claude-sonnet-4-6"]["calls"] == 2

    def test_empty_window(self, client, manager_headers, db_session):
        body = client.get(SUMMARY_URL, headers=manager_headers, params={"days": 1}).json()
        assert body["totals"]["calls"] == 0
        assert body["totals"]["error_rate"] == 0.0
        assert body["by_task"] == []
        assert body["by_model"] == []

    def test_days_validation(self, client, manager_headers, db_session):
        assert client.get(SUMMARY_URL, headers=manager_headers, params={"days": 0}).status_code == 422
        assert client.get(SUMMARY_URL, headers=manager_headers, params={"days": 9999}).status_code == 422
