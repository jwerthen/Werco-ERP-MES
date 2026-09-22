"""Personal one-shot follows: live authority, truthful evidence and atomic notifications."""

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import sessionmaker

from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.document import Document, DocumentType
from app.models.hank import HankTask
from app.models.notification import Notification, NotificationLog
from app.models.role_permission import RolePermission
from app.models.work_order import WorkOrder
from app.models.work_order_blocker import WorkOrderBlocker
from app.schemas.hank_watches import HankWatchCreate
from app.services import hank_watch_service as watches
from app.services.audit_service import AuditService, AuditWriteError

BASE = '/api/v1/hank'
NOW = datetime(2026, 9, 22, 12)


@pytest.fixture(autouse=True)
def watch_clock(monkeypatch, db_session):
    monkeypatch.setattr(watches, '_now', lambda value=None: value or NOW)
    monkeypatch.setattr(watches, 'SessionLocal', sessionmaker(bind=db_session.get_bind()))


def start(client, headers, work_order, condition='pdf_attached', **kwargs):
    return client.post(
        BASE + '/watches',
        headers=headers,
        json={
            'expected_company_id': kwargs.pop('expected_company_id', 1),
            'request_key': kwargs.pop('request_key', str(uuid4())),
            'work_order_id': work_order.id,
            'condition': condition,
            **kwargs,
        },
    )


def command(client, headers, task, verb, **kwargs):
    return client.post(
        f'{BASE}/watches/{task["id"]}/{verb}',
        headers=headers,
        json={
            'expected_company_id': kwargs.get('expected_company_id', 1),
            'expected_version': kwargs.get('expected_version', task['version']),
        },
    )


def pdf(db, work_order=None, *, company=1, kind=DocumentType.MATERIAL_CERT, number=None, created_at=None):
    row = Document(
        company_id=company,
        document_number=number or str(uuid4()),
        title='Material evidence',
        document_type=kind,
        status='draft',
        revision='B',
        file_name='evidence.pdf',
        mime_type='application/pdf',
        work_order_id=work_order.id if work_order else None,
        created_at=created_at or NOW,
    )
    db.add(row)
    db.flush()
    return row


def blocker(db, work_order, *, company=1, status='open'):
    row = WorkOrderBlocker(company_id=company, work_order_id=work_order.id, title='Material missing', status=status)
    db.add(row)
    db.flush()
    return row


def test_explicit_watch_creation_is_idempotent_and_not_an_action_proposal(
    client, db_session, auth_headers, test_work_order
):
    key = str(uuid4())
    first = start(client, auth_headers, test_work_order, request_key=key)
    assert first.status_code == 200, first.text
    task = first.json()
    assert task['kind'] == 'watch_work_order' and task['status'] == 'watching'
    assert task['last_checked_at'] is None and task['snoozed_until'] is None
    assert start(client, auth_headers, test_work_order, request_key=key).json() == task
    assert db_session.query(HankTask).count() == 1
    assert db_session.query(Notification).count() == 0
    assert db_session.query(AuditLog).filter_by(resource_type='hank_task').count() == 1
    assert client.get(BASE + '/tasks?status=watching', headers=auth_headers).json()['tasks'][0]['id'] == task['id']
    invalid = client.post(
        BASE + '/tasks',
        headers=auth_headers,
        json={
            'expected_company_id': 1,
            'request_key': str(uuid4()),
            'kind': 'watch_work_order',
            'input': {},
        },
    )
    assert invalid.status_code == 422
    assert command(client, auth_headers, task, 'check').json()['status'] == 'watching'
    assert db_session.query(AuditLog).filter_by(resource_type='hank_task').count() == 1
    assert db_session.query(Notification).count() == 0


