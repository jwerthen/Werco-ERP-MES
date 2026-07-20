"""Batch laser-nest PDF package: preview + confirm-and-commit import.

The new PDF-package path on the work-orders router:
  - ``POST /work-orders/{id}/laser-nest-packages/preview`` -- a ZIP of nest-report
    PDFs is detected, each PDF is AI-extracted, and editable rows are returned.
  - ``POST /work-orders/{id}/laser-nest-packages/import`` with a ``rows`` JSON
    form field -- the planner-CONFIRMED rows are persisted (no second AI call);
    each nest's PDF is stored as a DRAWING Document and attached, an audit
    ``CREATE`` row is written per nest, and the child laser WO is (re)built.

Offline by contract: the per-PDF AI extraction (``extract_nest_fields_from_pdf``,
imported into the work-orders endpoint module) is monkeypatched, so no real PDF
parsing or Anthropic call happens. The import-with-rows path does NOT call the
extractor at all -- it reads the re-sent PDF bytes only to store the Document --
so those tests assert it is never invoked.

The legacy CNC-program ZIP import (no ``rows``) is regression-guarded here too.
"""

import io
import json
import zipfile

import pytest
from fastapi import status
from sqlalchemy.orm import Session

import app.api.endpoints.work_orders as work_orders_endpoint
from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.document import Document
from app.models.laser_nest import LaserNest, LaserNestPackage
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder, WorkOrderStatus

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> None:
    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True))
        db.commit()


