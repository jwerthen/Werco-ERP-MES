"""Handoffs and procedures retain evidence without widening the employee's authority."""

import io
from uuid import uuid4

import pytest
from PIL import Image

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.hank_teamwork import HankHandoff, HankRoutine, HankRoutineRun
from app.models.notification import Notification
from app.models.role_permission import RolePermission
from app.models.user import User, UserRole
from app.schemas.hank_teamwork import HandoffCreate
from app.services.audit_service import AuditService, AuditWriteError
from app.services.hank_teamwork_service import HankTeamworkService

URL = '/api/v1/hank'
pytestmark = [pytest.mark.api]


def headers(user, **claims):
    return {'Authorization': f'Bearer {create_access_token(subject=user.id, company_id=user.company_id, **claims)}'}


def handoff_body(job, recipient, **changes):
    return {
        'expected_company_id': 1,
        'request_key': str(uuid4()),
        'work_order_id': job.id,
        'recipient_id': recipient.id,
        'summary': 'Second shift: finish the remaining brackets',
        'completed_work': '18 good parts',
        'remaining_work': 'Finish 12 and inspect',
        'problems': 'Check fixture before continuing',
        'quantity_remaining': 12,
        'document_ids': [],
        **changes,
    }


def command(row):
    return {'expected_company_id': 1, 'expected_version': row['version']}


