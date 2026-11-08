"""Owner ruling 3 (2026-11-05): Accept & apply is a NAMED PERSON's click, and what runs is what they saw.

``POST /api/v1/ai/recommendations/{id}/apply`` is the only executor of an AI recommendation.
These tests pin its gates (in order), the plan resolver behind the list's ``apply_action``
(what the person sees is what runs, fingerprint-checked), the per-action refusals, the
atomic claim, and the provenance every applied mutation carries.
"""

import re
from datetime import datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import text, update
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.ai_learning import AIInteractionEvent, AIRecommendation
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.purchasing import PurchaseOrder, Vendor
from app.models.quality import NonConformanceReport
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlocker
from app.services import llm_client
from app.services.ai_learning_service import AILearningService, RecommendationStateError

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

LIST_URL = "/api/v1/ai/recommendations"


def _apply_url(rec_id: int) -> str:
    return f"/api/v1/ai/recommendations/{rec_id}/apply"


def _headers(user: User, **claims) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id, **claims)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _user(db: Session, role: UserRole, tag: str, company_id: int = 1) -> User:
    user = User(
        email=f"{tag}@werco.com",
        employee_id=f"EMP-{tag.upper()}",
        first_name=tag.title(),
        last_name="Tester",
        hashed_password="not-a-real-hash",
        role=role,
        is_active=True,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _ensure_company(db: Session, company_id: int) -> None:
    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True))
        db.commit()


def _part(db: Session, number: str, company_id: int = 1, **kwargs) -> Part:
    fields = dict(
        part_number=number,
        name=f"Part {number}",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
        standard_cost=10.0,
    )
    fields.update(kwargs)
    part = Part(**fields)
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def _work_order(
    db: Session, number: str, *, priority=5, status_=WorkOrderStatus.RELEASED, part: Part = None
) -> WorkOrder:
    part = part or _part(db, f"P-{number}")
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


def _vendor(db: Session, code: str, *, is_active: bool = True, company_id: int = 1) -> Vendor:
    vendor = Vendor(code=code, name=f"Vendor {code}", is_active=is_active, company_id=company_id)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return vendor


def _blocker(
    db: Session, work_order: WorkOrder, *, status_="open", severity="medium", title="No sheet"
) -> WorkOrderBlocker:
    blocker = WorkOrderBlocker(
        company_id=1,
        work_order_id=work_order.id,
        category="material_missing",
        severity=severity,
        status=status_,
        title=title,
    )
    db.add(blocker)
    db.commit()
    db.refresh(blocker)
    return blocker


def _rec(
    db: Session,
    action: dict,
    *,
    target_type: str = None,
    target_id: int = None,
    rtype: str = "at_risk_delivery",
    source: str = "scheduling",
    company_id: int = 1,
) -> AIRecommendation:
    rec = AIRecommendation(
        company_id=company_id,
        source_module=source,
        recommendation_type=rtype,
        title="AI suggestion",
        summary="Evidence-backed suggestion",
        target_entity_type=target_type,
        target_entity_id=target_id,
        suggested_action={"autonomy": "apply_on_accept", **action},
        confidence_score=0.9,
        priority="high",
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


def _priority_rec(db: Session, work_order: WorkOrder, priority: int = 1, **action) -> AIRecommendation:
    return _rec(
        db,
        {"type": "adjust_work_order_priority", "work_order_id": work_order.id, "priority": priority, **action},
        target_type="work_order",
        target_id=work_order.id,
    )


def _listed(client: TestClient, headers: dict, rec_id: int, **params) -> dict:
    response = client.get(LIST_URL, headers=headers, params=params)
    assert response.status_code == status.HTTP_200_OK, response.text
    rows = [row for row in response.json() if row["id"] == rec_id]
    assert rows, f"recommendation {rec_id} not listed"
    return rows[0]


def _apply(client: TestClient, headers: dict, rec_id: int, fingerprint: str, reason: str = "Do it"):
    return client.post(
        _apply_url(rec_id), json={"reason": reason, "expected_fingerprint": fingerprint}, headers=headers
    )


def _list_then_apply(client: TestClient, headers: dict, rec_id: int):
    row = _listed(client, headers, rec_id)
    return row, _apply(client, headers, rec_id, row["apply_action"]["fingerprint"])


def _audit_rows(db: Session, resource_type: str, resource_id: int = None):
    query = db.query(AuditLog).filter(AuditLog.resource_type == resource_type)
    if resource_id is not None:
        query = query.filter(AuditLog.resource_id == resource_id)
    return query.order_by(AuditLog.id).all()


def _events(db: Session, event_type: str):
    return db.query(OperationalEvent).filter(OperationalEvent.event_type == event_type).all()


def _issue_api_token(client: TestClient, admin_headers: dict, user: User) -> dict:
    response = client.post("/api/v1/api-tokens/", json={"user_id": user.id, "label": "ai bot"}, headers=admin_headers)
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['token']}", "X-Requested-With": "XMLHttpRequest"}


# =============================================================================================
# Happy path and gates
# =============================================================================================


