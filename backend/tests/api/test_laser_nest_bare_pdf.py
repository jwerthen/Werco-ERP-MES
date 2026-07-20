"""Bare multi-page laser-nest PDF upload: preview (segment + split + extract)
and confirm-and-commit import (re-split by confirmed ``source_pages``).

The new bare-PDF shape on the standalone endpoints:
  - ``POST /work-orders/laser-nest-packages/standalone/preview`` with a single
    ``application/pdf`` upload — page count is read, AI segmentation (pass 0)
    groups pages into nests, the PDF is split DETERMINISTICALLY into per-segment
    files (``nest-p{first:03d}[-p{last:03d}].pdf``), and each segment runs the
    per-nest extraction. Rows carry ``source_pages`` / ``field_confidence`` /
    ``warning`` / ``passes``; the response carries ``source_page_count`` /
    ``skipped_pages`` / ``segmentation_warning``.
  - ``POST .../standalone/import`` with ``rows`` — the re-sent PDF is re-split
    by each row's confirmed ``source_pages`` (NO AI on the commit path); each
    row's ``source_file`` must equal the name derived from its pages, else the
    preview is stale (400).

Offline by contract: ``segment_nest_pdf`` and ``extract_nest_fields_from_pdf``
are monkeypatched at the endpoint import site. The PDFs themselves are REAL
(built in-test with pypdf) because the endpoint reads real page counts and the
split/Document paths shuffle real PDF bytes — the stored per-nest Documents are
re-parsed to prove the right pages landed in the right segment.
"""

import io
import json
import zipfile

import pytest
from fastapi import status
from pypdf import PdfReader, PdfWriter
from sqlalchemy.orm import Session

import app.api.endpoints.work_orders as work_orders_endpoint
from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.document import Document
from app.models.laser_nest import LaserNest, LaserNestPackage
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
_seq = {"n": 0}

# Distinct width per source page so a split segment's pages are identifiable
# after the round trip: source page N (1-based) has width _BASE_WIDTH + N.
_BASE_WIDTH = 200.0


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> None:
    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True))
        db.commit()


