"""User-facing operations contracts: reservation recovery, closure, safe draft edits and revision identity."""

from datetime import date, timedelta
from types import SimpleNamespace

from app.models.document import Document
from app.models.purchasing import Vendor
from app.models.quality import NonConformanceReport
from app.models.shipping import Shipment, ShipmentStatus
from app.models.user import UserRole
from tests.api.test_shipping_mark_shipped_rbac_followups import COMPANY_A, headers_for, make_part, make_user, make_wo


def test_shipment_pending_reservations_edit_cancel_and_invalid_quantities(client, db_session):
    user = make_user(db_session, role=UserRole.ADMIN)
    wo = make_wo(db_session, make_part(db_session))
    headers = headers_for(user)

    def create(quantity):
        return client.post(
            '/api/v1/shipping/', headers=headers, json={'work_order_id': wo.id, 'quantity_shipped': quantity}
        )

    for invalid in (0, -1):
        assert create(invalid).status_code == 422
    first = create(4).json()
    second = create(6).json()
    assert create(1).status_code == 409
    assert (
        client.put(f"/api/v1/shipping/{first['id']}", headers=headers, json={'quantity_shipped': 5}).status_code == 409
    )
    assert (
        client.put(f"/api/v1/shipping/{second['id']}", headers=headers, json={'status': 'shipped'}).status_code == 409
    )
    assert (
        client.put(f"/api/v1/shipping/{first['id']}", headers=headers, json={'status': 'cancelled'}).status_code == 200
    )
    assert client.post(f"/api/v1/shipping/{first['id']}/ship", headers=headers).status_code == 409
    ready = client.get('/api/v1/shipping/ready-to-ship', headers=headers).json()
    row = next(item for item in ready if item['work_order_id'] == wo.id)
    assert (row['quantity_remaining'], row['quantity_reserved'], row['quantity_shipped']) == (4, 6, 0)
    assert create(4).status_code == 200


def test_quality_detail_closure_requires_final_disposition_and_authorized_role(client, db_session):
    user = make_user(db_session, role=UserRole.ADMIN)
    viewer = make_user(db_session, role=UserRole.VIEWER)
    part = make_part(db_session)
    headers = headers_for(user)
    created = client.post(
        '/api/v1/quality/ncr',
        headers=headers,
        json={
            'part_id': part.id,
            'title': 'Material surface defect',
            'description': 'Inspection found a visible defect in the supplied part.',
            'source': 'incoming_inspection',
        },
    )
    assert created.status_code == 200, created.text
    record = created.json()
    payload = {
        'version': 0,
        'status': 'closed',
        'disposition': 'pending',
        'root_cause': 'Supplier handling damaged the finished surface.',
    }
    url = f"/api/v1/quality/ncr/{record['id']}"
    assert client.put(url, headers=headers, json=payload).status_code == 422
    payload['disposition'] = 'scrap'
    assert client.put(url, headers=headers_for(viewer), json=payload).status_code == 403
    assert client.put(url, headers=headers, json=payload).status_code == 200
    assert client.get(url, headers=headers).json()['closed_date'] == date.today().isoformat()


def test_po_draft_line_edit_rolls_totals_and_rejects_issued_lines(client, db_session):
    user = make_user(db_session, role=UserRole.ADMIN)
    part = make_part(db_session)
    vendor = Vendor(code='UXV', name='UX supplier', company_id=COMPANY_A, is_active=True)
    db_session.add(vendor)
    db_session.commit()
    headers = headers_for(user)
    response = client.post(
        '/api/v1/purchasing/purchase-orders',
        headers=headers,
        json={'vendor_id': vendor.id, 'lines': [{'part_id': part.id, 'quantity_ordered': 2, 'unit_price': 3}]},
    )
    assert response.status_code == 200, response.text
    po = response.json()
    payload = {
        'version': 0,
        'lines': [{'id': po['lines'][0]['id'], 'part_id': part.id, 'quantity_ordered': 4, 'unit_price': 5}],
    }
    url = f"/api/v1/purchasing/purchase-orders/{po['id']}"
    edited = client.put(url, headers=headers, json=payload)
    assert edited.status_code == 200, edited.text
    assert float(edited.json()['total']) == 20
    assert client.post(url + '/send', headers=headers).json()['delivery_status'] == 'not_dispatched'
    assert client.put(url, headers=headers, json=payload).status_code == 409