class TestApplyHappyPathAndGates:
    def test_apply_runs_as_the_clicking_person(self, client, db_session, admin_user, admin_headers):
        wo = _work_order(db_session, "WO-APPLY-1", priority=5)
        rec = _priority_rec(db_session, wo, 1)

        row = _listed(client, admin_headers, rec.id)
        action = row["apply_action"]
        assert action["can_apply"] is True
        assert action["can_mark_handled"] is True
        assert action["blocked_kind"] is None and action["blocked_reason"] is None
        assert action["summary"] == "Raise WO-APPLY-1 priority from P5 to P1"
        assert re.fullmatch(r"[0-9a-f]{16}", action["fingerprint"])
        assert row["target_href"] == f"/work-orders/{wo.id}"

        response = _apply(client, admin_headers, rec.id, action["fingerprint"])
        assert response.status_code == status.HTTP_200_OK, response.text
        body = response.json()
        assert body["applied"] is True
        assert body["apply_error"] is None
        assert body["recommendation"]["status"] == "accepted"
        assert body["recommendation"]["accepted_by"] == admin_user.id
        assert body["apply_result"]["new_priority"] == 1
        assert body["apply_result"]["summary"] == "Raise WO-APPLY-1 priority from P5 to P1"

        db_session.refresh(wo)
        assert wo.priority == 1
        rows = [r for r in _audit_rows(db_session, "work_order", wo.id) if r.action == "UPDATE"]
        assert len(rows) == 1
        extra = rows[0].extra_data
        assert extra["source"] == "ai_recommendation"
        assert extra["recommendation_id"] == rec.id
        assert extra["approved_by"] == admin_user.id
        assert extra["plan_fingerprint"] == action["fingerprint"]
        assert rows[0].user_id == admin_user.id

        accepted = (
            db_session.query(AIInteractionEvent)
            .filter(AIInteractionEvent.recommendation_id == rec.id, AIInteractionEvent.event_type == "accepted")
            .order_by(AIInteractionEvent.id)
            .all()
        )
        assert accepted, "the claim records an accepted event"
        assert not str(accepted[0].event_payload.get("note", "")).startswith("Suggest-only")
        assert len(_events(db_session, "ai_recommendation_applied")) == 1

    def test_missing_fingerprint_422(self, client, db_session, admin_headers):
        wo = _work_order(db_session, "WO-FP-422")
        rec = _priority_rec(db_session, wo, 1)
        assert client.post(_apply_url(rec.id), json={"reason": "x"}, headers=admin_headers).status_code == 422
        assert client.post(_apply_url(rec.id), headers=admin_headers).status_code == 422
        db_session.refresh(rec)
        assert rec.status == "pending"

    def test_legacy_auto_execute_tag_is_apply_on_accept(self, client, db_session, admin_headers):
        wo = _work_order(db_session, "WO-LEGACY", priority=6)
        rec = _priority_rec(db_session, wo, 2)
        # The ORM @validates refuses the legacy tag, so seed it the way old rows exist: Core.
        db_session.execute(
            update(AIRecommendation)
            .where(AIRecommendation.id == rec.id)
            .values(
                suggested_action={
                    "type": "adjust_work_order_priority",
                    "work_order_id": wo.id,
                    "priority": 2,
                    "autonomy": "auto_execute",
                }
            )
        )
        db_session.commit()
        db_session.expire_all()

        row, response = _list_then_apply(client, admin_headers, rec.id)
        assert row["apply_action"]["can_apply"] is True
        assert response.status_code == status.HTTP_200_OK, response.text
        db_session.refresh(wo)
        assert wo.priority == 2

    def test_suggest_only_allowlisted_type_409_has_no_action(self, client, db_session, admin_headers):
        wo = _work_order(db_session, "WO-SUGGEST", priority=5)
        rec = _priority_rec(db_session, wo, 1, autonomy="suggest_only")
        assert _listed(client, admin_headers, rec.id)["apply_action"] is None

        response = _apply(client, admin_headers, rec.id, "deadbeefdeadbeef")
        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json()["detail"].startswith("This recommendation has no action to apply.")
        db_session.refresh(wo)
        db_session.refresh(rec)
        assert wo.priority == 5 and rec.status == "pending"

    def test_egress_off_refuses_apply_403_and_list_says_why(
        self, client, db_session, supervisor_user, supervisor_headers, monkeypatch
    ):
        monkeypatch.setattr(llm_client, "_ai_egress_allowed", lambda company_id=None: False)
        wo = _work_order(db_session, "WO-EGRESS", priority=5)
        rec = _priority_rec(db_session, wo, 1)

        action = _listed(client, supervisor_headers, rec.id)["apply_action"]
        assert action["blocked_kind"] == "egress"
        assert action["can_apply"] is False
        assert action["can_mark_handled"] is True
        assert "AI egress is off" in action["blocked_reason"]
        # The plan is still shown, so a person knows what would have run.
        assert action["summary"] == "Raise WO-EGRESS priority from P5 to P1"

        response = _apply(client, supervisor_headers, rec.id, action["fingerprint"])
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert "AI egress is off" in response.json()["detail"]
        db_session.refresh(wo)
        db_session.refresh(rec)
        assert wo.priority == 5 and rec.status == "pending" and rec.accepted_by is None

    def test_role_gate_403_before_any_state_change(self, client, db_session, operator_headers):
        wo = _work_order(db_session, "WO-ROLE", priority=5)
        rec = _priority_rec(db_session, wo, 1)

        action = _listed(client, operator_headers, rec.id)["apply_action"]
        assert action["blocked_kind"] == "role"
        assert action["can_apply"] is False and action["can_mark_handled"] is False

        response = _apply(client, operator_headers, rec.id, action["fingerprint"])
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json()["detail"] == (
            "Your role (operator) cannot apply this action: Raise WO-ROLE priority from P5 to P1. "
            "It needs Supervisor, Manager or Admin."
        )
        db_session.refresh(rec)
        db_session.refresh(wo)
        assert rec.status == "pending" and wo.priority == 5
        assert db_session.query(AIInteractionEvent).filter(AIInteractionEvent.recommendation_id == rec.id).count() == 0

    def test_quality_can_apply_ncr_but_not_priority(self, client, db_session):
        quality = _user(db_session, UserRole.QUALITY, "quality")
        headers = _headers(quality)
        part = _part(db_session, "QA-NCR")
        ncr_rec = _rec(
            db_session,
            {"type": "create_draft_ncr", "part_id": part.id, "title": "Elevated scrap on QA-NCR"},
            target_type="part",
            target_id=part.id,
            rtype="quality_trend",
            source="quality",
        )
        wo = _work_order(db_session, "WO-QA", priority=5)
        priority_rec = _priority_rec(db_session, wo, 1)

        _row, response = _list_then_apply(client, headers, ncr_rec.id)
        assert response.status_code == status.HTTP_200_OK, response.text

        row = _listed(client, headers, priority_rec.id)
        assert row["apply_action"]["blocked_kind"] == "role"
        response = _apply(client, headers, priority_rec.id, row["apply_action"]["fingerprint"])
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json()["detail"].startswith("Your role (quality) cannot apply this action")

    def test_api_token_cannot_apply_or_mark_action_card_handled(self, client, db_session, admin_user, admin_headers):
        wo = _work_order(db_session, "WO-TOKEN", priority=5)
        rec = _priority_rec(db_session, wo, 1)
        suggest = _rec(db_session, {"type": "review_quote_margin", "autonomy": "suggest_only"}, rtype="note")
        token_headers = _issue_api_token(client, admin_headers, admin_user)

        action = _listed(client, token_headers, rec.id)["apply_action"]
        assert action["blocked_kind"] == "credential"
        assert action["can_apply"] is False and action["can_mark_handled"] is False

        response = _apply(client, token_headers, rec.id, action["fingerprint"])
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert "API tokens and agents" in response.json()["detail"]

        accept = client.post(f"{LIST_URL}/{rec.id}/accept", json={"reason": "bot"}, headers=token_headers)
        assert accept.status_code == status.HTTP_403_FORBIDDEN
        assert accept.json()["detail"] == (
            "API tokens and agents cannot mark an action card handled; a person who can apply it must."
        )
        db_session.refresh(rec)
        db_session.refresh(wo)
        assert rec.status == "pending" and wo.priority == 5

        # A suggest-only recommendation stays acceptable by an agent (record-only triage).
        ok = client.post(f"{LIST_URL}/{suggest.id}/accept", json={"reason": "bot"}, headers=token_headers)
        assert ok.status_code == status.HTTP_200_OK, ok.text
        assert ok.json()["applied"] is False

    def test_read_only_context_blocked_kind(self, client, db_session, supervisor_user):
        wo = _work_order(db_session, "WO-RO", priority=5)
        rec = _priority_rec(db_session, wo, 1)
        headers = _headers(supervisor_user, read_only=True)

        action = _listed(client, headers, rec.id)["apply_action"]
        assert action["blocked_kind"] == "read_only"
        assert action["can_apply"] is False and action["can_mark_handled"] is False

        response = _apply(client, headers, rec.id, action["fingerprint"])
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json()["detail"] == "Read-only company context cannot modify data"

    def test_apply_foreign_tenant_rec_404(self, client, db_session, admin_headers):
        _ensure_company(db_session, 2)
        foreign = _rec(db_session, {"type": "adjust_work_order_priority", "work_order_id": 1}, company_id=2)
        response = _apply(client, admin_headers, foreign.id, "deadbeefdeadbeef")
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["detail"] == "AI recommendation not found"


