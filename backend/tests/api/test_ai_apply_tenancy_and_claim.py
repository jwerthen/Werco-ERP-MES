"""Owner ruling 3 (2026-11-05): the Accept & apply door's claim, tenancy and provenance.

``tests/api/test_ai_recommendation_apply.py`` pins the gates and the per-action plans. This
module pins the properties that sit BETWEEN them and are easy to lose in a refactor:

* the claim is a real compare-and-set. A second /apply on an already-accepted card is
  refused by the route's pre-check before the claim is ever reached, so it cannot prove the
  claim. Here a concurrent decision lands AFTER the pre-check and BEFORE the claim, and the
  handler must never run;
* every action resolves its targets inside the active company only -- a crafted card naming
  another tenant's work order, blocker, part or vendor is refused and that tenant's rows are
  untouched;
* the closing/superseding sweeps and the record links are tenant-scoped too;
* EVERY audit row an applied action writes (status change, field update, PO create) carries
  the recommendation provenance, not just the first one;
* a refused apply broadcasts nothing, and an unrelated edit to the target does not trip the
  fingerprint (it hashes resolved facts, never the row version).
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.endpoints import ai_learning as ai_learning_endpoint
from app.models.ai_learning import AIInteractionEvent, AIRecommendation
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.purchasing import PurchaseOrder, Vendor
from app.models.quality import NonConformanceReport
from app.models.user import User
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlocker
from app.services.ai_learning_service import AILearningService

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

LIST_URL = "/api/v1/ai/recommendations"


# ---------------------------------------------------------------------------------------------
# Helpers (company-aware; the sibling module's helpers hard-code company 1)
# ---------------------------------------------------------------------------------------------


def _ensure_company(db: Session, company_id: int) -> None:
    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True))
        db.commit()


def _part(db: Session, number: str, *, company_id: int = 1, **kwargs) -> Part:
    fields = dict(
        part_number=number,
        name=f"Part {number}",
        part_type="raw_material",
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
    db: Session, number: str, *, company_id: int = 1, priority: int = 5, status_=WorkOrderStatus.RELEASED
) -> WorkOrder:
    part = _part(db, f"P-{number}", company_id=company_id)
    work_order = WorkOrder(
        work_order_number=number,
        part_id=part.id,
        quantity_ordered=1,
        status=status_,
        priority=priority,
        company_id=company_id,
    )
    db.add(work_order)
    db.commit()
    db.refresh(work_order)
    return work_order


def _vendor(db: Session, code: str, *, company_id: int = 1) -> Vendor:
    vendor = Vendor(code=code, name=f"Vendor {code}", is_active=True, company_id=company_id)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return vendor


def _blocker(db: Session, work_order: WorkOrder, *, status_: str = "open", severity: str = "medium"):
    blocker = WorkOrderBlocker(
        company_id=work_order.company_id,
        work_order_id=work_order.id,
        category="material_missing",
        severity=severity,
        status=status_,
        title="No sheet",
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
    company_id: int = 1,
    status_: str = "pending",
    rtype: str = "ai_card",
) -> AIRecommendation:
    rec = AIRecommendation(
        company_id=company_id,
        source_module="shop_floor",
        recommendation_type=rtype,
        status=status_,
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


def _listed(client: TestClient, headers: dict, rec_id: int) -> dict:
    response = client.get(LIST_URL, headers=headers)
    assert response.status_code == status.HTTP_200_OK, response.text
    rows = [row for row in response.json() if row["id"] == rec_id]
    assert rows, f"recommendation {rec_id} not listed"
    return rows[0]


def _apply(client: TestClient, headers: dict, rec_id: int, fingerprint: str):
    return client.post(
        f"{LIST_URL}/{rec_id}/apply",
        json={"reason": "Do it", "expected_fingerprint": fingerprint},
        headers=headers,
    )


def _status_of(db: Session, rec_id: int) -> str:
    return db.execute(text("SELECT status FROM ai_recommendations WHERE id = :i"), {"i": rec_id}).scalar()


def _audit_count(db: Session, *resource_types: str) -> int:
    return db.query(AuditLog).filter(AuditLog.resource_type.in_(list(resource_types))).count()


def _event_count(db: Session, event_type: str) -> int:
    return db.query(OperationalEvent).filter(OperationalEvent.event_type == event_type).count()


def _accepted_events(db: Session, rec_id: int) -> int:
    return (
        db.query(AIInteractionEvent)
        .filter(AIInteractionEvent.recommendation_id == rec_id, AIInteractionEvent.event_type == "accepted")
        .count()
    )


# =============================================================================================
# The claim is a compare-and-set, taken before any handler runs
# =============================================================================================


class TestClaimIsACompareAndSet:
    def test_apply_loses_to_a_decision_made_after_the_precheck_and_runs_nothing(
        self, client, db_session, supervisor_headers, monkeypatch
    ):
        """A concurrent person decides the card between the route's read and its claim.

        The pre-check saw ``pending``; the claim must still refuse, and the handler -- here a
        draft PO -- must never run. Without the ``status IN (pending)`` predicate on the
        claim the PO would be created on a card someone else already decided.
        """
        vendor = _vendor(db_session, "VRACE")
        part = _part(db_session, "PO-RACE", primary_supplier_id=vendor.id, reorder_quantity=4)
        rec = _rec(db_session, {"type": "create_draft_po", "part_id": part.id}, target_type="part", target_id=part.id)
        fingerprint = _listed(client, supervisor_headers, rec.id)["apply_action"]["fingerprint"]

        real_refusal = ai_learning_endpoint.apply_refusal

        def refusal_then_concurrent_dismiss(*args, **kwargs):
            outcome = real_refusal(*args, **kwargs)
            # Another person's dismissal commits after this request read the card as pending.
            db_session.execute(text("UPDATE ai_recommendations SET status = 'dismissed' WHERE id = :i"), {"i": rec.id})
            db_session.commit()
            return outcome

        monkeypatch.setattr(ai_learning_endpoint, "apply_refusal", refusal_then_concurrent_dismiss)

        response = _apply(client, supervisor_headers, rec.id, fingerprint)

        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        assert response.json()["detail"] == (
            "This recommendation is already dismissed; it can no longer be accepted. Refresh the Action Inbox."
        )
        db_session.expire_all()
        assert db_session.query(PurchaseOrder).count() == 0
        assert _audit_count(db_session, "purchase_order") == 0
        assert _event_count(db_session, "purchase_order_created") == 0
        assert _event_count(db_session, "ai_recommendation_applied") == 0
        rec = db_session.query(AIRecommendation).filter(AIRecommendation.id == rec.id).one()
        assert rec.status == "dismissed" and rec.accepted_by is None
        assert _accepted_events(db_session, rec.id) == 0

    def test_mark_handled_loses_to_a_decision_made_after_the_precheck(
        self, client, db_session, supervisor_headers, monkeypatch
    ):
        work_order = _work_order(db_session, "WO-ACC-RACE")
        rec = _rec(
            db_session,
            {"type": "adjust_work_order_priority", "work_order_id": work_order.id, "priority": 1},
            target_type="work_order",
            target_id=work_order.id,
        )
        real_refusal = ai_learning_endpoint.mark_handled_refusal

        def refusal_then_concurrent_snooze(*args, **kwargs):
            outcome = real_refusal(*args, **kwargs)
            db_session.execute(text("UPDATE ai_recommendations SET status = 'snoozed' WHERE id = :i"), {"i": rec.id})
            db_session.commit()
            return outcome

        monkeypatch.setattr(ai_learning_endpoint, "mark_handled_refusal", refusal_then_concurrent_snooze)

        response = client.post(f"{LIST_URL}/{rec.id}/accept", json={"reason": "done"}, headers=supervisor_headers)

        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        assert response.json()["detail"].startswith("This recommendation is already snoozed")
        assert _status_of(db_session, rec.id) == "snoozed"
        assert _accepted_events(db_session, rec.id) == 0


# =============================================================================================
# Every action resolves its targets inside the active company only
# =============================================================================================


def _foreign_world(db: Session) -> dict:
    """Company 2's rows that a crafted company-1 card will try to reach."""
    _ensure_company(db, 2)
    work_order = _work_order(db, "WO-FOREIGN", company_id=2, priority=6, status_=WorkOrderStatus.IN_PROGRESS)
    blocker = _blocker(db, work_order)
    vendor = _vendor(db, "VFOREIGN", company_id=2)
    part = _part(db, "PART-FOREIGN", company_id=2, primary_supplier_id=vendor.id, reorder_quantity=10)
    return {"work_order": work_order, "blocker": blocker, "vendor": vendor, "part": part}


