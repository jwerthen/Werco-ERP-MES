"""Original-byte storage/recovery API with synthetic immutable draft evidence."""

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.quote_nesting_draft import QuoteNestingRevision
from app.models.quote_nesting_source import QuoteNestingSourceAttempt as Attempt
from app.models.quote_nesting_source import QuoteNestingSourceBinding as Binding
from app.models.quote_nesting_source import QuoteNestingSourceIntent as Intent
from app.models.quote_nesting_source import QuoteNestingSourceReceipt as Receipt
from app.models.role_permission import RolePermission
from app.models.user import User, UserRole
from app.services import quote_nesting_source_storage as source_storage
from app.services import storage_service
from app.services.audit_service import AuditService
from tests.api.test_quote_nesting_drafts_contract import project, upload

pytestmark = [pytest.mark.api, pytest.mark.integration]
ORIGINAL = b'\xef\xbb\xbf0\r\nSECTION\r\n999\r\ncaf\xc3\xa9\xff\r\n0\r\nENDSEC\r\n0\r\nEOF\r\n'
SOURCE_HASH = hashlib.sha256(ORIGINAL).hexdigest()


@pytest.fixture
def local_storage(tmp_path, monkeypatch):
    monkeypatch.setenv('UPLOAD_DIR', str(tmp_path))
    backend = storage_service.LocalStorageBackend()
    storage_service.override_storage(backend)
    yield backend
    storage_service.reset_storage()


@pytest.fixture
def saved(client, admin_headers, local_storage):
    value = project()
    part = value['groups'][0]['quote']['parts'][0]
    part['revision'] = 'A'
    part['provenance'] = {
        'version': 1,
        'sourceName': 'synthetic.dxf',
        'sourceSha256': SOURCE_HASH,
        'sourceHashBasis': 'original-bytes',
        'geometrySha256': '1' * 64,
        'geometryVersion': 'werco-geometry-v1',
        'sourceUnits': 'in',
        'resolvedUnits': 'in',
        'unitDecision': 'declared',
        'importerVersion': 'werco-dxf-v2',
        'warnings': [],
    }
    second = copy.deepcopy(part)
    second.update(id='second', name='Second profile')
    second['provenance']['geometrySha256'] = '2' * 64
    value['groups'][0]['quote']['parts'].append(second)
    result = upload(client, admin_headers, value)
    assert result.status_code == 200, result.text
    record = result.json()
    path = f'/api/v1/quote-nesting/drafts/{record["draft_id"]}/revisions/1/sources'
    body = {
        'expected_company_id': 1,
        'request_key': str(uuid4()),
        'expected_input_sha256': record['content_sha256'],
        'source_sha256': SOURCE_HASH,
        'byte_count': len(ORIGINAL),
        'source_name': 'synthetic.dxf',
        'mime_type': 'application/dxf',
        'targets': [{'group_id': 'group', 'part_id': name} for name in ('part', 'second')],
    }
    return path, body, record


def intent(client, headers, saved):
    path, command, _ = saved
    result = client.post(path, json=command, headers=headers)
    assert result.status_code == 200, result.text
    return result.json()


def send(client, headers, path, intent_id, content=ORIGINAL):
    return client.post(
        f'{path}/{intent_id}/content?expected_company_id=1',
        content=content,
        headers={**headers, 'Content-Type': 'application/octet-stream'},
    )


def finish(client, headers, path, intent_id):
    return client.post(f'{path}/{intent_id}/finalize', json={'expected_company_id': 1}, headers=headers)