# =============================================================================================
# What runs is what was shown
# =============================================================================================


class TestWhatRunsIsWhatWasShown:
    def test_fingerprint_mismatch_409_and_nothing_written(self, client, db_session, admin_headers):
        wo = _work_order(db_session, "WO-MOVED", priority=5)
        rec = _priority_rec(db_session, wo, 1)
        fingerprint = _listed(client, admin_headers, rec.id)["apply_action"]["fingerprint"]

        wo.priority = 3  # someone re-prioritised the job after the card was loaded
        db_session.commit()
        audit_before = len(_audit_rows(db_session, "work_order", wo.id))

        response = _apply(client, admin_headers, rec.id, fingerprint)
        assert response.status_code == status.HTTP_409_CONFLICT
        detail = response.json()["detail"]
        assert "changed since you loaded it" in detail
        assert "Raise WO-MOVED priority from P3 to P1" in detail

        db_session.refresh(rec)
        db_session.refresh(wo)
        assert rec.status == "pending" and rec.accepted_by is None
        assert wo.priority == 3
        assert len(_audit_rows(db_session, "work_order", wo.id)) == audit_before
        assert _events(db_session, "ai_recommendation_applied") == []

    def test_crafted_label_cannot_mislead(self, client, db_session, admin_headers):
        _work_order(db_session, "WO-CRAFT-A", priority=5)
        acted = _work_order(db_session, "WO-CRAFT-B", priority=5)
        rec = _rec(
            db_session,
            {
                "type": "adjust_work_order_priority",
                "work_order_id": acted.id,
                "work_order_number": "WO-CRAFT-A",  # a stored label naming a different job
                "priority": 1,
            },
            target_type="work_order",
            target_id=acted.id,
        )
        action = _listed(client, admin_headers, rec.id)["apply_action"]
        assert action["summary"] == "Raise WO-CRAFT-B priority from P5 to P1"
        assert action["can_apply"] is True


# The card's "Open record" link is built from its TARGET; the action must change that record.
_MISMATCH_CASES = ["priority", "draft_po", "escalate_other_blocker", "escalate_blocker_on_another_job"]


