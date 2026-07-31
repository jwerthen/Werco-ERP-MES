"""Kiosk doc-viewer document routes (Kiosk Foundry redesign, backend A1/A2).

Two read-only routes live INSIDE the kiosk token fence so the viewer works on
both kiosk tiers:

1. ``GET /shop-floor/operations/{id}/documents`` -- discovery: the controlled
   part drawing (newest approved/released DRAWING for the WO's part -- ordered
   ``released_at DESC NULLS LAST, id DESC``; drafts NEVER surface), the
   operation's live laser-nest reference PDF (soft-delete-guarded via
   ``active_laser_nest``), the nest material, and the part's critical SPC
   characteristics (``is_critical`` AND ``is_active``, preferring rows scoped
   to this routing op or unscoped).
2. ``GET /shop-floor/documents/{id}/inline`` -- the single kiosk byte-serving
   route. Guard: tenant match AND (a CONTROLLED PART DRAWING -- type DRAWING,
   status approved/released, ``part_id`` set -- OR referenced by a LIVE
   non-deleted LaserNest). Nest reference PDFs are stored as released DRAWINGs
   with NO part_id, so they serve only while their nest is live; a draft /
   pending / obsolete part drawing never serves at the point of use. Every
   miss is a **404** -- never 403 -- so the route leaks no existence
   information about other documents (incl. cross-tenant ids and docs held
   only by soft-deleted nests). The Content-Disposition filename is sanitized
   (quotes / control chars / non-latin-1 dropped).

Both are locked here for tenancy, and the kiosk-scope fence section proves a
badge-minted ``scope="kiosk"`` operator token reaches BOTH new routes while the
desktop document surfaces stay fenced (regression pin).
"""

