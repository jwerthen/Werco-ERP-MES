"""Reviewed supply drafts: real validation, source association, replay and stale state."""

from datetime import date, timedelta

import pytest

from app.models.company import Company
from app.models.inventory import InventoryItem
from app.models.mrp import MRPAction, MRPSupplyLink
from app.models.purchasing import POStatus, PurchaseOrder, Vendor
from app.models.routing import Routing, RoutingOperation
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.services.mrp_service import MRPService
from tests.services.test_mrp_service import _seed_make_wo_with_purchased_component

pytestmark = pytest.mark.requires_db


@pytest.fixture
def shortage(db_session, admin_user):
    part = _seed_make_wo_with_purchased_component(db_session, 1, "DRAFT")
    vendor = Vendor(code="SUPPLY", name="Supply Vendor", company_id=1, is_active=True)
    db_session.add(vendor)
    db_session.flush()
    part.primary_supplier_id = vendor.id
    part.standard_cost = 12.5
    db_session.commit()
    run = MRPService(db_session, 1).run_mrp(admin_user.id, include_safety_stock=False)
    action = db_session.query(MRPAction).filter_by(mrp_run_id=run.id, part_id=part.id).one()
    return part, vendor, run, action


def review_and_payload(client, headers, action, **overrides):
    response = client.get(f"/api/v1/mrp/actions/{action.id}/supply-review", headers=headers)
    assert response.status_code == 200, response.text
    review = response.json()
    payload = dict(
        request_key="review-request-001",
        review_token=review["review_token"],
        quantity=review["quantity"],
        due_date=review["due_date"],
        vendor_id=review["vendor_id"],
        unit_price=review["unit_price"],
        work_center_id=review["work_center_id"],
        notes="Planner reviewed",
    )
    payload.update(overrides)
    return review, payload


def test_purchase_draft_replay_and_source_are_atomic(client, admin_headers, db_session, shortage):
    part, vendor, run, action = shortage
    review, payload = review_and_payload(client, admin_headers, action)
    assert review["part_number"] == part.part_number
    assert review["quantity"] == 10 and review["vendor_id"] == vendor.id
    endpoint = f"/api/v1/mrp/actions/{action.id}/supply-draft"
    response = client.post(endpoint, headers=admin_headers, json=payload)
    assert response.status_code == 200, response.text
    result = response.json()
    po = db_session.get(PurchaseOrder, result["id"])
    assert po.status == POStatus.DRAFT and po.total == 125
    assert po.lines[0].part_id == part.id and po.lines[0].quantity_ordered == 10
    assert f"action {action.id}" in po.notes
    assert not db_session.get(MRPAction, action.id).processed
    assert db_session.query(MRPSupplyLink).count() == 1
    assert client.post(endpoint, headers=admin_headers, json=payload).json()["replayed"] is True
    assert db_session.query(PurchaseOrder).count() == 1
    payload["quantity"] = 9
    conflict = client.post(endpoint, headers=admin_headers, json=payload)
    assert conflict.status_code == 409 and conflict.json()["detail"]["existing_draft"]["id"] == po.id
    actions = client.get(f"/api/v1/mrp/runs/{run.id}/actions", headers=admin_headers)
    assert actions.status_code == 200, actions.text
    assert actions.json()[0]["supply_draft"]["number"] == po.po_number
    assert client.get("/api/v1/mrp/shortages", headers=admin_headers).status_code == 200
    assert client.delete(f"/api/v1/mrp/runs/{run.id}", headers=admin_headers).status_code == 409
    rerun = MRPService(db_session, 1).run_mrp(None, include_safety_stock=False)
    assert not db_session.query(MRPAction).filter_by(mrp_run_id=rerun.id, part_id=part.id).all()
    # Cancelling the draft restores unmet demand on the next run.
    po.status = POStatus.CANCELLED
    db_session.commit()
    fresh = MRPService(db_session, 1).run_mrp(None, include_safety_stock=False)
    assert db_session.query(MRPAction).filter_by(mrp_run_id=fresh.id, part_id=part.id).one().quantity == 10