class TestActionMustChangeTheLinkedRecord:
    @pytest.mark.parametrize("case", _MISMATCH_CASES)
    def test_action_and_target_naming_different_records_is_refused(self, client, db_session, supervisor_headers, case):
        from app.services.ai_action_applier import TARGET_MISMATCH_DETAIL

        shown = _work_order(db_session, f"WO-SHOWN-{case}", priority=6, status_=WorkOrderStatus.IN_PROGRESS)
        acted = _work_order(db_session, f"WO-ACTED-{case}", priority=6, status_=WorkOrderStatus.IN_PROGRESS)
        shown_blocker = _blocker(db_session, shown)
        acted_blocker = _blocker(db_session, acted)
        vendor = _vendor(db_session, f"VMIS{_MISMATCH_CASES.index(case)}")
        shown_part = _part(db_session, f"MIS-SHOWN-{case}", primary_supplier_id=vendor.id, reorder_quantity=5)
        acted_part = _part(db_session, f"MIS-ACTED-{case}", primary_supplier_id=vendor.id, reorder_quantity=5)
        action, target_type, target_id, href = {
            "priority": (
                {"type": "adjust_work_order_priority", "work_order_id": acted.id, "priority": 1},
                "work_order",
                shown.id,
                f"/work-orders/{shown.id}",
            ),
            "draft_po": (
                {"type": "create_draft_po", "part_id": acted_part.id},
                "part",
                shown_part.id,
                f"/parts/{shown_part.id}",
            ),
            "escalate_other_blocker": (
                {"type": "escalate_blocker", "blocker_id": acted_blocker.id},
                "work_order_blocker",
                shown_blocker.id,
                f"/work-orders/{shown.id}",
            ),
            # The link goes to the targeted job; the blocker the action names sits on another.
            "escalate_blocker_on_another_job": (
                {"type": "escalate_blocker", "blocker_id": acted_blocker.id},
                "work_order",
                shown.id,
                f"/work-orders/{shown.id}",
            ),
        }[case]
        rec = _rec(db_session, action, target_type=target_type, target_id=target_id)

        row = _listed(client, supervisor_headers, rec.id)
        assert row["target_href"] == href
        apply_action = row["apply_action"]
        assert apply_action["blocked_kind"] == "target"
        assert apply_action["blocked_reason"] == TARGET_MISMATCH_DETAIL
        assert apply_action["can_apply"] is False
        assert apply_action["can_mark_handled"] is True

        response = _apply(client, supervisor_headers, rec.id, apply_action["fingerprint"])
        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        assert response.json()["detail"] == TARGET_MISMATCH_DETAIL

        db_session.expire_all()
        assert {wo.priority for wo in db_session.query(WorkOrder).filter(WorkOrder.id.in_([shown.id, acted.id]))} == {6}
        assert {b.status for b in db_session.query(WorkOrderBlocker)} == {"open"}
        assert db_session.query(PurchaseOrder).count() == 0
        assert _audit_rows(db_session, "work_order") == [] and _audit_rows(db_session, "work_order_blocker") == []
        assert db_session.query(AIRecommendation).filter(AIRecommendation.id == rec.id).one().status == "pending"

    def test_matching_target_and_action_still_apply(self, client, db_session, supervisor_headers):
        # Negative control: a blocker card targeting its own job's work order is consistent.
        wo = _work_order(db_session, "WO-MATCH", priority=6, status_=WorkOrderStatus.IN_PROGRESS)
        blocker = _blocker(db_session, wo)
        rec = _rec(
            db_session,
            {"type": "escalate_blocker", "blocker_id": blocker.id},
            target_type="work_order",
            target_id=wo.id,
        )

        row, response = _list_then_apply(client, supervisor_headers, rec.id)
        assert row["apply_action"]["can_apply"] is True
        assert response.status_code == status.HTTP_200_OK, response.text

    @pytest.mark.parametrize("suggested", [2, 1])
    def test_priority_never_demotes_and_noop_is_blocked(self, client, db_session, admin_headers, suggested):
        wo = _work_order(db_session, f"WO-P1-{suggested}", priority=1)
        version_before = wo.version
        rec = _priority_rec(db_session, wo, suggested)

        action = _listed(client, admin_headers, rec.id)["apply_action"]
        assert action["blocked_kind"] == "target"
        assert action["can_apply"] is False
        assert action["can_mark_handled"] is True
        assert "already P1" in action["blocked_reason"]

        response = _apply(client, admin_headers, rec.id, action["fingerprint"])
        assert response.status_code == status.HTTP_409_CONFLICT
        assert "already P1" in response.json()["detail"]
        db_session.refresh(wo)
        db_session.refresh(rec)
        assert wo.priority == 1 and wo.version == version_before and rec.status == "pending"

    def test_soft_deleted_released_wo_is_refused(self, client, db_session, admin_user, admin_headers):
        wo = _work_order(db_session, "WO-GONE", priority=5)
        rec = _priority_rec(db_session, wo, 1)
        wo.soft_delete(admin_user.id)
        db_session.commit()
        db_session.refresh(wo)
        version_before = wo.version

        action = _listed(client, admin_headers, rec.id)["apply_action"]
        assert action["blocked_kind"] == "target"
        assert action["blocked_reason"] == "Work order WO-GONE has been deleted"

        response = _apply(client, admin_headers, rec.id, action["fingerprint"])
        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json()["detail"] == "Work order WO-GONE has been deleted"
        db_session.refresh(wo)
        assert wo.priority == 5 and wo.version == version_before


# =============================================================================================
# escalate_blocker plans
# =============================================================================================


def _escalate_rec(db: Session, blocker: WorkOrderBlocker, rtype: str = "material_blocker_triage") -> AIRecommendation:
    return _rec(
        db,
        {"type": "escalate_blocker", "blocker_id": blocker.id, "work_order_id": blocker.work_order_id},
        target_type="work_order_blocker",
        target_id=blocker.id,
        rtype=rtype,
        source="shop_floor",
    )


