"""Operational Hank evidence and source-bound atomic command receipts."""

from datetime import datetime, timedelta

import pytest

from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.hank import HankTask
from app.models.inventory import InventoryItem
from app.models.purchasing import POReceipt, POStatus, PurchaseOrder, PurchaseOrderLine, Vendor
from app.models.role_permission import RolePermission
from app.models.shipping import Shipment
from app.models.time_entry import TimeEntry
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlocker
from app.services.audit_service import AuditService, AuditWriteError

from .test_hank_tasks import execute, propose

BASE = '/api/v1/hank'


@pytest.fixture
def po(db_session, test_part):
    vendor = Vendor(company_id=1, code='OPS-V', name='Operations vendor', is_active=True)
    db_session.add(vendor)
    db_session.flush()
    order = PurchaseOrder(company_id=1, po_number='OPS-PO', vendor_id=vendor.id, status=POStatus.SENT)
    db_session.add(order)
    db_session.flush()
    line = PurchaseOrderLine(
        company_id=1,
        purchase_order_id=order.id,
        part_id=test_part.id,
        line_number=1,
        quantity_ordered=10,
        quantity_received=0,
        unit_price=2,
    )
    db_session.add(line)
    db_session.commit()
    return order, line


@pytest.fixture
def running(db_session, test_work_order, test_user):
    test_work_order.status = WorkOrderStatus.IN_PROGRESS
    operation = db_session.query(WorkOrderOperation).filter_by(work_order_id=test_work_order.id).first()
    operation.status = OperationStatus.IN_PROGRESS
    entry = TimeEntry(
        company_id=1,
        work_order_id=test_work_order.id,
        operation_id=operation.id,
        user_id=test_user.id,
        clock_in=datetime.utcnow() - timedelta(hours=1),
    )
    db_session.add(entry)
    db_session.commit()
    return operation, entry


def delivery_input(po, **kwargs):
    return {
        'purchase_order_id': po[0].id,
        'lines': [
            {
                'po_line_id': po[1].id,
                'quantity_received': 3,
                'requires_inspection': False,
                'lot_number': 'HANK-LOT',
                **kwargs,
            }
        ],
    }


def test_receive_preview_is_inert_and_posts_receipt_stock_and_audits_once(client, auth_headers, db_session, po):
    task = propose(client, auth_headers, 'receive_delivery', delivery_input(po)).json()
    assert task['status'] == 'awaiting_review'
    assert 'Dock-to-stock' in ' '.join(task['preview']['changes'])
    assert db_session.query(POReceipt).count() == 0
    response = execute(client, auth_headers, task)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['status'] == 'completed'
    receipt = db_session.query(POReceipt).one()
    assert receipt.quantity_received == 3 and receipt.quantity_accepted == 3
    assert receipt.inspected_by is None and receipt.inspected_at is None
    assert db_session.query(InventoryItem).one().quantity_on_hand == 3
    assert execute(client, auth_headers, task).json() == result
    assert db_session.query(POReceipt).count() == 1
    assert {'receipt', 'inventory', 'purchase_order', 'receiving_delivery', 'hank_task'} <= {
        row.resource_type for row in db_session.query(AuditLog).all()
    }


def test_receive_inspection_is_explicit_and_does_not_accept_stock(client, auth_headers, db_session, po):
    data = delivery_input(po, requires_inspection=True)
    task = propose(client, auth_headers, 'receive_delivery', data).json()
    response = execute(client, auth_headers, task)
    assert response.status_code == 200, response.text
    receipt = db_session.query(POReceipt).one()
    assert receipt.requires_inspection and receipt.quantity_accepted == 0
    assert db_session.query(InventoryItem).count() == 0
    del data['lines'][0]['requires_inspection']
    assert propose(client, auth_headers, 'receive_delivery', data).status_code == 422


def test_receive_refuses_stale_quantities_and_foreign_line(client, auth_headers, db_session, po, test_part):
    task = propose(client, auth_headers, 'receive_delivery', delivery_input(po)).json()
    po[1].quantity_received = 1
    db_session.commit()
    assert execute(client, auth_headers, task).status_code == 409
    assert db_session.query(POReceipt).count() == 0
    db_session.add(Company(id=2, name='Foreign operational shop', slug='foreign-ops'))
    db_session.commit()
    po[1].company_id = 2
    db_session.commit()
    assert propose(client, auth_headers, 'receive_delivery', delivery_input(po)).status_code == 422