def test_later_attachment_of_older_pdf_completes_once_with_metadata_caveat(
    client, db_session, auth_headers, test_work_order, test_user
):
    already_linked = pdf(db_session, test_work_order)
    older_unlinked = pdf(db_session, created_at=datetime(2000, 1, 1))
    db_session.commit()
    task = start(client, auth_headers, test_work_order, document_type='material_cert').json()
    saved = db_session.get(HankTask, task['id'])
    assert saved.source_versions_json['document_ids'] == [already_linked.id]
    assert command(client, auth_headers, task, 'check').json()['status'] == 'watching'
    older_unlinked.work_order_id = test_work_order.id
    db_session.commit()
    completed = command(client, auth_headers, task, 'check')
    assert completed.status_code == 200, completed.text
    receipt = completed.json()
    assert receipt['status'] == 'completed' and receipt['version'] == 2
    assert receipt['result']['references'][0]['id'] == older_unlinked.id
    assert 'status draft' in receipt['result']['summary']
    assert 'not verified' in receipt['result']['warnings'][0]
    notice = db_session.query(Notification).one()
    assert notice.company_id == 1 and notice.user_id == test_user.id
    assert notice.event_key == 'hank.follow_up' and notice.link == f'/?hank_task={task["id"]}'
    assert db_session.query(NotificationLog).count() == 0
    assert command(client, auth_headers, task, 'check').json() == receipt
    assert watches.process_hank_watches(now=NOW + timedelta(minutes=6))['checked'] == 0
    assert db_session.query(Notification).count() == 1


def test_pdf_type_filter_and_foreign_document_do_not_trigger(client, db_session, auth_headers, test_work_order):
    db_session.add(Company(id=2, name='Foreign PDF shop', slug='watch-other-pdf'))
    db_session.commit()
    task = start(client, auth_headers, test_work_order, document_type='material_cert').json()
    pdf(db_session, test_work_order, kind=DocumentType.DRAWING)
    pdf(db_session, test_work_order, company=2)
    db_session.commit()
    assert command(client, auth_headers, task, 'check').json()['status'] == 'watching'
    assert db_session.query(Notification).count() == 0


def test_blocker_watch_requires_open_evidence_and_waits_for_all_current_blockers(
    client, db_session, auth_headers, test_work_order
):
    assert start(client, auth_headers, test_work_order, 'blockers_cleared').status_code == 409
    initial = blocker(db_session, test_work_order)
    db_session.commit()
    task = start(client, auth_headers, test_work_order, 'blockers_cleared').json()
    later = blocker(db_session, test_work_order, status='acknowledged')
    initial.status = 'resolved'
    db_session.commit()
    assert command(client, auth_headers, task, 'check').json()['status'] == 'watching'
    later.status = 'dismissed'
    db_session.commit()
    completed = command(client, auth_headers, task, 'check').json()
    assert completed['status'] == 'completed'
    assert 'no open or acknowledged blockers' in completed['result']['summary']
    assert 'does not establish' in completed['result']['warnings'][0]
    assert test_work_order.status.value == 'draft'  # The watch never releases production.


def test_snooze_preserves_baseline_and_worker_resumes_after_one_hour(client, db_session, auth_headers, test_work_order):
    task = start(client, auth_headers, test_work_order).json()
    snoozed_response = command(client, auth_headers, task, 'snooze')
    assert snoozed_response.status_code == 200, snoozed_response.text
    snoozed = snoozed_response.json()
    assert snoozed['status'] == 'snoozed' and snoozed['snoozed_until'] == '2026-09-22T13:00:00Z'
    assert command(client, auth_headers, task, 'snooze').json() == snoozed
    assert command(client, auth_headers, snoozed, 'check').status_code == 409
    pdf(db_session, test_work_order)
    db_session.commit()
    assert watches.process_hank_watches(now=NOW + timedelta(minutes=59))['checked'] == 0
    assert db_session.query(Notification).count() == 0
    result = watches.process_hank_watches(now=NOW + timedelta(hours=1, minutes=1))
    assert result == {'checked': 1, 'completed': 1, 'needs_attention': 0, 'failed': 0}
    db_session.expire_all()
    assert db_session.get(HankTask, task['id']).status == 'completed'
    assert db_session.query(Notification).count() == 1


