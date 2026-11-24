"""The Approved Supplier List's one hard gate on the PO doors ``test_vendor_asl_approval.py``
does not reach: the AI Accept & apply draft, the MRP single supply draft and purchase batch,
and the MRP auto-PO vendor picker -- plus the auto-evidence supplier count a registrar reads.

``test_vendor_asl_approval`` proves the shared ``create_purchase_order_command`` refuses a
Disqualified vendor. That does not prove a door ROUTES through it: the MRP doors build their
purchase orders directly and carry their own copy of the gate, and the AI applier wraps the
command's 409 into its own error. Each test here drives the real door end to end and reads
the stored rows, so deleting a door's call (or re-routing the applier around the command)
fails it. Every refusal is paired with a positive control on the same shape, so the test
cannot pass merely because the door refuses everything.
"""

from datetime import date, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models.ai_learning import AIRecommendation
from app.models.mrp import MRPAction, MRPSupplyLink
from app.models.purchasing import POStatus, PurchaseOrder, PurchaseOrderLine, Vendor
from app.models.supplier_part import SupplierPartMapping
from app.models.supplier_scorecard import SupplierScorecard
from app.models.user import UserRole
from app.services.ai_action_applier import AIActionApplier, AIActionApplyError
from app.services.auto_evidence_service import _query_suppliers
from app.services.mrp_auto_service import MRPAutoService
from app.services.mrp_service import MRPService
from tests.api.test_receiving_compliance import _next, make_user
from tests.api.test_vendor_asl_approval import DISQUALIFIED_TEXT, make_part, make_vendor
from tests.services.test_mrp_service import _seed_make_wo_with_purchased_component

pytestmark = [pytest.mark.api, pytest.mark.requires_db]


def po_count(db: Session) -> int:
    db.rollback()
    return db.query(PurchaseOrder).count()


# =========================================================================== AI Accept & apply