# (case id, builder -> (action, target_type, target_id), expected blocked reason)
_FOREIGN_CASES = [
    (
        "priority_on_foreign_wo",
        lambda w, local: (
            {"type": "adjust_work_order_priority", "work_order_id": w["work_order"].id, "priority": 1},
            "work_order",
            w["work_order"].id,
        ),
        "Work order not found",
    ),
    (
        "escalate_foreign_blocker",
        lambda w, local: (
            {"type": "escalate_blocker", "blocker_id": w["blocker"].id},
            "work_order_blocker",
            w["blocker"].id,
        ),
        "Blocker not found",
    ),
    (
        "acknowledge_foreign_blocker",
        lambda w, local: (
            {"type": "acknowledge_blocker", "blocker_id": w["blocker"].id},
            "work_order_blocker",
            w["blocker"].id,
        ),
        "Blocker not found",
    ),
    (
        "po_for_foreign_part",
        lambda w, local: ({"type": "create_draft_po", "part_id": w["part"].id}, "part", w["part"].id),
        "Part not found",
    ),
    (
        "po_to_foreign_vendor",
        lambda w, local: ({"type": "create_draft_po", "part_id": local["po_part"].id}, "part", local["po_part"].id),
        "Vendor not found for this company",
    ),
    (
        "ncr_on_foreign_wo",
        lambda w, local: (
            {
                "type": "create_draft_ncr",
                "part_id": local["ncr_part"].id,
                "work_order_id": w["work_order"].id,
                "title": "Scrap trend",
            },
            "part",
            local["ncr_part"].id,
        ),
        "Work order not found",
    ),
]