def test_exact_bytes_multiple_profile_binding_and_no_io_with_db_transaction(
    client,
    admin_headers,
    db_session,
    saved,
    local_storage,
    monkeypatch,
):
    path, command, original_revision = saved
    pending = intent(client, admin_headers, saved)
    assert pending['state'] == 'PENDING' and pending['receipt'] is None
    assert db_session.query(Attempt).count() == 0
    original_save = local_storage.save
    original_stream = local_storage.open_stream

    def save_checked(data, *, key):
        assert not db_session.in_transaction()
        with Session(db_session.get_bind()) as independent:
            attempt = independent.query(Attempt).one()
            assert attempt.storage_ref == key
            assert (
                independent.query(AuditLog).filter(AuditLog.resource_type == 'quote_nesting_source_attempt').count()
                == 1
            )
            assert independent.query(Receipt).count() == 0
        return original_save(data, key=key)

    def stream_checked(ref):
        assert not db_session.in_transaction()
        return original_stream(ref)

    monkeypatch.setattr(local_storage, 'save', save_checked)
    monkeypatch.setattr(local_storage, 'open_stream', stream_checked)
    result = send(client, admin_headers, path, pending['id'])
    assert result.status_code == 200, result.text
    attached = result.json()
    assert attached['state'] == 'ATTACHED'
    assert attached['receipt']['claim'] == 'server_hash_verified_unapproved'
    assert attached['receipt']['source_sha256'] == SOURCE_HASH
    assert attached['attempt_count'] == 1 and not attached['can_resume']
    assert db_session.query(Binding).count() == 2
    assert {t['provenance']['geometrySha256'] for t in attached['targets']} == {'1' * 64, '2' * 64}
    assert all(t['provenance']['reportedRevision'] == 'A' for t in attached['targets'])
    assert db_session.query(QuoteNestingRevision).one().estimate_json == original_revision['estimate']
    read = client.get(f'{path}/{pending["id"]}/download', headers=admin_headers)
    assert read.status_code == 200 and read.content == ORIGINAL
    assert read.headers['cache-control'] == 'private, no-store'
    assert read.headers['x-content-type-options'] == 'nosniff'
    assert 'attachment' in read.headers['content-disposition']
    replay = client.post(path, json=command, headers=admin_headers)
    assert replay.status_code == 200 and replay.json() == attached
    assert finish(client, admin_headers, path, pending['id']).json() == attached
    assert db_session.query(Attempt).count() == 1
    assert db_session.query(AuditLog).filter(AuditLog.resource_type.like('quote_nesting_source_%')).count() == 3


def test_timeout_after_write_recovers_same_attempt_without_second_upload(
    client, admin_headers, saved, local_storage, monkeypatch
):
    path = saved[0]
    pending = intent(client, admin_headers, saved)
    real_save = local_storage.save
    calls = []

    def uncertain(data, *, key):
        calls.append(key)
        real_save(data, key=key)
        raise TimeoutError('synthetic transport uncertainty')

    monkeypatch.setattr(local_storage, 'save', uncertain)
    result = send(client, admin_headers, path, pending['id'])
    assert result.status_code == 200, result.text
    assert len(calls) == 1
    assert finish(client, admin_headers, path, pending['id']).json() == result.json()
    assert len(calls) == 1


def test_partial_attempt_is_preserved_and_explicit_retry_gets_fresh_audited_key(
    client,
    admin_headers,
    saved,
    local_storage,
    monkeypatch,
    db_session,
):
    path = saved[0]
    pending = intent(client, admin_headers, saved)
    real_save = local_storage.save
    keys = []

    def partial(data, *, key):
        keys.append(key)
        real_save(data[:8], key=key)
        raise OSError('synthetic partial local write')

    monkeypatch.setattr(local_storage, 'save', partial)
    assert send(client, admin_headers, path, pending['id']).status_code == 503
    assert finish(client, admin_headers, path, pending['id']).status_code == 503
    assert db_session.query(Receipt).count() == 0
    monkeypatch.setattr(local_storage, 'save', real_save)
    done = send(client, admin_headers, path, pending['id'])
    assert done.status_code == 200, done.text
    attempts = db_session.query(Attempt).order_by(Attempt.ordinal).all()
    assert len(attempts) == 2 and attempts[0].storage_ref != attempts[1].storage_ref
    assert local_storage.read_bytes(attempts[0].storage_ref) == ORIGINAL[:8]
    assert local_storage.read_bytes(attempts[1].storage_ref) == ORIGINAL


def test_recovery_budget_stops_across_attempts_without_allocating_or_writing(
    client, admin_headers, saved, local_storage, monkeypatch, db_session
):
    pending = intent(client, admin_headers, saved)
    real_save = local_storage.save
    monkeypatch.setattr(local_storage, 'save', lambda data, *, key: real_save(data[:8], key=key))
    for _ in range(3):
        assert send(client, admin_headers, saved[0], pending['id']).status_code == 503
    assert db_session.query(Attempt).count() == 3
    audit_count = db_session.query(AuditLog).count()
    clock = [0.0]
    reads = []
    monkeypatch.setattr(source_storage, 'time', SimpleNamespace(monotonic=lambda: clock[0]))

    def slow_read(ref):
        reads.append(ref)
        clock[0] += 31
        raise TimeoutError('synthetic slow provider')

    monkeypatch.setattr(local_storage, 'open_stream', slow_read)
    monkeypatch.setattr(local_storage, 'save', lambda *a, **kw: pytest.fail('no write after recovery expiry'))
    result = send(client, admin_headers, saved[0], pending['id'])
    assert result.status_code == 503, result.text
    assert 'budget expired' in result.json()['detail']
    assert len(reads) == 2
    assert db_session.query(Attempt).count() == 3
    assert db_session.query(Receipt).count() == 0
    assert db_session.query(AuditLog).count() == audit_count


