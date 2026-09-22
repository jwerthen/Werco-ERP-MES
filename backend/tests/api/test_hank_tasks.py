"""Actual drafts/attachments, durable recovery, tenant authority and atomic evidence."""

from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.document import Document, DocumentType
from app.models.hank import HankTask
from app.models.purchasing import POStatus, PurchaseOrder, Vendor
from app.models.role_permission import RolePermission
from app.models.work_order import WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services.audit_service import AuditService, AuditWriteError
from app.services.hank_task_service import HankTaskService

BASE = '/api/v1/hank'


@pytest.fixture
def vendor(db_session):
    row = Vendor(company_id=1, code='HANK-V', name='Hank supply', is_active=True)
    db_session.add(row)
    db_session.commit()
    return row


@pytest.fixture
def document(db_session):
    row = Document(
        company_id=1,
        document_number='HANK-DOC',
        title='Material certificate',
        document_type=DocumentType.MATERIAL_CERT,
        file_name='cert.pdf',
        mime_type='application/pdf',
        status='released',
        revision='A',
    )
    db_session.add(row)
    db_session.commit()
    return row


def propose(client, headers, kind, input_data, key=None, company=1):
    return client.post(
        BASE + '/tasks',
        headers=headers,
        json={
            'expected_company_id': company,
            'request_key': key or str(uuid4()),
            'kind': kind,
            'input': input_data,
        },
    )


def execute(client, headers, task, version=None):
    return client.post(
        f'{BASE}/tasks/{task["id"]}/execute',
        headers=headers,
        json={
            'expected_company_id': 1,
            'expected_version': task['version'] if version is None else version,
        },
    )


def repeat_input(work_order):
    return {'source_work_order_id': work_order.id, 'quantity_ordered': 3, 'due_date': '2026-12-01'}


def test_repeat_job_proposal_is_inert_and_execute_replays_receipt(client, db_session, auth_headers, test_work_order):
    source_id = test_work_order.id
    before = db_session.query(WorkOrder).count()
    key = str(uuid4())
    first = propose(client, auth_headers, 'repeat_job', repeat_input(test_work_order), key)
    assert first.status_code == 200, first.text
    task = first.json()
    assert task['status'] == 'awaiting_review'
    assert task['created_at'].endswith('Z')
    assert db_session.query(WorkOrder).count() == before
    repeated = propose(client, auth_headers, 'repeat_job', repeat_input(test_work_order), key)
    assert repeated.json()['id'] == task['id']
    assert db_session.query(HankTask).count() == 1
    completed = execute(client, auth_headers, task)
    assert completed.status_code == 200, completed.text
    result = completed.json()
    assert result['status'] == 'completed' and result['version'] == 2
    copied_id = result['result']['references'][0]['id']
    copied = db_session.get(WorkOrder, copied_id)
    assert copied.id != source_id and copied.status == WorkOrderStatus.DRAFT
    assert copied.quantity_ordered == 3 and copied.released_at is None
    assert db_session.query(WorkOrderOperation).filter_by(work_order_id=copied.id).count() == 1
    assert execute(client, auth_headers, task).json() == result
    assert db_session.query(WorkOrder).count() == before + 1
    db_session.rollback()
    assert db_session.query(AuditLog).filter_by(resource_type='hank_task').count() == 2


def test_purchase_order_receipt_stays_draft(client, db_session, auth_headers, vendor, test_part):
    response = propose(
        client,
        auth_headers,
        'draft_purchase_order',
        {
            'vendor_id': vendor.id,
            'lines': [{'part_id': test_part.id, 'quantity_ordered': 4, 'unit_price': '12.25'}],
        },
    )
    assert response.status_code == 200, response.text
    assert db_session.query(PurchaseOrder).count() == 0
    result = execute(client, auth_headers, response.json())
    assert result.status_code == 200, result.text
    po = db_session.query(PurchaseOrder).one()
    assert po.status == POStatus.DRAFT and po.total == 49
    assert po.created_by is not None and len(po.lines) == 1
    assert result.json()['result']['references'][0]['url'] == f'/purchasing?po={po.id}'
    assert execute(client, auth_headers, response.json()).json() == result.json()
    assert db_session.query(PurchaseOrder).count() == 1


