"""API tests for per-row storage-backend dispatch (durable document storage).

Verifies that with an s3-style backend active:
- new document uploads persist tenant-prefixed object keys and stream back correctly,
- legacy local-path rows remain servable (per-row ref dispatch),
- deletion removes the stored object (documents are hard-deleted today),
- the PO pdf preview endpoint serves s3 refs but only this tenant's purchase_orders
  keys in the configured bucket (foreign bucket/tenant/category refs 404),
- RFQ package files persist s3 refs.
"""

import uuid
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import create_access_token
from app.models.company import Company
from app.models.document import Document
from app.models.purchasing import PurchaseOrder, Vendor
from app.models.rfq_quote import RfqPackageFile
from app.models.user import User, UserRole
from app.services import storage_service
from app.services.storage_service import S3StorageBackend, is_s3_ref, parse_s3_ref


def _make_po(db: Session, *, company_id: int, source_document_path: str) -> PurchaseOrder:
    """Create a vendor + purchase order row pointing at a source document path."""
    tag = uuid.uuid4().hex[:8]
    vendor = Vendor(code=f"V-{tag}", name=f"Vendor {tag}", is_active=True, company_id=company_id)
    db.add(vendor)
    db.flush()
    po = PurchaseOrder(
        po_number=f"PO-{tag}",
        vendor_id=vendor.id,
        source_document_path=source_document_path,
        company_id=company_id,
    )
    db.add(po)
    db.commit()
    db.refresh(po)
    return po


def _second_tenant_headers(db: Session) -> dict:
    """Create company 2 (if needed) plus a user in it, and return auth headers."""
    if not db.query(Company).filter(Company.id == 2).first():
        db.add(Company(id=2, name="Other Co", slug="other-co", is_active=True))
        db.commit()
    tag = uuid.uuid4().hex[:8]
    user = User(
        email=f"po-pdf-{tag}@other-co.test",
        employee_id=f"POPDF-{tag}",
        first_name="Tenant",
        last_name="Two",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",  # never logged in with; token below
        role=UserRole.MANAGER,
        is_active=True,
        company_id=2,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


class _FakeS3Body:
    def __init__(self, data: bytes):
        self._data = data
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk = self._data[self._offset :]
        else:
            chunk = self._data[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        pass


class FakeS3Client:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body):
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": _FakeS3Body(self.objects[(Bucket, Key)])}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {}

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)


@pytest.fixture
def fake_remote_storage(monkeypatch):
    # The PO pdf endpoint and move_pdf_to_po pin s3 refs to the CONFIGURED bucket;
    # keep settings in step with the fake backend's bucket.
    monkeypatch.setattr(settings, "S3_BUCKET_NAME", "test-bucket")
    backend = S3StorageBackend(
        bucket="test-bucket",
        region="us-east-1",
        endpoint_url="http://localhost:9000",
        access_key_id="test-key",
        secret_access_key="test-secret",
    )
    backend._client = FakeS3Client()
    storage_service.override_storage(backend)
    yield backend
    storage_service.reset_storage()