def test_resume_cancel_and_version_guards(client, db_session, auth_headers, test_work_order):
    task = start(client, auth_headers, test_work_order).json()
    snoozed = command(client, auth_headers, task, 'snooze').json()
    assert command(client, auth_headers, snoozed, 'resume', expected_version=9).status_code == 409
    resumed = command(client, auth_headers, snoozed, 'resume').json()
    assert resumed['status'] == 'watching' and resumed['snoozed_until'] is None
    assert command(client, auth_headers, snoozed, 'resume').json() == resumed
    cancelled = command(client, auth_headers, resumed, 'cancel').json()
    assert cancelled['status'] == 'cancelled'
    assert command(client, auth_headers, resumed, 'cancel').json() == cancelled
    assert command(client, auth_headers, cancelled, 'resume').status_code == 409
    assert watches.process_hank_watches(now=NOW + timedelta(hours=2))['checked'] == 0


def test_operator_can_watch_but_other_owner_cannot_recover(
    client, db_session, operator_headers, auth_headers, operator_user, test_work_order
):
    capability = client.get(BASE + '/capabilities', headers=operator_headers).json()
    assert capability['can_watch'] is True
    assert capability['allowed_kinds'] == ['report_production']
    task = start(client, operator_headers, test_work_order).json()
    assert client.get(f'{BASE}/tasks/{task["id"]}', headers=auth_headers).status_code == 404
    assert command(client, auth_headers, task, 'check').status_code == 404
    assert client.get(BASE + '/tasks?status=watching', headers=auth_headers).json()['tasks'] == []
    assert client.get(BASE + '/tasks?status=watching', headers=operator_headers).json()['tasks'][0]['id'] == task['id']


@pytest.mark.parametrize(
    'marker,value',
    [('_api_token_id', 55), ('_token_scope', 'api'), ('_token_scope', 'kiosk'), ('_read_only_company_context', True)],
)
def test_noninteractive_or_readonly_context_cannot_start_watch(db_session, test_user, test_work_order, marker, value):
    setattr(test_user, marker, value)
    service = watches.HankWatchService(db_session, test_user, 1)
    assert service.tasks.capabilities().can_watch is False
    with pytest.raises(HTTPException) as refused:
        service.prepare(
            HankWatchCreate(
                expected_company_id=1,
                request_key=str(uuid4()),
                work_order_id=test_work_order.id,
                condition='pdf_attached',
            ),
            AuditService(db_session, user=test_user, company_id=1),
        )
    assert refused.value.status_code == 403
    assert db_session.query(HankTask).count() == 0


@pytest.mark.parametrize('revocation', ['user', 'company', 'permissions', 'membership', 'credential'])
def test_worker_stops_quietly_after_authority_revoked(
    client, db_session, auth_headers, test_user, test_work_order, revocation
):
    task = start(client, auth_headers, test_work_order).json()
    pdf(db_session, test_work_order)
    if revocation == 'user':
        test_user.is_active = False
    elif revocation == 'company':
        db_session.get(Company, 1).is_active = False
    elif revocation == 'permissions':
        db_session.add(RolePermission(company_id=1, role=test_user.role, permissions=[]))
    elif revocation == 'membership':
        db_session.add(Company(id=2, slug='moved-watch-owner', name='New employer'))
        test_user.company_id = 2
    else:
        db_session.get(HankTask, task['id']).credential_key = 'api:888'
    db_session.commit()
    result = watches.process_hank_watches(now=NOW + timedelta(minutes=6))
    assert result == {'checked': 1, 'completed': 0, 'needs_attention': 1, 'failed': 0}
    db_session.expire_all()
    saved = db_session.get(HankTask, task['id'])
    assert saved.status == 'needs_attention' and saved.result_json is None
    assert 'no longer available' in saved.error_message
    assert db_session.query(Notification).count() == 0
    assert watches.process_hank_watches(now=NOW + timedelta(minutes=12))['checked'] == 0