def test_po_number_conflict_preserves_review_and_can_retry(
    client, db_session, auth_headers, vendor, test_part, monkeypatch
):
    proposal = propose(
        client,
        auth_headers,
        'draft_purchase_order',
        {
            'vendor_id': vendor.id,
            'lines': [{'part_id': test_part.id, 'quantity_ordered': 2, 'unit_price': 3}],
        },
    )
    assert proposal.status_code == 200, proposal.text
    task = proposal.json()
    real_flush = db_session.flush
    failed = False

    def conflict_at_po_header(*args, **kwargs):
        nonlocal failed
        if not failed and any(isinstance(row, PurchaseOrder) for row in db_session.new):
            failed = True
            raise IntegrityError('INSERT INTO purchase_orders', None, Exception('duplicate PO number'))
        return real_flush(*args, **kwargs)

    monkeypatch.setattr(db_session, 'flush', conflict_at_po_header)
    response = execute(client, auth_headers, task)
    assert response.status_code == 409, response.text
    assert failed
    # No manual rollback here: the endpoint must recover the failed transaction.
    assert db_session.query(PurchaseOrder).count() == 0
    assert db_session.query(AuditLog).filter_by(resource_type='purchase_order').count() == 0
    saved = db_session.get(HankTask, task['id'])
    assert saved.status == 'awaiting_review' and saved.version == task['version']
    assert saved.result_json is None
    assert db_session.query(AuditLog).filter_by(resource_type='hank_task').count() == 1

    retry = execute(client, auth_headers, task)
    assert retry.status_code == 200, retry.text
    assert retry.json()['status'] == 'completed'
    assert db_session.query(PurchaseOrder).count() == 1
    assert db_session.query(AuditLog).filter_by(resource_type='purchase_order').count() == 1
    assert db_session.query(AuditLog).filter_by(resource_type='hank_task').count() == 2


def test_attachment_receipt_and_required_document_audit(client, db_session, auth_headers, document, test_work_order):
    response = propose(
        client,
        auth_headers,
        'attach_document',
        {
            'document_id': document.id,
            'work_order_id': test_work_order.id,
        },
    )
    assert response.status_code == 200, response.text
    assert document.work_order_id is None
    result = execute(client, auth_headers, response.json())
    assert result.status_code == 200, result.text
    db_session.refresh(document)
    assert document.work_order_id == test_work_order.id
    assert document.status == 'released' and document.revision == 'A'
    assert db_session.query(AuditLog).filter_by(resource_type='document', resource_id=document.id).count() == 1
    assert execute(client, auth_headers, response.json()).status_code == 200
    assert db_session.query(AuditLog).filter_by(resource_type='document', resource_id=document.id).count() == 1


def test_child_plan_drift_invalidates_repeat_even_without_header_version_change(
    client, db_session, auth_headers, test_work_order
):
    task = propose(client, auth_headers, 'repeat_job', repeat_input(test_work_order)).json()
    version = test_work_order.version
    operation = db_session.query(WorkOrderOperation).filter_by(work_order_id=test_work_order.id).one()
    operation.name = 'Changed planning instructions'
    db_session.commit()
    assert test_work_order.version == version
    response = execute(client, auth_headers, task)
    assert response.status_code == 409, response.text
    assert db_session.query(WorkOrder).count() == 1
    assert db_session.get(HankTask, task['id']).status == 'awaiting_review'


def test_changed_pdf_revision_or_link_cannot_execute_review(
    client, db_session, auth_headers, document, test_work_order
):
    task = propose(
        client, auth_headers, 'attach_document', {'document_id': document.id, 'work_order_id': test_work_order.id}
    ).json()
    document.revision = 'B'
    db_session.commit()
    response = execute(client, auth_headers, task)
    assert response.status_code == 409
    db_session.refresh(document)
    assert document.work_order_id is None


