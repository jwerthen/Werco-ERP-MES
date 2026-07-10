"""RBAC + envelope locks for the five Lean Phase 1 /analytics endpoints (issue #88).

Role gates as declared in endpoints/analytics.py:
  * /analytics/flow, /analytics/wip-aging, /analytics/adoption --
    ADMIN/MANAGER/SUPERVISOR only,
  * /analytics/fpy, /analytics/scrap-pareto -- those three plus QUALITY.
Operators (and viewers) get 403 -- these are management/quality views, not
shop-floor surfaces. A permitted role gets a 200 whose envelope carries the
period + generated_at contract on an empty tenant (no 500s on no data).
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.user import UserRole
from tests.lean_phase1_helpers import headers_for, make_user

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

MANAGEMENT_ONLY = ["/api/v1/analytics/flow", "/api/v1/analytics/wip-aging", "/api/v1/analytics/adoption"]
QUALITY_ALLOWED = ["/api/v1/analytics/fpy", "/api/v1/analytics/scrap-pareto"]


@pytest.mark.parametrize("url", MANAGEMENT_ONLY + QUALITY_ALLOWED)
def test_operator_is_rejected_everywhere(client: TestClient, db_session: Session, url: str):
    operator = make_user(db_session, role=UserRole.OPERATOR)
    resp = client.get(url, headers=headers_for(operator))
    assert resp.status_code == status.HTTP_403_FORBIDDEN, f"{url}: {resp.status_code}"


@pytest.mark.parametrize("url", MANAGEMENT_ONLY + QUALITY_ALLOWED)
def test_manager_reads_every_leg_with_the_envelope_contract(client: TestClient, db_session: Session, url: str):
    manager = make_user(db_session, role=UserRole.MANAGER)
    resp = client.get(url, headers=headers_for(manager))
    assert resp.status_code == status.HTTP_200_OK, f"{url}: {resp.text}"
    body = resp.json()
    assert "generated_at" in body
    if url.endswith("wip-aging"):
        assert body["items"] == [] and body["total_open"] == 0
    else:
        assert "period_start" in body and "period_end" in body


@pytest.mark.parametrize("url", QUALITY_ALLOWED)
def test_quality_role_reads_the_yield_legs(client: TestClient, db_session: Session, url: str):
    quality = make_user(db_session, role=UserRole.QUALITY)
    assert client.get(url, headers=headers_for(quality)).status_code == status.HTTP_200_OK


@pytest.mark.parametrize("url", MANAGEMENT_ONLY)
def test_quality_role_is_not_widened_to_the_management_legs(client: TestClient, db_session: Session, url: str):
    quality = make_user(db_session, role=UserRole.QUALITY)
    assert client.get(url, headers=headers_for(quality)).status_code == status.HTTP_403_FORBIDDEN