def test_budget_expiring_during_provider_planning_creates_no_attempt(
    client, admin_headers, saved, local_storage, monkeypatch, db_session
):
    pending = intent(client, admin_headers, saved)
    clock = [0.0]
    monkeypatch.setattr(source_storage, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    real_plan = source_storage.plan_object

    def slow_plan(*args):
        result = real_plan(*args)
        clock[0] = 61
        return result

    monkeypatch.setattr(source_storage, 'plan_object', slow_plan)
    monkeypatch.setattr(local_storage, 'save', lambda *a, **kw: pytest.fail('expired plan must not write'))
    result = send(client, admin_headers, saved[0], pending['id'])
    assert result.status_code == 503, result.text
    assert db_session.query(Attempt).count() == 0
    assert db_session.query(Receipt).count() == 0


def test_write_finishing_after_budget_remains_tracked_and_later_finalize_does_not_rewrite(
    client, admin_headers, saved, local_storage, monkeypatch, db_session
):
    pending = intent(client, admin_headers, saved)
    clock = [0.0]
    monkeypatch.setattr(source_storage, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    real_save = local_storage.save
    keys = []

    def slow_save(data, *, key):
        keys.append(key)
        result = real_save(data, key=key)
        clock[0] += 61
        return result

    monkeypatch.setattr(local_storage, 'save', slow_save)
    result = send(client, admin_headers, saved[0], pending['id'])
    assert result.status_code == 503, result.text
    assert db_session.query(Attempt).count() == 1
    assert db_session.query(Receipt).count() == 0
    recovered = finish(client, admin_headers, saved[0], pending['id'])
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()['state'] == 'ATTACHED'
    assert db_session.query(Receipt).count() == 1
    assert len(keys) == 1


@pytest.mark.parametrize('failure', [None, 'attempt', 'receipt', 'read'])
def test_dedicated_remote_clients_close_on_success_and_each_failure_boundary(
    client, admin_headers, saved, monkeypatch, failure
):
    from tests.services.test_quote_nesting_source_storage import FakeS3

    objects = {}
    clients = []

    def fake_client(*args, **kwargs):
        result = FakeS3(objects)
        clients.append(result)
        return result

    monkeypatch.setattr('boto3.client', fake_client)
    backend = storage_service.S3StorageBackend(
        bucket='synthetic-cad',
        region='test',
        endpoint_url='https://synthetic.example.test',
        access_key_id='test-only',
        secret_access_key='test-only',
    )
    storage_service.override_storage(backend)
    pending = intent(client, admin_headers, saved)
    original_log = AuditService.log

    def fail_audit(self, *args, **kwargs):
        if failure in ('attempt', 'receipt') and kwargs.get('resource_type') == 'quote_nesting_source_' + failure:
            return None
        return original_log(self, *args, **kwargs)

    monkeypatch.setattr(AuditService, 'log', fail_audit)
    if failure == 'read':
        monkeypatch.setattr(FakeS3, 'get_object', lambda *a, **kw: (_ for _ in ()).throw(TimeoutError()))
    result = send(client, admin_headers, saved[0], pending['id'])
    assert result.status_code == (200 if failure is None else 503), result.text
    assert len(clients) == 2
    assert clients[0].closed == 0
    assert clients[1].closed == 1
    if failure is None:
        downloaded = client.get(f'{saved[0]}/{pending["id"]}/download', headers=admin_headers)
        assert downloaded.status_code == 200 and downloaded.content == ORIGINAL
        assert clients[-1].closed == 1
        assert clients[0].closed == 0


@pytest.mark.parametrize('stage', ['intent', 'attempt', 'receipt'])
def test_required_audit_failure_leaves_only_previously_committed_phases(
    client,
    admin_headers,
    saved,
    db_session,
    monkeypatch,
    stage,
):
    path, body, _ = saved
    pending = None if stage == 'intent' else intent(client, admin_headers, saved)
    original_log = AuditService.log

    def fail(self, *args, **kwargs):
        if kwargs.get('resource_type') == 'quote_nesting_source_' + stage:
            return None
        return original_log(self, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(AuditService, 'log', fail)
        result = (
            client.post(path, json=body, headers=admin_headers)
            if pending is None
            else send(client, admin_headers, path, pending['id'])
        )
    assert result.status_code == 503, result.text
    assert db_session.query(Intent).count() == (0 if stage == 'intent' else 1)
    assert db_session.query(Attempt).count() == (1 if stage == 'receipt' else 0)
    assert db_session.query(Receipt).count() == db_session.query(Binding).count() == 0
    if stage == 'receipt':
        recovered = finish(client, admin_headers, path, pending['id'])
        assert recovered.status_code == 200, recovered.text
        assert db_session.query(Attempt).count() == 1


def test_wrong_bytes_do_not_create_attempt_and_completed_replay_does_not_read_storage(
    client,
    admin_headers,
    saved,
    db_session,
    local_storage,
    monkeypatch,
):
    path, command, _ = saved
    pending = intent(client, admin_headers, saved)
    assert send(client, admin_headers, path, pending['id'], ORIGINAL + b'wrong').status_code == 409
    assert db_session.query(Attempt).count() == 0
    attached = send(client, admin_headers, path, pending['id']).json()
    monkeypatch.setattr(local_storage, 'open_stream', lambda _: (_ for _ in ()).throw(RuntimeError('offline')))
    assert client.post(path, json=command, headers=admin_headers).json() == attached
    assert finish(client, admin_headers, path, pending['id']).json() == attached
    assert client.get(f'{path}/{pending["id"]}/download', headers=admin_headers).status_code == 503


def test_eight_attempt_limit_and_pure_list_never_probes_storage(
    client, admin_headers, saved, local_storage, monkeypatch, db_session
):
    path = saved[0]
    pending = intent(client, admin_headers, saved)
    monkeypatch.setattr(local_storage, 'save', lambda *a, **kw: (_ for _ in ()).throw(OSError('unavailable')))
    for _ in range(8):
        assert send(client, admin_headers, path, pending['id']).status_code == 503
    assert send(client, admin_headers, path, pending['id']).status_code == 409
    assert db_session.query(Attempt).count() == 8
    monkeypatch.setattr(source_storage, 'resolve_object', lambda *a: pytest.fail('list must not read storage'))
    result = client.get(path, headers=admin_headers)
    assert result.status_code == 200 and result.json()['items'][0]['attempt_count'] == 8


@pytest.mark.parametrize(
    'change',
    [
        {'source_sha256': 'f' * 64},
        {'expected_input_sha256': 'f' * 64},
        {'targets': [{'group_id': 'other', 'part_id': 'part'}]},
    ],
)
def test_exact_revision_and_target_conflicts_are_rejected(client, admin_headers, saved, db_session, change):
    path, body, _ = saved
    result = client.post(path, json={**body, **change}, headers=admin_headers)
    assert result.status_code == 409
    assert db_session.query(Intent).count() == db_session.query(Attempt).count() == 0


@pytest.mark.parametrize(
    'change',
    [
        {'source_name': '../synthetic.dxf'},
        {'source_name': 'program.nc'},
        {'byte_count': True},
        {'byte_count': 5000000},
        {'source_sha256': None},
        {'mime_type': None},
        {'targets': []},
    ],
)
def test_strict_command_structure_rejects_unsafe_metadata(client, admin_headers, saved, change):
    path, body, _ = saved
    assert client.post(path, json={**body, **change}, headers=admin_headers).status_code == 422


def test_tenant_actor_company_and_effective_permission_fences(client, admin_headers, admin_user, saved, db_session):
    path, body, _ = saved
    pending = intent(client, admin_headers, saved)
    db_session.add(Company(id=2, name='Synthetic second company', slug='synthetic-second'))
    db_session.flush()
    other = User(
        company_id=2,
        email='source-other@example.test',
        employee_id='SOURCE-OTHER',
        first_name='Other',
        last_name='Synthetic',
        hashed_password='unused',
        role=UserRole.ADMIN,
        is_active=True,
    )
    db_session.add(other)
    db_session.commit()
    foreign = {'Authorization': 'Bearer ' + create_access_token(subject=other.id, company_id=2)}
    assert client.get(path, headers=foreign).status_code == 404
    assert client.get(f'{path}/{pending["id"]}/download', headers=foreign).status_code == 404
    assert (
        client.post(f'{path}/{pending["id"]}/finalize', json={'expected_company_id': 2}, headers=foreign).status_code
        == 404
    )
    assert client.post(path, json={**body, 'expected_company_id': 2}, headers=admin_headers).status_code == 409
    assert (
        client.post(
            f'{path}/{pending["id"]}/content?expected_company_id=2',
            content=ORIGINAL,
            headers={**admin_headers, 'Content-Type': 'application/octet-stream'},
        ).status_code
        == 409
    )
    for claims in ({'read_only': True, 'company_id': 1}, {'scope': 'kiosk'}):
        guarded = {'Authorization': 'Bearer ' + create_access_token(subject=admin_user.id, **claims)}
        assert finish(client, guarded, path, pending['id']).status_code == 403
    permission = RolePermission(company_id=1, role=UserRole.ADMIN, permissions=['purchasing:view'])
    db_session.add(permission)
    db_session.commit()
    page = client.get(path, headers=admin_headers).json()
    assert page['can_attach'] is False and page['items'][0]['can_resume'] is False
    assert send(client, admin_headers, path, pending['id']).status_code == 403
    permission.permissions = ['inventory:view', 'inventory:create']
    db_session.commit()
    assert client.get(path, headers=admin_headers).status_code == 403
    assert db_session.query(Attempt).count() == 0


def test_same_actor_requires_exact_api_credential_and_revocation_fences_completion(
    client,
    admin_headers,
    admin_user,
    saved,
    db_session,
):
    path, body, _ = saved
    tokens = []
    for label in ('Synthetic source A', 'Synthetic source B'):
        result = client.post(
            '/api/v1/api-tokens/', json={'user_id': admin_user.id, 'label': label}, headers=admin_headers
        )
        assert result.status_code == 201, result.text
        tokens.append(result.json())
    headers = {'Authorization': 'Bearer ' + tokens[0]['token']}
    pending = client.post(path, json=body, headers=headers).json()
    assert pending['submitted_api_token_id'] == tokens[0]['id']
    for wrong in (admin_headers, {'Authorization': 'Bearer ' + tokens[1]['token']}):
        assert client.post(path, json=body, headers=wrong).status_code == 409
        assert finish(client, wrong, path, pending['id']).status_code == 403
    attached = send(client, headers, path, pending['id'])
    assert attached.status_code == 200, attached.text
    assert attached.json()['receipt']['submitted_api_token_id'] == tokens[0]['id']
    for row in db_session.query(AuditLog).filter(AuditLog.resource_type.like('quote_nesting_source_%')):
        assert row.extra_data['credential']['api_token_id'] == tokens[0]['id']
        assert 'source_name' not in row.new_values and 'provenance' not in row.new_values
    result = client.post(
        f'/api/v1/api-tokens/{tokens[0]["id"]}/revoke', json={'reason': 'Synthetic done'}, headers=admin_headers
    )
    assert result.status_code == 200
    assert finish(client, headers, path, pending['id']).status_code in (401, 403)
    assert db_session.query(Receipt).count() == 1


def test_authority_is_rechecked_after_storage_io_without_erasing_tracked_bytes(
    client,
    admin_headers,
    saved,
    local_storage,
    monkeypatch,
    db_session,
):
    path = saved[0]
    pending = intent(client, admin_headers, saved)
    real_save = local_storage.save

    def revoke_write(data, *, key):
        result = real_save(data, key=key)
        with Session(db_session.get_bind()) as independent:
            independent.add(RolePermission(company_id=1, role=UserRole.ADMIN, permissions=['purchasing:view']))
            independent.commit()
        return result

    monkeypatch.setattr(local_storage, 'save', revoke_write)
    assert send(client, admin_headers, path, pending['id']).status_code == 403
    assert db_session.query(Attempt).count() == 1
    assert db_session.query(Receipt).count() == db_session.query(Binding).count() == 0
    attempt = db_session.query(Attempt).one()
    assert local_storage.read_bytes(attempt.storage_ref) == ORIGINAL


@pytest.mark.parametrize('replacement', [b'', ORIGINAL[:-1], ORIGINAL + b'oversized', b'x' * len(ORIGINAL)])
def test_corrupt_completed_bytes_never_leave_download_endpoint(client, admin_headers, saved, db_session, replacement):
    path = saved[0]
    pending = intent(client, admin_headers, saved)
    assert send(client, admin_headers, path, pending['id']).status_code == 200
    attempt = db_session.query(Attempt).one()
    Path(attempt.storage_ref).write_bytes(replacement)
    result = client.get(f'{path}/{pending["id"]}/download', headers=admin_headers)
    assert result.status_code == 503
    assert result.content != replacement
    assert client.get(path, headers=admin_headers).json()['items'][0]['state'] == 'ATTACHED'


def test_provider_change_does_not_read_same_key_in_different_root(
    client, admin_headers, saved, db_session, tmp_path, monkeypatch
):
    path = saved[0]
    pending = intent(client, admin_headers, saved)
    assert send(client, admin_headers, path, pending['id']).status_code == 200
    attempt = db_session.query(Attempt).one()
    monkeypatch.setenv('UPLOAD_DIR', str(tmp_path / 'different-provider-root'))
    assert client.get(f'{path}/{pending["id"]}/download', headers=admin_headers).status_code == 503
    assert Path(attempt.storage_ref).read_bytes() == ORIGINAL


def test_unexpected_save_reference_never_completes_even_when_planned_bytes_exist(
    client,
    admin_headers,
    saved,
    local_storage,
    monkeypatch,
    db_session,
):
    path = saved[0]
    pending = intent(client, admin_headers, saved)
    original_save = local_storage.save

    def wrong_ref(data, *, key):
        original_save(data, key=key)
        return '/foreign/object.dxf'

    monkeypatch.setattr(local_storage, 'save', wrong_ref)
    assert send(client, admin_headers, path, pending['id']).status_code == 503
    assert db_session.query(Receipt).count() == 0


def test_competing_intents_cannot_replace_a_completed_part_binding(client, admin_headers, saved, db_session):
    path, body, _ = saved
    first = intent(client, admin_headers, saved)
    second_result = client.post(path, json={**body, 'request_key': str(uuid4())}, headers=admin_headers)
    assert second_result.status_code == 200
    assert send(client, admin_headers, path, first['id']).status_code == 200
    result = send(client, admin_headers, path, second_result.json()['id'])
    assert result.status_code == 409
    assert db_session.query(Receipt).count() == 1 and db_session.query(Binding).count() == 2
    assert db_session.query(Attempt).count() == 2


def test_duplicate_json_and_legacy_text_hash_cannot_create_source_intent(client, admin_headers, saved, db_session):
    path, body, record = saved
    duplicate = json.dumps(body)[:-1] + ',"expected_company_id":1}'
    assert (
        client.post(path, content=duplicate, headers={**admin_headers, 'Content-Type': 'application/json'}).status_code
        == 422
    )
    value = copy.deepcopy(record['estimate'])
    value['groups'][0]['quote']['parts'][0]['provenance']['sourceHashBasis'] = 'utf8-text'
    newer = upload(client, admin_headers, value).json()
    newer_path = f'/api/v1/quote-nesting/drafts/{newer["draft_id"]}/revisions/1/sources'
    result = client.post(
        newer_path, json={**body, 'expected_input_sha256': newer['content_sha256']}, headers=admin_headers
    )
    assert result.status_code == 409
    assert db_session.query(Intent).count() == 0


def test_target_evidence_and_page_budgets_limit_response_amplification(client, admin_headers, saved, db_session):
    _, body, record = saved
    value = copy.deepcopy(record['estimate'])
    original = value['groups'][0]['quote']['parts'][0]
    original['provenance']['warnings'] = ['Synthetic warning ' + ('w' * 1950)] * 100
    value['groups'][0]['quote']['parts'] = [{**copy.deepcopy(original), 'id': str(i)} for i in range(4)]
    newer_response = upload(client, admin_headers, value)
    assert newer_response.status_code == 200, newer_response.text
    newer = newer_response.json()
    path = f'/api/v1/quote-nesting/drafts/{newer["draft_id"]}/revisions/1/sources'
    command = {
        **body,
        'expected_input_sha256': newer['content_sha256'],
        'targets': [{'group_id': 'group', 'part_id': str(i)} for i in range(4)],
    }
    rejected = client.post(path, json=command, headers=admin_headers)
    assert rejected.status_code == 413, rejected.text
    assert db_session.query(Intent).count() == 0
    command['targets'] = command['targets'][:1]
    assert client.post(path, json=command, headers=admin_headers).status_code == 200
    assert client.get(path, params={'per_page': 11}, headers=admin_headers).status_code == 422
