"""Independent run API identity, isolation, audit atomicity and immutable reports."""

import hashlib
import json
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.db.database import atomic_transaction
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.quote_nesting_draft import QuoteNestingDraft, QuoteNestingRevision
from app.models.quote_nesting_run import QuoteNestingRun, QuoteNestingRunCheckpoint
from app.models.role_permission import RolePermission
from app.models.user import UserRole
from app.services import quote_nesting_runs as service
from app.services.audit_service import AuditService, AuditWriteError
from tests.api.test_quote_nesting_drafts_contract import project, upload

pytestmark = [pytest.mark.api, pytest.mark.integration]
BASE = '/api/v1/quote-nesting/runs'
RUNTIME = dict(
    release='synthetic-release',
    protocol=1,
    solver_version='werco-contour-v4',
    bundle_sha256='e' * 64,
    node_version='v22.20.0',
)


@pytest.fixture(autouse=True)
def runtime_and_queue(monkeypatch):
    readiness = AsyncMock(return_value={'available': True, 'identity': RUNTIME})
    dispatch = Mock()
    monkeypatch.setattr('app.api.endpoints.quote_nesting_runs.runtime_status', readiness)
    monkeypatch.setattr('app.services.quote_nesting_run_outbox.enqueue_job_best_effort', dispatch)
    monkeypatch.setattr('app.services.quote_nesting_run_outbox.enqueue_job_fire_and_forget_fastfail', AsyncMock())
    return readiness, dispatch


def start_body(saved):
    return dict(
        draft_id=saved['draft_id'],
        revision_number=saved['revision_number'],
        input_sha256=saved['content_sha256'],
        expected_company_id=saved['company_id'],
        request_key=str(uuid4()),
    )


def run_audits(db):
    return db.query(AuditLog).filter(AuditLog.resource_type == 'quote_nesting_run').all()


