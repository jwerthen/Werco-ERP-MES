"""Upload bounds on every AI upload route, and the laser preview's zip caps + offload (roadmap #9-3b).

Each AI route refuses an oversized upload with **413** before it buffers the body, in two layers
(``app.core.upload_limits``):

* **Honest size.** Starlette counts every multipart part into ``UploadFile.size`` while parsing. When
  that count is over the route's cap the route answers 413 without one ``UploadFile.read`` call --
  pinned with a read recorder.
* **Lying size.** When the count is wrong (here: forced to 0 by a parser that never counts), the
  route still reads at most ``cap + 1`` bytes and answers 413 -- pinned by the recorder seeing
  exactly one read of ``cap + 1``.

Every route's caps are patched down to 1 KB, so no test builds a megabyte. Everything downstream of
the read (storage, text extraction, the LLM) is replaced by a function that fails the test if it
runs, so a refusal is proven to happen before any work. The one exception is the test that a
single-nest PDF just past the extractor's ~20 MB native cap is ACCEPTED at the real caps (it posts
about 20 MB), because that is the text fallback this route must keep reachable.

The laser package preview streams to disk under its own 50 MB cap (already 413) and gains three
things, each pinned below: the same honest-size pre-check, zip-bomb caps checked against the
central directory BEFORE any member is extracted, and the temp-file copy (ZIP and bare PDF) and the
extraction running on the default threadpool rather than the event loop. The package IMPORT path is
deliberately untouched here (#9-6 takes it); it shares only the unzip helper, which now writes each
ZIP entry exactly once (``tests/services/test_laser_zip_limits.py``).
"""

import asyncio
import io
import os
import tempfile
import zipfile
from typing import Optional

import pytest
import starlette.formparsers
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile as StarletteUploadFile

import app.api.endpoints.bom as bom_endpoint
import app.api.endpoints.laser_nests as laser_nests_endpoint
import app.api.endpoints.po_upload as po_upload_endpoint
import app.api.endpoints.qms_standards as qms_endpoint
import app.api.endpoints.routing as routing_endpoint
import app.api.endpoints.work_orders as work_orders_endpoint
from app.models.part import Part
from app.models.qms_standard import QMSStandard
from app.services import laser_nest_service, pdf_service

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

CAP = 1024
OVERSIZE = b"%PDF-1.4\n" + b"x" * (CAP * 3)

LASER_PREVIEW_URL = "/api/v1/work-orders/laser-nest-packages/standalone/preview"


def _must_not_run(name: str):
    def _fail(*args, **kwargs):
        raise AssertionError(f"{name} ran for an upload that should have been refused with 413")

    return _fail


@pytest.fixture
def upload_reads(monkeypatch) -> list:
    """The size argument of every ``UploadFile.read`` the app makes during the request."""
    calls: list = []
    original = StarletteUploadFile.read

    async def _recording_read(self, size: int = -1) -> bytes:
        calls.append(size)
        return await original(self, size)

    monkeypatch.setattr(StarletteUploadFile, "read", _recording_read)
    return calls


@pytest.fixture
def lying_upload_size(monkeypatch) -> None:
    """Make Starlette's multipart parser build uploads whose ``size`` stays 0 whatever arrives."""

    class _UncountedUploadFile(StarletteUploadFile):
        async def write(self, data: bytes) -> None:
            reported = self.size
            await super().write(data)
            self.size = reported

    monkeypatch.setattr(starlette.formparsers, "UploadFile", _UncountedUploadFile)


# --------------------------------------------------------------------------------------------------
# The AI document routes
# --------------------------------------------------------------------------------------------------

# route id -> (module, cap constant, file name, content type)
_ROUTES = {
    "po-upload/upload-po": (po_upload_endpoint, "MAX_FILE_SIZE", "po.pdf", "application/pdf"),
    "po-upload/upload-quote": (po_upload_endpoint, "MAX_FILE_SIZE", "quote.pdf", "application/pdf"),
    "po-upload/upload-invoice": (po_upload_endpoint, "MAX_FILE_SIZE", "quote.pdf", "application/pdf"),
    "routing/generate-from-drawing": (routing_endpoint, "MAX_FILE_SIZE", "drawing.pdf", "application/pdf"),
    "qms-standards/{id}/upload-pdf": (qms_endpoint, "QMS_PDF_MAX_BYTES", "standard.pdf", "application/pdf"),
    "bom/import/preview": (bom_endpoint, "BOM_IMPORT_MAX_BYTES", "bom.pdf", "application/pdf"),
    "bom/import": (bom_endpoint, "BOM_IMPORT_MAX_BYTES", "bom.pdf", "application/pdf"),
    "laser-nests/extract": (laser_nests_endpoint, "LASER_NEST_EXTRACT_MAX_BYTES", "nest.pdf", "application/pdf"),
}