def test_pending_cancel_version_and_no_execute(client, db_session, auth_headers, test_work_order):
    task = propose(client, auth_headers, 'repeat_job', repeat_input(test_work_order)).json()
    path = f'{BASE}/tasks/{task["id"]}/cancel'
    assert (
        client.post(path, headers=auth_headers, json={'expected_company_id': 1, 'expected_version': 8}).status_code
        == 409
    )
    cancel = client.post(path, headers=auth_headers, json={'expected_company_id': 1, 'expected_version': 1})
    assert cancel.status_code == 200 and cancel.json()['status'] == 'cancelled'
    assert execute(client, auth_headers, task).status_code == 409
    assert db_session.query(WorkOrder).count() == 1


def test_tenant_owner_permissions_and_credential_bound_recovery(
    client, db_session, auth_headers, admin_headers, test_user, test_work_order
):
    key = str(uuid4())
    task = propose(client, auth_headers, 'repeat_job', repeat_input(test_work_order), key).json()
    assert client.get(f'{BASE}/tasks/{task["id"]}', headers=admin_headers).status_code == 404
    assert client.get(BASE + '/tasks', headers=admin_headers).json()['tasks'] == []
    assert propose(client, admin_headers, 'repeat_job', repeat_input(test_work_order), key).status_code == 409
    assert propose(client, auth_headers, 'repeat_job', repeat_input(test_work_order), company=2).status_code == 409
    test_user._api_token_id = 987
    with pytest.raises(HTTPException) as forbidden:
        HankTaskService(db_session, test_user, 1).get(task['id'])
    assert forbidden.value.status_code == 404
    test_user._api_token_id = None
    db_session.add(RolePermission(company_id=1, role=test_user.role, permissions=['work_orders:view']))
    db_session.commit()
    assert execute(client, auth_headers, task).status_code == 403
    assert client.get(BASE + '/capabilities', headers=auth_headers).json()['allowed_kinds'] == ['attach_document']


def test_foreign_source_and_unknown_input_are_rejected(client, db_session, auth_headers, test_work_order):
    db_session.add(Company(id=2, slug='hank-other', name='Other shop'))
    foreign = WorkOrder(company_id=2, part_id=test_work_order.part_id, work_order_number='FOREIGN', quantity_ordered=2)
    db_session.add(foreign)
    db_session.commit()
    assert propose(client, auth_headers, 'repeat_job', repeat_input(foreign)).status_code == 404
    assert (
        propose(client, auth_headers, 'repeat_job', {**repeat_input(test_work_order), 'status': 'released'}).status_code
        == 422
    )
    assert client.get(BASE + '/capabilities').status_code == 401


@pytest.mark.parametrize('kind', ['repeat_job', 'draft_purchase_order', 'attach_document'])
def test_required_receipt_audit_failure_rolls_back_actual_mutation(
    client, db_session, auth_headers, monkeypatch, test_work_order, test_part, vendor, document, kind
):
    inputs = {
        'repeat_job': repeat_input(test_work_order),
        'draft_purchase_order': {
            'vendor_id': vendor.id,
            'lines': [{'part_id': test_part.id, 'quantity_ordered': 2, 'unit_price': 3}],
        },
        'attach_document': {'document_id': document.id, 'work_order_id': test_work_order.id},
    }
    response = propose(client, auth_headers, kind, inputs[kind])
    assert response.status_code == 200, response.text
    task = response.json()
    original = AuditService.log_required

    def fail_receipt(self, action, resource_type, **kwargs):
        if resource_type == 'hank_task' and action == 'UPDATE':
            raise AuditWriteError('simulated required audit failure')
        return original(self, action, resource_type, **kwargs)

    monkeypatch.setattr(AuditService, 'log_required', fail_receipt)
    failed = execute(client, auth_headers, task)
    assert failed.status_code == 503, failed.text
    db_session.rollback()
    assert db_session.query(WorkOrder).count() == 1
    assert db_session.query(PurchaseOrder).count() == 0
    db_session.refresh(document)
    assert document.work_order_id is None
    assert db_session.get(HankTask, task['id']).status == 'awaiting_review'
    assert db_session.query(AuditLog).filter_by(resource_type='hank_task').count() == 1


