"""Cancelled nest recovery must restore real work, quantities and audit evidence."""

from datetime import datetime

import pytest

from app.models.audit_log import AuditLog
from app.models.laser_nest import LaserNest
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlocker
from app.services.audit_service import AuditService
from tests.api import test_laser_nests_manual as manual_fixtures
from tests.api.test_laser_nests_manual import (
    COMPANY_A,
    COMPANY_B,
    _create_manual_nest,
    _upload_pdf,
    headers_for,
    make_user,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]
laser_setup = manual_fixtures.laser_setup
upload_dir = manual_fixtures.upload_dir


def deleted_nest(client, db, setup):
    headers = headers_for(setup['admin'])
    data = _create_manual_nest(client, headers, setup['parent'].id, {'cnc_number': '06203', 'planned_runs': 3}).json()
    nest = db.get(LaserNest, data['id'])
    document_id = _upload_pdf(client, headers, name='06203.pdf')
    attached = client.post(
        f"/api/v1/laser-nests/{nest.id}/attach-document", headers=headers, json={'document_id': document_id}
    )
    assert attached.status_code == 200
    data['document_id'] = document_id
    child_id = nest.operation.work_order_id
    assert client.delete(f"/api/v1/laser-nests/{nest.id}", headers=headers).status_code == 200
    db.expire_all()
    return data, child_id, headers


def test_restores_original_nest_and_queue_with_audited_quantity(client, db_session, laser_setup):
    data, child_id, headers = deleted_nest(client, db_session, laser_setup)
    nest_id, op_id = data['id'], data['work_order_operation_id']
    before = client.get(f'/api/v1/work-orders/{child_id}', headers=headers).json()
    row = next(o for o in before['operations'] if o['id'] == op_id)
    assert row['cancelled_nest_id'] == nest_id
    assert row['laser_nest'] is None
    response = client.post(f'/api/v1/laser-nests/{nest_id}/restore', headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()['operation_status'] == 'ready'
    assert response.json()['work_order_operation_id'] == op_id
    assert response.json()['document_id'] == data['document_id']
    db_session.expire_all()
    nest = db_session.get(LaserNest, nest_id)
    assert (nest.is_deleted, nest.deleted_at, nest.deleted_by) == (False, None, None)
    assert float(nest.operation.work_order.quantity_ordered) == 3
    assert float(nest.operation.quantity_complete) == 0
    queue = client.get(f"/api/v1/shop-floor/work-center-queue/{laser_setup['wc'].id}", headers=headers).json()
    assert any(o['operation_id'] == op_id and o['laser_nest']['id'] == nest_id for o in queue['queue'])
    after = client.get(f'/api/v1/work-orders/{child_id}', headers=headers).json()
    assert next(o for o in after['operations'] if o['id'] == op_id)['cancelled_nest_id'] is None
    audits = db_session.query(AuditLog).filter(AuditLog.company_id == COMPANY_A).all()
    restored = [a for a in audits if (a.extra_data or {}).get('transition') == 'restore_laser_nest']
    assert {a.resource_type for a in restored} == {'laser_nest', 'work_order_operation', 'work_order'}
    assert all(a.user_id == laser_setup['admin'].id for a in restored)
    assert client.post(f'/api/v1/laser-nests/{nest_id}/restore', headers=headers).status_code == 409


@pytest.mark.parametrize('mode,expected', [('draft', 'pending'), ('started', 'in_progress'), ('blocker', 'on_hold')])
def test_preserves_release_labor_and_blocker_state(client, db_session, laser_setup, mode, expected):
    data, child_id, headers = deleted_nest(client, db_session, laser_setup)
    op = db_session.get(WorkOrderOperation, data['work_order_operation_id'])
    if mode == 'draft':
        db_session.get(WorkOrder, child_id).status = WorkOrderStatus.DRAFT
    elif mode == 'started':
        op.actual_start = datetime.utcnow()
        op.quantity_complete = 1
    else:
        db_session.add(
            WorkOrderBlocker(
                company_id=COMPANY_A,
                work_order_id=child_id,
                operation_id=op.id,
                category='material_missing',
                severity='high',
                status='open',
                title='Missing sheet',
                reported_by=laser_setup['admin'].id,
            )
        )
    db_session.commit()
    response = client.post(f"/api/v1/laser-nests/{data['id']}/restore", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()['operation_status'] == expected
    db_session.expire_all()
    assert db_session.get(LaserNest, data['id']).is_deleted is False
    assert float(db_session.get(WorkOrderOperation, op.id).quantity_complete) == (1 if mode == 'started' else 0)
    if mode == 'blocker':
        assert db_session.query(WorkOrderBlocker).filter(WorkOrderBlocker.operation_id == op.id).one().status == 'open'


@pytest.mark.parametrize('terminal', [WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED])
def test_refuses_finished_work_orders(client, db_session, laser_setup, terminal):
    data, child_id, headers = deleted_nest(client, db_session, laser_setup)
    db_session.get(WorkOrder, child_id).status = terminal
    db_session.commit()
    assert client.post(f"/api/v1/laser-nests/{data['id']}/restore", headers=headers).status_code == 409
    db_session.expire_all()
    assert db_session.get(LaserNest, data['id']).is_deleted is True


def test_role_and_tenant_scope(client, db_session, laser_setup):
    data, child_id, headers = deleted_nest(client, db_session, laser_setup)
    operator = make_user(db_session, role=UserRole.OPERATOR)
    other_admin = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_B)
    url = f"/api/v1/laser-nests/{data['id']}/restore"
    assert client.post(url, headers=headers_for(operator)).status_code == 403
    assert client.post(url, headers=headers_for(other_admin)).status_code == 404
    db_session.expire_all()
    assert db_session.get(LaserNest, data['id']).is_deleted is True


def test_required_audit_failure_rolls_back_restore(client, db_session, laser_setup, monkeypatch):
    data, child_id, headers = deleted_nest(client, db_session, laser_setup)
    monkeypatch.setattr(AuditService, 'log', lambda *a, **kw: None)
    response = client.post(f"/api/v1/laser-nests/{data['id']}/restore", headers=headers)
    assert response.status_code == 503, response.text
    db_session.expire_all()
    nest = db_session.get(LaserNest, data['id'])
    assert nest.is_deleted is True
    assert nest.operation.status == OperationStatus.ON_HOLD
    assert float(db_session.get(WorkOrder, child_id).quantity_ordered) == 1