@pytest.fixture
def no_downstream_work(monkeypatch) -> None:
    """Storage, text extraction and every LLM entry point behind these routes fail the test if reached."""
    for module, name in [
        (po_upload_endpoint, "save_uploaded_document"),
        (po_upload_endpoint, "extract_text_from_document"),
        (po_upload_endpoint, "extract_po_data_with_llm"),
        (bom_endpoint, "save_uploaded_document"),
        (bom_endpoint, "extract_text_from_document"),
        (bom_endpoint, "extract_bom_data_with_llm"),
        (qms_endpoint, "run_llm_task"),
        (laser_nests_endpoint, "extract_nest_fields_from_pdf"),
        # routing imports its extractor at call time from the module, after writing the drawing.
        (pdf_service, "extract_text_from_pdf"),
    ]:
        monkeypatch.setattr(module, name, _must_not_run(f"{module.__name__}.{name}"))


@pytest.fixture
def capped_routes(monkeypatch) -> None:
    # raising=False: ``test_default_caps`` pins that every constant exists; here a missing one must
    # surface as the route's real behavior (no 413), not as a fixture error.
    for module, cap_name, _, _ in _ROUTES.values():
        monkeypatch.setattr(module, cap_name, CAP, raising=False)


def _post(client: TestClient, db: Session, headers: dict, route: str, body: bytes):
    _, _, file_name, content_type = _ROUTES[route]
    data: dict = {}
    url = f"/api/v1/{route}"
    if route == "routing/generate-from-drawing":
        part = Part(
            part_number="UPL-BOUND-1",
            name="Bracket",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=1,
        )
        db.add(part)
        db.commit()
        data["part_id"] = str(part.id)
    elif route == "qms-standards/{id}/upload-pdf":
        standard = QMSStandard(company_id=1, name="AS9100D", version="D", standard_body="SAE", is_active=True)
        db.add(standard)
        db.commit()
        url = f"/api/v1/qms-standards/{standard.id}/upload-pdf"
    return client.post(url, headers=headers, data=data, files={"file": (file_name, io.BytesIO(body), content_type)})


@pytest.mark.usefixtures("capped_routes", "no_downstream_work")
class TestAiDocumentRoutes:
    @pytest.mark.parametrize("route", sorted(_ROUTES))
    def test_size_over_the_cap_is_refused_413_before_the_body_is_read(
        self, client, db_session, admin_headers, upload_reads, route
    ):
        resp = _post(client, db_session, admin_headers, route, OVERSIZE)

        assert resp.status_code == 413, resp.text
        assert resp.json()["detail"] == "File too large (max 1 KB)."
        assert upload_reads == [], "the pre-check must refuse before any UploadFile.read"

    @pytest.mark.usefixtures("lying_upload_size")
    @pytest.mark.parametrize("route", sorted(_ROUTES))
    def test_body_over_the_cap_behind_a_lying_size_is_refused_413_after_a_bounded_read(
        self, client, db_session, admin_headers, upload_reads, route
    ):
        resp = _post(client, db_session, admin_headers, route, OVERSIZE)

        assert resp.status_code == 413, resp.text
        assert resp.json()["detail"] == "File too large (max 1 KB)."
        assert upload_reads == [CAP + 1], "the read must ask for at most cap + 1 bytes"


def test_default_caps():
    """The production caps. PO and routing keep their 10 MB and QMS its 20 MB, now as 413 instead
    of 400. BOM had none (20 MB, like QMS). The single-nest PDF gets the laser package's 50 MB,
    deliberately ABOVE the extractor's 20 MB native-document cap
    (``laser_nest_extraction_service._MAX_NATIVE_PDF_BYTES``): a PDF between the two is still read
    through the flattened-text fallback, as it was before the cap and as the bare-PDF package
    preview still does. Capping at the native size would have turned that fallback into a 413."""
    from app.services.laser_nest_extraction_service import _MAX_NATIVE_PDF_BYTES

    mb = 1024 * 1024
    assert po_upload_endpoint.MAX_FILE_SIZE == 10 * mb
    assert routing_endpoint.MAX_FILE_SIZE == 10 * mb
    assert qms_endpoint.QMS_PDF_MAX_BYTES == 20 * mb
    assert bom_endpoint.BOM_IMPORT_MAX_BYTES == 20 * mb
    assert work_orders_endpoint.LASER_UPLOAD_MAX_BYTES == 50 * mb
    assert laser_nests_endpoint.LASER_NEST_EXTRACT_MAX_BYTES == work_orders_endpoint.LASER_UPLOAD_MAX_BYTES
    assert laser_nests_endpoint.LASER_NEST_EXTRACT_MAX_BYTES > _MAX_NATIVE_PDF_BYTES