@pytest.mark.parametrize('missing', ['deleted', 'foreign'])
def test_source_disappearance_is_quiet_and_cannot_expose_other_company(
    client, db_session, auth_headers, test_work_order, missing
):
    task = start(client, auth_headers, test_work_order).json()
    if missing == 'deleted':
        test_work_order.is_deleted = True
    else:
        db_session.add(Company(id=2, slug='foreign-watch-source', name='Foreign shop'))
        foreign = WorkOrder(
            company_id=2, work_order_number='SECRET-JOB', part_id=test_work_order.part_id, quantity_ordered=1
        )
        db_session.add(foreign)
        db_session.flush()
        pdf(db_session, foreign, company=2, number='SECRET-DOC')
        saved = db_session.get(HankTask, task['id'])
        saved.input_json = {**saved.input_json, 'work_order_id': foreign.id}
    db_session.commit()
    checked = command(client, auth_headers, task, 'check')
    assert checked.status_code == 200, checked.text
    assert checked.json()['status'] == 'needs_attention'
    assert 'SECRET' not in checked.text
    assert db_session.query(Notification).count() == 0


@pytest.mark.parametrize('mode', ['manual', 'worker'])
def test_required_watch_audit_failure_rolls_back_receipt_and_notification(
    client, db_session, auth_headers, test_work_order, monkeypatch, mode
):
    task = start(client, auth_headers, test_work_order).json()
    pdf(db_session, test_work_order)
    db_session.commit()
    original = AuditService.log_required

    def fail(self, action, resource_type, **kwargs):
        if resource_type == 'hank_task' and action == 'UPDATE':
            raise AuditWriteError('Required watch audit unavailable')
        return original(self, action, resource_type, **kwargs)

    monkeypatch.setattr(AuditService, 'log_required', fail)
    if mode == 'manual':
        assert command(client, auth_headers, task, 'check').status_code == 503
    else:
        assert watches.process_hank_watches(now=NOW + timedelta(minutes=6))['failed'] == 1
    db_session.expire_all()
    saved = db_session.get(HankTask, task['id'])
    assert saved.status == 'watching' and saved.version == 1 and saved.last_checked_at is None
    assert db_session.query(Notification).count() == 0
    monkeypatch.setattr(AuditService, 'log_required', original)
    assert command(client, auth_headers, task, 'check').json()['status'] == 'completed'
    assert db_session.query(Notification).count() == 1


def test_unchanged_worker_checks_are_bounded_spaced_and_silent(client, db_session, auth_headers, test_work_order):
    ids = [start(client, auth_headers, test_work_order).json()['id'] for _ in range(3)]
    assert watches.process_hank_watches(limit=2, now=NOW)['checked'] == 2
    assert watches.process_hank_watches(limit=2, now=NOW + timedelta(minutes=1))['checked'] == 1
    assert watches.process_hank_watches(now=NOW + timedelta(minutes=2))['checked'] == 0
    db_session.expire_all()
    assert all(db_session.get(HankTask, row_id).version == 1 for row_id in ids)
    assert db_session.query(Notification).count() == 0
    assert db_session.query(AuditLog).filter_by(resource_type='hank_task').count() == 3


def test_watch_scope_and_malformed_requests_are_refused(client, db_session, auth_headers, test_work_order):
    assert start(client, auth_headers, test_work_order, expected_company_id=2).status_code == 409
    assert (
        start(
            client, auth_headers, test_work_order, condition='blockers_cleared', document_type='material_cert'
        ).status_code
        == 422
    )
    assert start(client, auth_headers, test_work_order, request_key='not-a-uuid').status_code == 422
    db_session.add(Company(id=2, slug='foreign-start', name='Foreign'))
    foreign = WorkOrder(
        company_id=2, work_order_number='SECRET-WO', part_id=test_work_order.part_id, quantity_ordered=1
    )
    db_session.add(foreign)
    db_session.commit()
    assert start(client, auth_headers, foreign).status_code == 404
    assert db_session.query(HankTask).count() == 0