@pytest.mark.parametrize('failed_resource', ['receipt', 'inventory', 'hank_task'])
def test_receiving_domain_or_completion_audit_failure_rolls_everything_back(
    client,
    auth_headers,
    db_session,
    po,
    monkeypatch,
    failed_resource,
):
    task = propose(client, auth_headers, 'receive_delivery', delivery_input(po)).json()
    original_create = AuditService.log_create
    original_required = AuditService.log_required

    def create(self, resource_type, *args, **kwargs):
        return None if resource_type == failed_resource else original_create(self, resource_type, *args, **kwargs)

    def required(self, action, resource_type, *args, **kwargs):
        if resource_type == failed_resource:
            raise AuditWriteError('required evidence unavailable')
        return original_required(self, action, resource_type, *args, **kwargs)

    monkeypatch.setattr(AuditService, 'log_create', create)
    monkeypatch.setattr(AuditService, 'log_required', required)
    response = execute(client, auth_headers, task)
    assert response.status_code == 503, response.text
    db_session.rollback()
    assert db_session.query(POReceipt).count() == 0 and db_session.query(InventoryItem).count() == 0
    assert db_session.get(PurchaseOrderLine, po[1].id).quantity_received == 0
    assert db_session.get(HankTask, task['id']).status == 'awaiting_review'


def test_production_replays_without_duplicate_counts_and_leaves_clock_open(client, auth_headers, db_session, running):
    op, entry = running
    task = propose(
        client, auth_headers, 'report_production', {'operation_id': op.id, 'quantity_complete_delta': 2}
    ).json()
    assert db_session.get(TimeEntry, entry.id).quantity_produced in (None, 0)
    response = execute(client, auth_headers, task)
    assert response.status_code == 200, response.text
    assert execute(client, auth_headers, task).json() == response.json()
    db_session.refresh(op)
    db_session.refresh(entry)
    assert op.quantity_complete == 2 and entry.quantity_produced == 2 and entry.clock_out is None
    assert op.status == OperationStatus.IN_PROGRESS


def test_production_optional_hold_discloses_and_closes_entire_crew(
    client,
    auth_headers,
    db_session,
    running,
    operator_user,
):
    op, entry = running
    crew = TimeEntry(
        company_id=1,
        work_order_id=op.work_order_id,
        operation_id=op.id,
        user_id=operator_user.id,
        clock_in=datetime.utcnow() - timedelta(minutes=5),
    )
    db_session.add(crew)
    db_session.commit()
    data = {
        'operation_id': op.id,
        'quantity_complete_delta': 1,
        'hold': {'category': 'quality_hold', 'severity': 'high', 'note': 'Stop for weld review'},
    }
    task = propose(client, auth_headers, 'report_production', data).json()
    assert 'ALL 2' in ' '.join(task['preview']['warnings'])
    result = execute(client, auth_headers, task)
    assert result.status_code == 200, result.text
    db_session.refresh(op)
    db_session.refresh(entry)
    db_session.refresh(crew)
    assert op.status == OperationStatus.ON_HOLD and entry.clock_out and crew.clock_out
    assert db_session.query(WorkOrderBlocker).one().note == 'Stop for weld review'
    assert db_session.query(AuditLog).filter_by(resource_type='work_order_blocker').count() == 1


def test_production_source_changes_require_new_review(client, auth_headers, db_session, running):
    op, entry = running
    task = propose(
        client, auth_headers, 'report_production', {'operation_id': op.id, 'quantity_complete_delta': 2}
    ).json()
    entry.clock_out = datetime.utcnow()
    db_session.commit()
    assert execute(client, auth_headers, task).status_code == 409
    assert db_session.get(WorkOrderOperation, op.id).quantity_complete == 0


def test_production_audit_failure_restores_counts_and_hold(client, auth_headers, db_session, running, monkeypatch):
    op, entry = running
    task = propose(
        client,
        auth_headers,
        'report_production',
        {'operation_id': op.id, 'quantity_complete_delta': 2, 'hold': {'category': 'quality_hold', 'note': 'Hold'}},
    ).json()
    original = AuditService.log_required

    def fail(self, action, resource_type, *args, **kwargs):
        if resource_type == 'hank_task':
            raise AuditWriteError('receipt failed')
        return original(self, action, resource_type, *args, **kwargs)

    monkeypatch.setattr(AuditService, 'log_required', fail)
    assert execute(client, auth_headers, task).status_code == 503
    db_session.rollback()
    assert db_session.get(WorkOrderOperation, op.id).quantity_complete == 0
    assert db_session.get(TimeEntry, entry.id).clock_out is None
    assert db_session.query(WorkOrderBlocker).count() == 0