class TestEscalateBlocker:
    def test_escalate_critical_blocker_stays_critical(self, client, db_session, supervisor_headers):
        wo = _work_order(db_session, "WO-CRIT", priority=6, status_=WorkOrderStatus.IN_PROGRESS)
        blocker = _blocker(db_session, wo, severity="critical")
        rec = _escalate_rec(db_session, blocker)

        row, response = _list_then_apply(client, supervisor_headers, rec.id)
        summary = row["apply_action"]["summary"]
        assert "severity" not in summary
        assert summary == (
            "Acknowledge blocker 'No sheet' and expedite WO-CRIT from P6 to P2. "
            "This does not resolve the blocker or lift its hold."
        )
        assert row["target_href"] == f"/work-orders/{wo.id}"
        assert response.status_code == status.HTTP_200_OK, response.text
        db_session.refresh(blocker)
        db_session.refresh(wo)
        assert blocker.status == "acknowledged"
        assert blocker.severity == "critical"
        assert wo.priority == 2

    def test_escalate_acknowledged_medium_writes_one_provenance_severity_row(
        self, client, db_session, supervisor_user, supervisor_headers
    ):
        wo = _work_order(db_session, "WO-SEV", priority=2, status_=WorkOrderStatus.IN_PROGRESS)
        blocker = _blocker(db_session, wo, status_="acknowledged", severity="medium")
        rec = _escalate_rec(db_session, blocker)

        row, response = _list_then_apply(client, supervisor_headers, rec.id)
        assert row["apply_action"]["summary"].startswith("Raise blocker 'No sheet' severity from medium to high.")
        assert response.status_code == status.HTTP_200_OK, response.text

        rows = _audit_rows(db_session, "work_order_blocker", blocker.id)
        updates = [r for r in rows if r.action == "UPDATE"]
        assert len(updates) == 1
        assert [r for r in rows if r.action == "STATUS_CHANGE"] == []
        extra = updates[0].extra_data
        assert extra["source"] == "ai_recommendation"
        assert extra["approved_by"] == supervisor_user.id
        assert updates[0].old_values == {"severity": "medium"}
        assert updates[0].new_values == {"severity": "high"}
        db_session.refresh(wo)
        assert wo.priority == 2

    def test_escalate_on_a_finished_job_is_a_closed_target(self, client, db_session, supervisor_headers):
        # The nightly sweep retires this card ("Target closed: work order WO-DONE is
        # complete"); until it runs, the plan must refuse rather than acknowledge a blocker
        # on a finished job. One definition of a closed target for the list, /apply and sweep.
        wo = _work_order(db_session, "WO-DONE", priority=6, status_=WorkOrderStatus.COMPLETE)
        blocker = _blocker(db_session, wo, severity="medium")
        rec = _escalate_rec(db_session, blocker)

        row, response = _list_then_apply(client, supervisor_headers, rec.id)
        action = row["apply_action"]
        assert action["blocked_kind"] == "target"
        assert action["blocked_reason"] == "Work order WO-DONE is complete; nothing to escalate"
        assert action["can_mark_handled"] is True
        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        assert response.json()["detail"] == "Work order WO-DONE is complete; nothing to escalate"
        db_session.refresh(wo)
        db_session.refresh(blocker)
        db_session.refresh(rec)
        assert wo.priority == 6
        assert (blocker.status, blocker.severity) == ("open", "medium")
        assert _audit_rows(db_session, "work_order_blocker", blocker.id) == []
        assert rec.status == "pending"

    def test_escalate_resolved_blocker_is_blocked_and_not_reopened(self, client, db_session, supervisor_headers):
        wo = _work_order(db_session, "WO-RES", priority=6, status_=WorkOrderStatus.IN_PROGRESS)
        blocker = _blocker(db_session, wo, status_="resolved")
        rec = _escalate_rec(db_session, blocker)

        action = _listed(client, supervisor_headers, rec.id)["apply_action"]
        assert action["blocked_kind"] == "target"
        assert action["blocked_reason"] == "Blocker 'No sheet' is already resolved; nothing to escalate"

        response = _apply(client, supervisor_headers, rec.id, action["fingerprint"])
        assert response.status_code == status.HTTP_409_CONFLICT
        db_session.refresh(blocker)
        db_session.refresh(wo)
        assert blocker.status == "resolved" and wo.priority == 6


# =============================================================================================
# Draft PO
# =============================================================================================


def _po_rec(db: Session, part: Part, **action) -> AIRecommendation:
    return _rec(
        db,
        {"type": "create_draft_po", "part_id": part.id, **action},
        target_type="part",
        target_id=part.id,
        rtype="inventory_risk",
        source="inventory",
    )


