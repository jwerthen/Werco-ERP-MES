"""The company egress kill switches are a person's judgment (lead decision, Claude-plugin brief build 2026-11-15).

Four routes flip a company egress switch: ``PUT /companies/me/ai-egress`` (``allow_ai_egress``,
which also opens or closes every AI-external connector), ``PUT /companies/me/sms-egress``,
``PUT /admin/settings/shipping-profile`` (``allow_carrier_egress``) and
``PUT /receiving/print-profile`` (``allow_print_egress``). Pinned here:

- **Any API token is refused 403** ``API_TOKEN_EGRESS_SWITCH`` before anything is written: the
  switch is unchanged, the profile is not created, and no audit row lands -- whatever the
  token's role (an Admin-bound full token) or scope (a read-only token is refused earlier, by
  the read-only gate, with its own text).
- **A person still flips it**: an Admin signed in to the app gets 200 and the switch moves.
- **The AI flip is REQUIRED evidence**: if its UPDATE or its STATUS_CHANGE row cannot be
  written, the flip rolls back with the rows already written, and the answer is 503.
- None of the four is an MCP tool (``catalog.EGRESS_SWITCH_OPERATIONS`` within
  ``EXCLUDED_OPERATIONS``) -- the catalog side is pinned in ``tests/test_mcp_catalog.py``;
  this file checks that the routes named there are exactly the ones refused here.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.endpoints.companies import AI_EGRESS_AUDIT_UNAVAILABLE_DETAIL
from app.core.config import settings
from app.core.credentials import API_TOKEN_EGRESS_SWITCH, READ_ONLY_ROUTE_WRITES_DETAIL
from app.mcp.catalog import EGRESS_SWITCH_OPERATIONS, EXCLUDED_OPERATIONS
from app.models.audit_log import AuditLog
from app.models.carrier_account import CompanyShippingProfile
from app.models.company import Company
from app.models.print_profile import CompanyPrintProfile
from app.models.user import User
from app.services.audit_service import AuditService, AuditWriteError
from tests.api.export_control_helpers import bearer, issue_token

pytestmark = pytest.mark.api

API = settings.API_V1_PREFIX.rstrip("/")

# (path, body that turns the switch ON) for each egress kill-switch route.
EGRESS_SWITCHES: Dict[str, Tuple[str, Dict[str, Any]]] = {
    "ai": (f"{API}/companies/me/ai-egress", {"allow_ai_egress": True}),
    "sms": (f"{API}/companies/me/sms-egress", {"allow_sms_egress": True}),
    "carrier": (f"{API}/admin/settings/shipping-profile", {"allow_carrier_egress": True}),
    "print": (f"{API}/receiving/print-profile", {"allow_print_egress": True}),
}


def _switch_state(db: Session, company_id: int) -> Dict[str, Any]:
    """Every egress switch of the company, the two profile rows, and the audit-row count."""
    db.expire_all()
    company = db.get(Company, company_id)
    shipping = db.query(CompanyShippingProfile).filter(CompanyShippingProfile.company_id == company_id).first()
    printing = db.query(CompanyPrintProfile).filter(CompanyPrintProfile.company_id == company_id).first()
    return {
        "allow_ai_egress": bool(company.allow_ai_egress),
        "allow_sms_egress": bool(company.allow_sms_egress),
        "shipping_profile": None if shipping is None else bool(shipping.allow_carrier_egress),
        "print_profile": None if printing is None else bool(printing.allow_print_egress),
        "audit_rows": db.query(AuditLog).count(),
    }


def _company_audit_rows(db: Session, company_id: int):
    db.expire_all()
    return (
        db.query(AuditLog)
        .filter(AuditLog.resource_type == "company", AuditLog.resource_id == company_id)
        .order_by(AuditLog.id)
        .all()
    )


def test_the_refused_routes_are_exactly_the_mcp_egress_exclusions():
    walked = {("PUT", path) for path, _body in EGRESS_SWITCHES.values()}
    assert walked == set(EGRESS_SWITCH_OPERATIONS)
    assert set(EGRESS_SWITCH_OPERATIONS) <= set(EXCLUDED_OPERATIONS)


class TestApiTokensAreRefused:
    @pytest.mark.parametrize("switch", sorted(EGRESS_SWITCHES))
    def test_an_admin_bound_full_token_is_refused_and_nothing_changes(
        self, client: TestClient, db_session: Session, admin_user: User, admin_headers: dict, switch: str
    ):
        _id, token = issue_token(client, admin_headers, admin_user)
        path, body = EGRESS_SWITCHES[switch]
        before = _switch_state(db_session, admin_user.company_id)

        response = client.put(path, json=body, headers=bearer(token))

        assert response.status_code == 403, response.text[:400]
        assert response.json()["detail"] == API_TOKEN_EGRESS_SWITCH
        assert _switch_state(db_session, admin_user.company_id) == before, "the refusal wrote something"

    @pytest.mark.parametrize("switch", sorted(EGRESS_SWITCHES))
    def test_a_read_only_token_is_refused_by_the_read_only_gate(
        self, client: TestClient, db_session: Session, admin_user: User, admin_headers: dict, switch: str
    ):
        _id, token = issue_token(client, admin_headers, admin_user, scope="read_only", ai_external=False)
        path, body = EGRESS_SWITCHES[switch]
        before = _switch_state(db_session, admin_user.company_id)

        response = client.put(path, json=body, headers=bearer(token))

        assert response.status_code == 403, response.text[:400]
        assert response.json()["detail"] == READ_ONLY_ROUTE_WRITES_DETAIL
        assert _switch_state(db_session, admin_user.company_id) == before

    def test_turning_a_switch_off_is_refused_too(
        self, client: TestClient, db_session: Session, admin_user: User, admin_headers: dict
    ):
        """Closing egress is as much a judgment as opening it (and would cut a connector off)."""
        company = db_session.get(Company, admin_user.company_id)
        company.allow_ai_egress = True
        db_session.commit()
        _id, token = issue_token(client, admin_headers, admin_user)

        response = client.put(f"{API}/companies/me/ai-egress", json={"allow_ai_egress": False}, headers=bearer(token))

        assert response.status_code == 403
        assert response.json()["detail"] == API_TOKEN_EGRESS_SWITCH
        db_session.expire_all()
        assert db_session.get(Company, admin_user.company_id).allow_ai_egress is True


class TestAPersonStillFlipsIt:
    @pytest.mark.parametrize("switch", sorted(EGRESS_SWITCHES))
    def test_an_admin_signed_in_to_the_app_flips_the_switch(
        self, client: TestClient, db_session: Session, admin_user: User, admin_headers: dict, switch: str
    ):
        path, body = EGRESS_SWITCHES[switch]
        response = client.put(path, json=body, headers=admin_headers)
        assert response.status_code == 200, response.text[:400]
        (field,) = body
        assert response.json()[field] is True
        state = _switch_state(db_session, admin_user.company_id)
        key = {
            "ai": "allow_ai_egress",
            "sms": "allow_sms_egress",
            "carrier": "shipping_profile",
            "print": "print_profile",
        }
        assert state[key[switch]] is True


class TestTheAiFlipIsRequiredEvidence:
    def test_a_flip_writes_both_rows_in_its_own_transaction(
        self, client: TestClient, db_session: Session, admin_user: User, admin_headers: dict
    ):
        response = client.put(f"{API}/companies/me/ai-egress", json={"allow_ai_egress": True}, headers=admin_headers)
        assert response.status_code == 200, response.text[:400]
        rows = _company_audit_rows(db_session, admin_user.company_id)
        assert [row.action for row in rows] == ["UPDATE", "STATUS_CHANGE"]
        assert rows[0].old_values == {"allow_ai_egress": False}
        assert rows[0].new_values == {"allow_ai_egress": True}
        assert rows[0].extra_data["changes"] == {"allow_ai_egress": {"old": False, "new": True}}
        assert rows[1].old_values == {"status": "ai_egress_disabled"}
        assert rows[1].new_values == {"status": "ai_egress_enabled"}
        assert {row.user_id for row in rows} == {admin_user.id}

    def test_a_no_op_writes_nothing(
        self, client: TestClient, db_session: Session, admin_user: User, admin_headers: dict
    ):
        before = _switch_state(db_session, admin_user.company_id)
        response = client.put(f"{API}/companies/me/ai-egress", json={"allow_ai_egress": False}, headers=admin_headers)
        assert response.status_code == 200
        assert response.json()["allow_ai_egress"] is False
        assert _switch_state(db_session, admin_user.company_id) == before

    @pytest.mark.parametrize("failing_call", [1, 2], ids=["update_row_fails", "status_change_row_fails"])
    def test_an_audit_failure_rolls_the_flip_back(
        self,
        client: TestClient,
        db_session: Session,
        admin_user: User,
        admin_headers: dict,
        failing_call: int,
        monkeypatch,
    ):
        """Either required row failing answers 503 with nothing saved -- including, when the
        STATUS_CHANGE row is the one that fails, the UPDATE row already written before it."""
        real = AuditService.log_required
        calls = {"n": 0}

        def flaky(self, action, resource_type, **kwargs):
            calls["n"] += 1
            if calls["n"] == failing_call:
                raise AuditWriteError("simulated audit outage")
            return real(self, action, resource_type, **kwargs)

        monkeypatch.setattr(AuditService, "log_required", flaky)
        before = _switch_state(db_session, admin_user.company_id)

        response = client.put(f"{API}/companies/me/ai-egress", json={"allow_ai_egress": True}, headers=admin_headers)

        assert response.status_code == 503, response.text[:400]
        assert response.json()["detail"] == AI_EGRESS_AUDIT_UNAVAILABLE_DETAIL
        assert calls["n"] == failing_call
        assert _switch_state(db_session, admin_user.company_id) == before
        assert _company_audit_rows(db_session, admin_user.company_id) == []