class TestApplyIsTenantScoped:
    @pytest.mark.parametrize("case_id,build,expected", _FOREIGN_CASES, ids=[c[0] for c in _FOREIGN_CASES])
    def test_a_crafted_card_cannot_reach_another_company(
        self, client, db_session, admin_headers, case_id, build, expected
    ):
        world = _foreign_world(db_session)
        local = {
            # A company-1 part whose primary supplier id points at company 2's vendor.
            "po_part": _part(db_session, "PO-LOCAL", primary_supplier_id=world["vendor"].id, reorder_quantity=5),
            "ncr_part": _part(db_session, "NCR-LOCAL"),
        }
        action, target_type, target_id = build(world, local)
        rec = _rec(db_session, action, target_type=target_type, target_id=target_id)

        row = _listed(client, admin_headers, rec.id)
        apply_action = row["apply_action"]
        assert apply_action["blocked_kind"] == "target"
        assert apply_action["blocked_reason"] == expected
        # Nothing about company 2 leaks into what company 1 is shown.
        assert "FOREIGN" not in apply_action["summary"]
        assert row["target_href"] in (None, f"/parts/{target_id}", f"/work-orders/{target_id}")
        if target_type == "work_order_blocker":
            assert row["target_href"] is None  # company 2's blocker -> company 2's WO id stays hidden

        response = _apply(client, admin_headers, rec.id, apply_action["fingerprint"])
        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        assert response.json()["detail"] == expected

        db_session.expire_all()
        foreign_wo = db_session.query(WorkOrder).filter(WorkOrder.id == world["work_order"].id).one()
        foreign_blocker = db_session.query(WorkOrderBlocker).filter(WorkOrderBlocker.id == world["blocker"].id).one()
        assert foreign_wo.priority == 6
        assert (foreign_blocker.status, foreign_blocker.severity) == ("open", "medium")
        assert db_session.query(PurchaseOrder).count() == 0
        assert db_session.query(NonConformanceReport).count() == 0
        assert _audit_count(db_session, "work_order", "work_order_blocker", "purchase_order", "ncr") == 0
        assert _status_of(db_session, rec.id) == "pending"