class TestDraftPurchaseOrder:
    def test_draft_po_uses_current_primary_supplier(self, client, db_session, supervisor_user, supervisor_headers):
        minted_with = _vendor(db_session, "VA")
        current = _vendor(db_session, "VB")
        part = _part(db_session, "PO-CUR", part_type="raw_material", primary_supplier_id=minted_with.id)
        part.reorder_quantity = 25
        db_session.commit()
        # A legacy card carried the mint-time vendor; Accept & apply must ignore it.
        rec = _po_rec(db_session, part, vendor_id=minted_with.id, suggested_qty=25)
        part.primary_supplier_id = current.id
        db_session.commit()

        row, response = _list_then_apply(client, supervisor_headers, rec.id)
        assert row["apply_action"]["summary"] == (
            "Create a DRAFT purchase order to Vendor VB (VB) for 25 x PO-CUR at $10.00 each. "
            "Nothing is sent to the vendor."
        )
        assert row["target_href"] == f"/parts/{part.id}"
        assert response.status_code == status.HTTP_200_OK, response.text
        result = response.json()["apply_result"]
        po = db_session.query(PurchaseOrder).filter(PurchaseOrder.id == result["purchase_order_id"]).one()
        assert po.vendor_id == current.id
        assert po.created_by == supervisor_user.id
        assert re.fullmatch(r"PO-\d{8}-\d{3}", po.po_number)

        creates = [r for r in _audit_rows(db_session, "purchase_order", po.id) if r.action == "CREATE"]
        assert len(creates) == 1
        assert creates[0].extra_data["source"] == "ai_recommendation"
        assert creates[0].extra_data["recommendation_id"] == rec.id
        assert creates[0].extra_data["vendor_code"] == "VB"
        assert len(_events(db_session, "purchase_order_created")) == 1

        # The next draft the same day takes the next locked number.
        second = _po_rec(db_session, part, suggested_qty=5)
        _row2, response2 = _list_then_apply(client, supervisor_headers, second.id)
        assert response2.status_code == status.HTTP_200_OK, response2.text
        second_number = response2.json()["apply_result"]["po_number"]
        assert int(second_number.rsplit("-", 1)[1]) == int(po.po_number.rsplit("-", 1)[1]) + 1

    def test_draft_po_refusals(self, client, db_session, admin_user, supervisor_headers):
        cases = []
        no_supplier = _part(db_session, "PO-NOSUP", reorder_quantity=10)
        cases.append((_po_rec(db_session, no_supplier), "PO-NOSUP has no primary supplier"))

        inactive = _vendor(db_session, "VOFF", is_active=False)
        inactive_part = _part(db_session, "PO-INACT", primary_supplier_id=inactive.id, reorder_quantity=10)
        cases.append((_po_rec(db_session, inactive_part), "Vendor Vendor VOFF is inactive"))

        live = _vendor(db_session, "VLIVE")
        deleted_part = _part(db_session, "PO-DEL", primary_supplier_id=live.id, reorder_quantity=10)
        deleted_rec = _po_rec(db_session, deleted_part)
        deleted_part.soft_delete(admin_user.id)
        db_session.commit()
        cases.append((deleted_rec, "Part PO-DEL has been deleted"))

        no_qty = _part(db_session, "PO-NOQTY", primary_supplier_id=live.id)
        cases.append((_po_rec(db_session, no_qty, suggested_qty=0), "PO-NOQTY has no reorder quantity"))

        for rec, expected in cases:
            action = _listed(client, supervisor_headers, rec.id)["apply_action"]
            assert action["blocked_kind"] == "target", expected
            assert expected in action["blocked_reason"]
            response = _apply(client, supervisor_headers, rec.id, action["fingerprint"])
            assert response.status_code == status.HTTP_409_CONFLICT, expected
            assert expected in response.json()["detail"]
        assert db_session.query(PurchaseOrder).count() == 0
        # No supplier means no vendor is ever named on the card.
        assert "Vendor" not in _listed(client, supervisor_headers, cases[0][0].id)["apply_action"]["summary"]

    def test_open_po_warning_listed(self, client, db_session, supervisor_headers):
        vendor = _vendor(db_session, "VWARN")
        part = _part(db_session, "PO-WARN", primary_supplier_id=vendor.id, reorder_quantity=10)
        first = _po_rec(db_session, part)
        _row, response = _list_then_apply(client, supervisor_headers, first.id)
        assert response.status_code == status.HTTP_200_OK, response.text
        po_number = response.json()["apply_result"]["po_number"]

        second = _po_rec(db_session, part)
        action = _listed(client, supervisor_headers, second.id)["apply_action"]
        assert action["can_apply"] is True
        assert action["warnings"] == [f"PO-WARN already has open PO(s): {po_number}"]

    def test_an_open_po_raised_after_the_card_loaded_is_a_plan_change(
        self, client, db_session, supervisor_user, supervisor_headers
    ):
        """The duplicate-PO warning is part of what the person approves, so it is fingerprinted.

        A buyer raises a PO for the part by hand between the list load and the click: the
        person approved a card that showed no warning, so /apply must 409 and the re-read
        must show the warning, rather than silently creating a second draft PO.
        """
        from decimal import Decimal

        from app.schemas.purchasing import POCreate, POLineCreate
        from app.services.audit_service import AuditService
        from app.services.erp_draft_commands import create_purchase_order_command

        vendor = _vendor(db_session, "VRACE")
        part = _part(db_session, "PO-RACE", primary_supplier_id=vendor.id, reorder_quantity=10)
        rec = _po_rec(db_session, part)
        loaded = _listed(client, supervisor_headers, rec.id)["apply_action"]
        assert loaded["warnings"] == [] and loaded["can_apply"] is True

        manual = create_purchase_order_command(
            db_session,
            POCreate(
                vendor_id=vendor.id,
                lines=[POLineCreate(part_id=part.id, quantity_ordered=Decimal("3"), unit_price=Decimal("1"))],
            ),
            supervisor_user,
            1,
            AuditService(db_session, supervisor_user),
        )
        db_session.commit()

        response = _apply(client, supervisor_headers, rec.id, loaded["fingerprint"])
        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        assert "changed since you loaded it" in response.json()["detail"]
        assert db_session.query(PurchaseOrder).count() == 1
        db_session.refresh(rec)
        assert rec.status == "pending"

        reread = _listed(client, supervisor_headers, rec.id)["apply_action"]
        assert reread["fingerprint"] != loaded["fingerprint"]
        assert reread["warnings"] == [f"PO-RACE already has open PO(s): {manual.po_number}"]
        # Having now seen the warning, the person can still choose to apply.
        confirmed = _apply(client, supervisor_headers, rec.id, reread["fingerprint"])
        assert confirmed.status_code == status.HTTP_200_OK, confirmed.text
        assert db_session.query(PurchaseOrder).count() == 2