def _draft_po_recommendation(db: Session, part_id: int) -> AIRecommendation:
    rec = AIRecommendation(
        company_id=1,
        source_module="inventory",
        recommendation_type="reorder",
        title="Reorder below point",
        summary="Below reorder point",
        target_entity_type="part",
        target_entity_id=part_id,
        suggested_action={"type": "create_draft_po", "part_id": part_id, "autonomy": "apply_on_accept"},
        confidence_score=0.9,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


def test_ai_draft_po_to_a_disqualified_primary_supplier_is_refused_with_the_asl_text(db_session):
    """The applier reaches the vendor through the part's CURRENT primary supplier, so a
    supplier Disqualified after the recommendation was raised is still the one it names."""
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    disqualified = make_vendor(db_session, approval_status="disqualified")
    part = make_part(db_session, part_type="raw_material", primary_supplier_id=disqualified.id, reorder_quantity=10)
    rec = _draft_po_recommendation(db_session, part.id)

    with pytest.raises(AIActionApplyError) as refused:
        AIActionApplier(db_session, company_id=1, user=admin).apply(rec)
    assert str(refused.value) == DISQUALIFIED_TEXT.format(disqualified.name)
    assert po_count(db_session) == 0

    # Positive control: the same shape against a Conditional vendor drafts (a warning, not a gate).
    conditional = make_vendor(db_session, approval_status="conditional")
    other = make_part(db_session, part_type="raw_material", primary_supplier_id=conditional.id, reorder_quantity=10)
    result = AIActionApplier(db_session, company_id=1, user=admin).apply(_draft_po_recommendation(db_session, other.id))
    db_session.commit()
    assert db_session.get(PurchaseOrder, result["purchase_order_id"]).vendor_id == conditional.id


# =========================================================================== MRP single supply draft


@pytest.fixture
def shortage(db_session, admin_user):
    """A released work order short one purchased component, its primary supplier set."""
    part = _seed_make_wo_with_purchased_component(db_session, 1, f"ASL{_next()}")
    vendor = make_vendor(db_session, approval_status="approved", is_approved=True)
    part.primary_supplier_id = vendor.id
    part.standard_cost = 4.0
    db_session.commit()
    run = MRPService(db_session, 1).run_mrp(admin_user.id, include_safety_stock=False)
    action = db_session.query(MRPAction).filter_by(mrp_run_id=run.id, part_id=part.id).one()
    return part, vendor, action


def _review(client, headers, action):
    response = client.get(f"/api/v1/mrp/actions/{action.id}/supply-review", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _draft_payload(review, vendor_id, key="asl-supply-draft"):
    return dict(
        request_key=key,
        review_token=review["review_token"],
        quantity=review["quantity"],
        due_date=review["due_date"],
        vendor_id=vendor_id,
        unit_price=review["unit_price"],
        work_center_id=review["work_center_id"],
        notes="Planner reviewed",
    )


def test_mrp_supply_review_never_offers_a_disqualified_supplier(client, admin_headers, db_session, shortage):
    _, vendor, action = shortage
    review = _review(client, admin_headers, action)
    assert review["vendor_id"] == vendor.id and vendor.id in {v["id"] for v in review["vendors"]}

    vendor.approval_status = "disqualified"
    vendor.is_approved = False
    db_session.commit()
    review = _review(client, admin_headers, action)
    assert review["vendor_id"] is None  # the primary supplier is no longer the default
    assert vendor.id not in {v["id"] for v in review["vendors"]}


def test_mrp_supply_draft_to_a_disqualified_supplier_is_409_and_writes_nothing(
    client, admin_headers, db_session, shortage
):
    _, vendor, action = shortage
    review = _review(client, admin_headers, action)
    # Disqualified AFTER the planner opened the review: the review token does not cover the
    # vendor, so it is the gate in the draft verb itself that has to refuse.
    vendor.approval_status = "disqualified"
    vendor.is_approved = False
    db_session.commit()
    endpoint = f"/api/v1/mrp/actions/{action.id}/supply-draft"
    refused = client.post(endpoint, headers=admin_headers, json=_draft_payload(review, vendor.id))
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == DISQUALIFIED_TEXT.format(vendor.name)
    assert po_count(db_session) == 0 and db_session.query(MRPSupplyLink).count() == 0

    # Positive control: the same review drafts to an Approved supplier.
    approved = make_vendor(db_session, approval_status="approved", is_approved=True)
    created = client.post(
        endpoint, headers=admin_headers, json=_draft_payload(review, approved.id, key="asl-approved-draft")
    )
    assert created.status_code == 200, created.text
    assert po_count(db_session) == 1


# =========================================================================== MRP purchase batch


def test_mrp_purchase_batch_refuses_a_disqualified_supplier_before_any_draft(client, admin_headers, db_session):
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    good = make_vendor(db_session, approval_status="conditional")  # a warning, never a gate
    bad = make_vendor(db_session, approval_status="approved", is_approved=True)
    parts = [_seed_make_wo_with_purchased_component(db_session, 1, f"ASLB{_next()}") for _ in range(2)]
    for part, vendor in zip(parts, (good, bad)):
        part.primary_supplier_id = vendor.id
        part.standard_cost = 3.0
    db_session.commit()
    run = MRPService(db_session, 1).run_mrp(admin.id, include_safety_stock=False)
    actions = db_session.query(MRPAction).filter_by(mrp_run_id=run.id).order_by(MRPAction.id).all()
    review = client.get(
        "/api/v1/mrp/purchase-batch/review",
        headers=admin_headers,
        params=[("action_ids", action.id) for action in actions],
    )
    assert review.status_code == 200, review.text
    request = {
        "request_key": "asl-batch",
        "lines": [
            {
                "action_id": row["action_id"],
                "review_token": row["review_token"],
                "quantity": row["quantity"],
                "due_date": row["due_date"],
                "vendor_id": row["vendor_id"],
                "unit_price": row["unit_price"],
                "notes": "Reviewed",
            }
            for row in review.json()["lines"]
        ],
    }
    assert {line["vendor_id"] for line in request["lines"]} == {good.id, bad.id}

    bad.approval_status = "disqualified"
    bad.is_approved = False
    db_session.commit()
    refused = client.post("/api/v1/mrp/purchase-batch/drafts", headers=admin_headers, json=request)
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == DISQUALIFIED_TEXT.format(bad.name)
    # All-or-nothing: the good vendor's group was not drafted either.
    assert po_count(db_session) == 0 and db_session.query(MRPSupplyLink).count() == 0

    bad.approval_status = "approved"
    bad.is_approved = True
    db_session.commit()
    created = client.post("/api/v1/mrp/purchase-batch/drafts", headers=admin_headers, json=request)
    assert created.status_code == 200, created.text
    assert po_count(db_session) == 2
    by_vendor = {po.vendor_id: po.id for po in db_session.query(PurchaseOrder).all()}
    assert _warning_codes(_po_create_extra(db_session, by_vendor[good.id])) == ["vendor_conditional"]
    assert "supplier_approval_warnings" not in _po_create_extra(db_session, by_vendor[bad.id])


# =========================================================================== MRP auto-PO vendor picker


def _po_history(db: Session, vendor: Vendor, part) -> None:
    po = PurchaseOrder(
        company_id=1,
        po_number=f"PO-ASLH-{_next():05d}",
        vendor_id=vendor.id,
        status=POStatus.CLOSED,
        order_date=date.today() - timedelta(days=30),
    )
    db.add(po)
    db.flush()
    db.add(
        PurchaseOrderLine(
            company_id=1,
            purchase_order_id=po.id,
            line_number=1,
            part_id=part.id,
            quantity_ordered=5,
            unit_price=1,
            line_total=5,
        )
    )
    db.commit()


def _map(db: Session, part, vendor: Vendor) -> None:
    db.add(
        SupplierPartMapping(
            company_id=1,
            part_id=part.id,
            vendor_id=vendor.id,
            supplier_part_number=f"SPN-{_next():05d}",
            is_active=True,
        )
    )
    db.commit()


def test_mrp_auto_picker_skips_a_disqualified_vendor_at_every_priority(db_session):
    """An AUTO_DRAFT pass picks a supplier with nobody looking: by supplier-part mapping, then by
    PO history. Neither may land on a Disqualified vendor; a Conditional one stays pickable (a
    warning on the PO, not a gate). There is no third priority: a part with no sourcing gets
    no supplier, however many active vendors exist."""
    service = MRPAutoService(db_session, company_id=1)

    # No sourcing -- no supplier, not the first active vendor (the removed fallback).
    disqualified = make_vendor(db_session, approval_status="disqualified")
    fresh_part = make_part(db_session)
    assert service._get_preferred_vendor(fresh_part.id) is None
    conditional = make_vendor(db_session, approval_status="conditional")
    assert service._get_preferred_vendor(fresh_part.id) is None
    _map(db_session, fresh_part, conditional)
    assert service._get_preferred_vendor(fresh_part.id).id == conditional.id

    # Priority 1 -- a mapping to the disqualified vendor is passed over for another MAPPED one.
    mapped = make_part(db_session)
    _map(db_session, mapped, disqualified)
    mapped_alternate = make_vendor(db_session, approval_status="approved", is_approved=True)
    _map(db_session, mapped, mapped_alternate)
    assert service._get_preferred_vendor(mapped.id).id == mapped_alternate.id

    # Priority 2 -- years of PO history with the disqualified vendor is passed over too.
    bought = make_part(db_session)
    _po_history(db_session, disqualified, bought)
    _po_history(db_session, disqualified, bought)
    history_vendor = make_vendor(db_session, approval_status="approved", is_approved=True)
    _po_history(db_session, history_vendor, bought)
    assert service._get_preferred_vendor(bought.id).id == history_vendor.id


@pytest.mark.parametrize("sourcing", ["mapping", "history"])
def test_mrp_auto_draft_never_reroutes_a_part_whose_only_supplier_is_disqualified(db_session, sourcing):
    """A part whose only supplier on record was Disqualified must not be drafted to some other
    active vendor (a calibration lab, say) as a normal-looking PO. The action records which
    supplier the ASL stopped instead, and no purchase order is written; a buyer picks the
    supplier."""
    from app.models.mrp import MRPRun, PlanningAction
    from app.services.mrp_auto_service import DisqualifiedSourcingError, MRPAutoMode

    make_vendor(db_session, name="Acme Calibration Lab", approval_status="approved", is_approved=True)
    disqualified = make_vendor(db_session, name="Former Plater", approval_status="disqualified")
    part = make_part(db_session)
    if sourcing == "mapping":
        _map(db_session, part, disqualified)
    else:
        _po_history(db_session, disqualified, part)
    before = po_count(db_session)

    service = MRPAutoService(db_session, company_id=1)
    with pytest.raises(DisqualifiedSourcingError):
        service._get_preferred_vendor(part.id)

    run = MRPRun(run_number=f"MRP-ASL-{_next():05d}", company_id=1)
    db_session.add(run)
    db_session.commit()
    action = MRPAction(
        mrp_run_id=run.id,
        part_id=part.id,
        action_type=PlanningAction.ORDER,
        priority=3,
        quantity=4,
        required_date=date.today() + timedelta(days=14),
        suggested_order_date=date.today(),
        company_id=1,
    )
    db_session.add(action)
    db_session.commit()

    results = service.process_actions([action], MRPAutoMode.AUTO_DRAFT)
    assert results["pos_created"] == 0 and results["errors"] == 1
    db_session.refresh(action)
    assert action.result_po_id is None and not action.processed
    assert action.error_message.startswith("Former Plater is Disqualified on the Approved Supplier List")
    assert "MRP supply review" in action.error_message
    assert po_count(db_session) == before


# =========================================================================== auto evidence


def test_auto_evidence_counts_approved_on_the_asl_not_merely_active(db_session):
    for status_ in ("approved", "approved_customer_directed", "conditional", "disqualified", None):
        make_vendor(db_session, approval_status=status_)
    make_vendor(db_session, approval_status="approved", is_active=False)  # switched off
    gone = make_vendor(db_session, approval_status="approved")
    gone.soft_delete(make_user(db_session, role=UserRole.ADMIN, company_id=1).id)
    db_session.commit()
    make_vendor(db_session, company_id=2, approval_status="approved")

    warning = _query_suppliers(db_session, 1)
    assert warning["description"] == "6 suppliers (5 active), 2 approved on the ASL, 0 performance scorecards"
    assert warning["health_detail"] == "5 active suppliers but no performance scorecards"

    vendor = db_session.query(Vendor).filter(Vendor.company_id == 1).first()
    db_session.add(
        SupplierScorecard(
            company_id=1,
            vendor_id=vendor.id,
            period_type="quarterly",
            period_start=date(2026, 7, 1),
            period_end=date(2026, 9, 30),
            overall_score=90.0,
        )
    )
    db_session.commit()
    healthy = _query_suppliers(db_session, 1)
    assert healthy["health_detail"] == "2 suppliers approved on the ASL, 1 performance evaluations"


# =========================================================================== warnings on the create row


def _po_create_extra(db: Session, po_id: int) -> dict:
    from app.models.audit_log import AuditLog

    db.rollback()
    row = (
        db.query(AuditLog)
        .filter(
            AuditLog.resource_type == "purchase_order",
            AuditLog.resource_id == po_id,
            AuditLog.action.in_(["CREATE", "CREATE_PO"]),
        )
        .one()
    )
    return row.extra_data or {}


def _warning_codes(extra: dict) -> list:
    return [warning["code"] for warning in extra.get("supplier_approval_warnings", [])]


def test_the_mrp_doors_record_the_asl_warnings_on_the_create_row(client, admin_headers, db_session, shortage):
    """Every create through ``create_purchase_order_command`` stamps the advisory warnings on its
    create audit row; the doors that build their POs directly (MRP review draft, MRP auto) did
    not, so a draft to a Conditional supplier carried no evidence the warning existed."""
    part, vendor, action = shortage
    vendor.approval_status, vendor.is_approved = "conditional", False
    db_session.commit()
    review = _review(client, admin_headers, action)
    created = client.post(
        f"/api/v1/mrp/actions/{action.id}/supply-draft",
        headers=admin_headers,
        json=_draft_payload(review, vendor.id, key="asl-warn-draft"),
    )
    assert created.status_code == 200, created.text
    [po] = db_session.query(PurchaseOrder).all()
    assert _warning_codes(_po_create_extra(db_session, po.id)) == ["vendor_conditional"]

    # The auto-PO path (the 6 AM AUTO_DRAFT job) -- an approved supplier records no warnings key.
    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    auto_po = MRPAutoService(db_session, company_id=1)._create_po_from_action(action, "auto_draft", admin.id)
    db_session.commit()
    assert _warning_codes(_po_create_extra(db_session, auto_po.id)) == ["vendor_conditional"]


def test_the_open_po_loader_records_the_asl_warnings_on_the_create_row(client, db_session):
    import io

    from tests.api.test_receiving_compliance import headers_for

    admin = make_user(db_session, role=UserRole.ADMIN, company_id=1)
    vendor = make_vendor(db_session)  # not on the ASL
    part = make_part(db_session)
    csv = f"vendor_code,part_number,quantity,unit_price\n{vendor.code},{part.part_number},3,1.50\n".encode()
    response = client.post(
        "/api/v1/purchasing/purchase-orders/import",
        headers=headers_for(admin),
        files={"file": ("open-pos.csv", io.BytesIO(csv), "text/csv")},
    )
    assert response.status_code == 200, response.text
    [po] = db_session.query(PurchaseOrder).all()
    assert _warning_codes(_po_create_extra(db_session, po.id)) == ["vendor_not_on_asl"]
