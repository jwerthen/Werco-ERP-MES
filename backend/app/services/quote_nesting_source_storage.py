"""Frozen, tracked CAD object locations around the unchanged legacy adapters.

The caller must commit every fresh attempt before save_once. No key is reused
by this application path, and this module never deletes or repairs an object.
"""

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

from app.services.storage_service import S3StorageBackend, StorageBackend, get_storage, resolve_upload_dir

SOURCE_IO_SECONDS = 60.0
SOURCE_CONNECT_SECONDS = 3
SOURCE_READ_SECONDS = 8


class SourceStorageError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SourceIOBudget:
    """Cooperative operation budget; an in-flight I/O call is not interrupted."""

    deadline: float

    @classmethod
    def start(cls) -> 'SourceIOBudget':
        return cls(time.monotonic() + SOURCE_IO_SECONDS)

    def check(self) -> None:
        if time.monotonic() >= self.deadline:
            raise SourceStorageError('storage_budget_exhausted')


class CadS3StorageBackend(S3StorageBackend):
    """Independent bounded SDK client; never mutate the shared legacy client."""

    def _create_client(self) -> Any:
        import boto3
        from botocore.config import Config

        return boto3.client(
            's3',
            region_name=self.region or None,
            endpoint_url=self.endpoint_url or None,
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
            config=Config(
                connect_timeout=SOURCE_CONNECT_SECONDS,
                read_timeout=SOURCE_READ_SECONDS,
                retries={'total_max_attempts': 1, 'mode': 'standard'},
            ),
        )

    def open_stream(self, ref: str) -> Iterator[bytes]:
        # Open lazily, so closing a not-yet-started iterator cannot leak a body
        # obtained before the caller's next budget check.
        bucket, key = self._bucket_key(ref)
        body = self.client.get_object(Bucket=bucket, Key=key)['Body']
        try:
            while True:
                chunk = body.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            body.close()


def _cad_backend() -> StorageBackend:
    source = get_storage()
    if not source.is_remote:
        return source
    if not isinstance(source, S3StorageBackend):
        raise SourceStorageError('provider_unavailable')
    return CadS3StorageBackend(
        bucket=source.bucket,
        region=source.region,
        endpoint_url=source.endpoint_url,
        access_key_id=source.access_key_id,
        secret_access_key=source.secret_access_key,
    )


def _close_backend(backend: StorageBackend | None) -> None:
    if isinstance(backend, CadS3StorageBackend):
        try:
            backend.client.close()
        except Exception:
            # Cleanup cannot erase a verified receipt or mask the original
            # storage/audit error. Shared legacy clients are never disposed.
            pass


def digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    ).hexdigest()


def provider_identity(backend: StorageBackend) -> dict:
    if not backend.is_remote:
        return {'kind': 'local', 'root': str(Path(resolve_upload_dir()).resolve())}
    endpoint = getattr(backend, 'endpoint_url', None) or ''
    if endpoint:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
            raise SourceStorageError('provider_unavailable')
        if parsed.query or parsed.fragment:
            raise SourceStorageError('provider_unavailable')
        endpoint = urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip('/'), '', ''))
    bucket = getattr(backend, 'bucket', None)
    if not isinstance(bucket, str) or not bucket or '/' in bucket:
        raise SourceStorageError('provider_unavailable')
    return {'kind': 's3', 'endpoint': endpoint, 'region': getattr(backend, 'region', None) or '', 'bucket': bucket}


@dataclass(frozen=True)
class ObjectPlan:
    backend: StorageBackend
    object_key: str
    storage_ref: str
    provider: dict

    def close(self) -> None:
        _close_backend(self.backend)


