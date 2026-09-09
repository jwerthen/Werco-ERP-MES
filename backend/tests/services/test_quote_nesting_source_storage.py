"""Storage identity and bounded reads without shared-adapter behavior changes."""

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from app.services import quote_nesting_source_storage as storage
from app.services import storage_service
from app.services.storage_service import S3StorageBackend

pytestmark = pytest.mark.unit


class FakeS3:
    def __init__(self, objects=None):
        self.objects = {} if objects is None else objects
        self.closed = 0

    def close(self):
        self.closed += 1

    def put_object(self, Bucket, Key, Body):
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        from io import BytesIO

        return {'Body': BytesIO(self.objects[(Bucket, Key)])}


@pytest.fixture
def remote(monkeypatch):
    objects = {}
    monkeypatch.setattr('boto3.client', lambda *a, **kw: FakeS3(objects))
    backend = S3StorageBackend(
        bucket='synthetic-cad',
        region='test-region',
        endpoint_url='https://synthetic.example.test/',
        access_key_id='test-only',
        secret_access_key='test-only',
    )
    storage_service.override_storage(backend)
    yield backend
    storage_service.reset_storage()


def test_remote_plan_reference_is_exact_and_excludes_credentials(remote):
    content = b'synthetic original\r\n'
    plan = storage.plan_object(7, 13)
    assert plan.storage_ref == f's3://synthetic-cad/7/nesting-cad/13/{plan.object_key}.dxf'
    assert len(plan.object_key) == 36
    assert plan.provider == {
        'kind': 's3',
        'endpoint': 'https://synthetic.example.test',
        'region': 'test-region',
        'bucket': 'synthetic-cad',
    }
    storage.save_once(plan, content)
    resolved = storage.resolve_object(
        7, 13, plan.object_key, plan.storage_ref, plan.provider, storage.digest(plan.provider)
    )
    assert storage.verified_bytes(resolved, len(content), hashlib.sha256(content).hexdigest()) == content


@pytest.mark.parametrize('change', ['company', 'intent', 'key', 'bucket', 'digest', 'endpoint', 'region'])
def test_foreign_or_changed_provider_reference_is_never_read(remote, change):
    plan = storage.plan_object(7, 13)
    company, intent, key, ref, provider_hash = 7, 13, plan.object_key, plan.storage_ref, storage.digest(plan.provider)
    if change == 'company':
        company = 8
    elif change == 'intent':
        intent = 14
    elif change == 'key':
        key = str(uuid4())
    elif change == 'bucket':
        ref = ref.replace('synthetic-cad', 'other-cad')
    elif change == 'digest':
        provider_hash = '0' * 64
    elif change == 'endpoint':
        remote.endpoint_url = 'https://different-provider.example.test'
    else:
        remote.region = 'different-region'
    with pytest.raises(storage.SourceStorageError):
        storage.resolve_object(company, intent, key, ref, plan.provider, provider_hash)


def test_local_symlink_cannot_redirect_generated_attempt(tmp_path, monkeypatch):
    monkeypatch.setenv('UPLOAD_DIR', str(tmp_path / 'root'))
    backend = storage_service.LocalStorageBackend()
    storage_service.override_storage(backend)
    try:
        plan = storage.plan_object(7, 13)
        outside = tmp_path / 'outside'
        outside.mkdir()
        directory = Path(plan.storage_ref).parent
        directory.parent.mkdir(parents=True)
        directory.symlink_to(outside, target_is_directory=True)
        with pytest.raises(storage.SourceStorageError, match='invalid_storage_reference'):
            storage.save_once(plan, b'private')
        assert list(outside.iterdir()) == []
    finally:
        storage_service.reset_storage()