def test_failed_oldest_watch_backs_off_without_starving_later_work(
    client, db_session, auth_headers, test_work_order, monkeypatch
):
    first = start(client, auth_headers, test_work_order).json()
    second = start(client, auth_headers, test_work_order).json()
    evaluate = watches._evaluate_locked

    def fail_first(db, task, owner, audit, now):
        if task.id == first['id']:
            raise RuntimeError('A source check failed')
        return evaluate(db, task, owner, audit, now)

    monkeypatch.setattr(watches, '_evaluate_locked', fail_first)
    assert watches.process_hank_watches(limit=1, now=NOW)['failed'] == 1
    db_session.expire_all()
    saved = db_session.get(HankTask, first['id'])
    assert saved.error_code == watches.CHECK_FAILED
    assert saved.last_checked_at is None and saved.version == 1
    assert watches.process_hank_watches(limit=1, now=NOW + timedelta(minutes=1))['checked'] == 1
    db_session.expire_all()
    assert db_session.get(HankTask, second['id']).last_checked_at is not None
    monkeypatch.setattr(watches, '_evaluate_locked', evaluate)
    # Once eligible, the earlier failed attempt gets its retry instead of being
    # permanently ranked behind continuously due healthy work.
    assert watches.process_hank_watches(limit=1, now=NOW + timedelta(minutes=31))['checked'] == 1
    db_session.expire_all()
    saved = db_session.get(HankTask, first['id'])
    assert saved.error_code is None and saved.error_message is None and saved.last_checked_at is not None
    assert saved.version == 1
    assert db_session.query(Notification).count() == 0


def test_failed_check_diagnostic_cannot_overwrite_newer_employee_command(
    client, db_session, auth_headers, test_work_order
):
    task = start(client, auth_headers, test_work_order).json()
    cancelled = command(client, auth_headers, task, 'cancel').json()
    watches._record_failed_check(task['id'], 1, 'watching', None, NOW + timedelta(minutes=1))
    db_session.expire_all()
    saved = db_session.get(HankTask, task['id'])
    assert saved.status == 'cancelled' and saved.version == cancelled['version'] and saved.error_code is None


def test_notification_insert_failure_rolls_back_terminal_task_and_audit(
    client, db_session, auth_headers, test_work_order
):
    from sqlalchemy import event

    task = start(client, auth_headers, test_work_order).json()
    pdf(db_session, test_work_order)
    db_session.commit()

    def refuse_notification(_mapper, _connection, _target):
        raise RuntimeError('Notification insertion unavailable')

    event.listen(Notification, 'before_insert', refuse_notification)
    try:
        result = watches.process_hank_watches(now=NOW + timedelta(minutes=6))
        assert result['failed'] == 1 and result['completed'] == 0
    finally:
        event.remove(Notification, 'before_insert', refuse_notification)
    db_session.expire_all()
    saved = db_session.get(HankTask, task['id'])
    assert saved.status == 'watching' and saved.version == 1 and saved.result_json is None
    assert db_session.query(Notification).count() == 0
    assert db_session.query(AuditLog).filter_by(resource_type='hank_task').count() == 1
    assert command(client, auth_headers, task, 'check').json()['status'] == 'completed'
    assert db_session.query(Notification).count() == 1


def test_postgres_claim_skips_locked_rows_and_rechecks_due_state(db_session):
    from sqlalchemy.dialects import postgresql

    statement = str(watches._claim_query(db_session, 42, NOW).statement.compile(dialect=postgresql.dialect()))
    assert 'FOR UPDATE SKIP LOCKED' in statement
    assert 'hank_tasks.status IN' in statement
    assert 'hank_tasks.last_checked_at IS NULL' in statement
    assert 'hank_tasks.snoozed_until IS NULL' in statement
    assert 'hank_tasks.error_code' in statement


