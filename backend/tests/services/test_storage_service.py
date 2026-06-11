"""Unit tests for the durable file-storage layer (app/services/storage_service.py)."""

import os
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from app.services import storage_service
from app.services.storage_service import (
    LocalStorageBackend,
    S3StorageBackend,
    backend_for_ref,
    is_s3_ref,
    parse_s3_ref,
    sanitize_ext,
)


@pytest.fixture(autouse=True)
def _reset_storage_singletons():
    storage_service.reset_storage()
    yield
    storage_service.reset_storage()


class _FakeS3Body:
    def __init__(self, data: bytes):
        self._data = data
        self._offset = 0
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk = self._data[self._offset :]
            self._offset = len(self._data)
            return chunk
        chunk = self._data[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


class FakeS3Client:
    """Minimal in-memory stand-in for the boto3 S3 client."""

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
def s3_backend(monkeypatch):
    # move_pdf_to_po pins s3 source refs to the CONFIGURED bucket; keep settings in
    # step with the fake backend's bucket so canonical refs pass containment.
    monkeypatch.setattr(storage_service.settings, "S3_BUCKET_NAME", "test-bucket")
    backend = S3StorageBackend(
        bucket="test-bucket",
        region="us-east-1",
        endpoint_url="http://localhost:9000",
        access_key_id="test-key",
        secret_access_key="test-secret",
    )
    backend._client = FakeS3Client()
    return backend


@pytest.mark.unit
class TestRefHelpers:
    def test_is_s3_ref(self):
        assert is_s3_ref("s3://bucket/1/documents/a.pdf")
        assert not is_s3_ref("/app/uploads/a.pdf")
        assert not is_s3_ref("uploads/rfq_packages/RFQ-1/a.pdf")
        assert not is_s3_ref(None)
        assert not is_s3_ref("")

    def test_parse_s3_ref(self):
        assert parse_s3_ref("s3://bucket/1/documents/a.pdf") == ("bucket", "1/documents/a.pdf")

    @pytest.mark.parametrize("bad", ["s3://", "s3://bucket", "s3://bucket/", "/local/path"])
    def test_parse_s3_ref_rejects_malformed(self, bad):
        with pytest.raises(ValueError):
            parse_s3_ref(bad)

    def test_sanitize_ext_keeps_safe_extensions(self):
        assert sanitize_ext("drawing.PDF") == ".pdf"
        assert sanitize_ext("model.step") == ".step"

    def test_sanitize_ext_drops_unsafe_extensions(self):
        assert sanitize_ext("no-extension") == ""
        assert sanitize_ext("weird.p df") == ""
        assert sanitize_ext("traversal.pdf/../../etc") == ""
        assert sanitize_ext(None) == ""


@pytest.mark.unit
class TestLocalStorageBackend:
    def test_round_trip(self, tmp_path):
        backend = LocalStorageBackend()
        key = str(tmp_path / "nested" / "dir" / "file.pdf")

        ref = backend.save(b"hello local", key=key)

        assert ref == key  # stored ref IS the path the caller built (legacy rows stay valid)
        assert backend.exists(ref)
        assert backend.read_bytes(ref) == b"hello local"
        assert b"".join(backend.open_stream(ref)) == b"hello local"
        with backend.as_local_path(ref) as local:
            # Local backend yields the real path directly, no copies.
            assert Path(local) == Path(ref)
        backend.delete(ref)
        assert not backend.exists(ref)
        backend.delete(ref)  # idempotent

    def test_is_not_remote(self):
        assert LocalStorageBackend().is_remote is False


@pytest.mark.unit
class TestS3StorageBackend:
    def test_save_returns_canonical_ref(self, s3_backend):
        ref = s3_backend.save(b"data", key="1/documents/abc.pdf")
        assert ref == "s3://test-bucket/1/documents/abc.pdf"
        assert s3_backend.is_remote is True

    def test_round_trip(self, s3_backend):
        ref = s3_backend.save(b"s3 payload", key="1/shipping/label.pdf")

        assert s3_backend.exists(ref)
        assert s3_backend.read_bytes(ref) == b"s3 payload"
        assert b"".join(s3_backend.open_stream(ref)) == b"s3 payload"
        s3_backend.delete(ref)
        assert not s3_backend.exists(ref)

    def test_as_local_path_materializes_and_cleans_up(self, s3_backend):
        ref = s3_backend.save(b"%PDF-1.4 fake", key="1/purchase_orders/9/doc.pdf")

        with s3_backend.as_local_path(ref) as local:
            assert os.path.exists(local)
            assert Path(local).suffix == ".pdf"  # extension preserved for type-sniffing parsers
            assert Path(local).read_bytes() == b"%PDF-1.4 fake"
        assert not os.path.exists(local)

    def test_reads_dispatch_on_ref_bucket_not_configured_bucket(self, s3_backend):
        # Rows written before a bucket rename must remain resolvable.
        s3_backend._client.put_object(Bucket="old-bucket", Key="1/documents/x.pdf", Body=b"old")
        assert s3_backend.read_bytes("s3://old-bucket/1/documents/x.pdf") == b"old"

    def test_client_is_created_eagerly_at_init(self):
        # Sync endpoints run in Starlette's threadpool and botocore Session client
        # creation is not thread-safe, so the client must be built once in __init__
        # rather than lazily on first use.
        backend = S3StorageBackend(
            bucket="test-bucket",
            region="us-east-1",
            endpoint_url="http://localhost:9000",
            access_key_id="test-key",
            secret_access_key="test-secret",
        )
        assert backend._client is not None
        assert backend.client is backend._client
        # The test seam stays intact: swapping _client swaps what .client returns.
        fake = FakeS3Client()
        backend._client = fake
        assert backend.client is fake

    def test_missing_configuration_fails_fast(self):
        with pytest.raises(RuntimeError, match="AWS_ACCESS_KEY_ID"):
            S3StorageBackend(bucket="b", access_key_id="", secret_access_key="s")
        with pytest.raises(RuntimeError, match="S3_BUCKET_NAME"):
            S3StorageBackend(bucket="", access_key_id="k", secret_access_key="s")
        with pytest.raises(RuntimeError, match="AWS_SECRET_ACCESS_KEY"):
            S3StorageBackend(bucket="b", access_key_id="k", secret_access_key="")

    def test_missing_configuration_lists_every_missing_credential(self):
        # All gaps reported at once so ops fixes the deployment in one pass.
        with pytest.raises(RuntimeError) as exc_info:
            S3StorageBackend(bucket="", access_key_id="", secret_access_key="")
        message = str(exc_info.value)
        assert "S3_BUCKET_NAME" in message
        assert "AWS_ACCESS_KEY_ID" in message
        assert "AWS_SECRET_ACCESS_KEY" in message

    def test_read_bytes_missing_key_raises(self, s3_backend):
        with pytest.raises(ClientError):
            s3_backend.read_bytes("s3://test-bucket/1/documents/does-not-exist.pdf")

    def test_open_stream_missing_key_raises(self, s3_backend):
        # get_object is called eagerly (before iteration), so the endpoint's
        # ref_exists pre-check is what turns this into a clean 404.
        with pytest.raises(ClientError):
            s3_backend.open_stream("s3://test-bucket/1/documents/does-not-exist.pdf")

    def test_exists_reraises_unexpected_client_errors(self, s3_backend):
        # Only not-found codes mean False; auth/permission failures must surface.
        class _DeniedClient(FakeS3Client):
            def head_object(self, Bucket, Key):
                raise ClientError({"Error": {"Code": "403"}}, "HeadObject")

        s3_backend._client = _DeniedClient()
        with pytest.raises(ClientError):
            s3_backend.exists("s3://test-bucket/1/documents/x.pdf")


@pytest.mark.unit
class TestDispatchAndAccessors:
    def test_get_storage_defaults_to_local(self, monkeypatch):
        monkeypatch.setattr(storage_service.settings, "STORAGE_BACKEND", "local")
        assert isinstance(storage_service.get_storage(), LocalStorageBackend)

    def test_get_storage_s3(self, monkeypatch):
        monkeypatch.setattr(storage_service.settings, "STORAGE_BACKEND", "s3")
        monkeypatch.setattr(storage_service.settings, "AWS_ACCESS_KEY_ID", "k")
        monkeypatch.setattr(storage_service.settings, "AWS_SECRET_ACCESS_KEY", "s")
        monkeypatch.setattr(storage_service.settings, "S3_BUCKET_NAME", "bucket")
        backend = storage_service.get_storage()
        assert isinstance(backend, S3StorageBackend)
        assert storage_service.get_storage() is backend  # cached singleton

    def test_get_storage_rejects_unknown_backend(self, monkeypatch):
        monkeypatch.setattr(storage_service.settings, "STORAGE_BACKEND", "gcs")
        with pytest.raises(RuntimeError, match="STORAGE_BACKEND"):
            storage_service.get_storage()

    def test_override_storage_wins(self, s3_backend, monkeypatch):
        monkeypatch.setattr(storage_service.settings, "STORAGE_BACKEND", "local")
        storage_service.override_storage(s3_backend)
        assert storage_service.get_storage() is s3_backend
        storage_service.override_storage(None)
        assert isinstance(storage_service.get_storage(), LocalStorageBackend)

    def test_per_row_dispatch_serves_legacy_local_rows_under_s3(self, s3_backend, tmp_path, monkeypatch):
        # STORAGE_BACKEND=s3 (override) must NOT break reads of pre-existing local rows.
        storage_service.override_storage(s3_backend)
        legacy = tmp_path / "legacy.pdf"
        legacy.write_bytes(b"legacy bytes")

        assert isinstance(backend_for_ref(str(legacy)), LocalStorageBackend)
        assert storage_service.read_ref_bytes(str(legacy)) == b"legacy bytes"
        assert storage_service.ref_exists(str(legacy))

        ref = s3_backend.save(b"remote bytes", key="1/documents/n.pdf")
        assert backend_for_ref(ref) is s3_backend
        assert storage_service.read_ref_bytes(ref) == b"remote bytes"


@pytest.mark.unit
class TestPdfServiceStorageIntegration:
    def test_save_uploaded_document_remote_uses_tenant_prefixed_key(self, s3_backend):
        from app.services.pdf_service import save_uploaded_document

        storage_service.override_storage(s3_backend)
        ref = save_uploaded_document(b"%PDF-1.4", "Vendor PO #42.pdf", company_id=7)

        bucket, key = parse_s3_ref(ref)
        assert bucket == "test-bucket"
        prefix, category, po_dir, name = key.split("/")
        assert prefix == "7"
        assert category == "purchase_orders"
        assert po_dir == "pending"
        assert name.endswith(".pdf")
        assert "Vendor" not in name  # object name is never user-controlled
        assert s3_backend.read_bytes(ref) == b"%PDF-1.4"

    def test_save_uploaded_document_without_company_stays_local(self, s3_backend, tmp_path, monkeypatch):
        from app.services.pdf_service import save_uploaded_document

        storage_service.override_storage(s3_backend)
        monkeypatch.chdir(tmp_path)
        ref = save_uploaded_document(b"%PDF-1.4", "scratch.pdf")

        assert not is_s3_ref(ref)
        assert ref == str(Path("uploads/purchase_orders/pending/scratch.pdf"))
        assert (tmp_path / ref).read_bytes() == b"%PDF-1.4"

    def test_save_uploaded_document_local_legacy_layout(self, tmp_path, monkeypatch):
        from app.services.pdf_service import save_uploaded_document

        monkeypatch.chdir(tmp_path)
        first = save_uploaded_document(b"a", "My PO!.pdf", po_id=12, company_id=7)
        second = save_uploaded_document(b"b", "My PO!.pdf", po_id=12, company_id=7)

        # Legacy semantics preserved exactly: sanitized stem + duplicate counter.
        assert first == str(Path("uploads/purchase_orders/12/My PO.pdf"))
        assert second == str(Path("uploads/purchase_orders/12/My PO_1.pdf"))

    def test_save_uploaded_document_local_pending_is_tenant_scoped(self, tmp_path, monkeypatch):
        # Pre-PO (pending) files with a known company land under pending/{company_id}
        # so the serving endpoint can enforce tenancy from the path alone.
        from app.services.pdf_service import save_uploaded_document

        monkeypatch.chdir(tmp_path)
        ref = save_uploaded_document(b"%PDF-1.4", "preview.pdf", company_id=7)

        assert ref == str(Path("uploads/purchase_orders/pending/7/preview.pdf"))
        assert (tmp_path / ref).read_bytes() == b"%PDF-1.4"

    def test_move_pdf_to_po_remote_copies_then_deletes(self, s3_backend):
        from app.services.pdf_service import move_pdf_to_po

        storage_service.override_storage(s3_backend)
        old_ref = s3_backend.save(b"%PDF-1.4", key="7/purchase_orders/pending/tmp.pdf")

        new_ref = move_pdf_to_po(old_ref, po_id=33, company_id=7)

        assert new_ref != old_ref
        _, new_key = parse_s3_ref(new_ref)
        assert new_key.startswith("7/purchase_orders/33/")
        assert new_key.endswith(".pdf")
        assert s3_backend.read_bytes(new_ref) == b"%PDF-1.4"
        assert not s3_backend.exists(old_ref)

    def test_move_pdf_to_po_remote_missing_source_is_noop(self, s3_backend):
        from app.services.pdf_service import move_pdf_to_po

        storage_service.override_storage(s3_backend)
        missing = "s3://test-bucket/7/purchase_orders/pending/gone.pdf"
        assert move_pdf_to_po(missing, po_id=33, company_id=7) == missing

    def test_move_pdf_to_po_rejects_s3_ref_in_foreign_bucket(self, s3_backend):
        # temp_path comes from the request body; a ref pointing at another bucket
        # must be rejected before any read/copy/delete happens.
        from app.services.pdf_service import move_pdf_to_po

        storage_service.override_storage(s3_backend)
        s3_backend._client.put_object(Bucket="other-bucket", Key="7/purchase_orders/pending/x.pdf", Body=b"%PDF-1.4")
        foreign = "s3://other-bucket/7/purchase_orders/pending/x.pdf"

        with pytest.raises(ValueError, match="bucket"):
            move_pdf_to_po(foreign, po_id=33, company_id=7)
        # Source untouched: nothing copied, nothing deleted.
        assert s3_backend._client.objects[("other-bucket", "7/purchase_orders/pending/x.pdf")] == b"%PDF-1.4"
        assert all(bucket == "other-bucket" for bucket, _ in s3_backend._client.objects)

    def test_move_pdf_to_po_rejects_s3_key_outside_tenant_purchase_orders(self, s3_backend):
        from app.services.pdf_service import move_pdf_to_po

        storage_service.override_storage(s3_backend)
        foreign_tenant = s3_backend.save(b"%PDF-1.4 t9", key="9/purchase_orders/pending/x.pdf")
        wrong_category = s3_backend.save(b"%PDF-1.4 doc", key="7/documents/x.pdf")

        with pytest.raises(ValueError, match="purchase_orders"):
            move_pdf_to_po(foreign_tenant, po_id=33, company_id=7)
        with pytest.raises(ValueError, match="purchase_orders"):
            move_pdf_to_po(wrong_category, po_id=33, company_id=7)
        # Sources untouched in both cases.
        assert s3_backend.exists(foreign_tenant)
        assert s3_backend.exists(wrong_category)

    def test_move_pdf_to_po_local_rejects_path_outside_uploads(self, tmp_path, monkeypatch):
        # The local branch used to shutil.move a fully user-controlled path; it must
        # now resolve (realpath) inside uploads/purchase_orders, mirroring the
        # serving guard in po_upload.py.
        from app.services.pdf_service import move_pdf_to_po

        monkeypatch.chdir(tmp_path)
        outside = tmp_path / "secrets.pdf"
        outside.write_bytes(b"%PDF-1.4 secret")

        with pytest.raises(ValueError, match="uploads/purchase_orders"):
            move_pdf_to_po(str(outside), po_id=5, company_id=7)
        with pytest.raises(ValueError, match="uploads/purchase_orders"):
            # Traversal inside an allowed-looking prefix must also be caught.
            move_pdf_to_po("uploads/purchase_orders/../../secrets.pdf", po_id=5, company_id=7)
        assert outside.read_bytes() == b"%PDF-1.4 secret"  # never moved

    def test_move_pdf_to_po_local_moves_tenant_pending_file(self, tmp_path, monkeypatch):
        from app.services.pdf_service import move_pdf_to_po

        monkeypatch.chdir(tmp_path)
        pending = Path("uploads/purchase_orders/pending/7")
        pending.mkdir(parents=True)
        (pending / "doc.pdf").write_bytes(b"%PDF-1.4")

        new_path = move_pdf_to_po(str(pending / "doc.pdf"), po_id=5, company_id=7)

        assert new_path == str(Path("uploads/purchase_orders/5/doc.pdf"))
        assert Path(new_path).read_bytes() == b"%PDF-1.4"
        assert not (pending / "doc.pdf").exists()

    def test_move_pdf_to_po_local_rejects_pending_source_outside_tenant_dir(self, tmp_path, monkeypatch):
        # Pending sources are tenant-scoped on disk; a pending path under another
        # tenant's directory (or the legacy un-tenanted pending root) must be
        # rejected before anything is moved, mirroring the s3 tenant-prefix check.
        from app.services.pdf_service import move_pdf_to_po

        monkeypatch.chdir(tmp_path)
        foreign = Path("uploads/purchase_orders/pending/9")
        foreign.mkdir(parents=True)
        (foreign / "doc.pdf").write_bytes(b"%PDF-1.4 t9")
        legacy = Path("uploads/purchase_orders/pending/old.pdf")
        legacy.write_bytes(b"%PDF-1.4 legacy")

        with pytest.raises(ValueError, match="pending"):
            move_pdf_to_po(str(foreign / "doc.pdf"), po_id=5, company_id=7)
        with pytest.raises(ValueError, match="tenant"):
            move_pdf_to_po(str(legacy), po_id=5)  # no company: numeric segment required
        # Sources untouched in both cases.
        assert (foreign / "doc.pdf").read_bytes() == b"%PDF-1.4 t9"
        assert legacy.read_bytes() == b"%PDF-1.4 legacy"

    def test_move_pdf_to_po_local_rejects_non_pending_source(self, tmp_path, monkeypatch):
        # Only pending/ sources are movable: a non-pending local source is another
        # PO's already-attached document. Allowing it would let a caller relocate
        # another tenant's PO document (passing only the containment guard) and make
        # it servable under their own PO row via the tenant-scoped DB lookup.
        from app.services.pdf_service import move_pdf_to_po

        monkeypatch.chdir(tmp_path)
        attached = Path("uploads/purchase_orders/41")
        attached.mkdir(parents=True)
        (attached / "doc.pdf").write_bytes(b"%PDF-1.4 victim")

        with pytest.raises(ValueError, match="not a pending upload"):
            move_pdf_to_po(str(attached / "doc.pdf"), po_id=5, company_id=7)
        assert (attached / "doc.pdf").read_bytes() == b"%PDF-1.4 victim"  # never moved

    def test_move_pdf_to_po_local_missing_source_is_noop(self, tmp_path, monkeypatch):
        # Local equivalence of the s3 missing-source no-op: return the input untouched.
        from app.services.pdf_service import move_pdf_to_po

        monkeypatch.chdir(tmp_path)
        missing = str(Path("uploads/purchase_orders/pending/7/never-written.pdf"))

        assert move_pdf_to_po(missing, po_id=5, company_id=7) == missing
        assert not Path("uploads/purchase_orders/5").exists()  # no destination dir created

    def test_extract_text_from_document_materializes_remote_refs(self, s3_backend, monkeypatch):
        from app.services import pdf_service

        storage_service.override_storage(s3_backend)
        ref = s3_backend.save(b"%PDF-1.4 fake", key="7/purchase_orders/pending/a.pdf")

        seen = {}

        def _fake_pdf_extract(file_path: str):
            seen["path"] = file_path
            return pdf_service.DocumentExtractionResult(text="ok", file_type="pdf")

        monkeypatch.setattr(pdf_service, "extract_text_from_pdf", _fake_pdf_extract)
        result = pdf_service.extract_text_from_document(ref)

        assert result.text == "ok"
        assert not is_s3_ref(seen["path"])  # parser received a REAL local file
        assert seen["path"].endswith(".pdf")
        assert not os.path.exists(seen["path"])  # temp file cleaned up afterwards


@pytest.mark.unit
class TestRfqParsingStorageIntegration:
    def test_parse_rfq_package_files_materializes_s3_refs(self, s3_backend, monkeypatch):
        """parse_rfq_package_files must hand the parsers a REAL local file for s3 rows.

        openpyxl/pdf2image/ezdxf cannot read s3:// refs; the loop materializes each
        ref through ref_as_local_path before dispatching on extension.
        """
        from types import SimpleNamespace

        from app.services import rfq_parsing_service as rfq_parser

        storage_service.override_storage(s3_backend)
        payload = b"%PDF-1.4 rfq drawing"
        ref = s3_backend.save(payload, key="7/rfq_packages/RFQ-1/drawing.pdf")
        record = SimpleNamespace(id=101, file_ext=".pdf", file_name="drawing.pdf", file_path=ref)

        seen = {}

        def _fake_parse_pdf_drawing(file_path: str, name: str):
            seen["path"] = file_path
            seen["existed_at_parse_time"] = os.path.exists(file_path)
            seen["bytes"] = Path(file_path).read_bytes()
            return {
                "file_name": name,
                "source_type": "pdf",
                "part_hint": "P-1",
                "text_length": 500,
                "document_kind": "drawing",
                "bom_items": [],
                "manufactured_parts": [],
            }

        monkeypatch.setattr(rfq_parser, "parse_pdf_drawing", _fake_parse_pdf_drawing)
        result = rfq_parser.parse_rfq_package_files([record])

        assert not is_s3_ref(seen["path"])  # parser got a local path, not the s3 ref
        assert seen["path"].endswith(".pdf")  # extension preserved for type-sniffing
        assert seen["existed_at_parse_time"]
        assert seen["bytes"] == payload
        assert not os.path.exists(seen["path"])  # temp file removed after parsing
        assert result["file_results"][101]["parse_status"] == "parsed"