@pytest.mark.parametrize("change", ["inventory", "new_run", "quantity", "past_date", "foreign_vendor"])
def test_stale_or_invalid_review_creates_nothing(client, admin_headers, db_session, shortage, change):
    part, _, _, action = shortage
    _, payload = review_and_payload(client, admin_headers, action)
    if change == "inventory":
        db_session.add(
            InventoryItem(
                company_id=1,
                part_id=part.id,
                quantity_on_hand=2,
                quantity_allocated=0,
                location="MAIN",
                status="available",
                is_active=True,
            )
        )
        db_session.commit()
    elif change == "new_run":
        MRPService(db_session, 1).run_mrp(None, include_safety_stock=False)
    elif change == "quantity":
        payload["quantity"] = 11
    elif change == "past_date":
        payload["due_date"] = str(date.today() - timedelta(days=1))
    else:
        db_session.add(Company(id=2, name="Other", slug="other"))
        vendor = Vendor(code="FOREIGN", name="Other Supplier", company_id=2)
        db_session.add(vendor)
        db_session.commit()
        payload["vendor_id"] = vendor.id
    response = client.post(f"/api/v1/mrp/actions/{action.id}/supply-draft", headers=admin_headers, json=payload)
    assert response.status_code in (409, 422), response.text
    assert db_session.query(PurchaseOrder).count() == db_session.query(MRPSupplyLink).count() == 0


def test_mark_reviewed_is_separate_and_routes_use_real_model(client, admin_headers, db_session, shortage):
    _, _, run, action = shortage
    assert client.get(f"/api/v1/mrp/runs/{run.id}", headers=admin_headers).status_code == 200
    response = client.post(f"/api/v1/mrp/actions/{action.id}/process", headers=admin_headers)
    assert response.status_code == 200, response.text
    assert "No supply document" in response.json()["message"]
    assert db_session.query(PurchaseOrder).count() == 0
    db_session.expire_all()
    assert db_session.get(MRPAction, action.id).processed
    _, payload = review_and_payload(client, admin_headers, action)
    assert (
        client.post(f"/api/v1/mrp/actions/{action.id}/supply-draft", headers=admin_headers, json=payload).status_code
        == 200
    )


def test_operator_cannot_create_and_foreign_recommendation_is_not_visible(
    client, operator_headers, admin_headers, db_session, shortage
):
    _, _, _, action = shortage
    assert client.get(f"/api/v1/mrp/actions/{action.id}/supply-review", headers=operator_headers).status_code == 403
    _, payload = review_and_payload(client, admin_headers, action)
    assert (
        client.post(f"/api/v1/mrp/actions/{action.id}/supply-draft", headers=operator_headers, json=payload).status_code
        == 403
    )
    db_session.add(Company(id=2, name="Other", slug="other"))
    db_session.commit()
    action.company_id = 2
    db_session.commit()
    assert client.get(f"/api/v1/mrp/actions/{action.id}/supply-review", headers=admin_headers).status_code == 404


def test_manufacture_copies_released_routing_and_does_not_release(
    client, admin_headers, admin_user, db_session, shortage, test_work_center
):
    part, _, _, action = shortage
    part.part_type = "manufactured"
    route = Routing(company_id=1, part_id=part.id, revision="A", status="released", is_active=True)
    db_session.add(route)
    db_session.flush()
    db_session.add(
        RoutingOperation(
            company_id=1,
            routing_id=route.id,
            sequence=10,
            name="Machine supply",
            work_center_id=test_work_center.id,
            setup_hours=1,
            run_hours_per_unit=0.5,
            is_active=True,
        )
    )
    db_session.commit()
    review, payload = review_and_payload(client, admin_headers, action)
    assert review["kind"] == "work_order" and review["routing"][0]["name"] == "Machine supply"
    response = client.post(f"/api/v1/mrp/actions/{action.id}/supply-draft", headers=admin_headers, json=payload)
    assert response.status_code == 200, response.text
    wo = db_session.get(WorkOrder, response.json()["id"])
    assert wo.status == WorkOrderStatus.DRAFT and wo.quantity_ordered == 10
    assert wo.operations[0].work_center_id == test_work_center.id
    assert wo.operations[0].run_time_hours == 5
    assert not db_session.get(MRPAction, action.id).processed