def test_document_revision_chain_preserves_previous_files_and_tenant_scope(client, db_session, monkeypatch):
    from app.api.endpoints import documents

    monkeypatch.setattr(
        documents,
        'get_storage',
        lambda: SimpleNamespace(is_remote=False, save=lambda content, key: '/tmp/ux-document-test.pdf'),
    )
    user = make_user(db_session, role=UserRole.ADMIN)
    headers = headers_for(user)

    def upload(**extra):
        return client.post(
            '/api/v1/documents/upload',
            headers=headers,
            files={'file': ('drawing.pdf', b'%PDF-1.4 example', 'application/pdf')},
            data={'title': 'Drawing', 'document_type': 'drawing', 'revision': 'A', **extra},
        )

    first = upload().json()
    same = upload(previous_revision_id=first['id'], revision_notes='Changed dimensions')
    assert same.status_code == 422
    next_revision = upload(previous_revision_id=first['id'], revision='B', revision_notes='Changed dimensions')
    assert next_revision.status_code == 200, next_revision.text
    second = next_revision.json()
    chain = client.get(f"/api/v1/documents/{second['id']}/revisions", headers=headers).json()
    assert {row['revision'] for row in chain} == {'A', 'B'}
    assert second['previous_revision_id'] == first['id']
    duplicate_ancestor = upload(previous_revision_id=second['id'], revision='A', revision_notes='Repeated identity')
    assert duplicate_ancestor.status_code == 422
    superseded = upload(previous_revision_id=first['id'], revision='C', revision_notes='Outdated predecessor')
    assert superseded.status_code == 409
    assert client.delete(f"/api/v1/documents/{first['id']}", headers=headers).status_code == 409
    assert db_session.get(Document, first['id']) is not None


def test_po_child_only_edits_advance_parent_token_and_reject_stale_save(client, db_session):
    user = make_user(db_session, role=UserRole.ADMIN)
    part = make_part(db_session)
    vendor = Vendor(code='UXLOCK', name='Draft edit supplier', company_id=COMPANY_A, is_active=True)
    db_session.add(vendor)
    db_session.commit()
    headers = headers_for(user)
    created = client.post(
        '/api/v1/purchasing/purchase-orders',
        headers=headers,
        json={'vendor_id': vendor.id, 'lines': [{'part_id': part.id, 'quantity_ordered': 2, 'unit_price': 3}]},
    )
    assert created.status_code == 200, created.text
    po = created.json()
    url = f"/api/v1/purchasing/purchase-orders/{po['id']}"
    # A line-note edit changes neither parent scalar fields nor totals.
    payload = {
        'version': 0,
        'expected_updated_at': po['updated_at'],
        'lines': [
            {
                'id': po['lines'][0]['id'],
                'part_id': part.id,
                'quantity_ordered': 2,
                'unit_price': 3,
                'notes': 'First edit',
            }
        ],
    }
    saved = client.put(url, headers=headers, json=payload)
    assert saved.status_code == 200, saved.text
    assert saved.json()['updated_at'] != po['updated_at']
    payload['lines'][0]['notes'] = 'Stale second edit'
    assert client.put(url, headers=headers, json=payload).status_code == 409
    assert client.get(url, headers=headers).json()['lines'][0]['notes'] == 'First edit'


def test_ncr_explicit_null_rejected_quantity_returns_validation_error(client, db_session):
    user = make_user(db_session, role=UserRole.ADMIN)
    headers = headers_for(user)
    response = client.post(
        '/api/v1/quality/ncr',
        headers=headers,
        json={
            'title': 'Null quantity test',
            'description': 'Inspection found a defect on the supplied material.',
            'source': 'incoming_inspection',
        },
    )
    assert response.status_code == 200, response.text
    url = f"/api/v1/quality/ncr/{response.json()['id']}"
    assert client.put(url, headers=headers, json={'version': 0, 'quantity_rejected': None}).status_code == 422
    # Omission remains legal for updates to independent fields.
    assert (
        client.put(url, headers=headers, json={'version': 0, 'containment_action': 'Quarantined lot'}).status_code
        == 200
    )
    assert float(client.get(url, headers=headers).json()['quantity_rejected']) == 0


def test_shipping_delivery_advances_without_dispatch_or_inventory_side_effects(client, db_session, monkeypatch):
    from app.api.endpoints import shipping

    user = make_user(db_session, role=UserRole.ADMIN)
    wo = make_wo(db_session, make_part(db_session))
    shipment = Shipment(
        company_id=COMPANY_A,
        shipment_number='UX-DELIVERED',
        work_order_id=wo.id,
        quantity_shipped=4,
        status=ShipmentStatus.SHIPPED,
        ship_date=date.today(),
    )
    db_session.add(shipment)
    db_session.commit()
    original_wo_status = wo.status

    # An ordinary delivery status update must not invoke dispatch's downstream followups.
    def unexpected_dispatch(*args, **kwargs):
        raise AssertionError('Delivery must not re-run dispatch')

    monkeypatch.setattr(shipping, 'generate_coc_for_shipment', unexpected_dispatch)
    headers = headers_for(user)
    url = f'/api/v1/shipping/{shipment.id}'
    response = client.put(url, headers=headers, json={'status': 'delivered'})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'delivered'
    db_session.refresh(shipment)
    db_session.refresh(wo)
    assert shipment.actual_delivery == date.today()
    assert wo.status == original_wo_status
    assert shipment.quantity_shipped == 4
    assert client.put(url, headers=headers, json={'status': 'delivered'}).status_code == 200
    for prohibited in ('shipped', 'pending', 'cancelled'):
        assert client.put(url, headers=headers, json={'status': prohibited}).status_code == 409
    assert client.put(url, headers=headers, json={'quantity_shipped': 5}).status_code == 409
