"""Restricted and export-controlled documents stay out of reach on EVERY read path (AS9100D 7.5.3.1).

Migration 131 gives each document an ``access_level``; ``app/services/document_access.py`` is the
one rule (``tests/test_document_access.py`` holds its SQL and Python forms together). That rule
is only as good as the call sites that apply it, and the risk is a read path that still serves a
document, its file, its title or its count to someone who may not open it. So this file walks
every enforcement site the PR names, through the real routes and services, with two cases each:

* ``restricted``: an Operator may not open it, a Supervisor may;
* ``export_controlled``: an Admin who is NOT export-authorized may not open it, an
  export-authorized Admin may (no role bypasses export control).

Routes that only Admin / Manager / Quality / Supervisor can call at all (the writes, the
template and laser-nest verbs, the Hank attach proposal) run the export-controlled case only:
every role that reaches them already opens ``restricted`` documents, so only export control can
hide a document there.

"Hidden" means the same answer as a document that does not exist: a 404 (``Document not found``
or the route's own missing-document text), ``None`` in place of the id, or simply absent from a
list -- and it is checked BEFORE any other check, so a later 409/422 cannot say anything about a
hidden document either. Writes refused that way change nothing.

Below the matrix: the credential cases (a crew-station badge token, a station token with no
person behind it, full / read-only / AI-external API tokens, all acting with the BOUND user's
flag), superusers and Platform Admins (no export bypass), and the revision-chain and
superseded-drawing rules.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterator, List, Optional
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.endpoints import documents as documents_router
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.document import Document, DocumentType
from app.models.engineering_change import ECOType, EngineeringChangeOrder
from app.models.hank import HankTask
from app.models.laser_nest import LaserNest, LaserNestPackage
from app.models.part import Part
from app.models.process_sheet import OperationStepRecord
from app.models.qms_standard import QMSClause, QMSClauseEvidence, QMSStandard
from app.models.time_entry import TimeEntry
from app.models.user import User, UserRole
from app.models.work_order import OperationStatus, WorkOrderStatus
from app.services import hank_watch_service
from app.services.copilot_service import CopilotService
from app.services.storage_service import LocalStorageBackend
from tests.api.kiosk_test_helpers import (
    bearer,
    kiosk_token_for,
    make_kiosk_station,
    make_user,
    make_wo_with_operation,
    make_work_center,
    mint_badge_token,
    queue_url,
    user_headers,
)
from tests.api.test_process_sheet_capture import _add_wo_step
from tests.api.test_work_order_duplicate import build_laser_source

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

API = "/api/v1"
STANDARD = "standard"
RESTRICTED = "restricted"
EXPORT = "export_controlled"
DOCUMENT_NOT_FOUND = "Document not found"
API_TOKENS_URL = f"{API}/api-tokens/"

_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


# --------------------------------------------------------------------------- #
# The world: one company, every persona, one work order with one operation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Case:
    """Who may not open a document at ``level`` and who may."""

    level: str
    hidden: str
    visible: str


RESTRICTED_CASE = Case(RESTRICTED, hidden="operator", visible="supervisor")
EXPORT_CASE = Case(EXPORT, hidden="admin", visible="cleared_admin")
BOTH = (RESTRICTED_CASE, EXPORT_CASE)
EXPORT_ONLY = (EXPORT_CASE,)


class World:
    def __init__(self, client: TestClient, db: Session, tmp_path, monkeypatch):
        self.client = client
        self.db = db
        self.files = tmp_path / "files"
        self.files.mkdir()
        self.uploads = tmp_path / "uploads"
        self.monkeypatch = monkeypatch
        self.users: Dict[str, User] = {
            "operator": make_user(db, role=UserRole.OPERATOR),
            "supervisor": make_user(db, role=UserRole.SUPERVISOR),
            "quality": make_user(db, role=UserRole.QUALITY),
            "manager": make_user(db, role=UserRole.MANAGER),
            "shipping": make_user(db, role=UserRole.SHIPPING),
            "admin": make_user(db, role=UserRole.ADMIN),
            "cleared_admin": make_user(db, role=UserRole.ADMIN),
            "cleared_operator": make_user(db, role=UserRole.OPERATOR),
            "superuser": make_user(db, role=UserRole.ADMIN),
            "platform_admin": make_user(db, role=UserRole.PLATFORM_ADMIN),
        }
        self.users["cleared_admin"].export_authorized = True
        self.users["cleared_operator"].export_authorized = True
        self.users["superuser"].is_superuser = True
        db.commit()
        self.headers = {key: user_headers(user) for key, user in self.users.items()}
        self.work_center = make_work_center(db)
        self.work_order, self.operation = make_wo_with_operation(db, work_center=self.work_center)
        self.part = db.get(Part, self.work_order.part_id)

    # -- requests -----------------------------------------------------------
    def request(self, method: str, path: str, persona: str, **kwargs):
        # Production gives every request its own session; the test client shares one. Expire it
        # so no request reads another's in-memory state (the work-order enrichment hides a nest
        # PDF with ``set_committed_value``, which would otherwise outlive its own request here).
        self.db.expire_all()
        return getattr(self.client, method)(f"{API}{path}", headers=self.headers[persona], **kwargs)

    def raw_get(self, path: str, headers: Dict[str, str]):
        """A GET with any credential (badge, station or API token), on a fresh session view."""
        self.db.expire_all()
        return self.client.get(f"{API}{path}", headers=headers)

    def get(self, path: str, persona: str, **kwargs):
        return self.request("get", path, persona, **kwargs)

    def post(self, path: str, persona: str, **kwargs):
        return self.request("post", path, persona, **kwargs)

    def put(self, path: str, persona: str, **kwargs):
        return self.request("put", path, persona, **kwargs)

    def patch(self, path: str, persona: str, **kwargs):
        return self.request("patch", path, persona, **kwargs)

    def delete(self, path: str, persona: str, **kwargs):
        return self.request("delete", path, persona, **kwargs)

    # -- records ------------------------------------------------------------
    def document(
        self,
        level: str,
        *,
        document_type: DocumentType = DocumentType.DRAWING,
        part_id: Optional[int] = None,
        work_order_id: Optional[int] = None,
        status: str = "released",
        previous_revision_id: Optional[int] = None,
        revision: str = "A",
        released_at: Optional[datetime] = None,
        company_id: int = 1,
    ) -> Document:
        """A document with real bytes on disk; its number and title carry a unique canary."""
        n = _next()
        path = self.files / f"access-{n}.pdf"
        path.write_bytes(b"%PDF-1.4 access canary " + str(n).encode())
        document = Document(
            document_number=f"ACCDOC-{n:05d}",
            title=f"Access canary drawing {n}",
            revision=revision,
            document_type=document_type,
            status=status,
            access_level=level,
            file_name=f"access-{n}.pdf",
            file_path=str(path),
            file_size=path.stat().st_size,
            mime_type="application/pdf",
            part_id=part_id,
            work_order_id=work_order_id,
            previous_revision_id=previous_revision_id,
            released_at=released_at or datetime.utcnow(),
            company_id=company_id,
        )
        self.db.add(document)
        self.db.commit()
        self.db.refresh(document)
        return document

    def nest(self, *, document_id: Optional[int], operation=None) -> LaserNest:
        operation = operation or self.operation
        package = LaserNestPackage(
            company_id=1, child_work_order_id=operation.work_order_id, package_name=f"Pkg {_next()}"
        )
        self.db.add(package)
        self.db.flush()
        nest = LaserNest(
            company_id=1,
            package_id=package.id,
            work_order_operation_id=operation.id,
            nest_name=f"NEST-{_next()}",
            cnc_number=f"CNC{_next()}",
            planned_runs=1,
            document_id=document_id,
        )
        self.db.add(nest)
        self.db.commit()
        self.db.refresh(nest)
        return nest

    def fresh(self, model, row_id):
        self.db.expire_all()
        return self.db.get(model, row_id)


@pytest.fixture
def w(client, db_session, tmp_path, monkeypatch) -> World:
    world = World(client, db_session, tmp_path, monkeypatch)
    monkeypatch.setattr(documents_router, "UPLOAD_DIR", str(world.uploads))
    monkeypatch.setattr(documents_router, "get_storage", lambda: LocalStorageBackend())
    return world


# --------------------------------------------------------------------------- #
# Small assertions
# --------------------------------------------------------------------------- #


def _hidden(response, detail: str = DOCUMENT_NOT_FOUND) -> None:
    assert (response.status_code, response.json().get("detail")) == (404, detail), response.text


def _ids(response) -> List[int]:
    assert response.status_code == 200, response.text
    return [row["id"] for row in response.json()]


def _walk(value: Any) -> Iterator[Dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _nest_payloads(body: Any) -> List[Dict[str, Any]]:
    """Every laser-nest payload in a response (the shapes all carry ``has_document``)."""
    return [row for row in _walk(body) if "has_document" in row]


def _assert_nest_hidden(body: Any, document: Document) -> None:
    """Every nest payload reads as a nest with no PDF: no id, no file name, ``has_document`` false."""
    rows = _nest_payloads(body)
    assert rows, "the response carries no laser-nest payload, so it proves nothing"
    for row in rows:
        assert row["has_document"] is False, row
        assert row.get("document_file_name") is None, row
        assert row.get("document_id") is None, row
    assert document.file_name not in json.dumps(body)


def _assert_nest_visible(body: Any, document: Document) -> None:
    rows = [row for row in _nest_payloads(body) if row["has_document"]]
    assert rows, body
    for row in rows:
        assert row["document_file_name"] == document.file_name
        assert row["document_id"] == document.id


def _upload(w: World, persona: str, **values):
    data = {"title": "Access canary upload", "document_type": "drawing", **{k: str(v) for k, v in values.items()}}
    return w.post(
        "/documents/upload",
        persona,
        data=data,
        files={"file": ("upload.pdf", b"%PDF-1.4 upload", "application/pdf")},
    )


def _uploaded_files(w: World) -> List[str]:
    return sorted(p.name for p in w.uploads.rglob("*") if p.is_file()) if w.uploads.exists() else []


def _audit_count(w: World, resource_type: str, resource_id: int) -> int:
    w.db.expire_all()
    return (
        w.db.query(AuditLog)
        .filter(AuditLog.resource_type == resource_type, AuditLog.resource_id == resource_id)
        .count()
    )


# --------------------------------------------------------------------------- #
# The sites. Each takes the world and a Case and asserts BOTH sides.
# --------------------------------------------------------------------------- #


def site_documents_list(w: World, case: Case) -> None:
    document = w.document(case.level)
    for params in ({}, {"access_level": case.level}, {"search": document.document_number}):
        assert document.id not in _ids(w.get("/documents/", case.hidden, params=params)), params
        assert document.id in _ids(w.get("/documents/", case.visible, params=params)), params


def site_documents_get(w: World, case: Case) -> None:
    document = w.document(case.level)
    _hidden(w.get(f"/documents/{document.id}", case.hidden))
    shown = w.get(f"/documents/{document.id}", case.visible)
    assert shown.status_code == 200, shown.text
    assert shown.json()["access_level"] == case.level


def site_documents_revisions(w: World, case: Case) -> None:
    document = w.document(case.level)
    _hidden(w.get(f"/documents/{document.id}/revisions", case.hidden))
    assert _ids(w.get(f"/documents/{document.id}/revisions", case.visible)) == [document.id]


def site_documents_download(w: World, case: Case) -> None:
    document = w.document(case.level)
    _hidden(w.get(f"/documents/{document.id}/download", case.hidden))
    served = w.get(f"/documents/{document.id}/download", case.visible)
    assert served.status_code == 200, served.text
    assert served.content.startswith(b"%PDF-1.4 access canary")


def site_documents_attach(w: World, case: Case) -> None:
    document = w.document(case.level)
    # A work order that does not exist would be a 404 "Work order not found": the document check
    # answers first, so the reply says nothing about the document.
    for work_order_id in (999_999, w.work_order.id):
        refused = w.post(
            f"/documents/{document.id}/attach-work-order", case.hidden, json={"work_order_id": work_order_id}
        )
        _hidden(refused)
    assert w.fresh(Document, document.id).work_order_id is None
    assert _audit_count(w, "document", document.id) == 0
    attached = w.post(
        f"/documents/{document.id}/attach-work-order", case.visible, json={"work_order_id": w.work_order.id}
    )
    assert attached.status_code == 200, attached.text
    assert w.fresh(Document, document.id).work_order_id == w.work_order.id


def site_documents_delete(w: World, case: Case) -> None:
    document = w.document(case.level)
    _hidden(w.delete(f"/documents/{document.id}", case.hidden))
    assert w.fresh(Document, document.id) is not None
    assert _audit_count(w, "document", document.id) == 0
    deleted = w.delete(f"/documents/{document.id}", case.visible)
    assert deleted.status_code == 200, deleted.text
    assert w.fresh(Document, document.id) is None


def site_upload_predecessor(w: World, case: Case) -> None:
    document = w.document(case.level)
    before = w.db.query(Document).count()
    # Same label as the predecessor and no revision notes: each would be a 422 of its own, so a
    # 404 here proves the visibility check runs first.
    refused = _upload(w, case.hidden, previous_revision_id=document.id, revision="A")
    _hidden(refused, "Previous document revision not found")
    assert w.db.query(Document).count() == before
    assert _uploaded_files(w) == []
    revised = _upload(w, case.visible, previous_revision_id=document.id, revision="B", revision_notes="New hole")
    assert revised.status_code == 200, revised.text
    assert revised.json()["previous_revision_id"] == document.id
    assert revised.json()["access_level"] == case.level


def site_kiosk_discovery(w: World, case: Case) -> None:
    document = w.document(case.level, part_id=w.part.id)
    url = f"/shop-floor/operations/{w.operation.id}/documents"
    hidden = w.get(url, case.hidden)
    assert hidden.status_code == 200, hidden.text
    assert hidden.json()["drawing"] is None
    assert document.document_number not in hidden.text and document.title not in hidden.text
    shown = w.get(url, case.visible)
    assert shown.json()["drawing"]["document_id"] == document.id


def site_kiosk_inline(w: World, case: Case) -> None:
    document = w.document(case.level, part_id=w.part.id)
    _hidden(w.get(f"/shop-floor/documents/{document.id}/inline", case.hidden))
    served = w.get(f"/shop-floor/documents/{document.id}/inline", case.visible)
    assert served.status_code == 200, served.text
    assert served.headers["content-type"].startswith("application/pdf")


def site_kiosk_nest_document(w: World, case: Case) -> None:
    """Discovery's nest tab and the inline route for a nest's reference PDF (no part link)."""
    document = w.document(case.level)
    w.nest(document_id=document.id)
    url = f"/shop-floor/operations/{w.operation.id}/documents"
    nest = w.get(url, case.hidden).json()["nest"]
    assert nest["document_id"] is None and nest["file_name"] is None
    _hidden(w.get(f"/shop-floor/documents/{document.id}/inline", case.hidden))
    nest = w.get(url, case.visible).json()["nest"]
    assert nest["document_id"] == document.id and nest["file_name"] == document.file_name
    assert w.get(f"/shop-floor/documents/{document.id}/inline", case.visible).status_code == 200


def site_laser_nest_inline(w: World, case: Case) -> None:
    document = w.document(case.level)
    nest = w.nest(document_id=document.id)
    # The same text as a nest with no PDF at all.
    _hidden(w.get(f"/laser-nests/{nest.id}/document", case.hidden), "No document attached")
    served = w.get(f"/laser-nests/{nest.id}/document", case.visible)
    assert served.status_code == 200, served.text
    assert served.content.startswith(b"%PDF-1.4 access canary")


def site_laser_nest_attach(w: World, case: Case) -> None:
    document = w.document(case.level)
    nest = w.nest(document_id=None)
    _hidden(w.post(f"/laser-nests/{nest.id}/attach-document", case.hidden, json={"document_id": document.id}))
    assert w.fresh(LaserNest, nest.id).document_id is None
    attached = w.post(f"/laser-nests/{nest.id}/attach-document", case.visible, json={"document_id": document.id})
    assert attached.status_code == 200, attached.text
    assert attached.json()["document_id"] == document.id and attached.json()["has_document"] is True


def site_laser_nest_manual_response(w: World, case: Case) -> None:
    """``manual_nest_response_dict``: the PATCH answer names no PDF the caller may not open."""
    document = w.document(case.level)
    nest = w.nest(document_id=document.id)
    hidden = w.patch(f"/laser-nests/{nest.id}", case.hidden, json={"material": "A36"})
    assert hidden.status_code == 200, hidden.text
    _assert_nest_hidden(hidden.json(), document)
    shown = w.patch(f"/laser-nests/{nest.id}", case.visible, json={"material": "304 SS"})
    _assert_nest_visible(shown.json(), document)


def site_work_order_nest_fields(w: World, case: Case) -> None:
    document = w.document(case.level)
    nest = w.nest(document_id=document.id)
    hidden = w.get(f"/work-orders/{w.work_order.id}", case.hidden)
    assert hidden.status_code == 200, hidden.text
    # No id, no name, no flag: the same as a nest without a PDF.
    _assert_nest_hidden(hidden.json(), document)
    _assert_nest_visible(w.get(f"/work-orders/{w.work_order.id}", case.visible).json(), document)
    # Hiding it in the payload never wrote the nest row.
    assert w.fresh(LaserNest, nest.id).document_id == document.id


def site_shop_floor_nest_payloads(w: World, case: Case) -> None:
    """``_laser_nest_payload``: operation detail, the operations list and the work-center queue."""
    document = w.document(case.level)
    w.nest(document_id=document.id)
    urls = (
        f"/shop-floor/operations/{w.operation.id}",
        f"/shop-floor/operations?work_center_id={w.work_center.id}",
        queue_url(w.work_center.id).removeprefix(API),
    )
    for url in urls:
        hidden = w.get(url, case.hidden)
        assert hidden.status_code == 200, (url, hidden.text)
        _assert_nest_hidden(hidden.json(), document)
        _assert_nest_visible(w.get(url, case.visible).json(), document)


def site_my_active_job(w: World, case: Case) -> None:
    document = w.document(case.level)
    w.nest(document_id=document.id)
    for persona in (case.hidden, case.visible):
        w.db.add(
            TimeEntry(
                company_id=1,
                user_id=w.users[persona].id,
                work_order_id=w.work_order.id,
                operation_id=w.operation.id,
                work_center_id=w.work_center.id,
                clock_in=datetime.utcnow() - timedelta(minutes=5),
            )
        )
    w.db.commit()
    hidden = w.get("/shop-floor/my-active-job", case.hidden)
    assert hidden.status_code == 200, hidden.text
    _assert_nest_hidden(hidden.json(), document)
    _assert_nest_visible(w.get("/shop-floor/my-active-job", case.visible).json(), document)


def site_work_order_template_and_duplicate(w: World, case: Case) -> None:
    """The drafts a template (or a duplicate) creates carry the source's nest PDF reference."""
    source, _wc, _operations, nests, _package = build_laser_source(w.db, with_document=True)
    document = w.db.get(Document, nests[0].document_id)
    document.access_level = case.level
    w.db.commit()
    saved = w.post(
        "/work-order-templates", case.visible, json={"source_work_order_id": source.id, "name": f"Set {_next()}"}
    )
    assert saved.status_code == 201, saved.text
    template_id = saved.json()["id"]
    for verb in ("template", "duplicate"):
        for persona, expect_visible in ((case.hidden, False), (case.visible, True)):
            if verb == "template":
                response = w.post(f"/work-order-templates/{template_id}/use", persona, json={})
            else:
                response = w.post(f"/work-orders/{source.id}/duplicate", persona, json={"quantity_ordered": 5})
            assert response.status_code == 201, (verb, persona, response.text)
            if expect_visible:
                _assert_nest_visible(response.json(), document)
            else:
                _assert_nest_hidden(response.json(), document)