def test_changed_request_key_payload_cannot_replay(client, db_session, auth_headers, test_work_order):
    key = str(uuid4())
    first = propose(client, auth_headers, 'repeat_job', repeat_input(test_work_order), key)
    assert first.status_code == 200
    changed = {**repeat_input(test_work_order), 'quantity_ordered': 8}
    assert propose(client, auth_headers, 'repeat_job', changed, key).status_code == 409
    assert db_session.query(HankTask).count() == 1


@pytest.mark.parametrize('kind,resource', [('repeat_job', 'work_order'), ('draft_purchase_order', 'purchase_order')])
def test_failed_domain_audit_cannot_hide_behind_successful_receipt(
    client, db_session, auth_headers, monkeypatch, test_work_order, test_part, vendor, kind, resource
):
    data = (
        repeat_input(test_work_order)
        if kind == 'repeat_job'
        else {
            'vendor_id': vendor.id,
            'lines': [{'part_id': test_part.id, 'quantity_ordered': 1, 'unit_price': 2}],
        }
    )
    task = propose(client, auth_headers, kind, data).json()
    original = AuditService.log_create

    def fail_one(self, resource_type, *args, **kwargs):
        return None if resource_type == resource else original(self, resource_type, *args, **kwargs)

    monkeypatch.setattr(AuditService, 'log_create', fail_one)
    failed = execute(client, auth_headers, task)
    assert failed.status_code == 503, failed.text
    db_session.rollback()
    assert db_session.query(WorkOrder).count() == 1
    assert db_session.query(PurchaseOrder).count() == 0
    assert db_session.get(HankTask, task['id']).status == 'awaiting_review'


def test_read_only_company_can_recover_receipt_but_cannot_execute(
    db_session, test_user, test_work_order, client, auth_headers
):
    task = propose(client, auth_headers, 'repeat_job', repeat_input(test_work_order)).json()
    test_user._read_only_company_context = True
    service = HankTaskService(db_session, test_user, 1)
    assert service.get(task['id']).id == task['id']
    assert service.list().tasks[0].id == task['id']
    assert service.capabilities().allowed_kinds == []