def plan_object(company_id: int, intent_id: int) -> ObjectPlan:
    backend = None
    try:
        backend = _cad_backend()
        provider = provider_identity(backend)
        key = str(uuid4())
        path_key = f'{company_id}/nesting-cad/{intent_id}/{key}.dxf'
        ref = f's3://{provider["bucket"]}/{path_key}' if backend.is_remote else str(Path(provider['root']) / path_key)
        if len(ref) > 2048:
            raise SourceStorageError('provider_unavailable')
        if not backend.is_remote and Path(ref).resolve() != Path(ref):
            raise SourceStorageError('invalid_storage_reference')
        return ObjectPlan(backend, key, ref, provider)
    except SourceStorageError:
        _close_backend(backend)
        raise
    except Exception as exc:
        _close_backend(backend)
        raise SourceStorageError('provider_unavailable') from exc


def resolve_object(
    company_id: int, intent_id: int, key: str, ref: str, provider: dict, provider_hash: str
) -> ObjectPlan:
    backend = None
    try:
        if str(UUID(key)) != key:
            raise SourceStorageError('invalid_storage_reference')
        if digest(provider) != provider_hash:
            raise SourceStorageError('invalid_storage_reference')
        backend = _cad_backend()
        if provider_identity(backend) != provider:
            raise SourceStorageError('provider_unavailable')
        path_key = f'{company_id}/nesting-cad/{intent_id}/{key}.dxf'
        expected = (
            f's3://{provider["bucket"]}/{path_key}' if backend.is_remote else str(Path(provider['root']) / path_key)
        )
        if expected != ref:
            raise SourceStorageError('invalid_storage_reference')
        if not backend.is_remote:
            path = Path(ref)
            if path.resolve() != path or not path.is_relative_to(Path(provider['root'])):
                raise SourceStorageError('invalid_storage_reference')
        return ObjectPlan(backend, key, ref, provider)
    except SourceStorageError:
        _close_backend(backend)
        raise
    except Exception as exc:
        _close_backend(backend)
        raise SourceStorageError('provider_unavailable') from exc


def save_once(plan: ObjectPlan, content: bytes, budget: SourceIOBudget | None = None) -> None:
    try:
        (budget or SourceIOBudget.start()).check()
        # The remote adapter expects its tenant key; legacy local save expects
        # the precommitted absolute path. No user filename enters either one.
        key = plan.storage_ref.split('/', 3)[3] if plan.backend.is_remote else plan.storage_ref
        if not plan.backend.is_remote and Path(key).resolve() != Path(key):
            raise SourceStorageError('invalid_storage_reference')
        if plan.backend.save(content, key=key) != plan.storage_ref:
            raise SourceStorageError('invalid_storage_reference')
    except SourceStorageError:
        raise
    except Exception as exc:
        raise SourceStorageError('storage_write_uncertain') from exc


def verified_bytes(
    plan: ObjectPlan, expected_count: int, expected_hash: str, budget: SourceIOBudget | None = None
) -> bytes:
    budget = budget or SourceIOBudget.start()
    chunks = []
    count = 0
    hasher = hashlib.sha256()
    iterator = None
    try:
        budget.check()
        iterator = iter(plan.backend.open_stream(plan.storage_ref))
        while True:
            budget.check()
            try:
                chunk = next(iterator)
            except StopIteration:
                break
            budget.check()
            count += len(chunk)
            if count > expected_count:
                raise SourceStorageError('stored_bytes_mismatch')
            hasher.update(chunk)
            chunks.append(chunk)
        budget.check()
        if count != expected_count or hasher.hexdigest() != expected_hash:
            raise SourceStorageError('stored_bytes_mismatch')
        return b''.join(chunks)
    except SourceStorageError:
        raise
    except FileNotFoundError as exc:
        raise SourceStorageError('stored_bytes_missing') from exc
    except Exception as exc:
        # Do not log vendor errors or paths. Absence, denial and timeout all
        # leave this attempt pending; none can establish source verification.
        raise SourceStorageError('storage_unavailable') from exc
    finally:
        close = getattr(iterator, 'close', None)
        if close:
            try:
                close()
            except Exception as exc:
                raise SourceStorageError('storage_unavailable') from exc