def _running_operation(w: World):
    return make_wo_with_operation(
        w.db,
        work_center=w.work_center,
        op_status=OperationStatus.IN_PROGRESS,
        wo_status=WorkOrderStatus.IN_PROGRESS,
    )


def site_step_attachment_capture(w: World, case: Case) -> None:
    work_order, operation = _running_operation(w)
    step = _add_wo_step(w.db, operation, step_type="photo", config=None)
    document = w.document(case.level, document_type=DocumentType.QUALITY_RECORD, work_order_id=work_order.id)
    url = f"/shop-floor/operations/{operation.id}/steps/{step.id}/records"
    _hidden(w.post(url, case.hidden, json={"attachment_document_id": document.id}), "Attachment document not found")
    assert w.db.query(OperationStepRecord).filter_by(work_order_operation_id=operation.id).count() == 0
    recorded = w.post(url, case.visible, json={"attachment_document_id": document.id})
    assert recorded.status_code == 201, recorded.text
    assert recorded.json()["attachment_document_id"] == document.id


def site_steps_view(w: World, case: Case) -> None:
    work_order, operation = _running_operation(w)
    step = _add_wo_step(w.db, operation, step_type="photo", config=None)
    document = w.document(case.level, document_type=DocumentType.QUALITY_RECORD, work_order_id=work_order.id)
    recorded = w.post(
        f"/shop-floor/operations/{operation.id}/steps/{step.id}/records",
        case.visible,
        json={"attachment_document_id": document.id},
    )
    assert recorded.status_code == 201, recorded.text
    url = f"/shop-floor/operations/{operation.id}/steps"

    def attachment_ids(persona: str) -> List[Optional[int]]:
        response = w.get(url, persona)
        assert response.status_code == 200, response.text
        return [record["attachment_document_id"] for s in response.json()["steps"] for record in s["records"]]

    assert attachment_ids(case.hidden) == [None]
    assert attachment_ids(case.visible) == [document.id]
    # The serialization never touched the stored row.
    assert w.fresh(OperationStepRecord, recorded.json()["id"]).attachment_document_id == document.id