from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.document import Document, DocumentType
from app.models.laser_nest import LaserNest, LaserNestPackage
from app.models.spc import SPCCharacteristic
from app.models.user import UserRole
from app.models.work_order import WorkOrderOperation
from tests.api.kiosk_test_helpers import (
    COMPANY_A,
    COMPANY_B,
    bearer,
    make_user,
    make_wo_with_operation,
    make_work_center,
    user_headers,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

FENCE_DETAIL = "Kiosk-scoped token cannot access this resource"

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def docs_url(operation_id: int) -> str:
    return f"/api/v1/shop-floor/operations/{operation_id}/documents"


def inline_url(document_id: int) -> str:
    return f"/api/v1/shop-floor/documents/{document_id}/inline"


def make_document(
    db: Session,
    *,
    company_id: int = COMPANY_A,
    part_id: Optional[int] = None,
    document_type: DocumentType = DocumentType.DRAWING,
    doc_status: str = "released",
    released_at: Optional[datetime] = None,
    revision: str = "A",
    title: Optional[str] = None,
    file_path: Optional[str] = None,
    file_name: Optional[str] = None,
) -> Document:
    n = _next()
    document = Document(
        document_number=f"KDOC-{n:05d}",
        revision=revision,
        title=title or f"Drawing {n}",
        document_type=document_type,
        part_id=part_id,
        file_name=file_name or f"kdoc-{n}.pdf",
        file_path=file_path,
        status=doc_status,
        released_at=released_at,
        company_id=company_id,
    )
    db.add(document)
    db.commit()
    db.refresh(document)
    return document


def attach_nest(
    db: Session,
    operation: WorkOrderOperation,
    *,
    company_id: int = COMPANY_A,
    document: Optional[Document] = None,
    material: Optional[str] = "304 SS",
    is_deleted: bool = False,
) -> LaserNest:
    """A LaserNest pointing at ``operation`` (and optionally at a reference PDF)."""
    n = _next()
    package = LaserNestPackage(
        company_id=company_id,
        child_work_order_id=operation.work_order_id,
        package_name=f"Package {n}",
        import_status="imported",
    )
    db.add(package)
    db.flush()
    nest = LaserNest(
        company_id=company_id,
        package_id=package.id,
        work_order_operation_id=operation.id,
        nest_name=f"CNC{n:04d}",
        cnc_number=f"CNC-{n:04d}",
        document_id=document.id if document else None,
        planned_runs=3,
        material=material,
        is_deleted=is_deleted,
    )
    db.add(nest)
    db.commit()
    db.refresh(nest)
    return nest


def make_spc(
    db: Session,
    *,
    part_id: int,
    company_id: int = COMPANY_A,
    name: Optional[str] = None,
    operation_number: Optional[int] = None,
    is_critical: bool = True,
    is_active: bool = True,
    nominal: Optional[float] = 0.5,
    usl: Optional[float] = 0.502,
    lsl: Optional[float] = 0.498,
    unit: str = "in",
) -> SPCCharacteristic:
    n = _next()
    row = SPCCharacteristic(
        name=name or f"Dim {n}",
        part_id=part_id,
        characteristic_type="dimensional",
        unit_of_measure=unit,
        specification_nominal=nominal,
        specification_usl=usl,
        specification_lsl=lsl,
        operation_number=operation_number,
        is_active=is_active,
        is_critical=is_critical,
        company_id=company_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def make_pdf_file(tmp_path: Path, name: str = "doc.pdf") -> Tuple[str, bytes]:
    """A real on-disk file for the FileResponse serving path."""
    content = b"%PDF-1.4 kiosk viewer test bytes"
    path = tmp_path / name
    path.write_bytes(content)
    return str(path), content


# ---------------------------------------------------------------------------
# A1 -- GET /shop-floor/operations/{id}/documents (discovery)
# ---------------------------------------------------------------------------


class TestOperationDocumentsDiscovery:
    def test_happy_path_full_payload(self, client: TestClient, db_session: Session):
        """Part + released drawing + live nest (+material) + critical dims, in one read."""
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_work_center(db_session)
        wo, op = make_wo_with_operation(db_session, work_center=wc)
        part = wo.part
        part.revision = "C"
        db_session.commit()

        drawing = make_document(
            db_session,
            part_id=part.id,
            doc_status="released",
            released_at=datetime(2026, 7, 1, 12, 0, 0),
            revision="B",
            title="Bracket drawing",
        )
        nest_doc = make_document(db_session, document_type=DocumentType.OTHER, doc_status="draft")
        nest = attach_nest(db_session, op, document=nest_doc, material="A36")
        dim = make_spc(db_session, part_id=part.id, name="Bore Ø", operation_number=None)

        resp = client.get(docs_url(op.id), headers=user_headers(operator))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        body = resp.json()

        assert body["part"] == {
            "id": part.id,
            "part_number": part.part_number,
            "name": part.name,
            "revision": "C",
        }
        assert body["drawing"] == {
            "document_id": drawing.id,
            "revision": "B",
            "title": "Bracket drawing",
            "status": "released",
            "released_at": "2026-07-01T12:00:00Z",
            "file_name": drawing.file_name,
        }
        assert body["nest"] == {
            "laser_nest_id": nest.id,
            "nest_name": nest.nest_name,
            "cnc_number": nest.cnc_number,
            "document_id": nest_doc.id,
            "file_name": nest_doc.file_name,
        }
        assert body["material"] == "A36"
        assert body["critical_dims"] == [
            {"id": dim.id, "name": "Bore Ø", "nominal": 0.5, "usl": 0.502, "lsl": 0.498, "unit_of_measure": "in"}
        ]

    def test_drawing_selection_newest_released_wins_and_draft_ignored(self, client: TestClient, db_session: Session):
        """released_at DESC NULLS LAST, id DESC: the newest released/approved
        drawing wins; a draft (even the newest row) never surfaces."""
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_work_center(db_session)
        wo, op = make_wo_with_operation(db_session, work_center=wc)
        part_id = wo.part_id
        headers = user_headers(operator)

        # Draft only -> no drawing at all.
        make_document(db_session, part_id=part_id, doc_status="draft")
        assert client.get(docs_url(op.id), headers=headers).json()["drawing"] is None

        # Approved with NO released_at -> chosen (better than nothing).
        approved = make_document(db_session, part_id=part_id, doc_status="approved", released_at=None)
        assert client.get(docs_url(op.id), headers=headers).json()["drawing"]["document_id"] == approved.id

        # A released doc WITH a date beats the date-less approved one (NULLS LAST) ...
        older = make_document(
            db_session, part_id=part_id, doc_status="released", released_at=datetime(2026, 6, 1), revision="A"
        )
        assert client.get(docs_url(op.id), headers=headers).json()["drawing"]["document_id"] == older.id

        # ... and the NEWEST released_at wins over an older one.
        newer = make_document(
            db_session, part_id=part_id, doc_status="released", released_at=datetime(2026, 7, 1), revision="B"
        )
        body = client.get(docs_url(op.id), headers=headers).json()
        assert body["drawing"]["document_id"] == newer.id
        assert body["drawing"]["revision"] == "B"

    def test_soft_deleted_nest_is_null(self, client: TestClient, db_session: Session):
        """The nest block routes through active_laser_nest: a soft-deleted nest
        (and its material) never leaks into the viewer."""
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_work_center(db_session)
        _, op = make_wo_with_operation(db_session, work_center=wc)
        nest_doc = make_document(db_session, document_type=DocumentType.OTHER)
        attach_nest(db_session, op, document=nest_doc, material="A36", is_deleted=True)

        body = client.get(docs_url(op.id), headers=user_headers(operator)).json()
        assert body["nest"] is None
        assert body["material"] is None

    def test_critical_dims_filter_and_operation_preference(self, client: TestClient, db_session: Session):
        """Only is_critical AND is_active rows render; rows scoped to THIS op
        (digits of "OP10") or unscoped are preferred over other-op rows."""
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_work_center(db_session)
        wo, op = make_wo_with_operation(db_session, work_center=wc)  # operation_number "OP10"
        part_id = wo.part_id

        match_op = make_spc(db_session, part_id=part_id, name="This op", operation_number=10)
        match_null = make_spc(db_session, part_id=part_id, name="Unscoped", operation_number=None)
        make_spc(db_session, part_id=part_id, name="Other op", operation_number=20)
        make_spc(db_session, part_id=part_id, name="Not critical", is_critical=False)
        make_spc(db_session, part_id=part_id, name="Retired", is_active=False)

        body = client.get(docs_url(op.id), headers=user_headers(operator)).json()
        assert [d["id"] for d in body["critical_dims"]] == [match_op.id, match_null.id]

    def test_critical_dims_fall_back_to_all_when_none_match_the_op(self, client: TestClient, db_session: Session):
        """When every critical row is scoped to some OTHER op, they all render
        rather than hiding the part's critical characteristics."""
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_work_center(db_session)
        wo, op = make_wo_with_operation(db_session, work_center=wc)

        other_a = make_spc(db_session, part_id=wo.part_id, name="Op 90 dim", operation_number=90)
        other_b = make_spc(db_session, part_id=wo.part_id, name="Op 95 dim", operation_number=95)

        body = client.get(docs_url(op.id), headers=user_headers(operator)).json()
        assert [d["id"] for d in body["critical_dims"]] == [other_a.id, other_b.id]

    def test_cross_tenant_operation_404(self, client: TestClient, db_session: Session):
        """A guessed foreign operation id is indistinguishable from a missing one."""
        operator_a = make_user(db_session, company_id=COMPANY_A, role=UserRole.OPERATOR)
        wc_b = make_work_center(db_session, company_id=COMPANY_B)
        _, op_b = make_wo_with_operation(db_session, company_id=COMPANY_B, work_center=wc_b)

        resp = client.get(docs_url(op_b.id), headers=user_headers(operator_a))
        assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text


# ---------------------------------------------------------------------------
# A2 -- GET /shop-floor/documents/{id}/inline (byte serving)
# ---------------------------------------------------------------------------


class TestShopFloorDocumentInline:
    def _part_id(self, db: Session) -> int:
        """A part to hang controlled drawings on (via the shared WO factory)."""
        wc = make_work_center(db)
        wo, _ = make_wo_with_operation(db, work_center=wc)
        return wo.part_id

    def test_serves_a_released_part_drawing_inline(self, client: TestClient, db_session: Session, tmp_path: Path):
        operator = make_user(db_session, role=UserRole.OPERATOR)
        path, content = make_pdf_file(tmp_path, "drawing.pdf")
        drawing = make_document(db_session, part_id=self._part_id(db_session), file_path=path, file_name="drw-9.pdf")

        resp = client.get(inline_url(drawing.id), headers=user_headers(operator))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.headers["content-type"].startswith("application/pdf")
        assert resp.headers["content-disposition"] == 'inline; filename="drw-9.pdf"'
        assert resp.content == content

    @pytest.mark.parametrize("doc_status", ["draft", "pending_approval", "obsolete"])
    def test_uncontrolled_part_drawing_statuses_404(
        self, client: TestClient, db_session: Session, tmp_path: Path, doc_status: str
    ):
        """Only approved/released part drawings serve -- a draft / pending /
        obsolete revision must never reach the shop floor at the point of use."""
        operator = make_user(db_session, role=UserRole.OPERATOR)
        path, _ = make_pdf_file(tmp_path)
        drawing = make_document(db_session, part_id=self._part_id(db_session), doc_status=doc_status, file_path=path)

        resp = client.get(inline_url(drawing.id), headers=user_headers(operator))
        assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
        assert resp.json()["detail"] == "Document not found"

    def test_partless_released_drawing_without_a_live_nest_404(
        self, client: TestClient, db_session: Session, tmp_path: Path
    ):
        """A released DRAWING with NO part_id is only servable through the
        live-nest branch; unreferenced, it is invisible."""
        operator = make_user(db_session, role=UserRole.OPERATOR)
        path, _ = make_pdf_file(tmp_path)
        orphan = make_document(db_session, part_id=None, file_path=path)

        resp = client.get(inline_url(orphan.id), headers=user_headers(operator))
        assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
        assert resp.json()["detail"] == "Document not found"

    def test_serves_a_partless_drawing_referenced_by_a_live_nest(
        self, client: TestClient, db_session: Session, tmp_path: Path
    ):
        """The canonical nest reference PDF shape (laser_nest_service): a
        released DRAWING with part_id NULL, viewable through its live nest."""
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_work_center(db_session)
        _, op = make_wo_with_operation(db_session, work_center=wc)
        path, content = make_pdf_file(tmp_path, "nest.pdf")
        nest_doc = make_document(db_session, part_id=None, file_path=path)
        attach_nest(db_session, op, document=nest_doc)

        resp = client.get(inline_url(nest_doc.id), headers=user_headers(operator))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.content == content

    def test_serves_a_non_drawing_doc_referenced_by_a_live_nest(
        self, client: TestClient, db_session: Session, tmp_path: Path
    ):
        """The live-nest branch is type-agnostic -- an older OTHER-typed nest
        PDF stays viewable through its nest reference."""
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_work_center(db_session)
        _, op = make_wo_with_operation(db_session, work_center=wc)
        path, content = make_pdf_file(tmp_path, "nest-other.pdf")
        nest_doc = make_document(db_session, document_type=DocumentType.OTHER, file_path=path)
        attach_nest(db_session, op, document=nest_doc)

        resp = client.get(inline_url(nest_doc.id), headers=user_headers(operator))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.content == content

    def test_cross_tenant_document_404(self, client: TestClient, db_session: Session, tmp_path: Path):
        operator_a = make_user(db_session, company_id=COMPANY_A, role=UserRole.OPERATOR)
        wc_b = make_work_center(db_session, company_id=COMPANY_B)
        wo_b, _ = make_wo_with_operation(db_session, company_id=COMPANY_B, work_center=wc_b)
        path, _ = make_pdf_file(tmp_path)
        drawing_b = make_document(db_session, company_id=COMPANY_B, part_id=wo_b.part_id, file_path=path)

        resp = client.get(inline_url(drawing_b.id), headers=user_headers(operator_a))
        assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
        assert resp.json()["detail"] == "Document not found"

    def test_non_drawing_unreferenced_document_404(self, client: TestClient, db_session: Session, tmp_path: Path):
        """A tenant-local doc that is neither a controlled part drawing nor a
        nest reference is NOT kiosk-viewable -- and the refusal is a 404, not
        a 403 (no existence leak about which documents exist behind the fence)."""
        operator = make_user(db_session, role=UserRole.OPERATOR)
        path, _ = make_pdf_file(tmp_path)
        cert = make_document(db_session, document_type=DocumentType.MATERIAL_CERT, file_path=path)

        resp = client.get(inline_url(cert.id), headers=user_headers(operator))
        assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
        assert resp.json()["detail"] == "Document not found"

    def test_document_referenced_only_by_a_soft_deleted_nest_404(
        self, client: TestClient, db_session: Session, tmp_path: Path
    ):
        """Deleting the nest closes the viewing grant its reference conferred --
        even though the doc itself is a released DRAWING (part_id NULL, so the
        controlled-part-drawing branch cannot resurrect it)."""
        operator = make_user(db_session, role=UserRole.OPERATOR)
        wc = make_work_center(db_session)
        _, op = make_wo_with_operation(db_session, work_center=wc)
        path, _ = make_pdf_file(tmp_path)
        nest_doc = make_document(db_session, part_id=None, file_path=path)
        attach_nest(db_session, op, document=nest_doc, is_deleted=True)

        resp = client.get(inline_url(nest_doc.id), headers=user_headers(operator))
        assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
        assert resp.json()["detail"] == "Document not found"

    def test_missing_file_404(self, client: TestClient, db_session: Session, tmp_path: Path):
        operator = make_user(db_session, role=UserRole.OPERATOR)
        drawing = make_document(
            db_session, part_id=self._part_id(db_session), file_path=str(tmp_path / "never-written.pdf")
        )

        resp = client.get(inline_url(drawing.id), headers=user_headers(operator))
        assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
        assert resp.json()["detail"] == "File not found"

    def test_content_disposition_filename_is_sanitized(self, client: TestClient, db_session: Session, tmp_path: Path):
        """Quotes, control chars, and non-ASCII code points are stripped so a
        stored file name can never break, forge, or UTF-8-poison the header.

        The non-ASCII cases are load-bearing: "–" (U+2013, multi-byte) and
        "ø" (U+00F8, a latin-1 high byte starlette CAN encode but that arrives
        as invalid UTF-8 at clients decoding header bytes as UTF-8 -- the bug
        this test originally exposed)."""
        operator = make_user(db_session, role=UserRole.OPERATOR)
        path, _ = make_pdf_file(tmp_path)
        drawing = make_document(
            db_session,
            part_id=self._part_id(db_session),
            file_path=path,
            file_name='ev"il\rna\nme–ø.pdf',
        )

        resp = client.get(inline_url(drawing.id), headers=user_headers(operator))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.headers["content-disposition"] == 'inline; filename="evilname.pdf"'


# ---------------------------------------------------------------------------
# Kiosk-scope fence: both new routes are REACHABLE for a badge-minted
# scope="kiosk" operator token; the desktop document surfaces stay fenced.
# ---------------------------------------------------------------------------


class TestKioskScopeFenceOnDocumentRoutes:
    def _kiosk_scoped_token(self, user) -> str:
        return create_access_token(subject=user.id, company_id=COMPANY_A, scope="kiosk")

    def test_kiosk_scoped_token_reaches_both_new_routes(self, client: TestClient, db_session: Session, tmp_path: Path):
        operator = make_user(db_session, role=UserRole.OPERATOR)
        headers = bearer(self._kiosk_scoped_token(operator))
        wc = make_work_center(db_session)
        wo, op = make_wo_with_operation(db_session, work_center=wc)
        path, content = make_pdf_file(tmp_path)
        drawing = make_document(db_session, part_id=wo.part_id, file_path=path)

        discovery = client.get(docs_url(op.id), headers=headers)
        assert discovery.status_code == status.HTTP_200_OK, discovery.text
        assert discovery.json()["drawing"]["document_id"] == drawing.id

        inline = client.get(inline_url(drawing.id), headers=headers)
        assert inline.status_code == status.HTTP_200_OK, inline.text
        assert inline.content == content

    def test_kiosk_scoped_token_still_fenced_off_the_desktop_document_routes(
        self, client: TestClient, db_session: Session, tmp_path: Path
    ):
        """Regression pin: opening the shop-floor inline route must NOT have
        widened the fence -- the desktop /documents list and download stay 403
        with the fence's own detail, even for an ADMIN's kiosk-scoped token
        and even on a document the shop-floor route WOULD serve."""
        admin = make_user(db_session, role=UserRole.ADMIN)
        headers = bearer(self._kiosk_scoped_token(admin))
        path, _ = make_pdf_file(tmp_path)
        drawing = make_document(db_session, file_path=path)

        for fenced_path in ("/api/v1/documents/", f"/api/v1/documents/{drawing.id}/download"):
            resp = client.get(fenced_path, headers=headers)
            assert resp.status_code == status.HTTP_403_FORBIDDEN, f"{fenced_path}: {resp.status_code} {resp.text}"
            assert resp.json()["detail"] == FENCE_DETAIL
