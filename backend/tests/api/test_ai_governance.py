"""The AI governance snapshot and copilot context describe the ruling-3 posture truthfully.

Owner ruling 3 (2026-11-05): there is no auto tier. ``apply_on_accept`` -- a named person's
Accept & apply -- is the only tier that changes a record, and it is OFF while the company's
AI egress is off.
"""

import pytest
from fastapi import status
from sqlalchemy import update

from app.models.ai_learning import AIRecommendation
from app.models.part import Part
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.services import llm_client
from app.services.ai_context_service import AIContextService

pytestmark = [pytest.mark.api, pytest.mark.requires_db]


@pytest.mark.parametrize("egress", [True, False])
def test_governance_has_no_auto_tier_and_tracks_egress(client, admin_headers, monkeypatch, egress):
    monkeypatch.setattr(llm_client, "_ai_egress_allowed", lambda company_id=None: egress)

    response = client.get("/api/v1/ai/governance", headers=admin_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["mode"] == "apply_on_accept"
    assert body["ai_egress_enabled"] is egress
    tiers = {tier["tier"]: tier for tier in body["autonomy_tiers"]}
    assert set(tiers) == {"observe", "draft", "recommend", "apply_on_accept"}
    assert tiers["apply_on_accept"]["enabled"] is egress
    assert tiers["apply_on_accept"]["requires_approval"] is True
    assert all(tier["requires_approval"] for name, tier in tiers.items() if name != "observe")
    assert not any("AI_AUTO_EXECUTE" in rule or "auto-execute" in rule.lower() for rule in body["approval_rules"])


def test_context_normalizes_a_legacy_auto_tag(db_session):
    part = Part(
        part_number="CTX-1",
        name="Context part",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=1,
    )
    db_session.add(part)
    db_session.flush()
    work_order = WorkOrder(
        work_order_number="WO-CTX-1",
        part_id=part.id,
        quantity_ordered=1,
        status=WorkOrderStatus.RELEASED,
        priority=5,
        company_id=1,
    )
    db_session.add(work_order)
    db_session.flush()
    rec = AIRecommendation(
        company_id=1,
        source_module="scheduling",
        recommendation_type="at_risk_delivery",
        title="Late",
        summary="Late",
        target_entity_type="work_order",
        target_entity_id=work_order.id,
        suggested_action={"type": "adjust_work_order_priority", "autonomy": "apply_on_accept"},
    )
    db_session.add(rec)
    db_session.flush()
    db_session.execute(
        update(AIRecommendation)
        .where(AIRecommendation.id == rec.id)
        .values(suggested_action={"type": "adjust_work_order_priority", "autonomy": "auto_execute"})
    )
    db_session.commit()

    rows = AIContextService(db_session).pending_recommendations_for(
        company_id=1, entity_type="work_order", entity_id=work_order.id
    )

    assert [row["autonomy"] for row in rows] == ["apply_on_accept"]


def test_policy_egress_read_denies_without_a_company_and_reads_the_seam_at_call_time(monkeypatch):
    """``ai_egress_allowed`` is the POLICY read behind the Accept & apply door.

    The underlying seam treats a missing company as "allowed" (an LLM-call edge); a policy
    read must not, or a request without a tenant would pass the egress gate. And the seam
    is looked up at call time, so the suite-wide patch (and a per-test OFF patch) applies.
    """
    seen = []

    def seam(company_id=None):
        seen.append(company_id)
        return company_id == 7

    monkeypatch.setattr(llm_client, "_ai_egress_allowed", seam)

    assert llm_client.ai_egress_allowed(None) is False
    assert llm_client.ai_egress_allowed(7) is True
    assert llm_client.ai_egress_allowed(8) is False
    assert seen == [7, 8]  # None never reaches the seam