def make_user(db: Session, *, role: UserRole = UserRole.ADMIN, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"barepdf-{n}@co{company_id}.test",
        employee_id=f"BAREPDF-{n:05d}",
        first_name="Bare",
        last_name=f"Co{company_id}",
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=True,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def make_laser_work_center(db: Session, *, company_id: int = COMPANY_A) -> WorkCenter:
    _ensure_company(db, company_id)
    n = _next()
    wc = WorkCenter(
        name=f"Laser Cutter {n}",
        code=f"LASER-BP-{n}",
        work_center_type="laser",
        description="laser fixture",
        hourly_rate=120,
        is_active=True,
        company_id=company_id,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def _pdf_bytes(page_count: int) -> bytes:
    """A real PDF whose page N (1-based) is blank with width _BASE_WIDTH + N."""
    writer = PdfWriter()
    for n in range(1, page_count + 1):
        writer.add_blank_page(width=_BASE_WIDTH + n, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _source_page_numbers(pdf_path: str) -> list[int]:
    """Recover which ORIGINAL pages a stored segment PDF contains (width tag)."""
    reader = PdfReader(pdf_path)
    return [round(float(page.mediabox.width) - _BASE_WIDTH) for page in reader.pages]


def _pdf_zip(*names: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in names:
            zf.writestr(name, b"%PDF-1.4\n%stub nest report\n")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def upload_dir(tmp_path, monkeypatch):
    """Keep storage + laser package roots hermetic (same as the PDF-import tests)."""
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))


@pytest.fixture
def no_segmentation(monkeypatch):
    """The commit path (and ZIP previews) must never re-run AI segmentation."""
    monkeypatch.setattr(
        work_orders_endpoint,
        "segment_nest_pdf",
        lambda *a, **k: pytest.fail("segment_nest_pdf must not be called on this path"),
    )


@pytest.fixture
def no_extraction(monkeypatch):
    """The commit path must never re-run the per-nest AI extraction."""
    monkeypatch.setattr(
        work_orders_endpoint,
        "extract_nest_fields_from_pdf",
        lambda *a, **k: pytest.fail("extract_nest_fields_from_pdf must not be called on this path"),
    )


def _mock_segmentation(monkeypatch, result: dict):
    """Stub the AI segmentation pass; records (file_name, page_count, company_id)."""
    calls = []

    def _fake(pdf_path, file_name, page_count, company_id=None):
        calls.append((file_name, page_count, company_id))
        return result

    monkeypatch.setattr(work_orders_endpoint, "segment_nest_pdf", _fake)
    return calls


def _mock_extraction(monkeypatch, table: dict):
    """Stub the per-segment extractor, keyed by (deterministic) segment name.

    Accepts (and records) the bare-PDF-path keyword args so tests can assert
    the endpoint disables the filename-as-CNC hint and threads segmentation
    hints through.
    """
    calls = []

    def _fake(pdf_path, file_name, company_id=None, *, cnc_hint=None, filename_is_cnc_hint=True):
        calls.append((file_name, company_id, cnc_hint, filename_is_cnc_hint))
        base = table.get(file_name, {"cnc_number": file_name.rsplit(".", 1)[0], "extraction_confidence": "low"})
        return {"source": "ai", "warning": None, **base}

    monkeypatch.setattr(work_orders_endpoint, "extract_nest_fields_from_pdf", _fake)
    return calls


def _preview_pdf(client, headers, pdf, *, name="nests.pdf", content_type="application/pdf"):
    return client.post(
        "/api/v1/work-orders/laser-nest-packages/standalone/preview",
        headers=headers,
        files={"file": (name, io.BytesIO(pdf), content_type)},
    )


def _import_pdf(
    client, headers, pdf, *, rows=None, work_center_id=None, name="nests.pdf", content_type="application/pdf"
):
    data = {}
    if rows is not None:
        data["rows"] = json.dumps(rows)
    if work_center_id is not None:
        data["work_center_id"] = str(work_center_id)
    return client.post(
        "/api/v1/work-orders/laser-nest-packages/standalone/import",
        headers=headers,
        data=data,
        files={"file": (name, io.BytesIO(pdf), content_type)},
    )


# A 4-page upload segmented into two nests ([1,2] and [3]) with page 4 skipped.
_SEGMENTATION_2_NESTS = {
    "nests": [
        {"pages": [1, 2], "cnc_number_hint": "05749"},
        {"pages": [3], "cnc_number_hint": None},
    ],
    "skipped_pages": [4],
    "confidence": "high",
    "warning": None,
}

_EXTRACTION_TABLE = {
    "nest-p001-p002.pdf": {
        "cnc_number": "05749",
        "material": "A36",
        "thickness": "0.25in",
        "sheet_size": "72.5x120",
        "planned_runs": 3,
        "confidence": {"cnc_number": "high", "material": "low", "thickness": "high"},
        "extraction_confidence": "low",
        "warning": None,
        "passes": 2,
    },
    "nest-p003.pdf": {
        "cnc_number": "05750",
        "material": "304SS",
        "thickness": "10ga",
        "sheet_size": "48x96",
        "planned_runs": 2,
        "confidence": {"cnc_number": "high", "material": "high"},
        "extraction_confidence": "high",
        "warning": "Verification pass skipped: API error: boom",
        "passes": 1,
    },
}


# --------------------------------------------------------------------------- #
# Preview
# --------------------------------------------------------------------------- #
class TestBarePdfPreview:
    def test_preview_segments_splits_and_extracts(self, client, db_session, monkeypatch):
        seg_calls = _mock_segmentation(monkeypatch, dict(_SEGMENTATION_2_NESTS))
        extract_calls = _mock_extraction(monkeypatch, _EXTRACTION_TABLE)
        admin = make_user(db_session)

        resp = _preview_pdf(client, headers_for(admin), _pdf_bytes(4))

        assert resp.status_code == status.HTTP_200_OK, resp.text
        data = resp.json()

        # Package-level bare-PDF extras.
        assert data["source_page_count"] == 4
        assert data["skipped_pages"] == [4]
        assert data["segmentation_warning"] is None
        assert data["nest_count"] == 2
        assert data["total_planned_runs"] == 5  # 3 + 2

        rows = {row["source_file"]: row for row in data["nests"]}
        assert set(rows) == {"nest-p001-p002.pdf", "nest-p003.pdf"}

        multi = rows["nest-p001-p002.pdf"]
        # The segment's page list rides on the row so import can re-split.
        assert multi["source_pages"] == [1, 2]
        assert multi["cnc_number"] == "05749"
        assert multi["material"] == "A36"
        assert multi["planned_runs"] == 3
        assert multi["confidence"] == "low"
        assert multi["field_confidence"] == {"cnc_number": "high", "material": "low", "thickness": "high"}
        assert multi["passes"] == 2
        assert multi["warning"] is None

        single = rows["nest-p003.pdf"]
        assert single["source_pages"] == [3]
        assert single["confidence"] == "high"
        assert single["passes"] == 1
        assert "Verification pass skipped" in single["warning"]

        # Segmentation ran once with the real page count, tenant-scoped.
        assert seg_calls == [("nests.pdf", 4, admin.company_id)]
        # Extraction ran once per SEGMENT (not per page): the skipped page got no call.
        assert sorted(name for name, _, _, _ in extract_calls) == ["nest-p001-p002.pdf", "nest-p003.pdf"]
        assert {company for _, company, _, _ in extract_calls} == {admin.company_id}
        # Synthetic split names must not be presented to the model as CNC
        # numbers; segmentation hints (when present) are threaded per segment.
        assert all(flag is False for _, _, _, flag in extract_calls)
        hints = {name: hint for name, _, hint, _ in extract_calls}
        assert hints["nest-p001-p002.pdf"] == _SEGMENTATION_2_NESTS["nests"][0].get("cnc_number_hint")
        # A preview persists nothing.
        assert db_session.query(WorkOrder).count() == 0
        assert db_session.query(LaserNest).count() == 0

    def test_preview_surfaces_degraded_segmentation_warning(self, client, db_session, monkeypatch):
        warning = "AI segmentation response failed validation; defaulted to one nest per page"
        _mock_segmentation(
            monkeypatch,
            {
                "nests": [{"pages": [page], "cnc_number_hint": None} for page in (1, 2, 3)],
                "skipped_pages": [],
                "confidence": "low",
                "warning": warning,
            },
        )
        _mock_extraction(monkeypatch, {})
        admin = make_user(db_session)

        resp = _preview_pdf(client, headers_for(admin), _pdf_bytes(3))

        assert resp.status_code == status.HTTP_200_OK, resp.text
        data = resp.json()
        assert data["segmentation_warning"] == warning
        assert data["source_page_count"] == 3
        assert data["skipped_pages"] == []
        assert data["nest_count"] == 3
        assert [row["source_pages"] for row in data["nests"]] == [[1], [2], [3]]

    def test_preview_unreadable_pdf_400_without_ai(self, client, db_session, no_segmentation, no_extraction):
        admin = make_user(db_session)

        resp = _preview_pdf(client, headers_for(admin), b"this is not a pdf at all")

        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        assert resp.json()["detail"] == "Could not read the PDF"

    def test_preview_over_page_cap_400_without_ai(
        self, client, db_session, monkeypatch, no_segmentation, no_extraction
    ):
        monkeypatch.setattr(work_orders_endpoint, "LASER_PDF_PACKAGE_MAX", 2, raising=False)
        admin = make_user(db_session)

        resp = _preview_pdf(client, headers_for(admin), _pdf_bytes(3))

        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        detail = resp.json()["detail"]
        assert "3 pages" in detail and "limit is 2" in detail

    def test_preview_requires_privileged_role(self, client, db_session, no_segmentation, no_extraction):
        operator = make_user(db_session, role=UserRole.OPERATOR)
        resp = _preview_pdf(client, headers_for(operator), _pdf_bytes(2))
        assert resp.status_code == status.HTTP_403_FORBIDDEN


# --------------------------------------------------------------------------- #
# Import (confirm-and-commit): re-split by confirmed source_pages, no AI
# --------------------------------------------------------------------------- #
class TestBarePdfImport:
    ROWS = [
        {
            "source_file": "nest-p001-p002.pdf",
            "cnc_number": "05749",
            "planned_runs": 3,
            "material": "A36",
            "thickness": "0.25in",
            "sheet_size": "72.5x120",
            "source_pages": [1, 2],
        },
        {
            "source_file": "nest-p003.pdf",
            "cnc_number": "05750",
            "planned_runs": 2,
            "material": "304SS",
            "source_pages": [3],
        },
    ]

    def test_import_creates_wo_and_per_segment_documents_with_correct_pages(
        self, client, db_session, no_segmentation, no_extraction
    ):
        admin = make_user(db_session)
        wc = make_laser_work_center(db_session)

        resp = _import_pdf(client, headers_for(admin), _pdf_bytes(4), rows=self.ROWS, work_center_id=wc.id)

        assert resp.status_code == status.HTTP_200_OK, resp.text
        child = resp.json()["child_work_order"]
        # Standalone: fresh part-less released laser WO sized to total runs.
        assert child["work_order_type"] == "laser_cutting"
        assert child["part_id"] is None
        assert child["parent_work_order_id"] is None
        assert child["status"] == "released"
        assert child["quantity_ordered"] == 5  # 3 + 2
        assert len(child["operations"]) == 2

        package = db_session.query(LaserNestPackage).filter_by(child_work_order_id=child["id"]).one()
        nests = {n.cnc_number: n for n in db_session.query(LaserNest).filter_by(package_id=package.id).all()}
        assert set(nests) == {"05749", "05750"}
        assert nests["05749"].planned_runs == 3
        assert nests["05749"].material == "A36"
        assert nests["05750"].planned_runs == 2

        # THE bare-PDF property: each nest's Document is the re-split segment
        # containing exactly its confirmed source pages of the original upload.
        doc_1 = db_session.query(Document).filter_by(id=nests["05749"].document_id).one()
        doc_2 = db_session.query(Document).filter_by(id=nests["05750"].document_id).one()
        assert doc_1.mime_type == "application/pdf"
        assert doc_1.file_name == "nest-p001-p002.pdf"
        assert _source_page_numbers(doc_1.file_path) == [1, 2]
        assert doc_2.file_name == "nest-p003.pdf"
        assert _source_page_numbers(doc_2.file_path) == [3]
        # Standalone: documents scope to the created laser WO itself.
        assert doc_1.work_order_id == child["id"]
        assert {doc_1.company_id, doc_2.company_id} == {admin.company_id}

        # Audit: one CREATE per nest, tagged as a PDF import.
        creates = (
            db_session.query(AuditLog).filter(AuditLog.resource_type == "laser_nest", AuditLog.action == "CREATE").all()
        )
        assert len(creates) == 2
        assert {row.resource_identifier for row in creates} == {"05749", "05750"}
        assert all(row.extra_data.get("source") == "pdf_import" for row in creates)

    def _assert_nothing_committed(self, db_session):
        assert db_session.query(WorkOrder).count() == 0
        assert db_session.query(LaserNest).count() == 0
        assert db_session.query(Document).count() == 0

    def test_bare_pdf_without_rows_is_rejected(self, client, db_session, no_segmentation, no_extraction):
        """A bare PDF is confirm-and-commit ONLY — without confirmed rows there
        is nothing trustworthy to persist (the legacy no-rows import is
        CNC-programs-only)."""
        admin = make_user(db_session)

        resp = _import_pdf(client, headers_for(admin), _pdf_bytes(4), rows=None)

        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        assert resp.json()["detail"] == "Preview the PDF first, then confirm the rows"
        self._assert_nothing_committed(db_session)

    def test_row_missing_source_pages_is_rejected(self, client, db_session, no_segmentation, no_extraction):
        admin = make_user(db_session)
        rows = [{"source_file": "nest-p001-p002.pdf", "cnc_number": "05749", "planned_runs": 3}]

        resp = _import_pdf(client, headers_for(admin), _pdf_bytes(4), rows=rows)

        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        assert "source_pages" in resp.json()["detail"]
        self._assert_nothing_committed(db_session)

    def test_stale_source_file_name_is_rejected(self, client, db_session, no_segmentation, no_extraction):
        """source_file must equal the name the split derives from source_pages;
        a mismatch means the rows came from a different preview."""
        admin = make_user(db_session)
        rows = [{"source_file": "nest-p001.pdf", "cnc_number": "05749", "planned_runs": 3, "source_pages": [1, 2]}]

        resp = _import_pdf(client, headers_for(admin), _pdf_bytes(4), rows=rows)

        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        detail = resp.json()["detail"]
        assert "stale" in detail and "nest-p001-p002.pdf" in detail
        self._assert_nothing_committed(db_session)

    @pytest.mark.parametrize(
        "bad_pages",
        [[], [0], [-1], [1, 3], [2, 1]],
        ids=["empty", "zero", "negative", "non_consecutive", "descending"],
    )
    def test_malformed_source_pages_shapes_are_schema_rejected(
        self, client, db_session, no_segmentation, no_extraction, bad_pages
    ):
        admin = make_user(db_session)
        rows = [{"source_file": "nest-p001.pdf", "cnc_number": "05749", "planned_runs": 1, "source_pages": bad_pages}]

        resp = _import_pdf(client, headers_for(admin), _pdf_bytes(4), rows=rows)

        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        assert "Invalid nest rows" in resp.json()["detail"]
        self._assert_nothing_committed(db_session)

    def test_out_of_range_source_pages_rejected_by_split(self, client, db_session, no_segmentation, no_extraction):
        """[4, 5] is a VALID shape but page 5 does not exist in a 4-page PDF —
        the deterministic re-split rejects it (ValueError -> 400)."""
        admin = make_user(db_session)
        rows = [{"source_file": "nest-p004-p005.pdf", "cnc_number": "05751", "planned_runs": 1, "source_pages": [4, 5]}]

        resp = _import_pdf(client, headers_for(admin), _pdf_bytes(4), rows=rows)

        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        assert "out of range" in resp.json()["detail"]
        self._assert_nothing_committed(db_session)

    def test_duplicate_segments_rejected(self, client, db_session, no_segmentation, no_extraction):
        """Identical ranges are caught by the page-disjointness check (which
        fires before split_pdf_segments' duplicate-name ValueError)."""
        admin = make_user(db_session)
        rows = [
            {"source_file": "nest-p001-p002.pdf", "cnc_number": "05749", "planned_runs": 1, "source_pages": [1, 2]},
            {"source_file": "nest-p001-p002.pdf", "cnc_number": "05749-DUP", "planned_runs": 2, "source_pages": [1, 2]},
        ]

        resp = _import_pdf(client, headers_for(admin), _pdf_bytes(4), rows=rows)

        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        assert "overlap" in resp.json()["detail"]
        self._assert_nothing_committed(db_session)

    def test_overlapping_distinct_segments_rejected(self, client, db_session, no_segmentation, no_extraction):
        """Overlapping-but-distinct ranges ([1,2] and [2,3]) pass per-row schema
        validation and derive distinct names, but would land page 2 in TWO
        nests' Documents -- the commit path enforces page-disjointness."""
        admin = make_user(db_session)
        rows = [
            {"source_file": "nest-p001-p002.pdf", "cnc_number": "05749", "planned_runs": 1, "source_pages": [1, 2]},
            {"source_file": "nest-p002-p003.pdf", "cnc_number": "05750", "planned_runs": 2, "source_pages": [2, 3]},
        ]

        resp = _import_pdf(client, headers_for(admin), _pdf_bytes(4), rows=rows)

        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        detail = resp.json()["detail"]
        assert "overlap" in detail and "[2]" in detail
        self._assert_nothing_committed(db_session)

    def test_unreadable_pdf_rejected(self, client, db_session, no_segmentation, no_extraction):
        admin = make_user(db_session)
        rows = [{"source_file": "nest-p001.pdf", "cnc_number": "05749", "planned_runs": 1, "source_pages": [1]}]

        resp = _import_pdf(client, headers_for(admin), b"garbage bytes", rows=rows)

        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        assert resp.json()["detail"] == "Could not read the PDF"
        self._assert_nothing_committed(db_session)

    def test_over_page_cap_rejected(self, client, db_session, monkeypatch, no_segmentation, no_extraction):
        monkeypatch.setattr(work_orders_endpoint, "LASER_PDF_PACKAGE_MAX", 2, raising=False)
        admin = make_user(db_session)
        rows = [{"source_file": "nest-p001.pdf", "cnc_number": "05749", "planned_runs": 1, "source_pages": [1]}]

        resp = _import_pdf(client, headers_for(admin), _pdf_bytes(3), rows=rows)

        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        assert "limit is 2" in resp.json()["detail"]
        self._assert_nothing_committed(db_session)


# --------------------------------------------------------------------------- #
# Round trip: the preview's own rows echo back into a clean import
# --------------------------------------------------------------------------- #
class TestPreviewToImportRoundTrip:
    def test_preview_rows_echoed_verbatim_import_cleanly(self, client, db_session, monkeypatch):
        """The wizard's contract end-to-end: preview a 4-page PDF, echo the rows
        it returned (source_file + source_pages verbatim) into the import, and
        the commit path re-derives the exact same segment names."""
        _mock_segmentation(monkeypatch, dict(_SEGMENTATION_2_NESTS))
        _mock_extraction(monkeypatch, _EXTRACTION_TABLE)
        admin = make_user(db_session)
        wc = make_laser_work_center(db_session)
        pdf = _pdf_bytes(4)

        preview = _preview_pdf(client, headers_for(admin), pdf)
        assert preview.status_code == status.HTTP_200_OK, preview.text

        rows = [
            {
                "source_file": row["source_file"],
                "cnc_number": row["cnc_number"],
                "planned_runs": row["planned_runs"],
                "material": row["material"],
                "source_pages": row["source_pages"],
            }
            for row in preview.json()["nests"]
        ]

        # The commit path must not touch either AI entry point.
        monkeypatch.setattr(
            work_orders_endpoint,
            "segment_nest_pdf",
            lambda *a, **k: pytest.fail("import must not re-run segmentation"),
        )
        monkeypatch.setattr(
            work_orders_endpoint,
            "extract_nest_fields_from_pdf",
            lambda *a, **k: pytest.fail("import must not re-run extraction"),
        )

        imported = _import_pdf(client, headers_for(admin), pdf, rows=rows, work_center_id=wc.id)
        assert imported.status_code == status.HTTP_200_OK, imported.text
        child = imported.json()["child_work_order"]
        assert child["quantity_ordered"] == 5
        assert db_session.query(LaserNest).count() == 2


# --------------------------------------------------------------------------- #
# ZIP path regression: unchanged behavior, no bare-PDF extras
# --------------------------------------------------------------------------- #
class TestZipPathUnchanged:
    def test_zip_preview_has_no_bare_pdf_extras_and_skips_segmentation(
        self, client, db_session, monkeypatch, no_segmentation
    ):
        _mock_extraction(
            monkeypatch,
            {
                "05749.pdf": {
                    "cnc_number": "05749",
                    "material": "A36",
                    "planned_runs": 3,
                    "extraction_confidence": "high",
                }
            },
        )
        admin = make_user(db_session)

        resp = client.post(
            "/api/v1/work-orders/laser-nest-packages/standalone/preview",
            headers=headers_for(admin),
            files={"file": ("nests.zip", io.BytesIO(_pdf_zip("05749.pdf")), "application/zip")},
        )

        assert resp.status_code == status.HTTP_200_OK, resp.text
        data = resp.json()
        # None of the bare-PDF package extras are set on the ZIP path.
        assert data["source_page_count"] is None
        assert data["skipped_pages"] is None
        assert data["segmentation_warning"] is None
        # And the rows carry no page lists / per-field confidence extras.
        row = data["nests"][0]
        assert row["source_file"] == "05749.pdf"
        assert row["source_pages"] is None
        assert row["field_confidence"] is None

    def test_zip_import_without_source_pages_still_works(self, client, db_session, no_segmentation, no_extraction):
        """ZIP rows never carry source_pages; the import must NOT demand them
        (that requirement is bare-PDF-only)."""
        admin = make_user(db_session)
        wc = make_laser_work_center(db_session)
        rows = [{"source_file": "05749.pdf", "cnc_number": "05749", "planned_runs": 3, "material": "A36"}]

        resp = client.post(
            "/api/v1/work-orders/laser-nest-packages/standalone/import",
            headers=headers_for(admin),
            data={"rows": json.dumps(rows), "work_center_id": str(wc.id)},
            files={"file": ("nests.zip", io.BytesIO(_pdf_zip("05749.pdf")), "application/zip")},
        )

        assert resp.status_code == status.HTTP_200_OK, resp.text
        child = resp.json()["child_work_order"]
        assert child["quantity_ordered"] == 3
        nest = db_session.query(LaserNest).one()
        assert nest.cnc_number == "05749"
        assert nest.document_id is not None


class TestUploadByteCap:
    """The streaming byte cap rejects oversized bodies BEFORE pypdf/AI work."""

    def test_oversized_upload_rejected_413(self, client, db_session, monkeypatch, no_segmentation, no_extraction):
        monkeypatch.setattr(work_orders_endpoint, "LASER_UPLOAD_MAX_BYTES", 64)
        admin = make_user(db_session)

        resp = _preview_pdf(client, headers_for(admin), _pdf_bytes(2))

        assert resp.status_code == 413, resp.text
        assert "limit" in resp.json()["detail"]

    def test_normal_upload_under_cap_unaffected(self, client, db_session, monkeypatch):
        _mock_segmentation(monkeypatch, dict(_SEGMENTATION_2_NESTS))
        _mock_extraction(monkeypatch, _EXTRACTION_TABLE)
        admin = make_user(db_session)

        resp = _preview_pdf(client, headers_for(admin), _pdf_bytes(4))

        assert resp.status_code == 200, resp.text
