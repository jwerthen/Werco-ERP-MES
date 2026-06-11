"""Durable file-storage layer for persistent document bytes.

Production (Railway) has no persistent volume, so anything written to local disk is
lost on redeploy -- an AS9100D/CMMC record-retention gap for quality documents,
carrier labels/BOLs, RFQ package files, and uploaded PO source documents. This module
provides a swappable storage backend behind one seam:

- ``LocalStorageBackend`` -- today's behavior, byte-for-byte. Write sites keep their
  legacy on-disk layouts (absolute paths under ``UPLOAD_DIR`` or repo-relative
  ``uploads/...`` paths) and the stored reference IS the filesystem path, so every
  existing DB row remains valid.
- ``S3StorageBackend`` -- boto3 against AWS S3 or any S3-compatible store (Railway
  buckets / Cloudflare R2 via ``S3_ENDPOINT_URL``). Stored reference format is
  ``s3://{bucket}/{key}``.

Reference dispatch is PER-ROW: a ref starting with ``s3://`` is served from S3,
anything else is a local path. Legacy local rows therefore stay servable even after
ops flips ``STORAGE_BACKEND=s3`` (and vice versa).

Keys for new remote writes are tenant-prefixed and never user-controlled, e.g.
``{company_id}/documents/{uuid}{ext}``. Access control stays where it is today: the
``Document`` / ``RfqPackageFile`` / ``PurchaseOrder`` rows are tenant-scoped and the
endpoints that resolve a ref enforce that scoping before any byte is served.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import uuid
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional, Tuple

from app.core.config import settings

logger = logging.getLogger(__name__)

S3_REF_PREFIX = "s3://"
_STREAM_CHUNK_SIZE = 64 * 1024
_SAFE_EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,10}$")


def resolve_upload_dir() -> str:
    """Resolve the local upload root (single source of truth).

    Previously duplicated in ``app/api/endpoints/documents.py`` and
    ``app/services/shipping_service.py``; the env-fallback semantics are preserved:
    prefer ``UPLOAD_DIR`` (default ``/app/uploads``), fall back to a local writable
    directory (``UPLOAD_DIR_FALLBACK``, default ``./uploads``) for tests/dev.
    """
    preferred_dir = os.getenv("UPLOAD_DIR", "/app/uploads")
    try:
        os.makedirs(preferred_dir, exist_ok=True)
        return preferred_dir
    except OSError:
        fallback_dir = os.path.abspath(os.getenv("UPLOAD_DIR_FALLBACK", "./uploads"))
        os.makedirs(fallback_dir, exist_ok=True)
        return fallback_dir


def is_s3_ref(ref: Optional[str]) -> bool:
    """True when a stored reference points at object storage rather than local disk."""
    return bool(ref) and str(ref).startswith(S3_REF_PREFIX)


def sanitize_ext(filename: Optional[str]) -> str:
    """Extract a safe, lowercase extension for use inside a storage key.

    Storage keys must never be user-controlled; anything that doesn't look like a
    plain alphanumeric extension is dropped.
    """
    ext = Path(filename or "").suffix.lower()
    return ext if _SAFE_EXT_RE.match(ext) else ""


def parse_s3_ref(ref: str) -> Tuple[str, str]:
    """Split ``s3://bucket/key`` into ``(bucket, key)``."""
    if not is_s3_ref(ref):
        raise ValueError(f"Not an s3 reference: {ref!r}")
    remainder = ref[len(S3_REF_PREFIX) :]
    bucket, _, key = remainder.partition("/")
    if not bucket or not key:
        raise ValueError(f"Malformed s3 reference: {ref!r}")
    return bucket, key


class StorageBackend(ABC):
    """Persistent byte storage behind stored references.

    ``save`` returns the reference to persist on the DB row; every read/delete
    operation takes that reference back.
    """

    #: True for object-store backends where ``save`` keys should be the canonical
    #: tenant-prefixed keys; False for the local backend, where write sites keep
    #: their legacy filesystem layout so behavior is byte-for-byte unchanged.
    is_remote: bool = False

    @abstractmethod
    def save(self, data: bytes, *, key: str) -> str:
        """Persist ``data`` under ``key`` and return the stored reference."""

    @abstractmethod
    def open_stream(self, ref: str) -> Iterator[bytes]:
        """Yield the stored bytes in chunks (for StreamingResponse)."""

    @abstractmethod
    def read_bytes(self, ref: str) -> bytes:
        """Return the full stored payload."""

    @abstractmethod
    def as_local_path(self, ref: str) -> Any:
        """Context manager yielding a REAL local ``Path`` for the stored bytes.

        OCR/parsing libraries (pdf2image, pytesseract, antiword, openpyxl, ezdxf)
        need an on-disk file. The local backend yields the path directly; the S3
        backend downloads to a NamedTemporaryFile and removes it afterwards.
        """

    @abstractmethod
    def delete(self, ref: str) -> None:
        """Remove the stored bytes (no-op when already gone)."""

    @abstractmethod
    def exists(self, ref: str) -> bool:
        """True when the stored bytes are retrievable."""