def make_user(db: Session, *, role: UserRole, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"pdfimp-{n}@co{company_id}.test",
        employee_id=f"PDFIMP-{n:05d}",
        first_name="Pdf",
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
        code=f"LASER-PDF-{n}",
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


def make_parent_work_order(db: Session, *, company_id: int = COMPANY_A) -> WorkOrder:
    n = _next()
    part = Part(
        part_number=f"ASM-PDF-{n}",
        name="Pdf nest assembly",
        part_type="assembly",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()
    wo = WorkOrder(
        work_order_number=f"WO-PDF-{n}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=1,
        status=WorkOrderStatus.RELEASED,
        priority=3,
        company_id=company_id,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def _pdf_zip(*names: str) -> bytes:
    """Build an in-memory ZIP of minimal %PDF stub files.

    The bytes only need to be present; the extractor (preview) is mocked, and the
    import path stores whatever bytes it reads as the Document payload.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in names:
            zf.writestr(name, b"%PDF-1.4\n%stub nest report\n")
    return buf.getvalue()


def _cnc_zip(*names: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in names:
            zf.writestr(name, "M30")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def upload_dir(tmp_path, monkeypatch):
    """Point the local storage + laser-package roots at a tmp dir.

    Document creation on import writes PDF bytes via ``resolve_upload_dir`` and the
    package extraction writes under ``_resolve_laser_upload_root`` -- both read
    ``UPLOAD_DIR`` from the environment at call time, so one env override keeps the
    whole test hermetic.
    """
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))


@pytest.fixture
def mock_pdf_extraction(monkeypatch):
    """Stub the per-PDF extractor used by the PREVIEW path.

    Keyed by file name so each PDF gets deterministic, distinct fields. Returns
    the call-log list so a test can assert how many times (and whether) it ran.
    """
    calls = []
    table = {
        "05749.pdf": {
            "cnc_number": "05749",
            "material": "A36",
            "thickness": "0.25in",
            "sheet_size": "72.5x120",
            "planned_runs": 3,
            "extraction_confidence": "high",
        },
        "05750.pdf": {
            "cnc_number": "05750",
            "material": "304SS",
            "thickness": "10ga",
            "sheet_size": "48x96",
            "planned_runs": 2,
            "extraction_confidence": "medium",
        },
        "05751.pdf": {
            "cnc_number": "05751",
            "material": "A36",
            "thickness": "0.5in",
            "sheet_size": "60x120",
            "planned_runs": 1,
            "extraction_confidence": "low",
        },
    }

    def _fake_extract(pdf_path, file_name, company_id=None, **kwargs):
        calls.append((file_name, company_id))
        base = table.get(file_name, {"cnc_number": file_name.rsplit(".", 1)[0], "extraction_confidence": "low"})
        return {**base, "source": "ai", "warning": None}

    monkeypatch.setattr(work_orders_endpoint, "extract_nest_fields_from_pdf", _fake_extract)
    return calls


def _preview(client, headers, wo_id, zip_bytes, *, name="nests.zip"):
    return client.post(
        f"/api/v1/work-orders/{wo_id}/laser-nest-packages/preview",
        headers=headers,
        files={"file": (name, io.BytesIO(zip_bytes), "application/zip")},
    )


def _import(client, headers, wo_id, zip_bytes, *, rows=None, work_center_id=None, name="nests.zip"):
    data = {}
    if rows is not None:
        data["rows"] = json.dumps(rows)
    if work_center_id is not None:
        data["work_center_id"] = str(work_center_id)
    return client.post(
        f"/api/v1/work-orders/{wo_id}/laser-nest-packages/import",
        headers=headers,
        data=data,
        files={"file": (name, io.BytesIO(zip_bytes), "application/zip")},
    )


# --------------------------------------------------------------------------- #
# Preview
# --------------------------------------------------------------------------- #
class TestPdfPreview:
    def test_preview_returns_extracted_rows(self, client, db_session, mock_pdf_extraction):
        admin = make_user(db_session, role=UserRole.ADMIN)
        parent = make_parent_work_order(db_session)

        resp = _preview(client, headers_for(admin), parent.id, _pdf_zip("05749.pdf", "05750.pdf"))

        assert resp.status_code == status.HTTP_200_OK, resp.text
        data = resp.json()
        assert data["nest_count"] == 2
        assert data["total_planned_runs"] == 5  # 3 + 2
        rows = {row["cnc_number"]: row for row in data["nests"]}
        assert set(rows) == {"05749", "05750"}

        first = rows["05749"]
        assert first["material"] == "A36"
        assert first["thickness"] == "0.25in"
        assert first["sheet_size"] == "72.5x120"
        assert first["planned_runs"] == 3
        assert first["confidence"] == "high"
        # source_file is the row key the wizard echoes back on import.
        assert first["source_file"] == "05749.pdf"
        # AI ran once per PDF, scoped to the caller's company.
        assert len(mock_pdf_extraction) == 2
        assert {company for _, company in mock_pdf_extraction} == {admin.company_id}

    def test_preview_over_cap_returns_400(self, client, db_session, monkeypatch):
        """A package over LASER_PDF_PACKAGE_MAX must 400 (ValueError -> 400),
        and must NOT fan out a single AI call. Patch the cap low and assert the
        extractor was never touched."""
        from app.services import laser_nest_service

        monkeypatch.setattr(laser_nest_service, "LASER_PDF_PACKAGE_MAX", 2, raising=False)
        monkeypatch.setattr(work_orders_endpoint, "LASER_PDF_PACKAGE_MAX", 2, raising=False)

        called = []
        monkeypatch.setattr(
            work_orders_endpoint,
            "extract_nest_fields_from_pdf",
            lambda *a, **k: called.append(1) or {"cnc_number": "x", "extraction_confidence": "low"},
        )

        admin = make_user(db_session, role=UserRole.ADMIN)
        parent = make_parent_work_order(db_session)

        resp = _preview(client, headers_for(admin), parent.id, _pdf_zip("a.pdf", "b.pdf", "c.pdf"))

        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        assert "limit is 2" in resp.json()["detail"]
        assert called == []  # over-cap is rejected before any extraction

    def test_preview_one_bad_pdf_does_not_sink_the_batch(self, client, db_session, monkeypatch):
        """Batch resilience: ``_parse_laser_nest_pdf_package_async`` gathers the
        per-PDF extractions with ``return_exceptions=True``. If ONE PDF's
        extraction raises (despite the never-raise contract -- belt and
        suspenders), the preview must still return a row for EVERY PDF (the failed
        one degraded to a filename-only / low-confidence row) at HTTP 200, not a
        500 that loses the whole package."""

        def _flaky_extract(pdf_path, file_name, company_id=None, **kwargs):
            if file_name == "05750.pdf":
                raise RuntimeError("extraction blew up on this one PDF")
            return {
                "cnc_number": file_name.rsplit(".", 1)[0],
                "material": "A36",
                "thickness": "0.25in",
                "sheet_size": "72.5x120",
                "planned_runs": 3,
                "extraction_confidence": "high",
                "source": "ai",
                "warning": None,
            }

        monkeypatch.setattr(work_orders_endpoint, "extract_nest_fields_from_pdf", _flaky_extract)

        admin = make_user(db_session, role=UserRole.ADMIN)
        parent = make_parent_work_order(db_session)

        resp = _preview(client, headers_for(admin), parent.id, _pdf_zip("05749.pdf", "05750.pdf", "05751.pdf"))

        assert resp.status_code == status.HTTP_200_OK, resp.text
        data = resp.json()
        # A row for EVERY PDF -- the failed one is not dropped.
        assert data["nest_count"] == 3
        rows = {row["source_file"]: row for row in data["nests"]}
        assert set(rows) == {"05749.pdf", "05750.pdf", "05751.pdf"}

        # The two healthy PDFs extracted normally.
        assert rows["05749.pdf"]["cnc_number"] == "05749"
        assert rows["05749.pdf"]["confidence"] == "high"
        assert rows["05751.pdf"]["cnc_number"] == "05751"

        # The failed PDF degraded to a filename-only / low-confidence row: the
        # async path rebuilds it from {"cnc_number": None, ...} so the nest_name
        # falls back to the filename stem and confidence is "low".
        degraded = rows["05750.pdf"]
        assert degraded["confidence"] == "low"
        assert degraded["nest_name"] == "05750"
        assert degraded["planned_runs"] == 1  # coerced floor for a missing value


# --------------------------------------------------------------------------- #
# Import with confirmed rows (PDF path)
# --------------------------------------------------------------------------- #
class TestPdfImport:
    def test_import_persists_confirmed_rows_and_creates_documents(self, client, db_session, monkeypatch):
        # The import path must NOT re-run the AI: any call is a bug.
        monkeypatch.setattr(
            work_orders_endpoint,
            "extract_nest_fields_from_pdf",
            lambda *a, **k: pytest.fail("import must not call the AI extractor"),
        )
        admin = make_user(db_session, role=UserRole.ADMIN)
        wc = make_laser_work_center(db_session)
        parent = make_parent_work_order(db_session)

        # Planner-confirmed values DIFFER from anything the model would have said:
        # the persisted values must be exactly these.
        rows = [
            {
                "source_file": "05749.pdf",
                "cnc_number": "05749-CONFIRMED",
                "material": "A36-EDITED",
                "thickness": "0.250in",
                "sheet_size": "72.5x120",
                "planned_runs": 4,
                "confidence": "high",
            },
            {
                "source_file": "05750.pdf",
                "cnc_number": "05750",
                "material": "304SS",
                "thickness": "10ga",
                "sheet_size": "48x96",
                "planned_runs": 2,
                "confidence": "medium",
            },
        ]

        resp = _import(
            client,
            headers_for(admin),
            parent.id,
            _pdf_zip("05749.pdf", "05750.pdf"),
            rows=rows,
            work_center_id=wc.id,
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        child = resp.json()["child_work_order"]
        assert child["parent_work_order_id"] == parent.id
        assert child["work_order_type"] == "laser_cutting"
        assert child["quantity_ordered"] == 6  # 4 + 2
        assert len(child["operations"]) == 2

        package = db_session.query(LaserNestPackage).filter_by(child_work_order_id=child["id"]).one()
        nests = db_session.query(LaserNest).filter_by(package_id=package.id).order_by(LaserNest.planned_runs).all()
        assert [n.planned_runs for n in nests] == [2, 4]

        by_cnc = {n.cnc_number: n for n in nests}
        assert set(by_cnc) == {"05749-CONFIRMED", "05750"}
        edited = by_cnc["05749-CONFIRMED"]
        # CONFIRMED values were persisted verbatim.
        assert edited.material == "A36-EDITED"
        assert edited.thickness == "0.250in"
        assert edited.sheet_size == "72.5x120"

        # Each nest got a DRAWING Document created + attached (document_id set).
        for nest in nests:
            assert nest.document_id is not None
            doc = db_session.query(Document).filter_by(id=nest.document_id).one()
            assert doc.company_id == admin.company_id
            assert doc.work_order_id == parent.id  # drawing scoped to the parent assembly
            assert doc.mime_type == "application/pdf"

    def test_import_writes_one_audit_create_per_nest(self, client, db_session, monkeypatch):
        monkeypatch.setattr(
            work_orders_endpoint,
            "extract_nest_fields_from_pdf",
            lambda *a, **k: pytest.fail("import must not call the AI extractor"),
        )
        admin = make_user(db_session, role=UserRole.ADMIN)
        wc = make_laser_work_center(db_session)
        parent = make_parent_work_order(db_session)

        audit_before = db_session.query(AuditLog).filter(AuditLog.resource_type == "laser_nest").count()

        rows = [
            {"source_file": "05749.pdf", "cnc_number": "05749", "planned_runs": 3, "material": "A36"},
            {"source_file": "05750.pdf", "cnc_number": "05750", "planned_runs": 2, "material": "304SS"},
        ]
        resp = _import(
            client, headers_for(admin), parent.id, _pdf_zip("05749.pdf", "05750.pdf"), rows=rows, work_center_id=wc.id
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text

        creates = (
            db_session.query(AuditLog).filter(AuditLog.resource_type == "laser_nest", AuditLog.action == "CREATE").all()
        )
        # One CREATE audit row PER nest. Regression guard for the autoflush=False
        # bug where the endpoint's post-build SELECT missed the last (unflushed)
        # nest, so the final nest of every import silently went unaudited.
        assert len(creates) == 2
        assert {row.resource_identifier for row in creates} == {"05749", "05750"}
        assert all(row.extra_data.get("source") == "pdf_import" for row in creates)
        assert all(row.extra_data.get("parent_work_order_id") == parent.id for row in creates)
        # Tenant-scoped audit rows.
        assert {row.company_id for row in creates} == {admin.company_id}
        assert audit_before == 0

    def test_import_rejects_path_traversal_in_source_file(self, client, db_session, monkeypatch):
        """A confirmed row's source_file is resolved inside the package dir with a
        traversal guard; an escaping path must 400, not read arbitrary bytes."""
        monkeypatch.setattr(
            work_orders_endpoint,
            "extract_nest_fields_from_pdf",
            lambda *a, **k: pytest.fail("import must not call the AI extractor"),
        )
        admin = make_user(db_session, role=UserRole.ADMIN)
        wc = make_laser_work_center(db_session)
        parent = make_parent_work_order(db_session)

        rows = [{"source_file": "../../etc/passwd", "cnc_number": "evil", "planned_runs": 1}]
        resp = _import(client, headers_for(admin), parent.id, _pdf_zip("05749.pdf"), rows=rows, work_center_id=wc.id)
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text

    def test_import_is_tenant_scoped(self, client, db_session, monkeypatch):
        """Company B cannot import against Company A's parent WO (404), and the
        nests/documents created by A are invisible to B."""
        monkeypatch.setattr(
            work_orders_endpoint,
            "extract_nest_fields_from_pdf",
            lambda *a, **k: pytest.fail("import must not call the AI extractor"),
        )
        admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
        wc_a = make_laser_work_center(db_session, company_id=COMPANY_A)
        parent_a = make_parent_work_order(db_session, company_id=COMPANY_A)

        rows = [{"source_file": "05749.pdf", "cnc_number": "05749", "planned_runs": 1}]
        ok = _import(
            client, headers_for(admin_a), parent_a.id, _pdf_zip("05749.pdf"), rows=rows, work_center_id=wc_a.id
        )
        assert ok.status_code == status.HTTP_200_OK, ok.text

        # Company B attempting to import against A's parent WO sees a 404 (the
        # parent is scoped out), so it cannot graft nests onto another tenant's WO.
        admin_b = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_B)
        cross = _import(client, headers_for(admin_b), parent_a.id, _pdf_zip("05749.pdf"), rows=rows)
        assert cross.status_code == status.HTTP_404_NOT_FOUND

        # A's nest is owned by company A only.
        nest = db_session.query(LaserNest).filter_by(cnc_number="05749").one()
        assert nest.company_id == COMPANY_A


# --------------------------------------------------------------------------- #
# Concurrency: the laser-child-WO advisory lock is taken on the import path
# --------------------------------------------------------------------------- #
class TestLaserChildWorkOrderLock:
    """The import path also routes child-laser-WO creation through
    ``_ensure_laser_child_work_order``, which takes a per-parent advisory lock to
    serialize creation so two concurrent imports (or an import racing a manual
    add) can't double-create the LASER_CUTTING child.

    The lock is a no-op on SQLite (the test DB), so real serialization can't be
    asserted here -- we spy on ``acquire_generator_lock`` (still running the real,
    harmless function) and prove it fired with the correct per-parent namespace +
    company id on the import path.
    """

    def test_import_acquires_per_parent_lock(self, client, db_session, monkeypatch):
        monkeypatch.setattr(
            work_orders_endpoint,
            "extract_nest_fields_from_pdf",
            lambda *a, **k: pytest.fail("import must not call the AI extractor"),
        )
        admin = make_user(db_session, role=UserRole.ADMIN)
        wc = make_laser_work_center(db_session)
        parent = make_parent_work_order(db_session)

        calls = []
        real_lock = work_orders_endpoint.acquire_generator_lock

        def _spy(db, namespace, company=None):
            calls.append((namespace, company))
            return real_lock(db, namespace, company)

        monkeypatch.setattr(work_orders_endpoint, "acquire_generator_lock", _spy)

        rows = [{"source_file": "05749.pdf", "cnc_number": "05749", "planned_runs": 1}]
        resp = _import(client, headers_for(admin), parent.id, _pdf_zip("05749.pdf"), rows=rows, work_center_id=wc.id)
        assert resp.status_code == status.HTTP_200_OK, resp.text

        # The generator-lock helper serves other namespaces too (e.g.
        # "work_order_number" for the child WO number), so filter for the
        # laser-child namespace rather than asserting a total call count.
        laser_calls = [c for c in calls if c[0].startswith("laser_child_work_order:")]
        assert laser_calls, f"expected a laser_child_work_order lock acquisition, got {calls}"
        assert (f"laser_child_work_order:{parent.id}", admin.company_id) in laser_calls


# --------------------------------------------------------------------------- #
# Legacy CNC-program ZIP import (no rows) -- regression guard
# --------------------------------------------------------------------------- #
class TestLegacyCncImportUnchanged:
    def test_cnc_file_zip_import_still_works_without_rows(self, client, db_session, monkeypatch):
        """The CNC-program path (no ``rows``) is untouched: fields inferred from
        filenames, no Documents created, the extractor never invoked."""
        monkeypatch.setattr(
            work_orders_endpoint,
            "extract_nest_fields_from_pdf",
            lambda *a, **k: pytest.fail("CNC-file import must not call the AI extractor"),
        )
        admin = make_user(db_session, role=UserRole.ADMIN)
        wc = make_laser_work_center(db_session)
        parent = make_parent_work_order(db_session)

        zip_bytes = _cnc_zip("NEST-A_A36_10ga_60x120_QTY3.nc", "NEST-B_304SS_0.25in_48x96_x2.tap")
        resp = _import(
            client, headers_for(admin), parent.id, zip_bytes, rows=None, work_center_id=wc.id, name="cnc.zip"
        )

        assert resp.status_code == status.HTTP_200_OK, resp.text
        child = resp.json()["child_work_order"]
        assert child["quantity_ordered"] == 5  # 3 + 2 from filenames
        assert len(child["operations"]) == 2

        package = db_session.query(LaserNestPackage).filter_by(child_work_order_id=child["id"]).one()
        nests = db_session.query(LaserNest).filter_by(package_id=package.id).all()
        assert sorted(n.planned_runs for n in nests) == [2, 3]
        # Legacy path attaches no Documents.
        assert all(n.document_id is None for n in nests)

    def test_cnc_file_zip_import_writes_audit_create_with_cnc_source(self, client, db_session, monkeypatch):
        """The legacy CNC-file path now also audits each created nest -- with
        ``extra_data["source"] == "cnc_file_import"`` (vs ``pdf_import`` on the
        PDF path). Regression guard: the CNC path previously created nests with
        only a WO event and no per-nest CREATE audit row."""
        monkeypatch.setattr(
            work_orders_endpoint,
            "extract_nest_fields_from_pdf",
            lambda *a, **k: pytest.fail("CNC-file import must not call the AI extractor"),
        )
        admin = make_user(db_session, role=UserRole.ADMIN)
        wc = make_laser_work_center(db_session)
        parent = make_parent_work_order(db_session)

        zip_bytes = _cnc_zip("NEST-A_A36_10ga_60x120_QTY3.nc", "NEST-B_304SS_0.25in_48x96_x2.tap")
        resp = _import(
            client, headers_for(admin), parent.id, zip_bytes, rows=None, work_center_id=wc.id, name="cnc.zip"
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text

        creates = (
            db_session.query(AuditLog).filter(AuditLog.resource_type == "laser_nest", AuditLog.action == "CREATE").all()
        )
        # One CREATE per CNC-file nest, tagged with the CNC source.
        assert len(creates) == 2
        assert all(row.extra_data.get("source") == "cnc_file_import" for row in creates)
        assert all(row.extra_data.get("parent_work_order_id") == parent.id for row in creates)
        assert {row.company_id for row in creates} == {admin.company_id}


# --------------------------------------------------------------------------- #
# Re-import supersession is audited (item 1)
# --------------------------------------------------------------------------- #
class TestReimportSupersessionAudited:
    def test_reimport_audits_superseded_nests_as_delete(self, client, db_session, monkeypatch):
        """Re-importing a DIFFERENT package onto the same parent WO wipes the prior
        import's nests. Each superseded (pre-existing, non-deleted) nest must get a
        DELETE audit row tagged ``reason == "superseded_by_reimport"`` BEFORE the
        wipe, in addition to CREATE rows for the new nests."""
        monkeypatch.setattr(
            work_orders_endpoint,
            "extract_nest_fields_from_pdf",
            lambda *a, **k: pytest.fail("import must not call the AI extractor"),
        )
        admin = make_user(db_session, role=UserRole.ADMIN)
        wc = make_laser_work_center(db_session)
        parent = make_parent_work_order(db_session)

        # First import: two nests.
        first_rows = [
            {"source_file": "05749.pdf", "cnc_number": "05749", "planned_runs": 3, "material": "A36"},
            {"source_file": "05750.pdf", "cnc_number": "05750", "planned_runs": 2, "material": "304SS"},
        ]
        first = _import(
            client,
            headers_for(admin),
            parent.id,
            _pdf_zip("05749.pdf", "05750.pdf"),
            rows=first_rows,
            work_center_id=wc.id,
        )
        assert first.status_code == status.HTTP_200_OK, first.text

        # Capture the IDs of the first import's nests -- these are the ones that
        # must be superseded (DELETE-audited) on the re-import.
        first_child_id = first.json()["child_work_order"]["id"]
        first_package = db_session.query(LaserNestPackage).filter_by(child_work_order_id=first_child_id).one()
        first_nest_ids = {n.id for n in db_session.query(LaserNest).filter_by(package_id=first_package.id).all()}
        assert len(first_nest_ids) == 2

        # Re-import: a DIFFERENT package (three new nests) onto the SAME parent WO.
        second_rows = [
            {"source_file": "06001.pdf", "cnc_number": "06001", "planned_runs": 1, "material": "A36"},
            {"source_file": "06002.pdf", "cnc_number": "06002", "planned_runs": 4, "material": "304SS"},
            {"source_file": "06003.pdf", "cnc_number": "06003", "planned_runs": 2, "material": "A572"},
        ]
        second = _import(
            client,
            headers_for(admin),
            parent.id,
            _pdf_zip("06001.pdf", "06002.pdf", "06003.pdf"),
            rows=second_rows,
            work_center_id=wc.id,
        )
        assert second.status_code == status.HTTP_200_OK, second.text

        # DELETE audit rows for the superseded (first-import) nests.
        deletes = (
            db_session.query(AuditLog).filter(AuditLog.resource_type == "laser_nest", AuditLog.action == "DELETE").all()
        )
        # Exactly one DELETE per first-import nest, all tagged superseded_by_reimport.
        assert len(deletes) == 2
        assert all(row.extra_data.get("reason") == "superseded_by_reimport" for row in deletes)
        assert all(row.extra_data.get("parent_work_order_id") == parent.id for row in deletes)
        # The DELETE rows reference exactly the first import's nest IDs (hard delete,
        # soft_delete=False -> these rows trace the wipe of the prior nests).
        assert {row.resource_id for row in deletes} == first_nest_ids
        assert {row.company_id for row in deletes} == {admin.company_id}

        # CREATE rows: 2 (first import) + 3 (re-import) = 5 total, the re-import's
        # three carry the new cnc numbers.
        creates = (
            db_session.query(AuditLog).filter(AuditLog.resource_type == "laser_nest", AuditLog.action == "CREATE").all()
        )
        assert len(creates) == 5
        create_idents = {row.resource_identifier for row in creates}
        assert {"06001", "06002", "06003"}.issubset(create_idents)

    def test_reimport_audits_superseded_legacy_cnc_nests(self, client, db_session, monkeypatch):
        """Supersession audit fires on the legacy CNC path too: a first CNC-file
        import then a second CNC-file import onto the same parent WO must
        DELETE-audit the first import's nests as superseded."""
        monkeypatch.setattr(
            work_orders_endpoint,
            "extract_nest_fields_from_pdf",
            lambda *a, **k: pytest.fail("CNC-file import must not call the AI extractor"),
        )
        admin = make_user(db_session, role=UserRole.ADMIN)
        wc = make_laser_work_center(db_session)
        parent = make_parent_work_order(db_session)

        first = _import(
            client,
            headers_for(admin),
            parent.id,
            _cnc_zip("NEST-A_A36_10ga_60x120_QTY3.nc", "NEST-B_304SS_0.25in_48x96_x2.tap"),
            rows=None,
            work_center_id=wc.id,
            name="cnc1.zip",
        )
        assert first.status_code == status.HTTP_200_OK, first.text
        first_child_id = first.json()["child_work_order"]["id"]
        first_package = db_session.query(LaserNestPackage).filter_by(child_work_order_id=first_child_id).one()
        first_nest_ids = {n.id for n in db_session.query(LaserNest).filter_by(package_id=first_package.id).all()}
        assert len(first_nest_ids) == 2

        second = _import(
            client,
            headers_for(admin),
            parent.id,
            _cnc_zip("NEST-C_A36_10ga_48x96_QTY1.nc"),
            rows=None,
            work_center_id=wc.id,
            name="cnc2.zip",
        )
        assert second.status_code == status.HTTP_200_OK, second.text

        deletes = (
            db_session.query(AuditLog).filter(AuditLog.resource_type == "laser_nest", AuditLog.action == "DELETE").all()
        )
        assert len(deletes) == 2
        assert {row.resource_id for row in deletes} == first_nest_ids
        assert all(row.extra_data.get("reason") == "superseded_by_reimport" for row in deletes)


# --------------------------------------------------------------------------- #
# rows JSON is Pydantic-validated -> 400 (item 3) and dedupe (item 4)
# --------------------------------------------------------------------------- #
class TestImportRowValidation:
    def _setup(self, db_session):
        admin = make_user(db_session, role=UserRole.ADMIN)
        wc = make_laser_work_center(db_session)
        parent = make_parent_work_order(db_session)
        return admin, wc, parent

    def _assert_no_nests(self, db_session):
        # A validation failure must commit NOTHING.
        assert db_session.query(LaserNest).count() == 0
        assert db_session.query(LaserNestPackage).count() == 0

    @pytest.fixture(autouse=True)
    def _no_ai(self, monkeypatch):
        monkeypatch.setattr(
            work_orders_endpoint,
            "extract_nest_fields_from_pdf",
            lambda *a, **k: pytest.fail("validation-rejected import must not call the AI extractor"),
        )

    @pytest.mark.parametrize(
        "bad_runs",
        [-5, 0, "two"],
        ids=["negative", "zero", "non_numeric"],
    )
    def test_invalid_planned_runs_rejected_400(self, client, db_session, bad_runs):
        admin, wc, parent = self._setup(db_session)
        rows = [{"source_file": "05749.pdf", "cnc_number": "05749", "planned_runs": bad_runs}]
        resp = _import(client, headers_for(admin), parent.id, _pdf_zip("05749.pdf"), rows=rows, work_center_id=wc.id)
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        assert "Invalid nest rows" in resp.json()["detail"]
        self._assert_no_nests(db_session)

    def test_over_long_cnc_number_rejected_400(self, client, db_session):
        admin, wc, parent = self._setup(db_session)
        rows = [{"source_file": "05749.pdf", "cnc_number": "X" * 101, "planned_runs": 1}]
        resp = _import(client, headers_for(admin), parent.id, _pdf_zip("05749.pdf"), rows=rows, work_center_id=wc.id)
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        assert "Invalid nest rows" in resp.json()["detail"]
        self._assert_no_nests(db_session)

    @pytest.mark.parametrize("source_file", [None, ""], ids=["missing", "empty"])
    def test_missing_or_empty_source_file_rejected_400(self, client, db_session, source_file):
        admin, wc, parent = self._setup(db_session)
        row = {"cnc_number": "05749", "planned_runs": 1}
        if source_file is not None:
            row["source_file"] = source_file  # explicit empty string
        rows = [row]
        resp = _import(client, headers_for(admin), parent.id, _pdf_zip("05749.pdf"), rows=rows, work_center_id=wc.id)
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        assert "Invalid nest rows" in resp.json()["detail"]
        self._assert_no_nests(db_session)

    def test_duplicate_source_file_rejected_400(self, client, db_session):
        """Two rows pointing at the SAME PDF would double-create nests/Documents
        and trip the package unique constraint -- the build dedupes and 400s with
        a clear message, committing nothing."""
        admin, wc, parent = self._setup(db_session)
        rows = [
            {"source_file": "05749.pdf", "cnc_number": "05749", "planned_runs": 1},
            {"source_file": "05749.pdf", "cnc_number": "05749-DUP", "planned_runs": 2},
        ]
        resp = _import(client, headers_for(admin), parent.id, _pdf_zip("05749.pdf"), rows=rows, work_center_id=wc.id)
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        detail = resp.json()["detail"]
        assert "Duplicate" in detail and "05749.pdf" in detail
        self._assert_no_nests(db_session)


# --------------------------------------------------------------------------- #
# IntegrityError / SQLAlchemyError -> 400, not 500 (item 5)
# --------------------------------------------------------------------------- #
class TestImportDbErrorBecomes400:
    def test_integrity_error_during_build_returns_400_not_500(self, client, db_session, monkeypatch):
        """A DB/constraint fault raised during the atomic build is translated to a
        clean 400 (not a 500), and the session is not poisoned: a subsequent valid
        import on the SAME client succeeds."""
        monkeypatch.setattr(
            work_orders_endpoint,
            "extract_nest_fields_from_pdf",
            lambda *a, **k: pytest.fail("import must not call the AI extractor"),
        )
        admin = make_user(db_session, role=UserRole.ADMIN)
        wc = make_laser_work_center(db_session)
        parent = make_parent_work_order(db_session)

        # Force an IntegrityError from inside build_laser_nest_child_work_order
        # (which runs INSIDE the atomic_transaction). This exercises the endpoint's
        # (IntegrityError, SQLAlchemyError) -> 400 catch.
        from sqlalchemy.exc import IntegrityError as SAIntegrityError

        def _boom(*args, **kwargs):
            raise SAIntegrityError("forced", params=None, orig=Exception("unique violation"))

        monkeypatch.setattr(work_orders_endpoint, "build_laser_nest_child_work_order", _boom)

        rows = [{"source_file": "05749.pdf", "cnc_number": "05749", "planned_runs": 1}]
        resp = _import(client, headers_for(admin), parent.id, _pdf_zip("05749.pdf"), rows=rows, work_center_id=wc.id)
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text
        # User-facing 400 message, not a leaked stack/500.
        assert "Could not import the nest package" in resp.json()["detail"]
        # Nothing committed.
        assert db_session.query(LaserNest).count() == 0

        # Session is not poisoned: undo the monkeypatch and re-import successfully
        # on the same client (same shared session).
        monkeypatch.undo()
        monkeypatch.setattr(
            work_orders_endpoint,
            "extract_nest_fields_from_pdf",
            lambda *a, **k: pytest.fail("import must not call the AI extractor"),
        )
        ok = _import(client, headers_for(admin), parent.id, _pdf_zip("05749.pdf"), rows=rows, work_center_id=wc.id)
        assert ok.status_code == status.HTTP_200_OK, ok.text
        assert db_session.query(LaserNest).count() == 1


# --------------------------------------------------------------------------- #
# Orphaned-CUI fix: storage blobs reaped on rollback (item 6)
# --------------------------------------------------------------------------- #
class TestRollbackReapsStorageBlobs:
    def test_saved_blobs_deleted_when_import_rolls_back(self, client, db_session, monkeypatch):
        """``_create_nest_document`` saves a REAL blob (disk/S3) BEFORE the atomic
        transaction commits, collecting each storage ref. If the import later rolls
        back, the endpoint must call ``delete_ref`` on every collected ref so no
        orphaned CUI blob is left behind -- and no Document/LaserNest row persists.

        We arrange for the SECOND nest's document save to raise inside the build
        (after the FIRST nest's blob has been saved + recorded), then assert the
        first blob's ref was reaped.
        """
        monkeypatch.setattr(
            work_orders_endpoint,
            "extract_nest_fields_from_pdf",
            lambda *a, **k: pytest.fail("import must not call the AI extractor"),
        )
        admin = make_user(db_session, role=UserRole.ADMIN)
        wc = make_laser_work_center(db_session)
        parent = make_parent_work_order(db_session)

        # Wrap the real document-saver: let the FIRST nest save its blob (so a real
        # ref lands in saved_storage_keys), then raise an SQLAlchemyError on the
        # SECOND -- inside the atomic_transaction, so it rolls back.
        from sqlalchemy.exc import SQLAlchemyError as SASQLAlchemyError

        from app.services import laser_nest_service

        real_create_doc = laser_nest_service._create_nest_document
        call_count = {"n": 0}

        def _failing_second_doc(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Real save: writes a blob to the tmp UPLOAD_DIR and appends its ref
                # to saved_storage_keys (passed through via kwargs).
                return real_create_doc(*args, **kwargs)
            raise SASQLAlchemyError("forced failure after first blob saved")

        monkeypatch.setattr(laser_nest_service, "_create_nest_document", _failing_second_doc)

        # Spy on delete_ref at the endpoint's import site to capture reaped refs.
        reaped = []
        monkeypatch.setattr(work_orders_endpoint, "delete_ref", lambda ref: reaped.append(ref))

        rows = [
            {"source_file": "05749.pdf", "cnc_number": "05749", "planned_runs": 1},
            {"source_file": "05750.pdf", "cnc_number": "05750", "planned_runs": 1},
        ]
        resp = _import(
            client,
            headers_for(admin),
            parent.id,
            _pdf_zip("05749.pdf", "05750.pdf"),
            rows=rows,
            work_center_id=wc.id,
        )
        # DB/constraint fault -> clean 400.
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text

        # The first nest's blob ref was reaped exactly once.
        assert len(reaped) == 1, f"expected one reaped blob ref, got {reaped}"
        # The reaped ref is the local path under the tmp UPLOAD_DIR (a real file
        # path string), the same value _create_nest_document recorded.
        assert reaped[0]

        # Nothing persisted: rollback dropped both the Document and the LaserNest.
        db_session.rollback()
        assert db_session.query(LaserNest).count() == 0
        assert db_session.query(Document).count() == 0