class TestSweepsAndLinksAreTenantScoped:
    def test_closing_a_blocker_stales_only_this_companys_cards(self, client, db_session, supervisor_headers):
        _ensure_company(db_session, 2)
        work_order = _work_order(db_session, "WO-CLOSE-T")
        blocker = _blocker(db_session, work_order)
        mine = _rec(db_session, {"type": "escalate_blocker"}, target_type="work_order_blocker", target_id=blocker.id)
        # Junk in another tenant that happens to name the same blocker id.
        theirs = _rec(
            db_session,
            {"type": "escalate_blocker"},
            target_type="work_order_blocker",
            target_id=blocker.id,
            company_id=2,
        )

        response = client.post(
            f"/api/v1/work-order-blockers/{blocker.id}/resolve",
            json={"resolution_note": "Sheet arrived"},
            headers=supervisor_headers,
        )

        assert response.status_code == status.HTTP_200_OK, response.text
        assert _status_of(db_session, mine.id) == "stale"
        assert _status_of(db_session, theirs.id) == "pending"

    def test_closed_target_sweep_reads_only_this_companys_records(self, db_session):
        _ensure_company(db_session, 2)
        their_done = _work_order(db_session, "WO-THEIRS-DONE", company_id=2, status_=WorkOrderStatus.COMPLETE)
        theirs = _rec(db_session, {"type": "note"}, target_type="work_order", target_id=their_done.id, company_id=2)
        # A company-1 card naming company 2's finished job: to company 1 that job does not exist.
        crafted = _rec(db_session, {"type": "note"}, target_type="work_order", target_id=their_done.id)

        summary = AILearningService(db_session).aggregate_learning_signals(company_ids=[1])
        db_session.commit()

        assert summary["closed_target_recommendations_staled"] == 1
        assert _status_of(db_session, theirs.id) == "pending"
        crafted_row = db_session.query(AIRecommendation).filter(AIRecommendation.id == crafted.id).one()
        assert crafted_row.status == "stale"
        assert crafted_row.status_reason == "Target closed: work order no longer exists"
        assert "WO-THEIRS-DONE" not in crafted_row.status_reason

    def test_apply_supersedes_only_same_company_same_type_open_siblings(self, client, db_session, supervisor_headers):
        _ensure_company(db_session, 2)
        work_order = _work_order(db_session, "WO-SIB-T", priority=6, status_=WorkOrderStatus.IN_PROGRESS)
        blocker = _blocker(db_session, work_order)

        def card(action_type: str, **kwargs) -> AIRecommendation:
            return _rec(
                db_session,
                {"type": action_type, "blocker_id": blocker.id},
                target_type="work_order_blocker",
                target_id=blocker.id,
                **kwargs,
            )

        applied = card("escalate_blocker")
        same_type = card("escalate_blocker", status_="snoozed")
        other_type = card("acknowledge_blocker")
        decided = card("escalate_blocker", status_="dismissed")
        other_company = card("escalate_blocker", company_id=2)

        fingerprint = _listed(client, supervisor_headers, applied.id)["apply_action"]["fingerprint"]
        response = _apply(client, supervisor_headers, applied.id, fingerprint)

        assert response.status_code == status.HTTP_200_OK, response.text
        assert _status_of(db_session, same_type.id) == "stale"
        assert _status_of(db_session, other_type.id) == "pending"
        assert _status_of(db_session, decided.id) == "dismissed"
        assert _status_of(db_session, other_company.id) == "pending"