def test_pending_shipment_reserves_only_and_has_packing_slip_receipt(client, auth_headers, db_session, test_work_order):
    test_work_order.status = WorkOrderStatus.COMPLETE
    test_work_order.quantity_complete = 5
    db_session.commit()
    task = propose(
        client, auth_headers, 'draft_shipment', {'work_order_id': test_work_order.id, 'quantity_shipped': 3}
    ).json()
    assert db_session.query(Shipment).count() == 0
    response = execute(client, auth_headers, task)
    assert response.status_code == 200, response.text
    assert execute(client, auth_headers, task).json() == response.json()
    shipment = db_session.query(Shipment).one()
    assert shipment.status.value == 'pending' and shipment.ship_date is None and shipment.quantity_shipped == 3
    assert any(ref['type'] == 'packing_slip' for ref in response.json()['result']['references'])
    over = propose(client, auth_headers, 'draft_shipment', {'work_order_id': test_work_order.id, 'quantity_shipped': 3})
    assert over.status_code == 409


def test_shipment_completion_audit_failure_rolls_back_reserved_quantity(
    client,
    auth_headers,
    db_session,
    test_work_order,
    monkeypatch,
):
    test_work_order.status = WorkOrderStatus.COMPLETE
    test_work_order.quantity_complete = 5
    db_session.commit()
    task = propose(
        client, auth_headers, 'draft_shipment', {'work_order_id': test_work_order.id, 'quantity_shipped': 3}
    ).json()
    original = AuditService.log_required

    def fail(self, action, resource_type, *args, **kwargs):
        if resource_type == 'hank_task':
            raise AuditWriteError('receipt failed')
        return original(self, action, resource_type, *args, **kwargs)

    monkeypatch.setattr(AuditService, 'log_required', fail)
    assert execute(client, auth_headers, task).status_code == 503
    db_session.rollback()
    assert db_session.query(Shipment).count() == 0


def test_readiness_is_pure_scoped_and_states_missing_coverage(client, auth_headers, db_session, test_work_order):
    endpoint = f'{BASE}/work-orders/{test_work_order.id}/readiness'
    before = db_session.query(AuditLog).count()
    result = client.get(endpoint, headers=auth_headers)
    assert result.status_code == 200, result.text
    report = result.json()
    assert report['checked_at'].endswith('Z') and report['company_id'] == 1
    assert {row['key'] for row in report['checks']} >= {
        'materials',
        'instructions',
        'quality',
        'blockers',
        'inspections',
    }
    assert next(row for row in report['checks'] if row['key'] == 'materials')['status'] == 'unknown'
    assert db_session.query(AuditLog).count() == before and db_session.query(HankTask).count() == 0
    test_work_order.is_deleted = True
    db_session.commit()
    assert client.get(endpoint, headers=auth_headers).status_code == 404


def test_evidence_sections_do_not_widen_permissions(client, auth_headers, db_session, test_work_order, test_user, po):
    db_session.add(RolePermission(company_id=1, role=test_user.role, permissions=['work_orders:view']))
    db_session.commit()
    response = client.get(f'{BASE}/work-orders/{test_work_order.id}/readiness', headers=auth_headers)
    assert response.status_code == 200
    by_key = {row['key']: row for row in response.json()['checks']}
    assert by_key['materials']['status'] == by_key['quality']['status'] == 'unknown'
    assert client.get(f'{BASE}/purchase-orders/{po[0].id}/impact', headers=auth_headers).status_code == 403
    assert (
        client.get(f'{BASE}/work-orders/{test_work_order.id}/shipping-packet', headers=auth_headers).status_code == 403
    )
    assert client.get(f'{BASE}/trace/lot/HANK-LOT', headers=auth_headers).status_code == 403


def test_supplier_draft_is_copyable_unsent_and_work_order_notes_are_historical(
    client,
    auth_headers,
    db_session,
    test_work_order,
    po,
):
    report = client.get(f'{BASE}/purchase-orders/{po[0].id}/impact', headers=auth_headers)
    assert report.status_code == 200, report.text
    assert po[0].po_number in report.json()['draft_text']
    assert 'no message has been sent' in ' '.join(report.json()['coverage_notes'])
    test_work_order.notes = 'Fixture clamp marks from prior setup'
    db_session.commit()
    knowledge = client.get(f'{BASE}/work-orders/{test_work_order.id}/knowledge', headers=auth_headers)
    assert knowledge.status_code == 200, knowledge.text
    assert 'Fixture clamp' in knowledge.json()['checks'][0]['detail']
    assert 'not approved instructions' in ' '.join(knowledge.json()['coverage_notes'])
    assert db_session.query(HankTask).count() == 0