def test_laser_extract_accepts_a_pdf_past_the_native_cap_so_the_text_fallback_stays_reachable(
    client, admin_headers, monkeypatch
):
    """Review finding F2: a single-nest PDF just over the extractor's native-document cap reaches
    the extractor (which reads it through the flattened-text fallback) instead of being refused.

    Real caps, no patching: the body is one native cap plus a PDF header, about 20 MB.
    """
    from app.services.laser_nest_extraction_service import _MAX_NATIVE_PDF_BYTES

    seen: dict = {}

    def _fake_extract(pdf_path, file_name, company_id=None):
        seen["bytes"] = os.path.getsize(pdf_path)
        return {"cnc_number": "05749", "source": "ai", "extraction_confidence": "low", "warning": None, "passes": 1}

    monkeypatch.setattr(laser_nests_endpoint, "extract_nest_fields_from_pdf", _fake_extract)
    body = b"%PDF-1.4\n" + b"\0" * _MAX_NATIVE_PDF_BYTES
    assert len(body) > _MAX_NATIVE_PDF_BYTES

    resp = client.post(
        "/api/v1/laser-nests/extract",
        headers=admin_headers,
        files={"file": ("nest.pdf", io.BytesIO(body), "application/pdf")},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["cnc_number"] == "05749"
    assert seen["bytes"] == len(body)


# --------------------------------------------------------------------------------------------------
# The laser package preview
# --------------------------------------------------------------------------------------------------


def _zip(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _preview(client: TestClient, headers: dict, body: bytes, *, name: str = "nests.zip"):
    return client.post(LASER_PREVIEW_URL, headers=headers, files={"file": (name, io.BytesIO(body), "application/zip")})


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@pytest.fixture
def laser_upload_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))


@pytest.fixture
def no_laser_ai(monkeypatch) -> None:
    monkeypatch.setattr(work_orders_endpoint, "segment_nest_pdf", _must_not_run("segment_nest_pdf"))
    monkeypatch.setattr(
        work_orders_endpoint, "extract_nest_fields_from_pdf", _must_not_run("extract_nest_fields_from_pdf")
    )


@pytest.fixture
def no_zip_extraction(monkeypatch) -> None:
    """Fail the test if any package member is extracted (both extract helpers call this)."""
    monkeypatch.setattr(laser_nest_service, "_safe_extract_zip", _must_not_run("_safe_extract_zip"))