# =============================================================================================
# Provenance on EVERY row, acknowledge_blocker through the door
# =============================================================================================


class TestEveryAuditRowCarriesProvenance:
    def test_escalating_an_open_blocker_stamps_all_three_rows(
        self, client, db_session, supervisor_user: User, supervisor_headers
    ):
        work_order = _work_order(db_session, "WO-PROV", priority=6, status_=WorkOrderStatus.IN_PROGRESS)
        blocker = _blocker(db_session, work_order, severity="low")
        rec = _rec(
            db_session,
            {"type": "escalate_blocker", "blocker_id": blocker.id, "work_order_id": work_order.id},
            target_type="work_order_blocker",
            target_id=blocker.id,
        )
        fingerprint = _listed(client, supervisor_headers, rec.id)["apply_action"]["fingerprint"]

        response = _apply(client, supervisor_headers, rec.id, fingerprint)
        assert response.status_code == status.HTTP_200_OK, response.text

        rows = db_session.query(AuditLog).filter(AuditLog.resource_type.in_(["work_order_blocker", "work_order"])).all()
        kinds = sorted((row.resource_type, row.action) for row in rows)
        assert kinds == [
            ("work_order", "UPDATE"),
            ("work_order_blocker", "STATUS_CHANGE"),
            ("work_order_blocker", "UPDATE"),
        ]
        for row in rows:
            extra = row.extra_data or {}
            assert extra.get("source") == "ai_recommendation", (row.resource_type, row.action)
            assert extra.get("recommendation_id") == rec.id
            assert extra.get("approved_by") == supervisor_user.id
            assert extra.get("plan_fingerprint") == fingerprint
            assert extra.get("action_type") == "escalate_blocker"
            assert row.user_id == supervisor_user.id

    def test_acknowledge_blocker_through_the_door(self, client, db_session, supervisor_user: User, supervisor_headers):
        work_order = _work_order(db_session, "WO-ACK", priority=6, status_=WorkOrderStatus.IN_PROGRESS)
        blocker = _blocker(db_session, work_order)
        rec = _rec(
            db_session,
            {"type": "acknowledge_blocker", "blocker_id": blocker.id},
            target_type="work_order_blocker",
            target_id=blocker.id,
        )
        action = _listed(client, supervisor_headers, rec.id)["apply_action"]
        assert (
            action["summary"] == "Acknowledge blocker 'No sheet'. This does not resolve the blocker or lift its hold."
        )

        response = _apply(client, supervisor_headers, rec.id, action["fingerprint"])

        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.json()["apply_result"]["status"] == "acknowledged"
        db_session.refresh(blocker)
        db_session.refresh(work_order)
        assert blocker.status == "acknowledged" and blocker.severity == "medium"
        assert work_order.priority == 6  # acknowledge never expedites
        (row,) = db_session.query(AuditLog).filter(AuditLog.resource_type == "work_order_blocker").all()
        assert row.action == "STATUS_CHANGE"
        assert row.extra_data["source"] == "ai_recommendation"
        assert row.extra_data["recommendation_id"] == rec.id
        assert row.extra_data["approved_by"] == supervisor_user.id

        # A second card for the same, now acknowledged, blocker has nothing left to do.
        again = _rec(
            db_session,
            {"type": "acknowledge_blocker", "blocker_id": blocker.id},
            target_type="work_order_blocker",
            target_id=blocker.id,
            rtype="second",
        )
        action = _listed(client, supervisor_headers, again.id)["apply_action"]
        assert action["blocked_kind"] == "target"
        assert action["blocked_reason"] == "Blocker 'No sheet' is already acknowledged"
        refused = _apply(client, supervisor_headers, again.id, action["fingerprint"])
        assert refused.status_code == status.HTTP_409_CONFLICT
        assert db_session.query(AuditLog).filter(AuditLog.resource_type == "work_order_blocker").count() == 1


# =============================================================================================
# Soft-deleted targets, broadcasts, and what the fingerprint does NOT hash
# =============================================================================================