@pytest.mark.api
@pytest.mark.requires_db
class TestDocumentStorageBackendDispatch:
    def _upload(self, client: TestClient, headers: dict, name: str = "spec.pdf") -> dict:
        response = client.post(
            "/api/v1/documents/upload",
            headers=headers,
            data={"title": "Spec", "document_type": "specification", "revision": "A"},
            files={"file": (name, b"%PDF-1.4 storage test\n", "application/pdf")},
        )
        assert response.status_code == status.HTTP_200_OK
        return response.json()

    def test_upload_persists_tenant_prefixed_s3_ref(
        self, client: TestClient, admin_headers: dict, db_session: Session, fake_remote_storage
    ):
        uploaded = self._upload(client, admin_headers)

        document = db_session.query(Document).filter(Document.id == uploaded["id"]).first()
        assert is_s3_ref(document.file_path)
        bucket, key = parse_s3_ref(document.file_path)
        assert bucket == "test-bucket"
        assert key == f"{document.company_id}/documents/{key.split('/')[-1]}"
        assert key.endswith(".pdf")

    def test_download_streams_s3_backed_document(self, client: TestClient, admin_headers: dict, fake_remote_storage):
        uploaded = self._upload(client, admin_headers, name="drawing rev B.pdf")

        response = client.get(f"/api/v1/documents/{uploaded['id']}/download", headers=admin_headers)

        assert response.status_code == status.HTTP_200_OK
        assert response.content == b"%PDF-1.4 storage test\n"
        assert response.headers["content-type"].startswith("application/pdf")
        # Same encoding FileResponse(filename=...) produces for non-ascii-safe names.
        assert response.headers["content-disposition"] == "attachment; filename*=utf-8''drawing%20rev%20B.pdf"

    def test_legacy_local_row_still_served_when_backend_is_s3(
        self,
        client: TestClient,
        admin_headers: dict,
        db_session: Session,
        fake_remote_storage,
        tmp_path,
    ):
        # Simulate a pre-migration row: bytes on local disk, ref is a plain path.
        legacy_path = tmp_path / "legacy-doc.pdf"
        legacy_path.write_bytes(b"%PDF-1.4 legacy\n")
        uploaded = self._upload(client, admin_headers)
        document = db_session.query(Document).filter(Document.id == uploaded["id"]).first()
        document.file_path = str(legacy_path)
        db_session.commit()

        response = client.get(f"/api/v1/documents/{uploaded['id']}/download", headers=admin_headers)

        assert response.status_code == status.HTTP_200_OK
        assert response.content == b"%PDF-1.4 legacy\n"

    def test_delete_removes_stored_object(
        self, client: TestClient, admin_headers: dict, db_session: Session, fake_remote_storage
    ):
        uploaded = self._upload(client, admin_headers)
        document = db_session.query(Document).filter(Document.id == uploaded["id"]).first()
        ref = document.file_path
        assert fake_remote_storage.exists(ref)

        response = client.delete(f"/api/v1/documents/{uploaded['id']}", headers=admin_headers)

        assert response.status_code == status.HTTP_200_OK
        assert not fake_remote_storage.exists(ref)

    def test_download_missing_s3_object_returns_404(
        self, client: TestClient, admin_headers: dict, db_session: Session, fake_remote_storage
    ):
        uploaded = self._upload(client, admin_headers)
        document = db_session.query(Document).filter(Document.id == uploaded["id"]).first()
        fake_remote_storage.delete(document.file_path)

        response = client.get(f"/api/v1/documents/{uploaded['id']}/download", headers=admin_headers)

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_download_ascii_filename_uses_plain_content_disposition(
        self, client: TestClient, admin_headers: dict, fake_remote_storage
    ):
        uploaded = self._upload(client, admin_headers, name="spec.pdf")

        response = client.get(f"/api/v1/documents/{uploaded['id']}/download", headers=admin_headers)

        assert response.status_code == status.HTTP_200_OK
        assert response.headers["content-disposition"] == 'attachment; filename="spec.pdf"'

    def test_download_null_mime_type_falls_back_to_filename_guess(
        self, client: TestClient, admin_headers: dict, db_session: Session, fake_remote_storage
    ):
        # Legacy rows can carry NULL mime_type; FileResponse used to guess the type
        # from the filename, so the s3 streaming path must do the same.
        uploaded = self._upload(client, admin_headers, name="spec.pdf")
        document = db_session.query(Document).filter(Document.id == uploaded["id"]).first()
        document.mime_type = None
        db_session.commit()

        response = client.get(f"/api/v1/documents/{uploaded['id']}/download", headers=admin_headers)

        assert response.status_code == status.HTTP_200_OK
        assert response.headers["content-type"].startswith("application/pdf")

    def test_download_null_mime_type_unguessable_name_uses_octet_stream(
        self, client: TestClient, admin_headers: dict, db_session: Session, fake_remote_storage
    ):
        uploaded = self._upload(client, admin_headers)
        document = db_session.query(Document).filter(Document.id == uploaded["id"]).first()
        document.mime_type = None
        document.file_name = "drawing-no-extension"
        db_session.commit()

        response = client.get(f"/api/v1/documents/{uploaded['id']}/download", headers=admin_headers)

        assert response.status_code == status.HTTP_200_OK
        assert response.headers["content-type"].startswith("application/octet-stream")

    def test_download_non_ascii_filename_is_rfc5987_encoded(
        self, client: TestClient, admin_headers: dict, db_session: Session, fake_remote_storage
    ):
        # Non-ascii original names (e.g. supplier drawings) must produce an
        # ascii-safe header on the s3 streaming path, same as FileResponse does.
        uploaded = self._upload(client, admin_headers)
        document = db_session.query(Document).filter(Document.id == uploaded["id"]).first()
        document.file_name = "Zeichnung-Ø25 µm.pdf"
        db_session.commit()

        response = client.get(f"/api/v1/documents/{uploaded['id']}/download", headers=admin_headers)

        assert response.status_code == status.HTTP_200_OK
        disposition = response.headers["content-disposition"]
        assert disposition == "attachment; filename*=utf-8''Zeichnung-%C3%9825%20%C2%B5m.pdf"
        assert disposition.encode("ascii")  # header must remain ascii-safe


