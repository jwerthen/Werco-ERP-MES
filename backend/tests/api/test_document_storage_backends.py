"""API tests for per-row storage-backend dispatch (durable document storage).

Verifies that with an s3-style backend active:
- new document uploads persist tenant-prefixed object keys and stream back correctly,
- legacy local-path rows remain servable (per-row ref dispatch),
- deletion removes the stored object (documents are hard-deleted today),
- the PO pdf preview endpoint serves s3 refs but only this tenant's purchase_orders
  keys in the configured bucket (foreign bucket/tenant/category refs 404),
- RFQ package files persist s3 refs.
"""

import pytest
from botocore.exceptions import ClientError
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.document import Document
from app.models.rfq_quote import RfqPackageFile
from app.services import storage_service
from app.services.storage_service import S3StorageBackend, is_s3_ref, parse_s3_ref


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
        self, client: TestClient, auth_headers: dict, fake_remote_storage
    ):
        # Pre-migration PO rows store paths relative to uploads/purchase_orders;
        # flipping STORAGE_BACKEND=s3 must not break serving them.
        import shutil
        import uuid as uuid_mod
        from pathlib import Path

        subdir = f"test-legacy-{uuid_mod.uuid4().hex}"
        local_dir = Path("uploads/purchase_orders") / subdir
        local_dir.mkdir(parents=True, exist_ok=True)
        (local_dir / "legacy.pdf").write_bytes(b"%PDF-1.4 legacy po\n")
        try:
            response = client.get(f"/api/v1/po-upload/pdf/{subdir}/legacy.pdf", headers=auth_headers)

            assert response.status_code == status.HTTP_200_OK
            assert response.content == b"%PDF-1.4 legacy po\n"
        finally:
            shutil.rmtree(local_dir, ignore_errors=True)


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