class TestSoftDeletedTargets:
    def test_escalate_never_expedites_a_deleted_work_order(self, client, db_session, admin_user, supervisor_headers):
        work_order = _work_order(db_session, "WO-ESC-GONE", priority=6, status_=WorkOrderStatus.IN_PROGRESS)
        blocker = _blocker(db_session, work_order)
        rec = _rec(
            db_session,
            {"type": "escalate_blocker", "blocker_id": blocker.id, "work_order_id": work_order.id},
            target_type="work_order_blocker",
            target_id=blocker.id,
        )
        work_order.soft_delete(admin_user.id)
        db_session.commit()
        db_session.refresh(work_order)
        version_before = work_order.version

        action = _listed(client, supervisor_headers, rec.id)["apply_action"]
        assert "expedite" not in action["summary"]

        response = _apply(client, supervisor_headers, rec.id, action["fingerprint"])
        assert response.status_code == status.HTTP_200_OK, response.text
        db_session.refresh(work_order)
        assert work_order.priority == 6 and work_order.version == version_before
        assert db_session.query(AuditLog).filter(AuditLog.resource_type == "work_order").count() == 0

    def test_draft_ncr_refuses_a_deleted_work_order(self, client, db_session, admin_user, supervisor_headers):
        part = _part(db_session, "NCR-WO-GONE")
        work_order = _work_order(db_session, "WO-NCR-GONE")
        rec = _rec(
            db_session,
            {"type": "create_draft_ncr", "part_id": part.id, "work_order_id": work_order.id, "title": "Scrap trend"},
            target_type="part",
            target_id=part.id,
        )
        work_order.soft_delete(admin_user.id)
        db_session.commit()

        action = _listed(client, supervisor_headers, rec.id)["apply_action"]
        assert action["blocked_reason"] == "Work order WO-NCR-GONE has been deleted"
        response = _apply(client, supervisor_headers, rec.id, action["fingerprint"])
        assert response.status_code == status.HTTP_409_CONFLICT
        assert db_session.query(NonConformanceReport).count() == 0
        assert _event_count(db_session, "ncr_created") == 0


class TestBroadcastAndFingerprintScope:
    def test_a_refused_apply_broadcasts_nothing(self, client, db_session, admin_headers, monkeypatch):
        calls = []
        monkeypatch.setattr(ai_learning_endpoint, "safe_broadcast", lambda *args, **kwargs: calls.append(args))
        work_order = _work_order(db_session, "WO-NO-BCAST")
        rec = _rec(
            db_session,
            {"type": "adjust_work_order_priority", "work_order_id": work_order.id, "priority": 1},
            target_type="work_order",
            target_id=work_order.id,
        )
        fingerprint = _listed(client, admin_headers, rec.id)["apply_action"]["fingerprint"]
        work_order.priority = 3
        db_session.commit()

        response = _apply(client, admin_headers, rec.id, fingerprint)

        assert response.status_code == status.HTTP_409_CONFLICT
        assert calls == []

    def test_an_unrelated_edit_does_not_change_the_fingerprint(self, client, db_session, admin_headers):
        work_order = _work_order(db_session, "WO-UNRELATED")
        rec = _rec(
            db_session,
            {"type": "adjust_work_order_priority", "work_order_id": work_order.id, "priority": 1},
            target_type="work_order",
            target_id=work_order.id,
        )
        fingerprint = _listed(client, admin_headers, rec.id)["apply_action"]["fingerprint"]
        version_before = work_order.version
        # Someone edits the job's notes: its version moves, but nothing the card shows does.
        work_order.notes = "Customer called"
        db_session.commit()
        db_session.refresh(work_order)
        assert work_order.version != version_before

        assert _listed(client, admin_headers, rec.id)["apply_action"]["fingerprint"] == fingerprint
        response = _apply(client, admin_headers, rec.id, fingerprint)
        assert response.status_code == status.HTTP_200_OK, response.text
        db_session.refresh(work_order)
        assert work_order.priority == 1