def test_creation_rolls_back_document_if_source_audit_fails(client, admin_headers, db_session, shortage, monkeypatch):
    from app.services.audit_service import AuditService

    original = AuditService.log_create

    def fail_link(self, entity_type, *args, **kwargs):
        if entity_type == "mrp_supply_link":
            raise RuntimeError("simulated transaction failure")
        return original(self, entity_type, *args, **kwargs)

    monkeypatch.setattr(AuditService, "log_create", fail_link)
    _, _, _, action = shortage
    _, payload = review_and_payload(client, admin_headers, action)
    with pytest.raises(RuntimeError, match="simulated transaction failure"):
        client.post(f"/api/v1/mrp/actions/{action.id}/supply-draft", headers=admin_headers, json=payload)
    assert db_session.query(PurchaseOrder).count() == 0
    assert db_session.query(MRPSupplyLink).count() == 0


def test_partial_draft_nets_remaining_demand_and_auto_job_skips_linked_action(
    client, admin_headers, db_session, shortage
):
    from app.services.mrp_auto_service import MRPAutoMode, MRPAutoService

    part, _, _, action = shortage
    _, payload = review_and_payload(client, admin_headers, action, quantity=4)
    response = client.post(f"/api/v1/mrp/actions/{action.id}/supply-draft", headers=admin_headers, json=payload)
    assert response.status_code == 200, response.text
    automatic = MRPAutoService(db_session, 1).process_actions([action], MRPAutoMode.AUTO_DRAFT)
    assert automatic['pos_created'] == 0 and db_session.query(PurchaseOrder).count() == 1
    run = MRPService(db_session, 1).run_mrp(None, include_safety_stock=False)
    assert db_session.query(MRPAction).filter_by(mrp_run_id=run.id, part_id=part.id).one().quantity == 6


def test_changed_routing_is_stale_and_manual_center_is_required(
    client, admin_headers, db_session, shortage, test_work_center
):
    part, _, _, action = shortage
    part.part_type = 'manufactured'
    db_session.commit()
    _, payload = review_and_payload(client, admin_headers, action)
    endpoint = f"/api/v1/mrp/actions/{action.id}/supply-draft"
    assert client.post(endpoint, headers=admin_headers, json=payload).status_code == 422
    route = Routing(company_id=1, part_id=part.id, revision='A', status='released', is_active=True)
    db_session.add(route)
    db_session.flush()
    op = RoutingOperation(
        company_id=1,
        routing_id=route.id,
        sequence=10,
        name='Machine supply',
        work_center_id=test_work_center.id,
        setup_hours=1,
        run_hours_per_unit=0.5,
        is_active=True,
    )
    db_session.add(op)
    db_session.commit()
    _, payload = review_and_payload(client, admin_headers, action)
    op.run_hours_per_unit = 2
    db_session.commit()
    response = client.post(endpoint, headers=admin_headers, json=payload)
    assert response.status_code == 409 and response.json()['detail']['code'] == 'MRP_REVIEW_STALE'
    assert db_session.query(MRPSupplyLink).count() == 0


def test_purchase_netting_excludes_closed_lines_and_foreign_supply(db_session, shortage):
    part, vendor, _, _ = shortage
    from app.models.purchasing import PurchaseOrderLine

    db_session.add(Company(id=2, name='Other', slug='other'))
    db_session.flush()
    for idx, (company_id, closed, ordered, received) in enumerate(
        [(1, False, 8, 3), (1, True, 100, 0), (2, False, 1000, 0)], 1
    ):
        po = PurchaseOrder(
            company_id=company_id, po_number=f'SUPPLY-{idx}', vendor_id=vendor.id, status=POStatus.PARTIAL
        )
        db_session.add(po)
        db_session.flush()
        db_session.add(
            PurchaseOrderLine(
                company_id=company_id,
                purchase_order_id=po.id,
                part_id=part.id,
                line_number=1,
                quantity_ordered=ordered,
                quantity_received=received,
                unit_price=1,
                line_total=ordered,
                is_closed=closed,
            )
        )
    db_session.commit()
    assert MRPService(db_session, 1).get_inventory_summary(part.id)[2] == 5