class LocalStorageBackend(StorageBackend):
    """Local-disk storage. The stored reference is the filesystem path itself.

    ``key`` is interpreted exactly as the legacy write sites built their paths
    (absolute under the resolved upload dir, or repo-relative ``uploads/...``), so
    enabling this layer with ``STORAGE_BACKEND=local`` is a no-op.
    """

    is_remote = False

    def save(self, data: bytes, *, key: str) -> str:
        path = Path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return key

    def open_stream(self, ref: str) -> Iterator[bytes]:
        def _iterator() -> Iterator[bytes]:
            with open(ref, "rb") as fh:
                while True:
                    chunk = fh.read(_STREAM_CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk

        return _iterator()

    def read_bytes(self, ref: str) -> bytes:
        with open(ref, "rb") as fh:
            return fh.read()

    @contextmanager
    def as_local_path(self, ref: str) -> Iterator[Path]:
        yield Path(ref)

    def delete(self, ref: str) -> None:
        if os.path.exists(ref):
            os.remove(ref)

    def exists(self, ref: str) -> bool:
        return os.path.exists(ref)


class S3StorageBackend(StorageBackend):
    """S3 / S3-compatible object storage (boto3).

    Stored reference format: ``s3://{bucket}/{key}``. The bucket/credentials are
    validated eagerly so a misconfigured deployment fails fast at startup instead of
    silently dropping documents. The boto3 client is also created eagerly in
    ``__init__``: botocore Session client creation is not thread-safe, and sync
    endpoints run in Starlette's threadpool, so a lazy first-use init could race.
    (``get_s3_storage()`` itself stays lazy, so app startup without s3 is unaffected.)
    """

    is_remote = True

    def __init__(
        self,
        *,
        bucket: Optional[str] = None,
        region: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
    ) -> None:
        self.bucket = bucket if bucket is not None else settings.S3_BUCKET_NAME
        self.region = region if region is not None else settings.AWS_REGION
        self.endpoint_url = endpoint_url if endpoint_url is not None else settings.S3_ENDPOINT_URL
        self.access_key_id = access_key_id if access_key_id is not None else settings.AWS_ACCESS_KEY_ID
        self.secret_access_key = secret_access_key if secret_access_key is not None else settings.AWS_SECRET_ACCESS_KEY
        self.validate_configuration()
        self._client: Any = self._create_client()

    def validate_configuration(self) -> None:
        missing = []
        if not self.bucket:
            missing.append("S3_BUCKET_NAME")
        if not self.access_key_id:
            missing.append("AWS_ACCESS_KEY_ID")
        if not self.secret_access_key:
            missing.append("AWS_SECRET_ACCESS_KEY")
        if missing:
            raise RuntimeError(
                "STORAGE_BACKEND=s3 requires object-storage credentials; missing: "
                + ", ".join(missing)
                + ". Set them in the environment (see docs/ENVIRONMENT_VARIABLES.md). "
                "For S3-compatible stores (Railway buckets, Cloudflare R2) also set S3_ENDPOINT_URL."
            )

    def _create_client(self) -> Any:
        import boto3

        return boto3.client(
            "s3",
            region_name=self.region or None,
            endpoint_url=self.endpoint_url or None,
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
        )

    @property
    def client(self) -> Any:
        # Tests swap ``_client`` for an in-memory fake; keep that seam intact.
        return self._client

    def _bucket_key(self, ref: str) -> Tuple[str, str]:
        # Parse bucket from the ref itself so rows written against a previous bucket
        # name remain resolvable after a bucket rename.
        return parse_s3_ref(ref)

    def save(self, data: bytes, *, key: str) -> str:
        clean_key = key.lstrip("/")
        self.client.put_object(Bucket=self.bucket, Key=clean_key, Body=data)
        return f"{S3_REF_PREFIX}{self.bucket}/{clean_key}"

    def open_stream(self, ref: str) -> Iterator[bytes]:
        bucket, key = self._bucket_key(ref)
        body = self.client.get_object(Bucket=bucket, Key=key)["Body"]

        def _iterator() -> Iterator[bytes]:
            try:
                while True:
                    chunk = body.read(_STREAM_CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk
            finally:
                body.close()

        return _iterator()

    def read_bytes(self, ref: str) -> bytes:
        bucket, key = self._bucket_key(ref)
        body = self.client.get_object(Bucket=bucket, Key=key)["Body"]
        try:
            return body.read()
        finally:
            body.close()

    @contextmanager
    def as_local_path(self, ref: str) -> Iterator[Path]:
        _, key = self._bucket_key(ref)
        suffix = sanitize_ext(key)
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        try:
            tmp.write(self.read_bytes(ref))
            tmp.flush()
            tmp.close()
            yield Path(tmp.name)
        finally:
            tmp.close()
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    def delete(self, ref: str) -> None:
        bucket, key = self._bucket_key(ref)
        self.client.delete_object(Bucket=bucket, Key=key)

    def exists(self, ref: str) -> bool:
        from botocore.exceptions import ClientError

        bucket, key = self._bucket_key(ref)
        try:
            self.client.head_object(Bucket=bucket, Key=key)
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return False
            raise


# ---------------------------------------------------------------------------
# Accessors. Module-level singletons, overridable for tests.
# ---------------------------------------------------------------------------

_local_backend: Optional[LocalStorageBackend] = None
_s3_backend: Optional[S3StorageBackend] = None
_override_backend: Optional[StorageBackend] = None


def get_local_storage() -> LocalStorageBackend:
    global _local_backend
    if _local_backend is None:
        _local_backend = LocalStorageBackend()
    return _local_backend


def get_s3_storage() -> S3StorageBackend:
    global _s3_backend
    if _s3_backend is None:
        _s3_backend = S3StorageBackend()
    return _s3_backend


def get_storage() -> StorageBackend:
    """The configured backend for NEW writes (``STORAGE_BACKEND``: local|s3)."""
    if _override_backend is not None:
        return _override_backend
    backend_name = (settings.STORAGE_BACKEND or "local").lower()
    if backend_name == "s3":
        return get_s3_storage()
    if backend_name != "local":
        raise RuntimeError(f"Unsupported STORAGE_BACKEND {backend_name!r}; expected 'local' or 's3'.")
    return get_local_storage()


def override_storage(backend: Optional[StorageBackend]) -> None:
    """Inject a backend (tests). Pass ``None`` to restore configured behavior."""
    global _override_backend
    _override_backend = backend


def reset_storage() -> None:
    """Drop cached backends so changed settings/env are picked up (tests)."""
    global _local_backend, _s3_backend, _override_backend
    _local_backend = None
    _s3_backend = None
    _override_backend = None


def backend_for_ref(ref: str) -> StorageBackend:
    """Per-row dispatch: ``s3://`` refs go to S3, anything else is a local path.

    Legacy local rows therefore remain servable when ``STORAGE_BACKEND=s3``, and s3
    rows remain servable if ops flips back to local.
    """
    remote_ref = is_s3_ref(ref)
    if _override_backend is not None and _override_backend.is_remote == remote_ref:
        return _override_backend
    if remote_ref:
        return get_s3_storage()
    return get_local_storage()


def open_ref_stream(ref: str) -> Iterator[bytes]:
    return backend_for_ref(ref).open_stream(ref)


def read_ref_bytes(ref: str) -> bytes:
    return backend_for_ref(ref).read_bytes(ref)


def ref_as_local_path(ref: str) -> Any:
    """Context manager yielding a real local ``Path`` for any stored ref."""
    return backend_for_ref(ref).as_local_path(ref)


def delete_ref(ref: str) -> None:
    backend_for_ref(ref).delete(ref)


def ref_exists(ref: str) -> bool:
    return backend_for_ref(ref).exists(ref)


def new_object_name(filename: Optional[str]) -> str:
    """A unique, never-user-controlled object name preserving a sanitized extension."""
    return f"{uuid.uuid4()}{sanitize_ext(filename)}"