def test_operational_roles_and_read_only_context_cannot_execute(
    client,
    auth_headers,
    operator_headers,
    db_session,
    po,
    test_work_order,
    test_user,
):
    from app.core.security import create_access_token

    assert propose(client, operator_headers, 'receive_delivery', delivery_input(po)).status_code == 403
    assert (
        propose(
            client,
            operator_headers,
            'draft_shipment',
            {
                'work_order_id': test_work_order.id,
                'quantity_shipped': 1,
            },
        ).status_code
        == 403
    )
    task = propose(client, auth_headers, 'receive_delivery', delivery_input(po)).json()
    readonly = {'Authorization': f'Bearer {create_access_token(subject=test_user.id, company_id=1, read_only=True)}'}
    assert client.get(f'{BASE}/tasks/{task["id"]}', headers=readonly).status_code == 200
    assert execute(client, readonly, task).status_code == 403
    assert db_session.query(POReceipt).count() == 0


def test_foreign_company_job_evidence_is_never_returned(client, auth_headers, db_session, test_work_order):
    db_session.add(Company(id=2, name='Private source', slug='private-source'))
    db_session.commit()
    test_work_order.company_id = 2
    test_work_order.notes = 'Private foreign instruction'
    db_session.commit()
    for report in ('knowledge', 'readiness', 'shipping-packet'):
        response = client.get(f'{BASE}/work-orders/{test_work_order.id}/{report}', headers=auth_headers)
        assert response.status_code == 404 and 'Private foreign' not in response.text


def test_hank_receiving_never_automatically_prints_and_trace_uses_recorded_lot(
    client,
    auth_headers,
    db_session,
    po,
    monkeypatch,
):
    def unexpected(*args, **kwargs):
        raise AssertionError('Hank must not perform printer I/O as part of the action transaction')

    monkeypatch.setattr('app.api.endpoints.receiving.enqueue_receipt_label', unexpected)
    task = propose(client, auth_headers, 'receive_delivery', delivery_input(po)).json()
    assert execute(client, auth_headers, task).status_code == 200
    trace = client.get(f'{BASE}/trace/lot/HANK-LOT', headers=auth_headers)
    assert trace.status_code == 200, trace.text
    assert any('quantity 3' in check['detail'] for check in trace.json()['checks'])
    assert 'recorded' in trace.json()['summary'].lower()


def test_report_scrap_creates_audited_ncr_without_implicitly_holding(client, auth_headers, db_session, running):
    from app.models.quality import NonConformanceReport

    op, entry = running
    task = propose(
        client,
        auth_headers,
        'report_production',
        {
            'operation_id': op.id,
            'quantity_scrapped_delta': 1,
            'scrap_reason': 'Cracked weld',
            'open_ncr': True,
        },
    ).json()
    result = execute(client, auth_headers, task)
    assert result.status_code == 200, result.text
    ncr = db_session.query(NonConformanceReport).one()
    assert ncr.quantity_affected == 1 and ncr.work_order_id == op.work_order_id
    assert any(row['type'] == 'ncr' and row['id'] == ncr.id for row in result.json()['result']['references'])
    db_session.refresh(entry)
    assert entry.clock_out is None and db_session.query(WorkOrderBlocker).count() == 0


def test_production_domain_audit_cannot_be_swallowed_by_successful_hank_audit(
    client,
    auth_headers,
    db_session,
    running,
    monkeypatch,
):
    op, entry = running
    task = propose(
        client, auth_headers, 'report_production', {'operation_id': op.id, 'quantity_complete_delta': 2}
    ).json()
    original = AuditService.log

    def fail(self, *args, **kwargs):
        if kwargs.get('resource_type') == 'work_order_operation':
            return None
        return original(self, *args, **kwargs)

    monkeypatch.setattr(AuditService, 'log', fail)
    response = execute(client, auth_headers, task)
    assert response.status_code == 503, response.text
    db_session.rollback()
    assert db_session.get(WorkOrderOperation, op.id).quantity_complete == 0
    assert db_session.get(TimeEntry, entry.id).quantity_produced == 0
    assert db_session.get(HankTask, task['id']).status == 'awaiting_review'