def site_qms_standard_link(w: World, case: Case) -> None:
    document = w.document(case.level)
    refused = w.post(
        "/qms-standards/", case.hidden, json={"name": "AS9100D", "version": "D", "document_id": document.id}
    )
    _hidden(refused)
    assert w.db.query(QMSStandard).count() == 0
    created = w.post(
        "/qms-standards/", case.visible, json={"name": "AS9100D", "version": "D", "document_id": document.id}
    )
    assert created.status_code == 201, created.text
    standard_id = created.json()["id"]
    # An edit that re-sends the link the row already holds is not choosing a document.
    echoed = w.put(f"/qms-standards/{standard_id}", case.hidden, json={"description": "x", "document_id": document.id})
    assert echoed.status_code == 200, echoed.text
    # Choosing ANOTHER hidden document is.
    other = w.document(case.level)
    _hidden(w.put(f"/qms-standards/{standard_id}", case.hidden, json={"document_id": other.id}))
    assert w.fresh(QMSStandard, standard_id).document_id == document.id


def site_qms_evidence_link(w: World, case: Case) -> None:
    document = w.document(case.level)
    standard = QMSStandard(name=f"STD-{_next()}", version="D", is_active=True, company_id=1)
    w.db.add(standard)
    w.db.flush()
    clause = QMSClause(standard_id=standard.id, company_id=1, clause_number="7.5.3", title="Control of information")
    w.db.add(clause)
    w.db.commit()
    body = {"evidence_type": "document", "title": "Controlled drawing", "document_id": document.id}
    _hidden(w.post(f"/qms-standards/clauses/{clause.id}/evidence", case.hidden, json=body))
    assert w.db.query(QMSClauseEvidence).count() == 0
    created = w.post(f"/qms-standards/clauses/{clause.id}/evidence", case.visible, json=body)
    assert created.status_code == 201, created.text
    evidence_id = created.json()["id"]
    echoed = w.put(
        f"/qms-standards/evidence/{evidence_id}", case.hidden, json={"title": "Re-titled", "document_id": document.id}
    )
    assert echoed.status_code == 200, echoed.text
    other = w.document(case.level)
    _hidden(w.put(f"/qms-standards/evidence/{evidence_id}", case.hidden, json={"document_id": other.id}))