# =============================================================================================
# Draft NCR
# =============================================================================================


def _ncr_rec(db: Session, part_id: int, **action) -> AIRecommendation:
    return _rec(
        db,
        {"type": "create_draft_ncr", "part_id": part_id, "title": "Elevated scrap", **action},
        target_type="part",
        target_id=part_id,
        rtype="quality_trend",
        source="quality",
    )


class TestDraftNCR:
    def test_draft_ncr_tenant_and_live_checks(self, client, db_session, admin_user, supervisor_headers):
        _ensure_company(db_session, 2)
        foreign_part = _part(db_session, "NCR-FOREIGN", company_id=2)
        foreign_rec = _ncr_rec(db_session, foreign_part.id)
        action = _listed(client, supervisor_headers, foreign_rec.id)["apply_action"]
        assert action["blocked_reason"] == "Part not found"
        response = _apply(client, supervisor_headers, foreign_rec.id, action["fingerprint"])
        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json()["detail"] == "Part not found"

        gone = _part(db_session, "NCR-GONE")
        gone_rec = _ncr_rec(db_session, gone.id)
        gone.soft_delete(admin_user.id)
        db_session.commit()
        action = _listed(client, supervisor_headers, gone_rec.id)["apply_action"]
        response = _apply(client, supervisor_headers, gone_rec.id, action["fingerprint"])
        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json()["detail"] == "Part NCR-GONE has been deleted"
        assert db_session.query(NonConformanceReport).count() == 0

    def test_draft_ncr_emits_ncr_created_event(self, client, db_session, supervisor_user, supervisor_headers):
        part = _part(db_session, "NCR-OK")
        rec = _ncr_rec(db_session, part.id, quantity_affected=3)
        row, response = _list_then_apply(client, supervisor_headers, rec.id)
        assert row["apply_action"]["summary"] == "Open an in-process NCR for NCR-OK: qty 3 — 'Elevated scrap'"
        assert response.status_code == status.HTTP_200_OK, response.text
        ncr_id = response.json()["apply_result"]["ncr_id"]
        ncr = db_session.query(NonConformanceReport).filter(NonConformanceReport.id == ncr_id).one()
        assert ncr.company_id == 1 and ncr.part_id == part.id and ncr.detected_by == supervisor_user.id
        events = _events(db_session, "ncr_created")
        assert len(events) == 1 and events[0].entity_id == ncr_id
        assert events[0].event_payload["ncr_number"] == ncr.ncr_number
        creates = [r for r in _audit_rows(db_session, "ncr", ncr_id) if r.action == "CREATE"]
        assert len(creates) == 1 and creates[0].extra_data["source"] == "ai_recommendation"


# =============================================================================================
# Races and rollback
# =============================================================================================