def test_old_revision_is_bound_exactly_and_unavailable_runtime_does_not_break_exact_retry(
    client, admin_headers, db_session, runtime_and_queue
):
    saved = upload(client, admin_headers).json()
    newer = upload(client, admin_headers, project('Newer inputs'), draft=saved['draft_id'], version=1)
    assert newer.status_code == 200
    body = start_body(saved)
    response = client.post(BASE, headers=admin_headers, json=body)
    assert response.status_code == 200, response.text
    run = response.json()
    assert run['revision_number'] == 1 and run['input_sha256'] == saved['content_sha256']
    assert run['status'] == 'QUEUED' and run['checkpoints'] == []
    assert run['settings']['approved'] is False and run['settings']['remnant_credit_usd'] == 0
    readiness, dispatch = runtime_and_queue
    dispatch.assert_called_once_with(
        'run_quote_nesting_job', company_id=1, run_id=run['id'], _job_id=f"quote-nesting:1:{run['id']}", fast_fail=True
    )
    readiness.return_value = {'available': False, 'identity': None}
    replay = client.post(BASE, headers=admin_headers, json=body)
    assert replay.status_code == 200 and replay.json() == run
    assert len(run_audits(db_session)) == 1 and dispatch.call_count == 2
    assert dispatch.call_args_list[0] == dispatch.call_args_list[1]
    listing = client.get(BASE, headers=admin_headers).json()
    assert len(listing['items']) == 1
    assert 'estimate' not in listing['items'][0] and 'settings' not in listing['items'][0]
    report = client.get(f"{BASE}/{run['id']}/report", headers=admin_headers).json()
    digest = report.pop('content_sha256')
    assert report['estimate'] == saved['estimate'] and report['status'] == 'UNAPPROVED'
    canonical = json.dumps(report, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    assert hashlib.sha256(canonical.encode()).hexdigest() == digest
    assert len(run_audits(db_session)) == 1


def test_start_requires_readiness_exact_hash_and_company_without_mutations(
    client, admin_headers, db_session, runtime_and_queue
):
    saved = upload(client, admin_headers).json()
    body = start_body(saved)
    readiness, dispatch = runtime_and_queue
    readiness.return_value = {'available': False, 'identity': None}
    assert client.post(BASE, headers=admin_headers, json=body).status_code == 503
    readiness.return_value = {'available': True, 'identity': RUNTIME}
    for change in ({'input_sha256': 'f' * 64}, {'expected_company_id': 2}):
        assert client.post(BASE, headers=admin_headers, json={**body, **change}).status_code == 409
    for change in ({'revision_number': True}, {'approved': True}, {'expected_company_id': '1'}):
        assert client.post(BASE, headers=admin_headers, json={**body, **change}).status_code == 422
    assert db_session.query(QuoteNestingRun).count() == 0 and run_audits(db_session) == []
    dispatch.assert_not_called()


def test_active_conflict_and_actor_content_bound_idempotency_never_create_extra_runs(client, admin_headers, db_session):
    saved = upload(client, admin_headers).json()
    body = start_body(saved)
    first = client.post(BASE, headers=admin_headers, json=body)
    assert first.status_code == 200, first.text
    assert client.post(BASE, headers=admin_headers, json={**body, 'request_key': str(uuid4())}).status_code == 409
    assert client.post(BASE, headers=admin_headers, json={**body, 'input_sha256': 'f' * 64}).status_code == 409
    assert db_session.query(QuoteNestingRun).count() == 1 and len(run_audits(db_session)) == 1


def test_failed_required_audit_rolls_back_start_and_cancel_and_dispatches_only_after_success(
    client, admin_headers, db_session, runtime_and_queue, monkeypatch
):
    saved = upload(client, admin_headers).json()
    body = start_body(saved)
    with monkeypatch.context() as patch:
        patch.setattr(AuditService, 'log', lambda *args, **kwargs: None)
        failed = client.post(BASE, headers=admin_headers, json=body)
    assert failed.status_code == 503
    assert db_session.query(QuoteNestingRun).count() == 0
    runtime_and_queue[1].assert_not_called()
    response = client.post(BASE, headers=admin_headers, json=body)
    assert response.status_code == 200, response.text
    run = response.json()
    cancel = dict(expected_company_id=1, expected_version=run['version'])
    with monkeypatch.context() as patch:
        patch.setattr(AuditService, 'log', lambda *args, **kwargs: None)
        assert client.post(f"{BASE}/{run['id']}/cancel", headers=admin_headers, json=cancel).status_code == 503
    db_session.expire_all()
    row = db_session.query(QuoteNestingRun).one()
    assert row.version == 1 and row.status == 'QUEUED' and row.cancel_requested is False
    assert len(run_audits(db_session)) == 1
    cancelled = client.post(f"{BASE}/{run['id']}/cancel", headers=admin_headers, json=cancel)
    assert cancelled.status_code == 200 and cancelled.json()['status'] == 'CANCELLED'
    replay = client.post(f"{BASE}/{run['id']}/cancel", headers=admin_headers, json=cancel)
    assert replay.status_code == 200 and replay.json() == cancelled.json()
    assert len(run_audits(db_session)) == 2
    assert db_session.query(QuoteNestingRevision).count() == 1


def test_effective_view_and_create_permissions_are_both_required_to_change_runs(client, admin_headers, db_session):
    saved = upload(client, admin_headers).json()
    permissions = RolePermission(company_id=1, role=UserRole.ADMIN, permissions=['purchasing:view'])
    db_session.add(permissions)
    db_session.commit()
    assert client.get(BASE, headers=admin_headers).status_code == 200
    assert client.post(BASE, headers=admin_headers, json=start_body(saved)).status_code == 403
    permissions.permissions = ['purchasing:create']
    db_session.commit()
    assert client.get(BASE, headers=admin_headers).status_code == 403
    assert client.post(BASE, headers=admin_headers, json=start_body(saved)).status_code == 403
    assert db_session.query(QuoteNestingRun).count() == 0


def test_foreign_run_and_revision_ids_do_not_disclose_inputs_or_allow_cancellation(
    client, admin_headers, admin_user, db_session
):
    other = Company(name='Synthetic other tenant', slug='run-other')
    db_session.add(other)
    db_session.flush()
    draft = QuoteNestingDraft(company_id=other.id, name='Foreign inputs', created_by=admin_user.id)
    db_session.add(draft)
    db_session.flush()
    from app.services.quote_nesting_drafts import canonical_json

    estimate = project('Foreign inputs')
    canonical = canonical_json(estimate)
    revision = QuoteNestingRevision(
        company_id=other.id,
        draft_id=draft.id,
        revision_number=1,
        draft_version=1,
        name='Foreign inputs',
        estimate_json=estimate,
        content_sha256=hashlib.sha256(canonical.encode()).hexdigest(),
        payload_schema_version=6,
        payload_bytes=len(canonical.encode()),
        created_by=admin_user.id,
        request_key=str(uuid4()),
        request_hash='a' * 64,
        review_issues_json=[],
    )
    db_session.add(revision)
    db_session.flush()
    row = QuoteNestingRun(
        company_id=other.id,
        draft_id=draft.id,
        revision_id=revision.id,
        revision_number=1,
        input_sha256=revision.content_sha256,
        request_key=str(uuid4()),
        request_hash='b' * 64,
        created_by=admin_user.id,
        settings_json={},
    )
    db_session.add(row)
    db_session.commit()
    for suffix in ('', '/report', '/checkpoints/1'):
        assert client.get(f'{BASE}/{row.id}{suffix}', headers=admin_headers).status_code == 404
    assert client.get(BASE, headers=admin_headers).json()['items'] == []
    assert (
        client.post(
            f'{BASE}/{row.id}/cancel', headers=admin_headers, json={'expected_company_id': 1, 'expected_version': 1}
        ).status_code
        == 404
    )
    body = dict(
        draft_id=draft.id,
        revision_number=1,
        input_sha256=revision.content_sha256,
        expected_company_id=1,
        request_key=str(uuid4()),
    )
    assert client.post(BASE, headers=admin_headers, json=body).status_code == 404
    assert db_session.query(QuoteNestingRun).count() == 1 and run_audits(db_session) == []


def test_runtime_binding_and_checkpoint_are_rolled_back_together_with_failed_audit(
    client, admin_headers, db_session, monkeypatch
):
    saved = upload(client, admin_headers).json()
    response = client.post(BASE, headers=admin_headers, json=start_body(saved))
    assert response.status_code == 200, response.text
    run_id = response.json()['id']
    with atomic_transaction(db_session):
        _, lease = service.claim_run(db_session, 1, run_id, runtime=RUNTIME)
    hello = {key: RUNTIME[key] for key in ('solver_version', 'bundle_sha256', 'node_version')}
    with monkeypatch.context() as patch:
        patch.setattr(AuditService, 'log', lambda *args, **kwargs: None)
        with pytest.raises(AuditWriteError), atomic_transaction(db_session):
            service.accept_hello(db_session, 1, run_id, lease, hello)
    row = db_session.query(QuoteNestingRun).one()
    assert row.version == 2 and row.bundle_sha256 is None
    with atomic_transaction(db_session):
        service.accept_hello(db_session, 1, run_id, lease, hello)
    quote = saved['estimate']['groups'][0]['quote']
    option = quote['options'][0]
    width, height = option['width'] * 25.4, option['height'] * 25.4
    message = dict(
        type='option',
        protocol=1,
        input_sha256=saved['content_sha256'],
        sequence=1,
        group_id='group',
        option_id='sheet',
        units='mm',
        requested=1,
        stock=dict(
            width=width,
            height=height,
            bedWidth=width,
            bedHeight=height,
            maxSheets=1,
            margin=quote['margin'] * 25.4,
            gap=quote['gap'] * 25.4,
            grainAxis='x',
        ),
        result=dict(
            option={**option, 'width': width, 'height': height},
            nest=None,
            error='Synthetic geometry review required',
            complete=False,
            area=0,
            cost=None,
        ),
    )
    with monkeypatch.context() as patch:
        patch.setattr(AuditService, 'log', lambda *args, **kwargs: None)
        with pytest.raises(AuditWriteError), atomic_transaction(db_session):
            service.append_checkpoint(db_session, 1, run_id, lease, message)
    db_session.expire_all()
    assert db_session.query(QuoteNestingRunCheckpoint).count() == 0
    row = db_session.query(QuoteNestingRun).one()
    assert row.version == 3 and row.evaluated_count == row.checkpoint_bytes == 0
    with atomic_transaction(db_session):
        service.append_checkpoint(db_session, 1, run_id, lease, message)
    row = db_session.query(QuoteNestingRun).one()
    assert row.evaluated_count == 1 and row.completed_count == 0
    before = row.version
    with pytest.raises(ValueError, match='worker_lost'), atomic_transaction(db_session):
        service.heartbeat(db_session, 1, run_id, str(uuid4()))
    assert db_session.query(QuoteNestingRun).one().version == before
    report = client.get(f'{BASE}/{run_id}/report', headers=admin_headers)
    assert report.status_code == 200 and report.json()['status'] == 'UNAPPROVED'
    for event in run_audits(db_session):
        assert 'estimate' not in event.new_values and 'result' not in event.new_values