def site_qms_auto_evidence(w: World, case: Case) -> None:
    document = w.document(case.level, document_type=DocumentType.PROCEDURE)
    standard = QMSStandard(name=f"STD-{_next()}", version="D", is_active=True, company_id=1)
    w.db.add(standard)
    w.db.flush()
    clause = QMSClause(
        standard_id=standard.id,
        company_id=1,
        clause_number="7.5",
        title="Documented information",
        description="Document control of every procedure and work instruction",
    )
    w.db.add(clause)
    w.db.commit()

    def block(persona: str) -> Dict[str, Any]:
        response = w.get(f"/qms-standards/clauses/{clause.id}/auto-evidence", persona)
        assert response.status_code == 200, response.text
        (found,) = [
            row for row in response.json()["discovered_evidence"] if row["title"].startswith("Document Control")
        ]
        return found

    hidden = block(case.hidden)
    assert hidden["total_count"] == 0 and hidden["examples"] == []
    assert document.title not in json.dumps(hidden) and document.document_number not in json.dumps(hidden)
    shown = block(case.visible)
    assert shown["total_count"] == 1
    assert [example["record_identifier"] for example in shown["examples"]] == [document.document_number]


def _eco(w: World, document_ids: List[int]) -> EngineeringChangeOrder:
    eco = EngineeringChangeOrder(
        eco_number=f"ECO-ACC-{_next():05d}",
        title="Move the hole",
        description="Move the mounting hole",
        eco_type=ECOType.DESIGN_CHANGE,
        reason_for_change="Fit problem at assembly",
        requested_by=w.users["cleared_admin"].id,
        affected_documents=json.dumps(document_ids),
        company_id=1,
    )
    w.db.add(eco)
    w.db.commit()
    return eco


def site_eco_affected_items(w: World, case: Case) -> None:
    document = w.document(case.level)
    standard = w.document(STANDARD)
    eco = _eco(w, [document.id, standard.id])

    def listed(persona: str) -> List[int]:
        response = w.get(f"/eco/eco/affected-items/{eco.id}", persona)
        assert response.status_code == 200, response.text
        return sorted(row["id"] for row in response.json()["documents"])

    assert listed(case.hidden) == [standard.id]
    assert listed(case.visible) == sorted([document.id, standard.id])


def _eco_body(document_ids: List[int]) -> Dict[str, Any]:
    return {
        "title": "ECO access",
        "description": "valid description here",
        "eco_type": "design_change",
        "reason_for_change": "valid reason here",
        "affected_documents": document_ids,
    }


def site_eco_create_update(w: World, case: Case) -> None:
    document = w.document(case.level)
    missing_id = 987_654
    refused = w.post("/eco/eco/", case.hidden, json=_eco_body([document.id]))
    missing = w.post("/eco/eco/", case.hidden, json=_eco_body([missing_id]))
    assert refused.status_code == missing.status_code == 422, (refused.text, missing.text)
    # The same sentence as an id that does not exist.
    assert refused.json()["detail"].replace(str(document.id), "N") == missing.json()["detail"].replace(
        str(missing_id), "N"
    )
    assert w.db.query(EngineeringChangeOrder).count() == 0
    created = w.post("/eco/eco/", case.visible, json=_eco_body([document.id]))
    assert created.status_code == 200, created.text
    eco_id = created.json()["id"]
    # Re-sending the list the ECO already holds is not choosing the hidden document again.
    echoed = w.put(
        f"/eco/eco/{eco_id}", case.hidden, json={"title": "ECO access v2", "affected_documents": [document.id]}
    )
    assert echoed.status_code == 200, echoed.text
    other = w.document(case.level)
    added = w.put(f"/eco/eco/{eco_id}", case.hidden, json={"affected_documents": [document.id, other.id]})
    assert added.status_code == 422, added.text
    assert str(other.id) in added.json()["detail"]
    assert json.loads(w.fresh(EngineeringChangeOrder, eco_id).affected_documents) == [document.id]


def _reaches_the_model(case: Case) -> bool:
    """Model-bound reads (copilot, Hank) never carry export_controlled metadata, for ANYONE."""
    return case.level != EXPORT


def site_copilot_search_documents(w: World, case: Case) -> None:
    """Copilot ``search_documents``. Export-controlled stays out even for the authorized Admin."""
    document = w.document(case.level, work_order_id=w.work_order.id)

    def found(persona: str, tool_input: Dict[str, Any]) -> List[int]:
        execution = CopilotService(w.db, company_id=1, user=w.users[persona]).execute_tool(
            "search_documents", tool_input
        )
        assert execution.is_error is False, execution.payload
        return [row["id"] for row in execution.payload["documents"]]

    for tool_input in ({"query": document.document_number}, {"work_order_id": w.work_order.id}, {}):
        assert document.id not in found(case.hidden, tool_input), tool_input
        assert (document.id in found(case.visible, tool_input)) is _reaches_the_model(case), tool_input
    # The visible persona does open it through the app itself: the exclusion is the model's only.
    assert w.get(f"/documents/{document.id}", case.visible).status_code == 200


def site_hank_operations_documents(w: World, case: Case) -> None:
    """``HankOperationsService.documents``: readiness, knowledge and the shipping packet.

    Model-bound like copilot: export-controlled stays out even for the authorized Admin.
    """
    document = w.document(case.level, work_order_id=w.work_order.id)
    for report in ("readiness", "knowledge", "shipping-packet"):
        url = f"/hank/work-orders/{w.work_order.id}/{report}"
        # The packet needs shipping:view, which an Operator lacks; Shipping cannot open restricted.
        hidden_persona = "shipping" if (report == "shipping-packet" and case.hidden == "operator") else case.hidden
        hidden = w.get(url, hidden_persona)
        assert hidden.status_code == 200, (report, hidden.text)
        assert document.document_number not in hidden.text and document.title not in hidden.text, report
        shown = w.get(url, case.visible)
        assert shown.status_code == 200, (report, shown.text)
        assert (document.document_number in shown.text) is _reaches_the_model(case), report
        if not _reaches_the_model(case):
            assert document.title not in shown.text, report


