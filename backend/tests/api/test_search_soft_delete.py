"""Soft-deleted work orders must never surface through any search path.

Covers the S1 compliance fix: ``WorkOrder.is_deleted == False`` on
- the global-search work-order branch (GET /search),
- the NL-search main query and its literal fallback (POST /search/nl),
- the recent-items work-order query (GET /search/recent).
"""

from datetime import date, timedelta

import pytest
from sqlalchemy.orm import Session

import app.services.llm_client as llm_client
from app.models.part import Part
from app.models.work_order import WorkOrder

pytestmark = pytest.mark.api

SEARCH_URL = "/api/v1/search/"
NL_URL = "/api/v1/search/nl"
RECENT_URL = "/api/v1/search/recent"

# "SOFTDEL" deliberately contains none of the NL rule-parser trigger terms
# (late/blocked/hot/wo/job/...), so querying it exercises the literal fallback.
CUSTOMER = "SOFTDEL CORP"


@pytest.fixture
def wo_pair(db_session: Session, test_part: Part):
    """One live and one soft-deleted work order, both released and late."""
    live = WorkOrder(
        work_order_number="WO-SD-KEEP",
        customer_name=CUSTOMER,
        part_id=test_part.id,
        quantity_ordered=10,
        status="released",
        priority=2,
        due_date=date.today() - timedelta(days=3),
        company_id=1,
    )
    deleted = WorkOrder(
        work_order_number="WO-SD-GONE",
        customer_name=CUSTOMER,
        part_id=test_part.id,
        quantity_ordered=10,
        status="released",
        priority=2,
        due_date=date.today() - timedelta(days=3),
        company_id=1,
        is_deleted=True,
    )
    db_session.add_all([live, deleted])
    db_session.commit()
    return live, deleted


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    """NL search must fall back to the rule parser — never call out."""

    def raise_not_configured(ctx, **kwargs):
        raise llm_client.LLMNotConfiguredError("api_key")

    monkeypatch.setattr(llm_client, "run_llm_task", raise_not_configured)


class TestGlobalSearchSoftDelete:
    def test_soft_deleted_work_order_absent(self, client, auth_headers, wo_pair):
        response = client.get(SEARCH_URL, headers=auth_headers, params={"q": "WO-SD"})
        assert response.status_code == 200
        titles = [r["title"] for r in response.json()["results"] if r["type"] == "work_order"]
        assert "WO-SD-KEEP" in titles
        assert "WO-SD-GONE" not in titles


class TestNaturalLanguageSearchSoftDelete:
    def test_main_query_excludes_soft_deleted(self, client, auth_headers, wo_pair):
        # "late jobs" hits the filtered main query (late + active_jobs, no fallback).
        response = client.post(NL_URL, headers=auth_headers, json={"query": "late jobs"})
        assert response.status_code == 200
        data = response.json()
        assert data["used_fallback"] is False
        titles = [r["title"] for r in data["results"]]
        assert "WO-SD-KEEP" in titles
        assert "WO-SD-GONE" not in titles

    def test_literal_fallback_excludes_soft_deleted(self, client, auth_headers, wo_pair):
        # "softdel" matches no rule filters -> literal work-order fallback path.
        response = client.post(NL_URL, headers=auth_headers, json={"query": "softdel"})
        assert response.status_code == 200
        data = response.json()
        assert data["used_fallback"] is True
        titles = [r["title"] for r in data["results"]]
        assert "WO-SD-KEEP" in titles
        assert "WO-SD-GONE" not in titles


class TestRecentItemsSoftDelete:
    def test_recent_excludes_soft_deleted(self, client, auth_headers, wo_pair):
        response = client.get(RECENT_URL, headers=auth_headers)
        assert response.status_code == 200
        wo_titles = [r["title"] for r in response.json() if r["type"] == "work_order"]
        assert "WO-SD-KEEP" in wo_titles
        assert "WO-SD-GONE" not in wo_titles