def test_po_review_discloses_each_line_date_and_instruction(client, auth_headers, vendor, test_part):
    response = propose(
        client,
        auth_headers,
        'draft_purchase_order',
        {
            'vendor_id': vendor.id,
            'required_date': '2026-11-01',
            'lines': [
                {
                    'part_id': test_part.id,
                    'quantity_ordered': 2,
                    'unit_price': 5,
                    'required_date': '2026-10-15',
                    'notes': 'Include material certificate',
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    changes = ' '.join(response.json()['preview']['changes'])
    assert '2026-10-15' in changes and 'Include material certificate' in changes


def test_component_part_drift_invalidates_repeat(client, db_session, auth_headers, test_work_order, test_part):
    from app.models.part import Part

    component = Part(
        company_id=1,
        part_number='OLD-COMPONENT',
        name='Component',
        part_type='manufactured',
        unit_of_measure='each',
        is_active=True,
    )
    db_session.add(component)
    db_session.flush()
    operation = db_session.query(WorkOrderOperation).filter_by(work_order_id=test_work_order.id).one()
    operation.component_part_id = component.id
    operation.name = 'OLD-COMPONENT - Cut'
    db_session.commit()
    task = propose(client, auth_headers, 'repeat_job', repeat_input(test_work_order)).json()
    component.part_number = 'NEW-COMPONENT'
    db_session.commit()
    assert execute(client, auth_headers, task).status_code == 409
    assert db_session.query(WorkOrder).count() == 1


def _inbox_task(
    db,
    owner,
    *,
    status='awaiting_review',
    company_id=1,
    credential_key='user',
    kind='repeat_job',
    label='Personal task',
):
    """Fixture receipts represent already-persisted work; inbox reads must never write."""
    from datetime import datetime

    row = HankTask(
        company_id=company_id,
        owner_id=owner.id,
        credential_key=credential_key,
        request_key=str(uuid4()),
        request_hash='a' * 64,
        kind=kind,
        title=label,
        status=status,
        version=1 if status == 'awaiting_review' else 2,
        input_json={},
        preview_json={'summary': f'Review {label}', 'changes': [], 'warnings': [], 'references': []},
        source_versions_json={},
        result_json=(
            {'summary': f'Finished {label}', 'warnings': [], 'references': []} if status == 'completed' else None
        ),
        completed_at=datetime.utcnow() if status in ('completed', 'cancelled') else None,
    )
    db.add(row)
    db.flush()
    return row


def test_inbox_status_filter_precedes_page_window_and_cursor_is_stable(client, db_session, auth_headers, test_user):
    oldest = _inbox_task(db_session, test_user, status='completed', label='Oldest completed')
    _inbox_task(db_session, test_user, status='cancelled')
    middle = _inbox_task(db_session, test_user, status='completed', label='Middle completed')
    _inbox_task(db_session, test_user)
    newest = _inbox_task(db_session, test_user, status='completed', label='Newest completed')
    _inbox_task(db_session, test_user)
    db_session.commit()
    first_response = client.get(BASE + '/tasks?status=completed&limit=2', headers=auth_headers)
    assert first_response.status_code == 200, first_response.text
    first = first_response.json()
    assert [row['id'] for row in first['tasks']] == [newest.id, middle.id]
    assert all(row['result']['summary'].startswith('Finished') for row in first['tasks'])
    assert first['has_more'] is True and first['next_before_id'] == middle.id

    # A new task arriving between pages must not duplicate or hide earlier receipts.
    added = _inbox_task(db_session, test_user, status='completed', label='Arrived after first page')
    db_session.commit()
    second_response = client.get(
        BASE + f'/tasks?status=completed&limit=2&before_id={first["next_before_id"]}',
        headers=auth_headers,
    )
    assert second_response.status_code == 200, second_response.text
    second = second_response.json()
    assert [row['id'] for row in second['tasks']] == [oldest.id]
    assert second['has_more'] is False and second['next_before_id'] is None
    refreshed = client.get(BASE + '/tasks?status=completed&limit=2', headers=auth_headers).json()
    assert [row['id'] for row in refreshed['tasks']] == [added.id, newest.id]
    exhausted = client.get(BASE + f'/tasks?status=completed&limit=2&before_id={oldest.id}', headers=auth_headers).json()
    assert exhausted == {'tasks': [], 'has_more': False, 'next_before_id': None}
    assert db_session.query(AuditLog).filter_by(resource_type='hank_task').count() == 0


def test_inbox_empty_filtered_page_does_not_count_unrelated_statuses(client, db_session, auth_headers, test_user):
    completed = _inbox_task(db_session, test_user, status='completed')
    for _ in range(4):
        _inbox_task(db_session, test_user, status='cancelled')
    db_session.commit()
    only_completed = client.get(BASE + '/tasks?status=completed&limit=1', headers=auth_headers).json()
    assert [row['id'] for row in only_completed['tasks']] == [completed.id]
    assert only_completed['has_more'] is False and only_completed['next_before_id'] is None
    assert client.get(BASE + '/tasks?status=awaiting_review&limit=1', headers=auth_headers).json() == {
        'tasks': [],
        'has_more': False,
        'next_before_id': None,
    }
    unfiltered = client.get(BASE + '/tasks?limit=5', headers=auth_headers).json()
    assert len(unfiltered['tasks']) == 5 and unfiltered['has_more'] is False
    assert [row['id'] for row in unfiltered['tasks']] == sorted(
        [row['id'] for row in unfiltered['tasks']], reverse=True
    )


def test_inbox_pages_and_receipts_are_company_user_credential_and_permission_scoped(
    client,
    db_session,
    auth_headers,
    test_user,
    admin_user,
):
    db_session.add(Company(id=2, slug='hank-inbox-foreign', name='Other inbox shop'))
    own_completed = _inbox_task(db_session, test_user, status='completed', label='Own completed')
    own_cancelled = _inbox_task(db_session, test_user, status='cancelled', label='Own cancelled')
    other_employee = _inbox_task(db_session, admin_user, status='completed', label='Other employee private')
    token_receipt = _inbox_task(
        db_session, test_user, status='completed', credential_key='api:777', label='API private'
    )
    other_company = _inbox_task(db_session, test_user, status='completed', company_id=2, label='Other company private')
    hidden_module = _inbox_task(
        db_session, test_user, status='completed', kind='draft_purchase_order', label='Purchasing private'
    )
    db_session.add(RolePermission(company_id=1, role=test_user.role, permissions=['work_orders:view']))
    db_session.commit()

    result = client.get(BASE + '/tasks?limit=2', headers=auth_headers).json()
    assert [row['id'] for row in result['tasks']] == [own_cancelled.id, own_completed.id]
    assert result['has_more'] is False and result['next_before_id'] is None
    filtered = client.get(BASE + '/tasks?status=completed&limit=1', headers=auth_headers).json()
    assert [row['id'] for row in filtered['tasks']] == [own_completed.id]
    assert filtered['has_more'] is False
    for row in (own_completed, own_cancelled):
        response = client.get(f'{BASE}/tasks/{row.id}', headers=auth_headers)
        assert response.status_code == 200 and response.json()['status'] == row.status
    for private in (other_employee, token_receipt, other_company):
        assert client.get(f'{BASE}/tasks/{private.id}', headers=auth_headers).status_code == 404
    assert client.get(f'{BASE}/tasks/{hidden_module.id}', headers=auth_headers).status_code == 403

    test_user._api_token_id = 777
    credential_service = HankTaskService(db_session, test_user, 1)
    assert [row.id for row in credential_service.list(status='completed', limit=1).tasks] == [token_receipt.id]
    assert credential_service.get(token_receipt.id).id == token_receipt.id
    for browser_task in (own_completed, own_cancelled):
        with pytest.raises(HTTPException) as missing:
            credential_service.get(browser_task.id)
        assert missing.value.status_code == 404
    test_user._api_token_id = None


def test_read_only_inbox_recovers_terminal_receipts_without_mutating_tasks(client, db_session, test_user):
    from app.core.security import create_access_token

    rows = [
        _inbox_task(db_session, test_user, status=status) for status in ('completed', 'cancelled', 'awaiting_review')
    ]
    db_session.commit()
    before = [(row.id, row.status, row.version, row.updated_at, row.last_checked_at, row.result_json) for row in rows]
    read_only_headers = {
        'Authorization': f'Bearer {create_access_token(subject=test_user.id, company_id=1, read_only=True)}'
    }
    response = client.get(BASE + '/tasks?limit=3', headers=read_only_headers)
    assert response.status_code == 200, response.text
    assert [row['status'] for row in response.json()['tasks']] == ['awaiting_review', 'cancelled', 'completed']
    assert client.get(BASE + '/capabilities', headers=read_only_headers).json()['can_write'] is False
    completed = client.get(BASE + '/tasks?status=completed', headers=read_only_headers).json()
    assert completed['tasks'][0]['result']['summary'].startswith('Finished')
    for row in rows:
        assert client.get(f'{BASE}/tasks/{row.id}', headers=read_only_headers).status_code == 200
    pending = rows[-1]
    for verb in ('execute', 'cancel'):
        refused = client.post(
            f'{BASE}/tasks/{pending.id}/{verb}',
            headers=read_only_headers,
            json={'expected_company_id': 1, 'expected_version': pending.version},
        )
        assert refused.status_code == 403
    db_session.rollback()
    for row in rows:
        db_session.refresh(row)
    after = [(row.id, row.status, row.version, row.updated_at, row.last_checked_at, row.result_json) for row in rows]
    assert after == before
    assert db_session.query(AuditLog).filter_by(resource_type='hank_task').count() == 0


@pytest.mark.parametrize('query', ['limit=0', 'limit=101', 'before_id=0', 'status=running', 'status=COMPLETE'])
def test_inbox_rejects_unbounded_or_unknown_filters(client, auth_headers, query):
    assert client.get(BASE + '/tasks?' + query, headers=auth_headers).status_code == 422