def _handoff_body(w: World, recipient: User, document_ids: List[int]) -> Dict[str, Any]:
    return {
        "expected_company_id": 1,
        "request_key": str(uuid4()),
        "work_order_id": w.work_order.id,
        "recipient_id": recipient.id,
        "summary": "Second shift: finish the brackets",
        "completed_work": "18 good parts",
        "remaining_work": "Finish 12 and inspect",
        "problems": "Check fixture first",
        "quantity_remaining": 12,
        "document_ids": document_ids,
    }


def site_hank_handoff_references(w: World, case: Case) -> None:
    """The recipient's view of a handoff names no document they may not open."""
    document = w.document(case.level, work_order_id=w.work_order.id)
    created = w.post("/hank/handoffs", case.visible, json=_handoff_body(w, w.users[case.hidden], [document.id]))
    assert created.status_code == 200, created.text
    handoff_id = created.json()["id"]

    def references(persona: str, path: str) -> List[int]:
        response = w.get(path, persona)
        assert response.status_code == 200, response.text
        rows = response.json()["handoffs"] if "handoffs" in response.json() else [response.json()]
        return [ref["id"] for row in rows for ref in row["document_references"]]

    for path in (f"/hank/handoffs/{handoff_id}", "/hank/handoffs"):
        assert references(case.hidden, path) == [], path
        assert references(case.visible, path) == [document.id], path


def site_hank_handoff_create(w: World, case: Case) -> None:
    document = w.document(case.level, work_order_id=w.work_order.id)
    recipient = w.users["quality"]
    refused = w.post("/hank/handoffs", case.hidden, json=_handoff_body(w, recipient, [document.id]))
    missing = w.post("/hank/handoffs", case.hidden, json=_handoff_body(w, recipient, [987_654]))
    assert refused.status_code == missing.status_code == 422, (refused.text, missing.text)
    assert refused.json()["detail"] == missing.json()["detail"]
    created = w.post("/hank/handoffs", case.visible, json=_handoff_body(w, recipient, [document.id]))
    assert created.status_code == 200, created.text


def site_hank_watch(w: World, case: Case) -> None:
    """No watch fires on, or records in its baseline, a document its owner may not open."""
    w.monkeypatch.setattr(hank_watch_service, "SessionLocal", sessionmaker(bind=w.db.get_bind()))
    baseline_document = w.document(case.level, work_order_id=w.work_order.id)

    def start(persona: str) -> Dict[str, Any]:
        response = w.post(
            "/hank/watches",
            persona,
            json={
                "expected_company_id": 1,
                "request_key": str(uuid4()),
                "work_order_id": w.work_order.id,
                "condition": "pdf_attached",
            },
        )
        assert response.status_code == 200, response.text
        return response.json()

    def check(persona: str, task: Dict[str, Any]) -> Dict[str, Any]:
        response = w.post(
            f"/hank/watches/{task['id']}/check",
            persona,
            json={"expected_company_id": 1, "expected_version": task["version"]},
        )
        assert response.status_code == 200, response.text
        return response.json()

    hidden_task, visible_task = start(case.hidden), start(case.visible)
    assert w.fresh(HankTask, hidden_task["id"]).source_versions_json["document_ids"] == []
    assert w.fresh(HankTask, visible_task["id"]).source_versions_json["document_ids"] == [baseline_document.id]

    arrived = w.document(case.level, work_order_id=w.work_order.id)
    hidden = check(case.hidden, hidden_task)
    assert hidden["status"] == "watching"
    assert arrived.document_number not in json.dumps(hidden)
    shown = check(case.visible, visible_task)
    assert shown["status"] == "completed", shown
    assert shown["result"]["references"][0]["id"] == arrived.id


def site_hank_attach_preview(w: World, case: Case) -> None:
    document = w.document(case.level)

    def propose(persona: str, work_order_id: int):
        return w.post(
            "/hank/tasks",
            persona,
            json={
                "expected_company_id": 1,
                "request_key": str(uuid4()),
                "kind": "attach_document",
                "input": {"document_id": document.id, "work_order_id": work_order_id},
            },
        )

    # The document first: a missing work order would otherwise answer 404 "Work order not found".
    for work_order_id in (999_999, w.work_order.id):
        _hidden(propose(case.hidden, work_order_id))
    assert w.db.query(HankTask).count() == 0
    shown = propose(case.visible, w.work_order.id)
    assert shown.status_code == 200, shown.text
    assert shown.json()["status"] == "awaiting_review"


SITES: List[tuple] = [
    ("documents_list", site_documents_list, BOTH),
    ("documents_get", site_documents_get, BOTH),
    ("documents_revisions", site_documents_revisions, BOTH),
    ("documents_download", site_documents_download, BOTH),
    ("documents_attach_work_order", site_documents_attach, EXPORT_ONLY),
    ("documents_delete", site_documents_delete, EXPORT_ONLY),
    ("upload_invisible_predecessor", site_upload_predecessor, EXPORT_ONLY),
    ("kiosk_discovery_drawing", site_kiosk_discovery, BOTH),
    ("kiosk_inline_drawing", site_kiosk_inline, BOTH),
    ("kiosk_nest_document", site_kiosk_nest_document, BOTH),
    ("laser_nest_inline", site_laser_nest_inline, BOTH),
    ("laser_nest_attach", site_laser_nest_attach, EXPORT_ONLY),
    ("laser_nest_manual_response", site_laser_nest_manual_response, EXPORT_ONLY),
    ("work_order_nest_fields", site_work_order_nest_fields, BOTH),
    ("shop_floor_nest_payloads", site_shop_floor_nest_payloads, BOTH),
    ("my_active_job_nest", site_my_active_job, BOTH),
    ("work_order_template_and_duplicate", site_work_order_template_and_duplicate, EXPORT_ONLY),
    ("step_attachment_capture", site_step_attachment_capture, BOTH),
    ("steps_view", site_steps_view, BOTH),
    ("qms_standard_link", site_qms_standard_link, EXPORT_ONLY),
    ("qms_evidence_link", site_qms_evidence_link, EXPORT_ONLY),
    ("qms_auto_evidence", site_qms_auto_evidence, BOTH),
    ("eco_affected_items", site_eco_affected_items, BOTH),
    ("eco_create_update", site_eco_create_update, EXPORT_ONLY),
    ("copilot_search_documents", site_copilot_search_documents, BOTH),
    ("hank_operations_documents", site_hank_operations_documents, BOTH),
    ("hank_handoff_references", site_hank_handoff_references, BOTH),
    ("hank_handoff_create", site_hank_handoff_create, BOTH),
    ("hank_watch", site_hank_watch, BOTH),
    ("hank_attach_preview", site_hank_attach_preview, EXPORT_ONLY),
]

MATRIX = [pytest.param(site, case, id=f"{name}-{case.level}") for name, site, cases in SITES for case in cases]


@pytest.mark.parametrize("site,case", MATRIX)
def test_enforcement_site_hides_what_the_caller_may_not_open(w: World, site: Callable[[World, Case], None], case):
    site(w, case)