def create(client, auth, body):
    response = client.post(URL + '/handoffs', headers=auth, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def routine_body(**changes):
    return {
        'expected_company_id': 1,
        'request_key': str(uuid4()),
        'title': 'Job review',
        'description': 'Before starting',
        'steps': [{'kind': 'readiness', 'title': 'Review gaps', 'instruction': 'Read all available evidence.'}],
        **changes,
    }


def make_routine(client, auth, **changes):
    response = client.post(URL + '/routines', headers=auth, json=routine_body(**changes))
    assert response.status_code == 200, response.text
    return response.json()


def approved_run(client, auth, job, **routine_changes):
    routine = make_routine(client, auth, **routine_changes)
    approved = client.post(URL + f'/routines/{routine["id"]}/approve', headers=auth, json=command(routine)).json()
    started = client.post(
        URL + f'/routines/{routine["id"]}/start',
        headers=auth,
        json={**command(approved), 'request_key': str(uuid4()), 'work_order_id': job.id},
    )
    assert started.status_code == 200, started.text
    return approved, started.json()


def test_handoff_lifecycle_is_recipient_acknowledged_and_audited(
    client, auth_headers, operator_headers, db_session, test_work_order, operator_user
):
    body = handoff_body(test_work_order, operator_user)
    row = create(client, auth_headers, body)
    assert row['status'] == 'open' and row['can_cancel'] and not row['can_acknowledge']
    assert create(client, auth_headers, body) == row
    assert db_session.query(HankHandoff).count() == 1
    assert db_session.query(Notification).count() == 1
    assert (
        client.post(URL + f'/handoffs/{row["id"]}/complete', headers=operator_headers, json=command(row)).status_code
        == 409
    )
    assert (
        client.post(URL + f'/handoffs/{row["id"]}/acknowledge', headers=auth_headers, json=command(row)).status_code
        == 403
    )
    ack = client.post(URL + f'/handoffs/{row["id"]}/acknowledge', headers=operator_headers, json=command(row))
    assert ack.status_code == 200, ack.text
    ack = ack.json()
    assert ack['status'] == 'acknowledged' and ack['acknowledged_at'].endswith('Z') and ack['can_complete']
    assert (
        client.post(URL + f'/handoffs/{row["id"]}/complete', headers=operator_headers, json=command(row)).status_code
        == 409
    )
    finished = client.post(URL + f'/handoffs/{row["id"]}/complete', headers=operator_headers, json=command(ack))
    assert finished.json()['status'] == 'completed'
    assert not finished.json()['can_cancel']
    assert db_session.query(AuditLog).filter_by(resource_type='hank_handoff').count() == 3
    assert db_session.query(Notification).count() == 3


def test_unrelated_employee_and_other_company_cannot_see_handoff(
    client, auth_headers, db_session, test_user, admin_user, test_work_order, operator_user
):
    row = create(client, auth_headers, handoff_body(test_work_order, operator_user))
    assert client.get(URL + f'/handoffs/{row["id"]}', headers=headers(admin_user)).status_code == 404
    assert client.get(URL + '/handoffs', headers=headers(admin_user)).json()['handoffs'] == []
    db_session.add(Company(id=2, name='Other', slug='other-handoff'))
    db_session.commit()
    body = handoff_body(test_work_order, operator_user, expected_company_id=2)
    assert client.post(URL + '/handoffs', headers=auth_headers, json=body).status_code == 409
    operator_user.company_id = 2
    db_session.commit()
    assert client.get(URL + f'/handoffs/{row["id"]}', headers=headers(operator_user)).status_code == 404
    assert db_session.query(HankHandoff).count() == 1


def test_handoff_source_identity_and_read_only_guards(
    client, auth_headers, db_session, test_user, test_work_order, operator_user
):
    assert (
        client.post(URL + '/handoffs', headers=auth_headers, json=handoff_body(test_work_order, test_user)).status_code
        == 422
    )
    assert (
        client.post(
            URL + '/handoffs',
            headers=headers(test_user, read_only=True),
            json=handoff_body(test_work_order, operator_user),
        ).status_code
        == 403
    )
    body = handoff_body(test_work_order, operator_user)
    row = create(client, auth_headers, body)
    assert (
        client.post(URL + '/handoffs', headers=auth_headers, json={**body, 'summary': 'Different'}).status_code == 409
    )
    test_work_order.is_deleted = True
    db_session.commit()
    assert client.get(URL + f'/handoffs/{row["id"]}', headers=auth_headers).status_code == 200
    assert (
        client.post(
            URL + '/handoffs', headers=auth_headers, json=handoff_body(test_work_order, operator_user)
        ).status_code
        == 404
    )
    assert (
        client.post(URL + f'/handoffs/{row["id"]}/cancel', headers=auth_headers, json=command(row)).status_code == 404
    )


@pytest.mark.parametrize('field,value', [('_api_token_id', 2), ('_token_scope', 'kiosk'), ('is_active', False)])
def test_service_rejects_noninteractive_handoff_writes(
    db_session, test_user, operator_user, test_work_order, field, value
):
    from fastapi import HTTPException

    setattr(test_user, field, value)
    with pytest.raises(HTTPException) as error:
        HankTeamworkService(db_session, test_user, 1).create_handoff(
            HandoffCreate(**handoff_body(test_work_order, operator_user)), AuditService(db_session, test_user)
        )
    assert error.value.status_code == 403
    assert db_session.query(HankHandoff).count() == 0


def test_required_handoff_audit_failure_rolls_back_notice_and_row(
    client, auth_headers, db_session, test_work_order, operator_user, monkeypatch
):
    def fail(*args, **kwargs):
        raise AuditWriteError('unavailable')

    monkeypatch.setattr(AuditService, 'log_required', fail)
    response = client.post(URL + '/handoffs', headers=auth_headers, json=handoff_body(test_work_order, operator_user))
    assert response.status_code == 503
    assert db_session.query(HankHandoff).count() == db_session.query(Notification).count() == 0


def test_people_list_only_active_same_company_names_and_current_permissions(
    client, auth_headers, db_session, operator_user, admin_user
):
    admin_user.is_active = False
    db_session.commit()
    response = client.get(URL + '/handoff-people', headers=auth_headers)
    assert [person['id'] for person in response.json()['people']] == [operator_user.id]
    assert 'email' not in response.text and 'hashed_password' not in response.text
    db_session.add(RolePermission(company_id=1, role=UserRole.OPERATOR, permissions=['inventory:view']))
    db_session.commit()
    assert client.get(URL + '/handoff-people', headers=auth_headers).json()['people'] == []


def photo_bytes():
    data = io.BytesIO()
    Image.new('RGB', (3, 3), 'yellow').save(data, format='PNG')
    return data.getvalue()


def test_handoff_photo_receipt_retry_download_and_other_user_refusal(
    client, auth_headers, db_session, test_work_order, operator_user, admin_user, monkeypatch, tmp_path
):
    monkeypatch.setenv('UPLOAD_DIR', str(tmp_path))
    row = create(client, auth_headers, handoff_body(test_work_order, operator_user))
    key = str(uuid4())
    payload = {**command(row), 'request_key': key}
    url = URL + f'/handoffs/{row["id"]}/attachments'
    response = client.post(
        url, headers=auth_headers, data=payload, files={'file': ('photo.png', photo_bytes(), 'image/png')}
    )
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved['version'] == 2 and len(saved['attachments']) == 1
    replay = client.post(
        url, headers=auth_headers, data=payload, files={'file': ('photo.png', photo_bytes(), 'image/png')}
    )
    assert replay.json() == saved
    download = client.get(saved['attachments'][0]['url'], headers=auth_headers)
    assert download.content == photo_bytes() and download.headers['cache-control'] == 'private, no-store'
    assert client.get(saved['attachments'][0]['url'], headers=headers(admin_user)).status_code == 404
    assert 'storage_ref' not in response.text and 'sha256' not in response.text
    assert len(list(tmp_path.rglob('*.png'))) == 1


def test_routine_draft_approval_snapshot_and_review_receipt(
    client, auth_headers, operator_headers, db_session, test_work_order
):
    routine, run = approved_run(client, auth_headers, test_work_order)
    assert run['status'] == 'active' and run['current_step'] == 0
    edit = {
        **command(routine),
        'title': 'Changed instructions',
        'description': '',
        'steps': [{'kind': 'checklist', 'title': 'A new step', 'instruction': 'Do something else'}],
    }
    changed = client.put(URL + f'/routines/{routine["id"]}', headers=auth_headers, json=edit)
    assert changed.json()['status'] == 'draft' and changed.json()['approved_by'] is None
    assert client.get(URL + '/routines', headers=operator_headers).json()['routines'] == []
    original = client.get(URL + f'/routine-runs/{run["id"]}', headers=auth_headers).json()
    assert original['title'] == routine['title'] and original['steps'] == routine['steps']
    assert (
        client.post(URL + f'/routine-runs/{run["id"]}/advance', headers=auth_headers, json=command(run)).status_code
        == 422
    )
    done = client.post(
        URL + f'/routine-runs/{run["id"]}/advance',
        headers=auth_headers,
        json={**command(run), 'note': 'Reviewed current material and instruction gaps.'},
    )
    assert done.status_code == 200, done.text
    assert done.json()['status'] == 'completed' and done.json()['results'][0]['note'].startswith('Reviewed')
    assert db_session.query(HankRoutineRun).count() == 1
    assert client.post(URL + '/routines', headers=operator_headers, json=routine_body()).status_code == 403


def test_routine_cannot_start_draft_or_skip_business_result(client, auth_headers, test_work_order):
    routine = make_routine(client, auth_headers)
    body = {**command(routine), 'request_key': str(uuid4()), 'work_order_id': test_work_order.id}
    assert client.post(URL + f'/routines/{routine["id"]}/start', headers=auth_headers, json=body).status_code == 409
    _, run = approved_run(
        client,
        auth_headers,
        test_work_order,
        steps=[{'kind': 'receive_delivery', 'title': 'Receive', 'instruction': 'Confirm receipt.'}],
    )
    response = client.post(
        URL + f'/routine-runs/{run["id"]}/advance',
        headers=auth_headers,
        json={**command(run), 'note': 'Pretend received'},
    )
    assert response.status_code == 422


def test_routine_handoff_step_requires_actual_recipient_completion(
    client, auth_headers, operator_headers, test_work_order, operator_user
):
    _, run = approved_run(
        client,
        auth_headers,
        test_work_order,
        steps=[{'kind': 'handoff', 'title': 'Handoff', 'instruction': 'Wait for recipient completion.'}],
    )
    handoff = create(client, auth_headers, handoff_body(test_work_order, operator_user))
    payload = {**command(run), 'handoff_id': handoff['id']}
    assert (
        client.post(URL + f'/routine-runs/{run["id"]}/advance', headers=auth_headers, json=payload).status_code == 422
    )
    ack = client.post(
        URL + f'/handoffs/{handoff["id"]}/acknowledge', headers=operator_headers, json=command(handoff)
    ).json()
    client.post(URL + f'/handoffs/{handoff["id"]}/complete', headers=operator_headers, json=command(ack))
    result = client.post(URL + f'/routine-runs/{run["id"]}/advance', headers=auth_headers, json=payload)
    assert result.status_code == 200, result.text
    assert result.json()['results'][0]['evidence'][0]['id'] == handoff['id']


def test_routine_required_audit_failure_and_version_conflict_preserve_definition(
    client, auth_headers, db_session, monkeypatch
):
    routine = make_routine(client, auth_headers)

    def fail(*args, **kwargs):
        raise AuditWriteError('unavailable')

    monkeypatch.setattr(AuditService, 'log_required', fail)
    response = client.post(URL + f'/routines/{routine["id"]}/approve', headers=auth_headers, json=command(routine))
    assert response.status_code == 503
    row = db_session.get(HankRoutine, routine['id'])
    assert row.status == 'draft' and row.version == 1 and row.approved_by is None


def test_queue_shows_actual_handoff_waiting_state_and_no_other_employee_work(
    client, auth_headers, operator_headers, test_work_order, operator_user, admin_user
):
    handoff = create(client, auth_headers, handoff_body(test_work_order, operator_user))
    sender = client.get(URL + '/work-queue?state=waiting_on_other', headers=auth_headers)
    assert sender.status_code == 200, sender.text
    assert sender.json()['items'][0]['key'] == f'handoff:{handoff["id"]}'
    assert client.get(URL + '/work-queue?state=waiting_on_you', headers=auth_headers).json()['items'] == []
    recipient = client.get(URL + '/work-queue?state=waiting_on_you', headers=operator_headers).json()['items']
    assert recipient[0]['status'] == 'open'
    assert client.get(URL + '/work-queue', headers=headers(admin_user)).json()['items'] == []


def completed_intake(db, owner, **plan_changes):
    """Seed a durable completed intake with the actual saved preview envelope."""
    from app.models.hank_intake import HankIntakeBatch, HankIntakeFile
    from app.schemas.hank_intake import IntakePlanInput

    batch = HankIntakeBatch(
        company_id=1,
        owner_id=owner.id,
        credential_key='user',
        request_key=str(uuid4()),
        request_hash='a' * 64,
    )
    db.add(batch)
    db.flush()
    plan = IntakePlanInput(title='Reviewed packet', document_type='other', **plan_changes)
    item = HankIntakeFile(
        company_id=1,
        batch_id=batch.id,
        ordinal=0,
        filename='reviewed.pdf',
        content_sha256='b' * 64,
        storage_ref='unused-for-receipt-evidence',
        file_size=100,
        status='completed',
        plan_json={'input': plan.model_dump(mode='json'), 'changes': [], 'warnings': [], 'references': []},
    )
    db.add(item)
    db.commit()
    return item


@pytest.mark.parametrize('matches', [True, False])
def test_routine_intake_evidence_matches_nested_saved_job_plan(
    client, auth_headers, db_session, test_user, test_work_order, matches
):
    _, run = approved_run(
        client,
        auth_headers,
        test_work_order,
        steps=[{'kind': 'document_intake', 'title': 'File source', 'instruction': 'Review and file this job PDF.'}],
    )
    item = completed_intake(
        db_session, test_user, work_order_id=test_work_order.id if matches else test_work_order.id + 999
    )
    response = client.post(
        URL + f'/routine-runs/{run["id"]}/advance',
        headers=auth_headers,
        json={**command(run), 'intake_file_id': item.id},
    )
    assert response.status_code == (200 if matches else 422), response.text
    stored = db_session.get(HankRoutineRun, run['id'])
    assert stored.status == ('completed' if matches else 'active')
    assert len(stored.results_json) == int(matches)


@pytest.mark.parametrize('matches', [True, False])
@pytest.mark.parametrize('receipt_only', [True, False])
def test_routine_intake_evidence_must_match_purchase_order_context(
    client, auth_headers, db_session, test_user, test_part, matches, receipt_only
):
    from app.models.purchasing import POReceipt, PurchaseOrder, PurchaseOrderLine, Vendor

    vendor = Vendor(company_id=1, code='ROUTINE', name='Routine supplier')
    db_session.add(vendor)
    db_session.flush()
    po = PurchaseOrder(company_id=1, po_number='ROUTINE-PO', vendor_id=vendor.id)
    db_session.add(po)
    db_session.commit()
    routine = make_routine(
        client,
        auth_headers,
        steps=[{'kind': 'document_intake', 'title': 'File delivery', 'instruction': 'Review this PO packet.'}],
    )
    approved = client.post(URL + f'/routines/{routine["id"]}/approve', headers=auth_headers, json=command(routine))
    assert approved.status_code == 200, approved.text
    started = client.post(
        URL + f'/routines/{routine["id"]}/start',
        headers=auth_headers,
        json={**command(approved.json()), 'request_key': str(uuid4()), 'purchase_order_id': po.id},
    )
    assert started.status_code == 200, started.text
    run = started.json()
    if receipt_only:
        receipt_po = po if matches else PurchaseOrder(company_id=1, po_number='OTHER-PO', vendor_id=vendor.id)
        db_session.add(receipt_po)
        db_session.flush()
        line = PurchaseOrderLine(
            company_id=1,
            purchase_order_id=receipt_po.id,
            part_id=test_part.id,
            line_number=1,
            quantity_ordered=1,
            unit_price=1,
        )
        db_session.add(line)
        db_session.flush()
        receipt = POReceipt(
            company_id=1,
            receipt_number='ROUTINE-RECEIPT',
            lot_number='ROUTINE-LOT',
            po_line_id=line.id,
            quantity_received=1,
            received_by=test_user.id,
        )
        db_session.add(receipt)
        db_session.flush()
        item = completed_intake(db_session, test_user, receipt_id=receipt.id)
    else:
        item = completed_intake(db_session, test_user, purchase_order_id=po.id if matches else po.id + 999)
    response = client.post(
        URL + f'/routine-runs/{run["id"]}/advance',
        headers=auth_headers,
        json={**command(run), 'intake_file_id': item.id},
    )
    assert response.status_code == (200 if matches else 422), response.text
    assert db_session.get(HankRoutineRun, run['id']).status == ('completed' if matches else 'active')


def test_routine_intake_evidence_rechecks_current_intake_authority(
    client, auth_headers, db_session, test_user, test_work_order
):
    _, run = approved_run(
        client,
        auth_headers,
        test_work_order,
        steps=[{'kind': 'document_intake', 'title': 'File source', 'instruction': 'Review and file source.'}],
    )
    item = completed_intake(db_session, test_user, work_order_id=test_work_order.id)
    test_user.role = UserRole.OPERATOR
    db_session.commit()
    response = client.post(
        URL + f'/routine-runs/{run["id"]}/advance',
        headers=auth_headers,
        json={**command(run), 'intake_file_id': item.id},
    )
    assert response.status_code == 403, response.text
    row = db_session.get(HankRoutineRun, run['id'])
    assert row.status == 'active' and row.version == 1 and row.results_json == []


def test_notice_insert_failure_rolls_back_handoff_and_required_audit(
    client, auth_headers, db_session, test_work_order, operator_user
):
    from sqlalchemy import event
    from sqlalchemy.exc import IntegrityError

    def fail_notice(*args, **kwargs):
        raise IntegrityError('notification insert', {}, RuntimeError('storage unavailable'))

    event.listen(Notification, 'before_insert', fail_notice)
    try:
        response = client.post(
            URL + '/handoffs', headers=auth_headers, json=handoff_body(test_work_order, operator_user)
        )
    finally:
        event.remove(Notification, 'before_insert', fail_notice)
    assert response.status_code == 409, response.text
    assert db_session.query(HankHandoff).count() == 0
    assert db_session.query(Notification).count() == 0
    assert db_session.query(AuditLog).filter_by(resource_type='hank_handoff').count() == 0


def test_photo_required_audit_failure_removes_uncommitted_blob_and_keeps_handoff(
    client, auth_headers, db_session, test_work_order, operator_user, monkeypatch, tmp_path
):
    monkeypatch.setenv('UPLOAD_DIR', str(tmp_path))
    row = create(client, auth_headers, handoff_body(test_work_order, operator_user))

    def fail(*args, **kwargs):
        raise AuditWriteError('unavailable')

    monkeypatch.setattr(AuditService, 'log_required', fail)
    response = client.post(
        URL + f'/handoffs/{row["id"]}/attachments',
        headers=auth_headers,
        data={**command(row), 'request_key': str(uuid4())},
        files={'file': ('photo.png', photo_bytes(), 'image/png')},
    )
    assert response.status_code == 503, response.text
    stored = db_session.get(HankHandoff, row['id'])
    assert stored.version == 1 and stored.attachments_json == []
    assert not list(tmp_path.rglob('*.png'))
    assert db_session.query(AuditLog).filter_by(resource_type='hank_handoff').count() == 1


def test_queue_filters_state_before_source_limit(
    client, auth_headers, db_session, test_user, operator_user, test_work_order
):
    from datetime import datetime, timedelta

    base = dict(
        company_id=1,
        sender_id=test_user.id,
        recipient_id=operator_user.id,
        sender_name='Sender',
        recipient_name='Recipient',
        work_order_id=test_work_order.id,
        work_order_number=test_work_order.work_order_number,
        request_hash='a' * 64,
        content_json={'summary': 'Review shift'},
    )
    wanted = HankHandoff(**base, request_key=str(uuid4()), updated_at=datetime.utcnow() - timedelta(days=1))
    db_session.add(wanted)
    db_session.add_all(HankHandoff(**base, request_key=str(uuid4()), status='completed') for _ in range(55))
    db_session.commit()
    response = client.get(URL + '/work-queue?state=waiting_on_other', headers=auth_headers)
    assert response.status_code == 200, response.text
    assert [item['id'] for item in response.json()['items']] == [wanted.id]
    assert response.json()['truncated'] is False