def test_new_shipment_reservation_invalidates_old_review(client, auth_headers, db_session, test_work_order):
    test_work_order.status = WorkOrderStatus.COMPLETE
    test_work_order.quantity_complete = 5
    db_session.commit()
    data = {'work_order_id': test_work_order.id, 'quantity_shipped': 2}
    old = propose(client, auth_headers, 'draft_shipment', data).json()
    other = propose(client, auth_headers, 'draft_shipment', data).json()
    assert execute(client, auth_headers, other).status_code == 200
    assert execute(client, auth_headers, old).status_code == 409
    assert db_session.query(Shipment).count() == 1


def test_purchasing_alternatives_exclude_held_expired_allocated_and_foreign_stock(
    client,
    auth_headers,
    db_session,
    po,
    test_part,
):
    from datetime import date

    items = [
        ('USABLE', 'available', 5, 1, None, 1),
        ('HELD', 'hold', 7, 0, None, 1),
        ('ALLOCATED', 'available', 7, 7, None, 1),
        ('EXPIRED', 'available', 7, 0, date(2020, 1, 1), 1),
        ('FOREIGN', 'available', 7, 0, None, 2),
    ]
    db_session.add(Company(id=2, name='Other stock', slug='other-stock'))
    for lot, status, quantity, allocated, expiry, company_id in items:
        db_session.add(
            InventoryItem(
                company_id=company_id,
                part_id=test_part.id,
                lot_number=lot,
                location='A',
                quantity_on_hand=quantity,
                quantity_allocated=allocated,
                status=status,
                expiration_date=expiry,
                is_active=True,
            )
        )
    other = PurchaseOrder(
        company_id=1,
        po_number='ALTERNATIVE-PO',
        vendor_id=po[0].vendor_id,
        status=POStatus.PARTIAL,
        supplier_confirmed_date=date(2026, 12, 1),
    )
    db_session.add(other)
    db_session.flush()
    db_session.add(
        PurchaseOrderLine(
            company_id=1,
            purchase_order_id=other.id,
            part_id=test_part.id,
            line_number=1,
            quantity_ordered=9,
            quantity_received=2,
            unit_price=2,
        )
    )
    db_session.commit()
    report = client.get(f'{BASE}/purchase-orders/{po[0].id}/impact', headers=auth_headers)
    assert report.status_code == 200, report.text
    stock = [row for row in report.json()['checks'] if row['key'].startswith('alternative_stock:')]
    assert len(stock) == 1 and '4 unallocated usable' in stock[0]['detail'] and 'USABLE' in stock[0]['detail']
    supply = [row for row in report.json()['checks'] if row['key'].startswith('alternative_po:')]
    assert len(supply) == 1 and '7 outstanding' in supply[0]['detail'] and '2026-12-01' in supply[0]['detail']
    assert supply[0]['references'][0]['id'] == other.id


def test_readiness_uses_canonical_required_traveler_evidence_counts(client, auth_headers, db_session, test_work_order):
    from app.models.process_sheet import ProcessSheet, WOOperationStep

    operation = db_session.query(WorkOrderOperation).filter_by(work_order_id=test_work_order.id).first()
    sheet = ProcessSheet(
        company_id=1, sheet_number='HANK-PS', title='Required traveler', revision='A', status='released'
    )
    db_session.add(sheet)
    db_session.flush()
    db_session.add(
        WOOperationStep(
            company_id=1,
            work_order_operation_id=operation.id,
            source_sheet_id=sheet.id,
            source_sheet_revision='A',
            sequence=1,
            label='Verify material',
            step_type='checkbox',
            is_required=True,
        )
    )
    db_session.commit()
    report = client.get(f'{BASE}/work-orders/{test_work_order.id}/readiness', headers=auth_headers)
    assert report.status_code == 200, report.text
    check = next(row for row in report.json()['checks'] if row['key'] == 'traveler_evidence')
    assert check['status'] == 'attention' and '0 of 1 required' in check['detail']


@pytest.mark.parametrize('kind', ['lot', 'serial'])
def test_trace_accepts_encoded_slashes_in_exact_record_identifier(client, auth_headers, db_session, test_part, kind):
    from urllib.parse import quote

    identifier = 'LOT/26-09' if kind == 'lot' else 'SERIAL/26-09'
    item = InventoryItem(
        company_id=1,
        part_id=test_part.id,
        quantity_on_hand=1,
        location='TRACE',
        lot_number=identifier if kind == 'lot' else None,
        serial_number=identifier if kind == 'serial' else None,
    )
    db_session.add(item)
    db_session.commit()
    response = client.get(f'{BASE}/trace/{kind}/{quote(identifier, safe="")}', headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json()['title'] == f'{kind.title()} trace: {identifier}'
    assert response.json()['checks'][0]['references'][0]['id'] == item.id