def test_a_standard_document_stays_open_to_everyone(w: World):
    """The control: migration 131 changes nothing until a document is flagged."""
    document = w.document(STANDARD, part_id=w.part.id)
    for persona in w.users:
        assert w.get(f"/documents/{document.id}", persona).status_code == 200, persona
        assert w.get(f"/shop-floor/documents/{document.id}/inline", persona).status_code == 200, persona


# --------------------------------------------------------------------------- #
# Personas the role rule could get wrong
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("persona", ["superuser", "platform_admin"])
def test_superuser_and_platform_admin_open_restricted_but_never_export_controlled(w: World, persona: str):
    restricted = w.document(RESTRICTED, part_id=w.part.id)
    controlled = w.document(EXPORT, part_id=w.part.id)
    listed = _ids(w.get("/documents/", persona))
    assert restricted.id in listed and controlled.id not in listed
    assert w.get(f"/documents/{restricted.id}", persona).status_code == 200
    for path in (
        f"/documents/{controlled.id}",
        f"/documents/{controlled.id}/download",
        f"/documents/{controlled.id}/revisions",
        f"/shop-floor/documents/{controlled.id}/inline",
    ):
        _hidden(w.get(path, persona))


def test_export_authorization_does_not_open_restricted_documents(w: World):
    """The levels are independent: an export-authorized Operator opens export_controlled, not restricted."""
    restricted = w.document(RESTRICTED)
    controlled = w.document(EXPORT)
    listed = _ids(w.get("/documents/", "cleared_operator"))
    assert controlled.id in listed and restricted.id not in listed
    assert w.get(f"/documents/{controlled.id}/download", "cleared_operator").status_code == 200
    _hidden(w.get(f"/documents/{restricted.id}/download", "cleared_operator"))


def test_another_companys_document_is_hidden_whatever_its_level(w: World):
    foreign = w.document(STANDARD, company_id=_company_two(w))
    for persona in ("cleared_admin", "superuser", "platform_admin"):
        _hidden(w.get(f"/documents/{foreign.id}", persona))
        _hidden(w.get(f"/documents/{foreign.id}/download", persona))
        assert foreign.id not in _ids(w.get("/documents/", persona))


def _company_two(w: World) -> int:
    from tests.api.kiosk_test_helpers import ensure_company

    ensure_company(w.db, 2)
    return 2


# --------------------------------------------------------------------------- #
# Revision chains and the kiosk drawing pick
# --------------------------------------------------------------------------- #


def test_revision_history_leaves_out_members_the_caller_may_not_open(w: World):
    rev_a = w.document(STANDARD, part_id=w.part.id)
    rev_b = w.document(RESTRICTED, part_id=w.part.id, previous_revision_id=rev_a.id, revision="B")
    rev_c = w.document(EXPORT, part_id=w.part.id, previous_revision_id=rev_b.id, revision="C")
    assert _ids(w.get(f"/documents/{rev_a.id}/revisions", "operator")) == [rev_a.id]
    _hidden(w.get(f"/documents/{rev_b.id}/revisions", "operator"))
    assert sorted(_ids(w.get(f"/documents/{rev_b.id}/revisions", "supervisor"))) == sorted([rev_a.id, rev_b.id])
    assert sorted(_ids(w.get(f"/documents/{rev_a.id}/revisions", "cleared_admin"))) == sorted(
        [rev_a.id, rev_b.id, rev_c.id]
    )
    # An export-authorized Operator: A and C, never B.
    assert sorted(_ids(w.get(f"/documents/{rev_c.id}/revisions", "cleared_operator"))) == sorted([rev_a.id, rev_c.id])


def test_kiosk_never_shows_a_superseded_revision_in_place_of_a_hidden_current_one(w: World):
    url = f"/shop-floor/operations/{w.operation.id}/documents"
    rev_a = w.document(STANDARD, part_id=w.part.id, released_at=datetime.utcnow() - timedelta(days=2))
    rev_b = w.document(RESTRICTED, part_id=w.part.id, previous_revision_id=rev_a.id, revision="B")
    assert w.get(url, "operator").json()["drawing"] is None
    assert w.get(url, "supervisor").json()["drawing"]["document_id"] == rev_b.id
    # Rev A is still the operator's to open directly; it is just not the CURRENT drawing.
    assert w.get(f"/documents/{rev_a.id}", "operator").status_code == 200


def test_kiosk_shows_the_newest_drawing_the_caller_may_open(w: World):
    """A separate (not superseded) drawing of the part is the operator's pick when the newer one is hidden."""
    url = f"/shop-floor/operations/{w.operation.id}/documents"
    older = w.document(STANDARD, part_id=w.part.id, released_at=datetime.utcnow() - timedelta(days=5))
    newer = w.document(EXPORT, part_id=w.part.id, released_at=datetime.utcnow() - timedelta(days=1))
    assert w.get(url, "operator").json()["drawing"]["document_id"] == older.id
    assert w.get(url, "admin").json()["drawing"]["document_id"] == older.id
    assert w.get(url, "cleared_admin").json()["drawing"]["document_id"] == newer.id


def test_hank_attach_execute_rechecks_the_document_at_execution(w: World):
    """Authorization revoked between proposal and execution: the execute is the same 404, nothing attached."""
    document = w.document(EXPORT)
    proposed = w.post(
        "/hank/tasks",
        "cleared_admin",
        json={
            "expected_company_id": 1,
            "request_key": str(uuid4()),
            "kind": "attach_document",
            "input": {"document_id": document.id, "work_order_id": w.work_order.id},
        },
    )
    assert proposed.status_code == 200, proposed.text
    task = proposed.json()
    w.users["cleared_admin"].export_authorized = False
    w.db.commit()
    executed = w.post(
        f"/hank/tasks/{task['id']}/execute",
        "cleared_admin",
        json={"expected_company_id": 1, "expected_version": task["version"]},
    )
    assert executed.status_code == 404, executed.text
    assert w.fresh(Document, document.id).work_order_id is None


# --------------------------------------------------------------------------- #
# Credentials act with the BOUND user's flag
# --------------------------------------------------------------------------- #


def _badge_token(w: World, persona: str) -> Dict[str, str]:
    station = make_kiosk_station(w.db, work_center=w.work_center)
    minted = mint_badge_token(w.client, kiosk_token_for(station), w.users[persona].employee_id)
    assert minted.status_code == 200, minted.text
    return bearer(minted.json()["access_token"])


def test_a_kiosk_badge_token_carries_the_badge_holders_flags(w: World):
    restricted = w.document(RESTRICTED, part_id=w.part.id, released_at=datetime.utcnow() - timedelta(days=2))
    controlled = w.document(EXPORT, part_id=w.part.id, released_at=datetime.utcnow() - timedelta(days=1))
    discovery = f"/shop-floor/operations/{w.operation.id}/documents"

    def inline(headers, document):
        return w.raw_get(f"/shop-floor/documents/{document.id}/inline", headers)

    operator = _badge_token(w, "operator")
    _hidden(inline(operator, restricted))
    _hidden(inline(operator, controlled))
    assert w.raw_get(discovery, operator).json()["drawing"] is None

    supervisor = _badge_token(w, "supervisor")
    assert inline(supervisor, restricted).status_code == 200
    _hidden(inline(supervisor, controlled))
    assert w.raw_get(discovery, supervisor).json()["drawing"]["document_id"] == restricted.id

    cleared = _badge_token(w, "cleared_operator")
    assert inline(cleared, controlled).status_code == 200
    _hidden(inline(cleared, restricted))
    assert w.raw_get(discovery, cleared).json()["drawing"]["document_id"] == controlled.id