class TestRacesAndRollback:
    def test_second_click_is_refused_and_exactly_one_po_exists(self, client, db_session, supervisor_headers):
        # The second click is refused by the route's pre-check (the card already reads
        # accepted), so this does NOT reach the claim. The compare-and-set claim itself is
        # pinned by test_ai_apply_tenancy_and_claim.py::TestClaimIsACompareAndSet, where the
        # decision lands after the pre-check; removing the claim's status predicate leaves
        # this test green and fails that one.
        vendor = _vendor(db_session, "VONE")
        part = _part(db_session, "PO-ONCE", primary_supplier_id=vendor.id, reorder_quantity=4)
        rec = _po_rec(db_session, part)
        fingerprint = _listed(client, supervisor_headers, rec.id)["apply_action"]["fingerprint"]

        assert _apply(client, supervisor_headers, rec.id, fingerprint).status_code == status.HTTP_200_OK
        second = _apply(client, supervisor_headers, rec.id, fingerprint)
        assert second.status_code == status.HTTP_409_CONFLICT
        assert second.json()["detail"] == (
            "This recommendation is already accepted; it can no longer be accepted. Refresh the Action Inbox."
        )
        assert db_session.query(PurchaseOrder).count() == 1

    def test_stale_read_snooze_cannot_overwrite_applied(self, db_session, admin_user):
        service = AILearningService(db_session)

        def flip(rec_id: int, new_status: str) -> None:
            db_session.execute(
                text("UPDATE ai_recommendations SET status = :s WHERE id = :i"), {"s": new_status, "i": rec_id}
            )

        snooze_rec = _rec(db_session, {"type": "note", "autonomy": "suggest_only"}, rtype="note")
        assert snooze_rec.status == "pending"  # loaded into the identity map as pending
        flip(snooze_rec.id, "accepted")
        with pytest.raises(RecommendationStateError):
            service.snooze_recommendation(recommendation_id=snooze_rec.id, company_id=1, user=admin_user, days=3)
        assert db_session.execute(
            text("SELECT status FROM ai_recommendations WHERE id = :i"), {"i": snooze_rec.id}
        ).scalar() == ("accepted")

        dismiss_rec = _rec(db_session, {"type": "note", "autonomy": "suggest_only"}, rtype="note")
        assert dismiss_rec.status == "pending"
        flip(dismiss_rec.id, "accepted")
        with pytest.raises(RecommendationStateError):
            service.set_recommendation_status(
                recommendation_id=dismiss_rec.id,
                company_id=1,
                user=admin_user,
                status="dismissed",
                expected_statuses={"pending", "snoozed"},
            )
        db_session.refresh(dismiss_rec)
        assert dismiss_rec.status == "accepted" and dismiss_rec.dismissed_by is None

        # Wake: the sweep reads the snoozed ids, and a person dismisses the card before the
        # sweep writes. The wake must be a compare-and-set from 'snoozed', never a blind
        # write that resurrects the dismissed card to pending.
        wake_rec = _rec(db_session, {"type": "note", "autonomy": "suggest_only"}, rtype="note")
        service.snooze_recommendation(recommendation_id=wake_rec.id, company_id=1, user=admin_user, days=1)
        db_session.commit()
        assert wake_rec.status == "snoozed"
        read_wake_times = service._snoozed_until_by_recommendation

        def wake_times_then_dismiss(company_id, recommendation_ids):
            result = read_wake_times(company_id, recommendation_ids)
            flip(wake_rec.id, "dismissed")
            return result

        service._snoozed_until_by_recommendation = wake_times_then_dismiss  # type: ignore[method-assign]
        woken = service._wake_snoozed_recommendations_for_company(1, datetime.utcnow() + timedelta(days=2))
        assert woken == 0
        db_session.refresh(wake_rec)
        assert wake_rec.status == "dismissed"

    def test_concurrent_wo_edit_is_409_not_500(self, client, db_session, admin_headers):
        wo = _work_order(db_session, "WO-RACE", priority=5)
        rec = _priority_rec(db_session, wo, 1)
        fingerprint = _listed(client, admin_headers, rec.id)["apply_action"]["fingerprint"]
        # Another writer bumped the row's version after this session loaded it.
        db_session.execute(text("UPDATE work_orders SET version = version + 1 WHERE id = :i"), {"i": wo.id})
        audit_before = len(_audit_rows(db_session, "work_order", wo.id))

        response = _apply(client, admin_headers, rec.id, fingerprint)
        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        db_session.expire_all()
        rec = db_session.query(AIRecommendation).filter(AIRecommendation.id == rec.id).one()
        assert rec.status == "pending" and rec.accepted_by is None
        assert len(_audit_rows(db_session, "work_order", wo.id)) == audit_before
        assert _events(db_session, "ai_recommendation_applied") == []

    def test_applier_failure_rolls_back_the_claim(self, client, db_session, admin_headers):
        wo = _work_order(db_session, "WO-TERM", priority=5, status_=WorkOrderStatus.COMPLETE)
        rec = _priority_rec(db_session, wo, 1)
        action = _listed(client, admin_headers, rec.id)["apply_action"]
        assert action["blocked_reason"] == "Cannot change priority on a terminal work order"

        response = _apply(client, admin_headers, rec.id, action["fingerprint"])
        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json()["detail"] == "Cannot change priority on a terminal work order"
        db_session.refresh(rec)
        assert rec.status == "pending" and rec.accepted_by is None
        assert db_session.query(AIInteractionEvent).filter(AIInteractionEvent.recommendation_id == rec.id).count() == 0

    def test_apply_supersedes_open_siblings(self, client, db_session, supervisor_user, supervisor_headers):
        wo = _work_order(db_session, "WO-SIB", priority=6, status_=WorkOrderStatus.IN_PROGRESS)
        blocker = _blocker(db_session, wo)
        triage = _escalate_rec(db_session, blocker)
        stale = _escalate_rec(db_session, blocker, rtype="stale_blocker_escalation")

        _row, response = _list_then_apply(client, supervisor_headers, triage.id)
        assert response.status_code == status.HTTP_200_OK, response.text
        db_session.refresh(stale)
        assert stale.status == "stale"
        assert stale.status_reason == (
            f"Superseded: recommendation #{triage.id} was applied by {supervisor_user.full_name}"
        )

    def test_apply_broadcasts_company_scoped(self, client, db_session, admin_headers, monkeypatch):
        from app.api.endpoints import ai_learning as ai_learning_endpoint
        from app.core.websocket import broadcast_dashboard_update, broadcast_work_order_update

        calls = []
        monkeypatch.setattr(
            ai_learning_endpoint, "safe_broadcast", lambda coro, *args, **kwargs: calls.append((coro, args, kwargs))
        )
        wo = _work_order(db_session, "WO-BCAST", priority=5)
        rec = _priority_rec(db_session, wo, 1)
        _row, response = _list_then_apply(client, admin_headers, rec.id)
        assert response.status_code == status.HTTP_200_OK, response.text

        targets = {coro for coro, _args, _kwargs in calls}
        assert {broadcast_work_order_update, broadcast_dashboard_update} <= targets
        assert all(kwargs.get("company_id") == 1 for _coro, _args, kwargs in calls)

    def test_a_failing_post_commit_broadcast_never_fails_a_committed_apply(
        self, client, db_session, supervisor_user, supervisor_headers, monkeypatch
    ):
        """The apply is committed before the broadcast: a nudge that raises must not turn it into
        an error response (the client would say "Nothing was changed" and a retry would 409)."""
        from app.api.endpoints import ai_learning as ai_learning_endpoint

        def explode(*_args, **_kwargs):
            raise RuntimeError("websocket fan-out is down")

        monkeypatch.setattr(ai_learning_endpoint, "_broadcast_blocker", explode)
        wo = _work_order(db_session, "WO-BCAST-FAIL", priority=6, status_=WorkOrderStatus.IN_PROGRESS)
        blocker = _blocker(db_session, wo)
        rec = _escalate_rec(db_session, blocker)

        _row, response = _list_then_apply(client, supervisor_headers, rec.id)

        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.json()["applied"] is True
        db_session.refresh(rec)
        db_session.refresh(blocker)
        assert rec.status == "accepted" and rec.accepted_by == supervisor_user.id
        assert blocker.status == "acknowledged"