def test_oversized_stream_stops_at_first_excess_chunk_and_closes(remote, monkeypatch):
    plan = storage.plan_object(7, 13)
    events = []

    def stream(_):
        try:
            yield b'12345'
            events.append('over-limit chunk consumed')
            yield b'67890'
            pytest.fail('must not consume unbounded stream after detecting overflow')
        finally:
            events.append('closed')

    monkeypatch.setattr(plan.backend, 'open_stream', stream)
    with pytest.raises(storage.SourceStorageError, match='stored_bytes_mismatch'):
        storage.verified_bytes(plan, 5, hashlib.sha256(b'12345').hexdigest())
    assert events == ['over-limit chunk consumed', 'closed']


def test_cad_s3_client_has_its_own_bounded_policy_without_changing_legacy(remote, monkeypatch):
    calls = []
    original = remote.client
    dedicated = FakeS3()

    def client(service, **kwargs):
        calls.append((service, kwargs))
        return dedicated

    monkeypatch.setattr('boto3.client', client)
    plan = storage.plan_object(7, 13)
    assert plan.backend is not remote
    assert plan.backend.client is dedicated
    assert remote.client is original
    assert len(calls) == 1
    service, arguments = calls[0]
    assert service == 's3'
    assert arguments['endpoint_url'] == remote.endpoint_url
    assert arguments['region_name'] == remote.region
    assert arguments['aws_access_key_id'] == remote.access_key_id
    assert arguments['aws_secret_access_key'] == remote.secret_access_key
    assert arguments['config'].connect_timeout == 3
    assert arguments['config'].read_timeout == 8
    assert arguments['config'].retries == {'total_max_attempts': 1, 'mode': 'standard'}
    plan.close()
    assert dedicated.closed == 1
    assert original.closed == 0


@pytest.mark.parametrize('operation', ['plan', 'resolve'])
def test_owned_client_is_closed_when_location_validation_fails(remote, monkeypatch, operation):
    plan = storage.plan_object(7, 13)
    dedicated = FakeS3()
    monkeypatch.setattr('boto3.client', lambda *a, **kw: dedicated)
    if operation == 'plan':
        remote.endpoint_url = 'https://user:password@synthetic.example.test/'
    else:
        remote.region = 'changed-region'
    with pytest.raises(storage.SourceStorageError):
        if operation == 'plan':
            storage.plan_object(7, 13)
        else:
            storage.resolve_object(
                7, 13, plan.object_key, plan.storage_ref, plan.provider, storage.digest(plan.provider)
            )
    assert dedicated.closed == 1
    assert remote.client.closed == 0
    plan.close()


def test_slow_stream_expires_between_chunks_and_closes_without_verifying(remote, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(storage.time, 'monotonic', lambda: clock[0])
    plan = storage.plan_object(7, 13)
    budget = storage.SourceIOBudget.start()
    events = []

    def stream(_):
        try:
            clock[0] += 59
            yield b'first'
            clock[0] += 2
            yield b'second'
            pytest.fail('expired reads must not request another chunk')
        finally:
            events.append('closed')

    monkeypatch.setattr(plan.backend, 'open_stream', stream)
    with pytest.raises(storage.SourceStorageError, match='storage_budget_exhausted'):
        storage.verified_bytes(plan, 11, hashlib.sha256(b'firstsecond').hexdigest(), budget)
    assert events == ['closed']


def test_expired_budget_never_starts_another_write_or_read(remote, monkeypatch):
    plan = storage.plan_object(7, 13)
    monkeypatch.setattr(storage.time, 'monotonic', lambda: 60.0)
    budget = storage.SourceIOBudget(60.0)
    monkeypatch.setattr(plan.backend, 'save', lambda *a, **kw: pytest.fail('expired write started'))
    monkeypatch.setattr(plan.backend, 'open_stream', lambda *a: pytest.fail('expired read started'))
    with pytest.raises(storage.SourceStorageError, match='storage_budget_exhausted'):
        storage.save_once(plan, b'original', budget)
    with pytest.raises(storage.SourceStorageError, match='storage_budget_exhausted'):
        storage.verified_bytes(plan, 8, hashlib.sha256(b'original').hexdigest(), budget)