@pytest.mark.api
@pytest.mark.requires_db
class TestPoUploadPdfServingS3:
    def test_serves_s3_purchase_order_ref(self, client: TestClient, auth_headers: dict, fake_remote_storage):
        ref = fake_remote_storage.save(b"%PDF-1.4 po\n", key="1/purchase_orders/pending/abc.pdf")

        response = client.get(f"/api/v1/po-upload/pdf/{ref}", headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        assert response.content == b"%PDF-1.4 po\n"
        assert response.headers["content-type"].startswith("application/pdf")

    def test_rejects_s3_ref_outside_purchase_orders(self, client: TestClient, auth_headers: dict, fake_remote_storage):
        # Mirrors the local traversal guard: only PO source documents are servable
        # here. 404 (not 403) so foreign keys look identical to missing ones.
        ref = fake_remote_storage.save(b"%PDF-1.4 doc\n", key="1/documents/secret.pdf")

        response = client.get(f"/api/v1/po-upload/pdf/{ref}", headers=auth_headers)

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_rejects_s3_ref_in_foreign_bucket(self, client: TestClient, auth_headers: dict, fake_remote_storage):
        # A tenant-shaped key in a bucket other than the configured one must 404,
        # even when the object exists there.
        fake_remote_storage._client.put_object(
            Bucket="other-bucket", Key="1/purchase_orders/pending/abc.pdf", Body=b"%PDF-1.4 foreign\n"
        )

        response = client.get(
            "/api/v1/po-upload/pdf/s3://other-bucket/1/purchase_orders/pending/abc.pdf", headers=auth_headers
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_rejects_s3_ref_with_foreign_tenant_prefix(
        self, client: TestClient, auth_headers: dict, fake_remote_storage
    ):
        # auth_headers belongs to company 1; another tenant's PO document must 404
        # (indistinguishable from missing), never serve.
        ref = fake_remote_storage.save(b"%PDF-1.4 tenant2\n", key="2/purchase_orders/pending/abc.pdf")

        response = client.get(f"/api/v1/po-upload/pdf/{ref}", headers=auth_headers)

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_missing_s3_ref_returns_404(self, client: TestClient, auth_headers: dict, fake_remote_storage):
        response = client.get(
            "/api/v1/po-upload/pdf/s3://test-bucket/1/purchase_orders/pending/missing.pdf",
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_serves_s3_ref_with_collapsed_double_slash(
        self, client: TestClient, auth_headers: dict, fake_remote_storage
    ):
        # Proxies/routers may collapse "s3://" to "s3:/" inside a path segment;
        # the endpoint must normalize it back to the canonical ref.
        ref = fake_remote_storage.save(b"%PDF-1.4 po\n", key="1/purchase_orders/pending/xyz.pdf")
        collapsed = ref.replace("s3://", "s3:/")

        response = client.get(f"/api/v1/po-upload/pdf/{collapsed}", headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        assert response.content == b"%PDF-1.4 po\n"

    def test_malformed_s3_ref_returns_404(self, client: TestClient, auth_headers: dict, fake_remote_storage):
        # "s3://bucket-only" parses to no key and must not 500.
        response = client.get("/api/v1/po-upload/pdf/s3://test-bucket", headers=auth_headers)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_legacy_local_relative_path_still_served_when_backend_is_s3(
        self, client: TestClient, auth_headers: dict, db_session: Session, fake_remote_storage
    ):
        # Pre-migration PO rows store paths relative to uploads/purchase_orders;
        # flipping STORAGE_BACKEND=s3 must not break serving them. Local paths are
        # only served when this tenant owns a PO row pointing at them.
        import shutil

        subdir = f"test-legacy-{uuid.uuid4().hex}"
        local_dir = Path("uploads/purchase_orders") / subdir
        local_dir.mkdir(parents=True, exist_ok=True)
        (local_dir / "legacy.pdf").write_bytes(b"%PDF-1.4 legacy po\n")
        _make_po(db_session, company_id=1, source_document_path=f"uploads/purchase_orders/{subdir}/legacy.pdf")
        try:
            response = client.get(f"/api/v1/po-upload/pdf/{subdir}/legacy.pdf", headers=auth_headers)

            assert response.status_code == status.HTTP_200_OK
            assert response.content == b"%PDF-1.4 legacy po\n"
        finally:
            shutil.rmtree(local_dir, ignore_errors=True)


@pytest.fixture
def write_local_po_file():
    """Write files under uploads/purchase_orders; unlink them (and any then-empty
    parent dirs) after the test. Filenames must be unique (xdist workers share cwd)."""
    import contextlib
    import os

    base = Path("uploads/purchase_orders")
    created: list = []

    def _write(rel: str, data: bytes = b"%PDF-1.4 local\n") -> str:
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        created.append(target)
        return str(target)

    yield _write

    for target in created:
        with contextlib.suppress(OSError):
            target.unlink()
        parent = target.parent
        while parent != base:
            with contextlib.suppress(OSError):  # non-empty dirs (shared with other tests) stay
                os.rmdir(parent)
            parent = parent.parent


@pytest.mark.api
@pytest.mark.requires_db
class TestPoUploadPdfServingLocal:
    """Tenant scoping for the LOCAL branch of GET /po-upload/pdf/{path}.

    Pending (pre-PO) files are tenant-scoped by path segment (pending/{company_id}/...);
    PO-attached files are served only when the active tenant owns a PurchaseOrder row
    whose source_document_path matches. All rejections are 404 so foreign/forbidden
    paths are indistinguishable from missing ones (same choice as the s3 branch).
    """

    def test_serves_own_tenant_pending_file(self, client: TestClient, auth_headers: dict, write_local_po_file):
        name = f"preview-{uuid.uuid4().hex}.pdf"
        write_local_po_file(f"pending/1/{name}", b"%PDF-1.4 t1 pending\n")

        response = client.get(f"/api/v1/po-upload/pdf/pending/1/{name}", headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        assert response.content == b"%PDF-1.4 t1 pending\n"
        assert response.headers["content-type"].startswith("application/pdf")

    def test_rejects_foreign_tenant_pending_file(self, client: TestClient, auth_headers: dict, write_local_po_file):
        # auth_headers belongs to company 1; company 2's pending preview must 404
        # even though the file exists on disk.
        name = f"preview-{uuid.uuid4().hex}.pdf"
        write_local_po_file(f"pending/2/{name}", b"%PDF-1.4 t2 pending\n")

        response = client.get(f"/api/v1/po-upload/pdf/pending/2/{name}", headers=auth_headers)

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_rejects_legacy_untenanted_pending_file(self, client: TestClient, auth_headers: dict, write_local_po_file):
        # Pre-fix pending files lived directly in pending/ with no tenant segment.
        # They are ephemeral pre-PO previews, so 404ing them is acceptable breakage;
        # re-uploading regenerates them under pending/{company_id}/.
        name = f"old-{uuid.uuid4().hex}.pdf"
        write_local_po_file(f"pending/{name}", b"%PDF-1.4 legacy pending\n")

        response = client.get(f"/api/v1/po-upload/pdf/pending/{name}", headers=auth_headers)

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_serves_po_linked_file_for_owning_tenant(
        self, client: TestClient, auth_headers: dict, db_session: Session, write_local_po_file
    ):
        subdir = f"test-po-{uuid.uuid4().hex}"
        path = write_local_po_file(f"{subdir}/po.pdf", b"%PDF-1.4 t1 po\n")
        _make_po(db_session, company_id=1, source_document_path=path)

        response = client.get(f"/api/v1/po-upload/pdf/{subdir}/po.pdf", headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        assert response.content == b"%PDF-1.4 t1 po\n"

    def test_rejects_po_linked_file_for_foreign_tenant(
        self, client: TestClient, db_session: Session, write_local_po_file
    ):
        # The same path that serves for company 1 must 404 for company 2, even when
        # company 2 has PO rows of its own.
        subdir = f"test-po-{uuid.uuid4().hex}"
        path = write_local_po_file(f"{subdir}/po.pdf", b"%PDF-1.4 t1 po\n")
        _make_po(db_session, company_id=1, source_document_path=path)
        tenant2_headers = _second_tenant_headers(db_session)
        _make_po(db_session, company_id=2, source_document_path="uploads/purchase_orders/999/other.pdf")

        response = client.get(f"/api/v1/po-upload/pdf/{subdir}/po.pdf", headers=tenant2_headers)

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_rejects_unlinked_local_path(self, client: TestClient, auth_headers: dict, write_local_po_file):
        # A non-pending file with no matching PO row for this tenant must 404.
        subdir = f"test-orphan-{uuid.uuid4().hex}"
        write_local_po_file(f"{subdir}/orphan.pdf")

        response = client.get(f"/api/v1/po-upload/pdf/{subdir}/orphan.pdf", headers=auth_headers)

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_rejects_traversal_attempt_with_404(self, client: TestClient, auth_headers: dict):
        # %2F-encoded slashes survive client/proxy dot-segment normalization, so the
        # endpoint sees "../../../etc/passwd". Must 404 (not the old 403) so probes
        # are indistinguishable from missing files.
        response = client.get("/api/v1/po-upload/pdf/..%2F..%2F..%2Fetc%2Fpasswd", headers=auth_headers)

        assert response.status_code == status.HTTP_404_NOT_FOUND
        # "PDF not found" proves the request reached the endpoint guard (a router
        # miss after URL normalization would say "Not Found" instead).
        assert response.json()["detail"] == "PDF not found"

    def test_rejects_dotdot_smuggled_pending_prefix(self, client: TestClient, auth_headers: dict, write_local_po_file):
        # "pending/1/../2/x.pdf" textually starts with this tenant's pending prefix
        # but resolves to company 2's file; the tenant check runs on the resolved
        # segments, so it must 404.
        name = f"smuggle-{uuid.uuid4().hex}.pdf"
        write_local_po_file(f"pending/2/{name}", b"%PDF-1.4 t2 pending\n")

        response = client.get(f"/api/v1/po-upload/pdf/pending%2F1%2F..%2F2%2F{name}", headers=auth_headers)

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["detail"] == "PDF not found"


@pytest.mark.api
@pytest.mark.requires_db
class TestRfqPackageStorageS3:
    def test_rfq_files_persist_s3_refs(
        self, client: TestClient, admin_headers: dict, db_session: Session, fake_remote_storage
    ):
        response = client.post(
            "/api/v1/rfq-packages/",
            headers=admin_headers,
            data={"customer_name": "Acme"},
            files=[("files", ("drawing.pdf", b"%PDF-1.4 rfq\n", "application/pdf"))],
        )

        assert response.status_code == status.HTTP_200_OK
        package_id = response.json()["id"]
        rows = db_session.query(RfqPackageFile).filter(RfqPackageFile.rfq_package_id == package_id).all()
        assert rows
        for row in rows:
            assert is_s3_ref(row.file_path)
            _, key = parse_s3_ref(row.file_path)
            company_prefix, category = key.split("/")[0], key.split("/")[1]
            assert company_prefix == str(row.company_id)
            assert category == "rfq_packages"
            assert fake_remote_storage.read_bytes(row.file_path) == b"%PDF-1.4 rfq\n"