def test_a_crew_station_token_with_no_person_sees_standard_nest_pdfs_only(w: World):
    station = make_kiosk_station(w.db, work_center=w.work_center)
    headers = bearer(kiosk_token_for(station))
    restricted = w.document(RESTRICTED)
    w.nest(document_id=restricted.id)
    _other_wo, other_operation = make_wo_with_operation(w.db, work_center=w.work_center)
    standard = w.document(STANDARD)
    w.nest(document_id=standard.id, operation=other_operation)

    queue = w.raw_get(queue_url(w.work_center.id).removeprefix(API), headers)
    assert queue.status_code == 200, queue.text
    by_document = {row["document_id"]: row for row in _nest_payloads(queue.json())}
    assert restricted.id not in by_document and restricted.file_name not in queue.text
    assert by_document[standard.id]["has_document"] is True
    # A supervisor reading the same queue sees both.
    as_person = w.get(queue_url(w.work_center.id).removeprefix(API), "supervisor").json()
    assert {row["document_id"] for row in _nest_payloads(as_person)} >= {restricted.id, standard.id}


def _api_token(w: World, persona: str, **extra) -> Dict[str, str]:
    response = w.client.post(
        API_TOKENS_URL,
        headers=w.headers["admin"],
        json={"user_id": w.users[persona].id, "label": f"bot {_next()}", **extra},
    )
    assert response.status_code == 201, response.text
    return bearer(response.json()["token"])


def test_a_full_api_token_acts_with_the_bound_users_role_and_live_export_flag(w: World):
    restricted = w.document(RESTRICTED)
    controlled = w.document(EXPORT)

    def opens(headers, document) -> bool:
        response = w.raw_get(f"/documents/{document.id}/download", headers)
        assert response.status_code in (200, 404), response.text
        return response.status_code == 200

    operator = _api_token(w, "operator")
    assert (opens(operator, restricted), opens(operator, controlled)) == (False, False)
    supervisor = _api_token(w, "supervisor")
    assert (opens(supervisor, restricted), opens(supervisor, controlled)) == (True, False)
    admin = _api_token(w, "admin")
    assert (opens(admin, restricted), opens(admin, controlled)) == (True, False)
    cleared = _api_token(w, "cleared_admin")
    assert (opens(cleared, restricted), opens(cleared, controlled)) == (True, True)
    # The flag is read from the live user row: granting after mint opens it at once.
    w.users["admin"].export_authorized = True
    w.db.commit()
    assert opens(admin, controlled) is True
    w.users["admin"].export_authorized = False
    w.db.commit()
    assert opens(admin, controlled) is False


def test_a_read_only_api_token_reaches_no_document_route_and_reads_nests_with_the_bound_flag(w: World):
    controlled = w.document(EXPORT, part_id=w.part.id)
    nest = w.nest(document_id=controlled.id)
    admin = _api_token(w, "admin", scope="read_only", ai_external=False)
    cleared = _api_token(w, "cleared_admin", scope="read_only", ai_external=False)
    for headers in (admin, cleared):
        # The Documents tag is on the read-only deny floor.
        assert w.raw_get(f"/documents/{controlled.id}", headers).status_code == 403
    inline = f"/shop-floor/documents/{controlled.id}/inline"
    # The access rule answers first for the Admin (the same 404 as a missing document); the
    # authorized holder gets past it, and a PDF is not JSON, so the read-only fence refuses it.
    _hidden(w.raw_get(inline, admin))
    refused = w.raw_get(inline, cleared)
    assert refused.status_code == 403 and not refused.content.startswith(b"%PDF")
    discovery = f"/shop-floor/operations/{w.operation.id}/documents"
    hidden = w.raw_get(discovery, admin)
    assert hidden.status_code == 200, hidden.text
    assert hidden.json()["drawing"] is None and hidden.json()["nest"]["document_id"] is None
    shown = w.raw_get(discovery, cleared).json()
    assert shown["drawing"]["document_id"] == controlled.id and shown["nest"]["document_id"] == controlled.id
    detail = f"/work-orders/{w.work_order.id}"
    _assert_nest_hidden(w.raw_get(detail, admin).json(), controlled)
    _assert_nest_visible(w.raw_get(detail, cleared).json(), controlled)
    # A read-only token never writes: hiding the PDF in its payload left the nest row alone.
    assert w.fresh(LaserNest, nest.id).document_id == controlled.id


def test_an_ai_external_token_sees_standard_documents_only_whoever_it_is_bound_to(w: World):
    """Its answers go to an outside AI service: no restricted or export-controlled PDF, even for an
    authorized Admin or a Supervisor. A standard PDF still reads normally (the control)."""
    # A cleared customer, so redaction keeps the work order's text and only the access rule decides.
    w.db.add(Customer(name="Open Commerce Co", code="OPENCO", company_id=1))
    w.work_order.customer_name = "Open Commerce Co"
    w.part.customer_name = "Open Commerce Co"
    w.db.commit()
    controlled = w.document(EXPORT, part_id=w.part.id)
    nest = w.nest(document_id=controlled.id)
    tokens = {
        persona: _api_token(w, persona, scope="read_only", ai_external=True)
        for persona in ("admin", "cleared_admin", "supervisor")
    }
    for persona, headers in tokens.items():
        for path in (
            f"/documents/{controlled.id}",
            f"/documents/{controlled.id}/download",
            f"/shop-floor/documents/{controlled.id}/inline",
            f"/shop-floor/operations/{w.operation.id}/documents",
            f"/laser-nests/{nest.id}/document",
        ):
            assert w.raw_get(path, headers).status_code == 403, (persona, path)
    detail = f"/shop-floor/operations/{w.operation.id}"
    for level in (EXPORT, RESTRICTED):
        document = controlled if level == EXPORT else w.document(RESTRICTED, part_id=w.part.id)
        nest.document_id = document.id
        w.db.commit()
        for persona, headers in tokens.items():
            response = w.raw_get(detail, headers)
            assert response.status_code == 200, (persona, response.text)
            _assert_nest_hidden(response.json(), document)
    # The bound people themselves, signed in, open what their flags allow.
    _assert_nest_visible(w.get(detail, "supervisor").json(), document)
    standard = w.document(STANDARD, part_id=w.part.id)
    nest.document_id = standard.id
    w.db.commit()
    for persona, headers in tokens.items():
        _assert_nest_visible(w.raw_get(detail, headers).json(), standard)


# --------------------------------------------------------------------------- #
# A platform account's export authorization stays in its home company
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("persona", ["platform_admin", "superuser"])
def test_a_platform_accounts_export_authorization_does_not_follow_it_into_another_tenant(w: World, persona: str):
    other = _company_two(w)
    account = w.users[persona]
    home_document = w.document(EXPORT, part_id=w.part.id)
    foreign_document = w.document(EXPORT, company_id=other)
    foreign_restricted = w.document(RESTRICTED, company_id=other)
    at_home = w.headers[persona]
    in_other = user_headers(account, active_company_id=other)

    # Granted by a platform principal in the home company (the platform-principal rule).
    granter = "platform_admin" if persona == "superuser" else "superuser"
    granted = w.put(
        f"/users/{account.id}/export-authorization",
        granter,
        json={"export_authorized": True, "reason": "Program lead at home"},
    )
    assert granted.status_code == 200, granted.text

    # Home: open.
    assert w.raw_get(f"/documents/{home_document.id}", at_home).status_code == 200
    assert w.raw_get(f"/documents/{home_document.id}/download", at_home).status_code == 200
    assert home_document.id in _ids(w.raw_get("/documents/", at_home))
    # Switched into another tenant: the grant does not travel, whatever the role.
    for path in (f"/documents/{foreign_document.id}", f"/documents/{foreign_document.id}/download"):
        _hidden(w.raw_get(path, in_other))
    listed = _ids(w.raw_get("/documents/", in_other))
    assert foreign_document.id not in listed
    # ...while the role pass-through for restricted still applies there.
    assert foreign_restricted.id in listed
    # And the other tenant's access level cannot be changed from there either (404 first).
    changed = w.client.post(
        f"{API}/documents/{foreign_document.id}/access-level",
        headers=in_other,
        json={"access_level": STANDARD, "reason": "probe"},
    )
    _hidden(changed)
    assert w.fresh(Document, foreign_document.id).access_level == EXPORT