@pytest.mark.usefixtures("laser_upload_dir", "no_laser_ai")
class TestLaserPackagePreview:
    def test_size_over_the_cap_is_refused_413_before_anything_is_copied(self, client, admin_headers, monkeypatch):
        monkeypatch.setattr(work_orders_endpoint, "LASER_UPLOAD_MAX_BYTES", CAP)
        monkeypatch.setattr(work_orders_endpoint, "_save_upload_to_temp", _must_not_run("_save_upload_to_temp"))

        resp = _preview(client, admin_headers, b"PK" + b"x" * (CAP * 3))

        assert resp.status_code == 413, resp.text
        assert resp.json()["detail"] == "Upload exceeds the 1 KB limit. Split the package into smaller batches."

    @pytest.mark.usefixtures("lying_upload_size", "no_zip_extraction")
    def test_body_over_the_cap_behind_a_lying_size_is_refused_413_by_the_streaming_copy(
        self, client, admin_headers, monkeypatch
    ):
        monkeypatch.setattr(work_orders_endpoint, "LASER_UPLOAD_MAX_BYTES", CAP)

        resp = _preview(client, admin_headers, b"PK" + b"x" * (CAP * 3))

        assert resp.status_code == 413, resp.text
        assert "1 KB limit" in resp.json()["detail"]

    @pytest.mark.usefixtures("no_zip_extraction")
    def test_too_many_members_is_refused_413_and_nothing_is_extracted(self, client, admin_headers, monkeypatch):
        monkeypatch.setattr(laser_nest_service, "LASER_ZIP_MAX_MEMBERS", 3, raising=False)

        resp = _preview(client, admin_headers, _zip({f"{n:05d}.cnc": "M30" for n in range(4)}))

        assert resp.status_code == 413, resp.text
        detail = resp.json()["detail"]
        assert "4 files and folders" in detail and "the limit is 3" in detail

    @pytest.mark.usefixtures("no_zip_extraction")
    def test_too_large_unpacked_is_refused_413_and_nothing_is_extracted(self, client, admin_headers, monkeypatch):
        monkeypatch.setattr(laser_nest_service, "LASER_ZIP_MAX_UNCOMPRESSED_BYTES", 16 * 1024, raising=False)
        bomb = _zip({"a.pdf": b"\0" * (32 * 1024)})
        assert len(bomb) < CAP  # tiny on the wire; only the central directory gives it away

        resp = _preview(client, admin_headers, bomb)

        assert resp.status_code == 413, resp.text
        assert "too large once unpacked (over the 16 KB limit)" in resp.json()["detail"]

    def test_temp_copy_and_extraction_run_off_the_event_loop(self, client, admin_headers, monkeypatch):
        """A legacy CNC package (no AI) previews normally, with the copy and the unzip on worker threads."""
        seen: dict = {}
        original_mkstemp = tempfile.mkstemp
        original_extract = laser_nest_service._safe_extract_zip

        def _recording_mkstemp(*args, **kwargs):
            suffix: Optional[str] = kwargs.get("suffix", args[0] if args else None)
            if suffix == ".zip":
                seen["copy_on_loop"] = _on_event_loop()
            return original_mkstemp(*args, **kwargs)

        def _recording_extract(*args, **kwargs):
            seen["extract_on_loop"] = _on_event_loop()
            return original_extract(*args, **kwargs)

        monkeypatch.setattr(tempfile, "mkstemp", _recording_mkstemp)
        monkeypatch.setattr(laser_nest_service, "_safe_extract_zip", _recording_extract)

        resp = _preview(client, admin_headers, _zip({"05749.cnc": "M30", "05750.cnc": "M30"}))

        assert resp.status_code == 200, resp.text
        assert len(resp.json()["nests"]) == 2
        assert seen == {"copy_on_loop": False, "extract_on_loop": False}

    def test_bare_pdf_temp_copy_runs_off_the_event_loop(self, client, admin_headers, monkeypatch):
        """Review finding F3: the bare-PDF branch copies its upload on a worker thread too.

        A one-page PDF previews with segmentation and extraction stubbed; the ``.pdf`` temp file
        must be created off the event loop (``_save_upload_to_temp(..., offload=True)``).
        """
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        pdf = io.BytesIO()
        writer.write(pdf)

        monkeypatch.setattr(
            work_orders_endpoint,
            "segment_nest_pdf",
            lambda pdf_path, file_name, page_count, company_id=None: {
                "nests": [{"pages": [1], "cnc_number_hint": None}],
                "skipped_pages": [],
                "confidence": "high",
                "warning": None,
            },
        )
        monkeypatch.setattr(
            work_orders_endpoint,
            "extract_nest_fields_from_pdf",
            lambda pdf_path, file_name, company_id=None, *, cnc_hint=None, filename_is_cnc_hint=True: {
                "cnc_number": "05749",
                "extraction_confidence": "high",
                "source": "ai",
                "warning": None,
                "passes": 1,
            },
        )
        seen: list = []
        original_mkstemp = tempfile.mkstemp

        def _recording_mkstemp(*args, **kwargs):
            suffix: Optional[str] = kwargs.get("suffix", args[0] if args else None)
            if suffix == ".pdf":
                seen.append(_on_event_loop())
            return original_mkstemp(*args, **kwargs)

        monkeypatch.setattr(tempfile, "mkstemp", _recording_mkstemp)

        resp = client.post(
            LASER_PREVIEW_URL,
            headers=admin_headers,
            files={"file": ("nests.pdf", io.BytesIO(pdf.getvalue()), "application/pdf")},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["source_page_count"] == 1  # the bare-PDF branch ran, not the ZIP one
        assert [row["cnc_number"] for row in resp.json()["nests"]] == ["05749"]
        assert seen == [False], "the bare-PDF temp copy must run on a worker thread, not the event loop"