def test_watch_limits_apply_to_new_and_resumed_work(client, db_session, auth_headers, test_work_order, monkeypatch):
    # Lower the fixed constants to exercise the same boundary without creating
    # hundreds of irrelevant source rows or test-only business effects.
    monkeypatch.setattr(watches, 'WATCH_LIMIT', 2)
    first = start(client, auth_headers, test_work_order).json()
    second = start(client, auth_headers, test_work_order).json()
    assert start(client, auth_headers, test_work_order).status_code == 409
    assert command(client, auth_headers, second, 'snooze').status_code == 200
    assert start(client, auth_headers, test_work_order).status_code == 409  # Snoozed still consumes a slot.
    saved = db_session.get(HankTask, first['id'])
    saved.status = 'needs_attention'
    db_session.commit()
    assert start(client, auth_headers, test_work_order).status_code == 200
    assert command(client, auth_headers, first, 'resume').status_code == 409
    monkeypatch.setattr(watches, 'BASELINE_LIMIT', 2)
    other = WorkOrder(company_id=1, part_id=test_work_order.part_id, work_order_number='MANY-PDFS', quantity_ordered=1)
    db_session.add(other)
    db_session.flush()
    for _ in range(3):
        pdf(db_session, other)
    db_session.commit()
    monkeypatch.setattr(watches, 'WATCH_LIMIT', 50)
    response = start(client, auth_headers, other)
    assert response.status_code == 409 and 'baseline limit' in response.text


def test_eligible_failure_retries_under_a_full_healthy_batch_without_starving_new_work(
    client,
    db_session,
    auth_headers,
    test_work_order,
    test_user,
    admin_user,
    operator_user,
):
    first = start(client, auth_headers, test_work_order).json()
    watches._record_failed_check(first['id'], 1, 'watching', None, NOW)
    failed = db_session.get(HankTask, first['id'])
    # One hundred healthy watches span three employees (each below the50watch cap).
    # Their recent checks remain due every five minutes; class-priority sorting
    # would prevent the eligible older failure from ever receiving a retry.
    healthy_ids = []
    owners = (test_user, admin_user, operator_user)
    for index in range(100):
        row = HankTask(
            company_id=1,
            owner_id=owners[index % 3].id,
            credential_key='user',
            request_key=str(uuid4()),
            request_hash='b' * 64,
            kind='watch_work_order',
            title='Healthy recurring watch',
            status='watching',
            version=1,
            input_json=dict(failed.input_json),
            preview_json=dict(failed.preview_json),
            source_versions_json=dict(failed.source_versions_json),
            created_at=NOW,
            updated_at=NOW + timedelta(minutes=25),
            last_checked_at=NOW + timedelta(minutes=25),
        )
        db_session.add(row)
        db_session.flush()
        healthy_ids.append(row.id)
    db_session.commit()
    result = watches.process_hank_watches(limit=100, now=NOW + timedelta(minutes=31))
    assert result == {'checked': 100, 'completed': 0, 'needs_attention': 0, 'failed': 0}
    db_session.expire_all()
    recovered = db_session.get(HankTask, first['id'])
    assert recovered.error_code is None and recovered.last_checked_at == NOW + timedelta(minutes=31)
    checked_healthy = (
        db_session.query(HankTask)
        .filter(
            HankTask.id.in_(healthy_ids),
            HankTask.last_checked_at == NOW + timedelta(minutes=31),
        )
        .count()
    )
    assert checked_healthy == 99
    assert watches.process_hank_watches(limit=100, now=NOW + timedelta(minutes=32))['checked'] == 1
    assert db_session.query(Notification).count() == 0