# --------------------------------------------------------------------------- #
# The audit log: a document row the reader may not open is redacted on the way out
# --------------------------------------------------------------------------- #

REDACTED_VALUES = {"redacted": "document not available to you"}
REDACTED_DESCRIPTION = "Document change (restricted)"
REDACTED_IDENTIFIER = "Restricted document"


def _audit_rows_for(w: World, persona: str, document_id: int, **params) -> List[Dict[str, Any]]:
    response = w.get("/audit/", persona, params={"resource_type": "document", "limit": 500, **params})
    assert response.status_code == 200, response.text
    return [row for row in response.json() if row["resource_id"] == document_id]


def _assert_redacted(rows: List[Dict[str, Any]], document: Document, title: str) -> None:
    assert rows, "no audit rows for the document, so the check proves nothing"
    for row in rows:
        assert row["description"] == REDACTED_DESCRIPTION, row
        assert row["resource_identifier"] == REDACTED_IDENTIFIER, row
        for key in ("old_values", "new_values"):
            assert row[key] in (None, REDACTED_VALUES), row
    raw = json.dumps(rows)
    for secret in (title, document.document_number, document.file_name):
        assert secret not in raw, secret


def _uploaded_export_document(w: World, title: str) -> Document:
    response = _upload(w, "cleared_admin", title=title, access_level=EXPORT)
    assert response.status_code == 200, response.text
    return w.fresh(Document, response.json()["id"])


def test_audit_rows_of_a_document_the_reader_may_not_open_are_redacted(w: World):
    title = "Audit canary bracket ITAR"
    document = _uploaded_export_document(w, title)
    stored_before = {
        row.id: (row.description, row.resource_identifier, row.new_values)
        for row in w.db.query(AuditLog).filter(AuditLog.resource_type == "document").all()
    }

    # A Manager (no export authorization) sees the row exists, never what it says.
    rows = _audit_rows_for(w, "manager", document.id)
    _assert_redacted(rows, document, title)
    assert all(row["new_values"] == REDACTED_VALUES for row in rows if row["action"] == "CREATE")
    # Who did it stays visible: it is about the actor, not the document.
    assert {row["user_id"] for row in rows} == {w.users["cleared_admin"].id}
    # An Admin without the authorization: the same.
    _assert_redacted(_audit_rows_for(w, "admin", document.id), document, title)

    # A search that matched only hidden text (the number is in the stored identifier and
    # description) does not return the row -- the authorized Admin's same search does, below...
    assert _audit_rows_for(w, "manager", document.id, search=document.document_number) == []
    # ...while a search on what the reader may see still does, redacted.
    actor_name = rows[0]["user_name"]
    by_actor = _audit_rows_for(w, "manager", document.id, search=actor_name)
    assert by_actor
    _assert_redacted(by_actor, document, title)

    # The authorized Admin reads it in full.
    full = _audit_rows_for(w, "cleared_admin", document.id)
    (created,) = [row for row in full if row["action"] == "CREATE"]
    assert created["new_values"]["title"] == title
    assert created["new_values"]["access_level"] == EXPORT
    assert created["resource_identifier"] == document.document_number
    assert _audit_rows_for(w, "cleared_admin", document.id, search=document.document_number)

    # Redaction happens on the way out: nothing stored changed.
    w.db.expire_all()
    for row in w.db.query(AuditLog).filter(AuditLog.id.in_(stored_before)).all():
        assert (row.description, row.resource_identifier, row.new_values) == stored_before[row.id]


def test_a_standard_or_restricted_document_is_not_redacted_for_a_manager(w: World):
    for level in (STANDARD, RESTRICTED):
        response = _upload(w, "quality", title=f"Plain {level} drawing", access_level=level)
        assert response.status_code == 200, response.text
        rows = _audit_rows_for(w, "manager", response.json()["id"])
        (created,) = [row for row in rows if row["action"] == "CREATE"]
        assert created["new_values"]["title"] == f"Plain {level} drawing"
        assert created["description"] != REDACTED_DESCRIPTION


def test_a_deleted_document_is_judged_by_its_last_recorded_level(w: World):
    from app.services.audit_service import AuditService

    still_controlled = _uploaded_export_document(w, "Deleted while controlled")
    cleared_later = _uploaded_export_document(w, "Cleared before deletion")
    lowered = w.post(
        f"/documents/{cleared_later.id}/access-level",
        "cleared_admin",
        json={"access_level": STANDARD, "reason": "Customer released the drawing"},
    )
    assert lowered.status_code == 200, lowered.text
    for document in (still_controlled, cleared_later):
        assert w.delete(f"/documents/{document.id}", "cleared_admin").status_code == 200
        assert w.fresh(Document, document.id) is None

    _assert_redacted(_audit_rows_for(w, "manager", still_controlled.id), still_controlled, "Deleted while controlled")
    readable = _audit_rows_for(w, "manager", cleared_later.id)
    assert readable and all(row["description"] != REDACTED_DESCRIPTION for row in readable)
    assert any((row["new_values"] or {}).get("title") == "Cleared before deletion" for row in readable)

    # A document deleted before migration 131 left no level in its trail: it was standard then.
    legacy_id = 987_001
    AuditService(w.db, user=w.users["cleared_admin"], company_id=1).log(
        action="DELETE",
        resource_type="document",
        resource_id=legacy_id,
        resource_identifier="LEGACY-DOC-1",
        description="Deleted document LEGACY-DOC-1",
        old_values={"title": "Legacy drawing", "document_number": "LEGACY-DOC-1"},
    )
    w.db.commit()
    (legacy,) = _audit_rows_for(w, "manager", legacy_id)
    assert legacy["resource_identifier"] == "LEGACY-DOC-1" and legacy["old_values"]["title"] == "Legacy drawing"


def test_the_integrity_record_view_names_no_hidden_document(w: World):
    document = _uploaded_export_document(w, "Integrity canary")
    w.db.expire_all()
    (record,) = (
        w.db.query(AuditLog)
        .filter(AuditLog.resource_type == "document", AuditLog.resource_id == document.id, AuditLog.action == "CREATE")
        .all()
    )
    url = f"/audit/integrity/record/{record.sequence_number}"
    hidden = w.get(url, "admin")
    assert hidden.status_code == 200, hidden.text
    assert hidden.json()["resource_identifier"] == REDACTED_IDENTIFIER
    assert document.document_number not in hidden.text and "Integrity canary" not in hidden.text
    shown = w.get(url, "cleared_admin")
    assert shown.status_code == 200, shown.text
    assert shown.json()["resource_identifier"] == document.document_number
    # The verification itself is unaffected by the redaction.
    assert hidden.json()["hash_valid"] == shown.json()["hash_valid"]
