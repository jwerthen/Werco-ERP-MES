"""Real batch grouping, stale validation, tenant boundaries and stable replay."""

from datetime import date, timedelta

import pytest

from app.models.company import Company
from app.models.inventory import InventoryItem
from app.models.mrp import MRPAction, MRPSupplyLink
from app.models.purchasing import POStatus, PurchaseOrder, Vendor
from app.services.mrp_service import MRPService
from tests.services.test_mrp_service import _seed_make_wo_with_purchased_component

pytestmark = pytest.mark.requires_db


@pytest.fixture
def batch(db_session, admin_user):
    vendors = [Vendor(company_id=1, code=f'BATCH-{i}', name=f'Batch supplier {i}', is_active=True) for i in range(2)]
    db_session.add_all(vendors)
    db_session.commit()
    parts = [_seed_make_wo_with_purchased_component(db_session, 1, f'BATCH-{i}') for i in range(3)]
    for i, part in enumerate(parts):
        part.primary_supplier_id = vendors[i // 2].id
        part.standard_cost = i + 1.25
    db_session.commit()
    run = MRPService(db_session, 1).run_mrp(admin_user.id, include_safety_stock=False)
    actions = db_session.query(MRPAction).filter_by(mrp_run_id=run.id).order_by(MRPAction.id).all()
    return parts, vendors, actions


def payload(client, headers, actions):
    result = client.get(
        '/api/v1/mrp/purchase-batch/review', headers=headers, params=[('action_ids', row.id) for row in actions]
    )
    assert result.status_code == 200, result.text
    return {
        'request_key': 'stable-batch-request',
        'lines': [
            dict(
                action_id=row['action_id'],
                review_token=row['review_token'],
                quantity=row['quantity'],
                due_date=row['due_date'],
                vendor_id=row['vendor_id'],
                unit_price=row['unit_price'],
                notes='Reviewed requirement',
            )
            for row in result.json()['lines']
        ],
    }


def test_group_by_supplier_preserves_lines_dates_source_and_replay(client, admin_headers, db_session, batch):
    parts, vendors, actions = batch
    request = payload(client, admin_headers, actions)
    request['lines'][0]['quantity'] = 2.5
    request['lines'][1]['due_date'] = str(date.today() + timedelta(days=9))
    result = client.post('/api/v1/mrp/purchase-batch/drafts', headers=admin_headers, json=request)
    assert result.status_code == 200, result.text
    body = result.json()
    assert len(body['purchase_orders']) == 2 and len(body['drafts']) == 3
    orders = db_session.query(PurchaseOrder).order_by(PurchaseOrder.vendor_id).all()
    assert all(po.status == POStatus.DRAFT for po in orders)
    assert len(orders[0].lines) == 2 and len(orders[1].lines) == 1
    assert orders[0].vendor_id == vendors[0].id
    assert orders[0].total == 2.5 * 1.25 + 10 * 2.25
    assert orders[0].required_date == min(line.required_date for line in orders[0].lines)
    assert orders[0].lines[1].required_date == date.today() + timedelta(days=9)
    assert all('action ' in line.notes for po in orders for line in po.lines)
    assert db_session.query(MRPSupplyLink).count() == 3
    assert all(not action.processed and action.result_po_id for action in actions)
    request['lines'].reverse()  # Selection ordering does not change retry identity.
    replay = client.post('/api/v1/mrp/purchase-batch/drafts', headers=admin_headers, json=request)
    assert replay.status_code == 200 and replay.json()['replayed']
    assert db_session.query(PurchaseOrder).count() == 2
    request['lines'][0]['unit_price'] = 99
    assert client.post('/api/v1/mrp/purchase-batch/drafts', headers=admin_headers, json=request).status_code == 409
    assert db_session.query(PurchaseOrder).count() == 2
    # Grouping still nets every linked draft line, including partial supply, on rerun.
    fresh = MRPService(db_session, 1).run_mrp(None, include_safety_stock=False)
    residual = db_session.query(MRPAction).filter_by(mrp_run_id=fresh.id).all()
    assert len(residual) == 1 and residual[0].part_id == parts[0].id and residual[0].quantity == 7.5


@pytest.mark.parametrize(
    'change', ['inventory', 'vendor', 'new_run', 'quantity', 'past_date', 'duplicate', 'manufacture']
)
def test_invalid_selection_is_atomic(client, admin_headers, db_session, batch, change):
    parts, vendors, actions = batch
    request = payload(client, admin_headers, actions)
    if change == 'inventory':
        db_session.add(
            InventoryItem(
                company_id=1,
                part_id=parts[2].id,
                quantity_on_hand=1,
                quantity_allocated=0,
                location='MAIN',
                status='available',
                is_active=True,
            )
        )
    elif change == 'vendor':
        vendors[1].is_active = False
    elif change == 'new_run':
        MRPService(db_session, 1).run_mrp(None, include_safety_stock=False)
    elif change == 'quantity':
        request['lines'][-1]['quantity'] = 11
    elif change == 'past_date':
        request['lines'][-1]['due_date'] = str(date.today() - timedelta(days=1))
    elif change == 'duplicate':
        request['lines'].append(request['lines'][0])
    else:
        parts[-1].part_type = 'manufactured'
    db_session.commit()
    result = client.post('/api/v1/mrp/purchase-batch/drafts', headers=admin_headers, json=request)
    assert result.status_code in (409, 422), result.text
    assert db_session.query(PurchaseOrder).count() == db_session.query(MRPSupplyLink).count() == 0


def test_existing_single_draft_blocks_entire_batch_without_extra_supply(client, admin_headers, db_session, batch):
    _, _, actions = batch
    request = payload(client, admin_headers, actions)
    single = dict(request['lines'][0], request_key='single-before-batch')
    action_id = single.pop('action_id')
    assert (
        client.post(f'/api/v1/mrp/actions/{action_id}/supply-draft', headers=admin_headers, json=single).status_code
        == 200
    )
    result = client.post('/api/v1/mrp/purchase-batch/drafts', headers=admin_headers, json=request)
    assert result.status_code == 409 and result.json()['detail']['existing_drafts'][0]['action_id'] == action_id
    assert db_session.query(PurchaseOrder).count() == 1
    assert db_session.query(MRPSupplyLink).count() == 1


def test_batch_roles_and_foreign_action_supplier_are_enforced(
    client, admin_headers, operator_headers, db_session, batch
):
    _, _, actions = batch
    request = payload(client, admin_headers, actions)
    assert (
        client.get(
            '/api/v1/mrp/purchase-batch/review', headers=operator_headers, params={'action_ids': actions[0].id}
        ).status_code
        == 403
    )
    assert client.post('/api/v1/mrp/purchase-batch/drafts', headers=operator_headers, json=request).status_code == 403
    db_session.add(Company(id=2, name='Other', slug='other-batch'))
    vendor = Vendor(company_id=2, code='FOREIGN', name='Foreign supplier', is_active=True)
    db_session.add(vendor)
    db_session.commit()
    request['lines'][0]['vendor_id'] = vendor.id
    assert client.post('/api/v1/mrp/purchase-batch/drafts', headers=admin_headers, json=request).status_code == 422
    actions[0].company_id = 2
    db_session.commit()
    assert (
        client.get(
            '/api/v1/mrp/purchase-batch/review', headers=admin_headers, params={'action_ids': actions[0].id}
        ).status_code
        == 404
    )
    assert db_session.query(PurchaseOrder).count() == 0
