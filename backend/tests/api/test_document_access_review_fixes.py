"""Restricted document viewing: the fixes from the independent review of PR #370.

Each test pins one upheld finding, through the real routes and services:

1. Audit-log redaction covers the Hank intake-file and task rows that carry an export-controlled
   document's title, number and extracted fields, not only ``document`` rows.
2. Hank chat's ``prepare_hank_task`` (attach_document) answers an export_controlled document as a
   missing one, so its number, title and revision never reach the model.
3. A level change re-walks the revision chain after its locks are granted, so a revision committed
   while it waited moves with the rest.
4. Attaching a document to an export-controlled customer's work order raises it (the upload rule).
5. Hank chat treats an intake file filed as an export_controlled document as missing.
6. Every work-order response that embeds a nest (PUT, release, operation PUT) hides a nest PDF the
   caller may not open, ``document_id`` included.
7. An audit ``search`` never matches, counts or pages past hidden text.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.endpoints import documents as documents_router
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.document import Document
from app.models.governance import CustomerContract
from app.models.hank_intake import HankIntakeBatch, HankIntakeFile
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderStatus
from app.schemas.hank_intake import IntakeExtraction
from app.services import document_access_service
from app.services.audit_service import AuditService
from app.services.copilot_service import CopilotService
from app.services.document_access import REDACTED_AUDIT_VALUES, revision_chain_ids
from app.services.hank_chat_documents import attachment_manifest, document_evidence
from app.services.storage_service import LocalStorageBackend
from tests.api.kiosk_test_helpers import make_wo_with_operation
from tests.api.test_document_access_enforcement import EXPORT, STANDARD, World

pytestmark = [pytest.mark.api, pytest.mark.requires_db]


@pytest.fixture
def w(client, db_session, tmp_path, monkeypatch) -> World:
    """The enforcement suite's world: every persona, one work order, local file storage."""
    world = World(client, db_session, tmp_path, monkeypatch)
    monkeypatch.setattr(documents_router, "UPLOAD_DIR", str(world.uploads))
    monkeypatch.setattr(documents_router, "get_storage", lambda: LocalStorageBackend())
    return world


REDACTED_DESCRIPTION = "Document change (restricted)"
REDACTED_IDENTIFIER = "Restricted document"


def _audit(w: World, persona: str, **params) -> List[Dict[str, Any]]:
    response = w.get("/audit/", persona, params={"limit": 500, **params})
    assert response.status_code == 200, response.text
    return response.json()


def _assert_row_redacted(row: Dict[str, Any], *secrets: str) -> None:
    assert row["description"] == REDACTED_DESCRIPTION, row
    assert row["resource_identifier"] == REDACTED_IDENTIFIER, row
    for key in ("old_values", "new_values"):
        assert row[key] in (None, dict(REDACTED_AUDIT_VALUES)), row
    raw = json.dumps(row)
    for secret in secrets:
        assert secret not in raw, secret


def _propose_attach(w: World, persona: str, document_id: int, work_order_id: int):
    return w.post(
        "/hank/tasks",
        persona,
        json={
            "expected_company_id": 1,
            "request_key": str(uuid4()),
            "kind": "attach_document",
            "input": {"document_id": document_id, "work_order_id": work_order_id},
        },
    )


def _intake_file(w: World, owner_persona: str, *, document: Document, status: str = "completed") -> HankIntakeFile:
    owner = w.users[owner_persona]
    batch = HankIntakeBatch(
        company_id=1, owner_id=owner.id, credential_key="user", request_key=str(uuid4()), request_hash="a" * 64
    )
    w.db.add(batch)
    w.db.flush()
    row = HankIntakeFile(
        company_id=1,
        batch_id=batch.id,
        ordinal=0,
        filename="controlled-drawing.pdf",
        content_sha256=uuid4().hex + uuid4().hex,
        storage_ref="private/controlled.pdf",
        file_size=100,
        status=status,
        version=3,
        page_count=1,
        analysis_json=IntakeExtraction(
            classification="drawing",
            confidence="high",
            summary="SECRET-EXTRACT heat 4471 bracket",
            evidence=[{"page": 1, "excerpt": "heat 4471"}],
            fields=[
                {
                    "name": "heat_number",
                    "value": "SECRET-HEAT-4471",
                    "confidence": "high",
                    "evidence": [{"page": 1, "excerpt": "heat 4471"}],
                }
            ],
            lines=[],
        ).model_dump(),
        result_json={"document_id": document.id, "document_number": document.document_number},
    )
    w.db.add(row)
    w.db.commit()
    return row


def _controlled_customer_work_order(w: World) -> WorkOrder:
    customer = Customer(name="Synthetic Defense Co", code="SDC", company_id=1)
    w.db.add(customer)
    w.db.flush()
    w.db.add(
        CustomerContract(
            company_id=1,
            customer_id=customer.id,
            contract_name="Program",
            itar_controlled=True,
            default_data_classification="itar_export_controlled",
            active=True,
            expiration_date=datetime.utcnow() + timedelta(days=30),
        )
    )
    w.work_order.customer_name = "Synthetic Defense Co"
    w.db.commit()
    return w.work_order


# --------------------------------------------------------------------------- 1. Hank audit rows


def test_hank_task_audit_rows_naming_a_hidden_document_are_redacted(w: World):
    document = w.document(EXPORT)
    proposed = _propose_attach(w, "cleared_admin", document.id, w.work_order.id)
    assert proposed.status_code == 200, proposed.text
    task = proposed.json()
    executed = w.post(
        f"/hank/tasks/{task['id']}/execute",
        "cleared_admin",
        json={"expected_company_id": 1, "expected_version": task["version"]},
    )
    assert executed.status_code == 200, executed.text
    for persona in ("manager", "admin"):
        rows = [row for row in _audit(w, persona, resource_type="hank_task") if row["resource_id"] == task["id"]]
        assert len(rows) == 2, rows  # CREATE and the execute UPDATE
        for row in rows:
            _assert_row_redacted(row, document.document_number, document.title)
        # A search on the hidden number finds nothing at all.
        assert _audit(w, persona, search=document.document_number) == []
    full = [row for row in _audit(w, "cleared_admin", resource_type="hank_task") if row["resource_id"] == task["id"]]
    assert any(document.document_number in json.dumps(row) for row in full)
    assert any(row["resource_identifier"] == f"Attach {document.document_number}" for row in full)


def test_hank_intake_audit_rows_naming_a_hidden_document_are_redacted(w: World):
    document = w.document(EXPORT)
    intake = _intake_file(w, "cleared_admin", document=document)
    audit = AuditService(w.db, w.users["cleared_admin"], company_id=1)
    audit.log_required(
        "UPDATE",
        "hank_intake_file",
        resource_id=intake.id,
        old_values={"status": "awaiting_review", "version": 1},
        new_values={
            "status": "planned",
            "version": 2,
            "plan": {
                "input": {
                    "title": document.title,
                    "description": "SECRET-DESCRIPTION hole .250",
                    "reviewed_fields": [{"name": "heat_number", "value": "SECRET-HEAT-4471"}],
                }
            },
        },
    )
    audit.log_required(
        "UPDATE",
        "hank_intake_file",
        resource_id=intake.id,
        old_values={"status": "planned", "version": 2},
        new_values={
            "status": "completed",
            "version": 3,
            "result": {"document_id": document.id, "document_number": document.document_number},
        },
    )
    w.db.commit()
    secrets = (document.title, document.document_number, "SECRET-DESCRIPTION", "SECRET-HEAT-4471")
    for persona in ("manager", "admin"):
        rows = [r for r in _audit(w, persona, resource_type="hank_intake_file") if r["resource_id"] == intake.id]
        assert len(rows) == 2, rows
        for row in rows:
            _assert_row_redacted(row, *secrets)
        assert _audit(w, persona, search="SECRET") == []
    full = [r for r in _audit(w, "cleared_admin", resource_type="hank_intake_file") if r["resource_id"] == intake.id]
    assert any("SECRET-HEAT-4471" in json.dumps(row) for row in full)


def test_hank_rows_about_a_visible_document_are_not_redacted(w: World):
    document = w.document(STANDARD)
    proposed = _propose_attach(w, "cleared_admin", document.id, w.work_order.id)
    assert proposed.status_code == 200, proposed.text
    rows = [r for r in _audit(w, "manager", resource_type="hank_task") if r["resource_id"] == proposed.json()["id"]]
    assert rows and rows[0]["resource_identifier"] == f"Attach {document.document_number}"


# --------------------------------------------------------------------------- 2. chat attach preview


def test_chat_attach_preview_answers_an_export_controlled_document_as_missing(w: World):
    hidden = w.document(EXPORT)
    shown = w.document(STANDARD)
    copilot = CopilotService(w.db, company_id=1, user=w.users["cleared_admin"])
    execution = copilot.execute_tool(
        "prepare_hank_task",
        {"kind": "attach_document", "input": {"document_id": hidden.id, "work_order_id": w.work_order.id}},
    )
    assert execution.is_error is True
    raw = json.dumps(execution.payload)
    assert "Document not found" in raw
    for secret in (hidden.document_number, hidden.title):
        assert secret not in raw
    missing = copilot.execute_tool(
        "prepare_hank_task",
        {"kind": "attach_document", "input": {"document_id": 999_999, "work_order_id": w.work_order.id}},
    )
    assert missing.is_error is True and missing.payload == execution.payload
    ok = copilot.execute_tool(
        "prepare_hank_task",
        {"kind": "attach_document", "input": {"document_id": shown.id, "work_order_id": w.work_order.id}},
    )
    assert ok.is_error is False, ok.payload
    # The person's own route still proposes the export-controlled attach (the person may open it).
    assert _propose_attach(w, "cleared_admin", hidden.id, w.work_order.id).status_code == 200


# --------------------------------------------------------------------------- 3. chain re-walk


def test_a_level_change_covers_a_revision_committed_while_it_waited(w: World, monkeypatch):
    rev_a = w.document(STANDARD)
    rev_b = w.document(STANDARD, previous_revision_id=rev_a.id, revision="B")
    calls = {"n": 0}
    inserted: Dict[str, int] = {}

    def interleaved(db, company_id, document_id):
        ids = revision_chain_ids(db, company_id, document_id)
        calls["n"] += 1
        if calls["n"] == 1:
            # A concurrent upload commits revision C after this walk and before the locks.
            rev_c = Document(
                document_number=f"RACE-{uuid4().hex[:8]}",
                title="Race revision C",
                revision="C",
                document_type=rev_b.document_type,
                status="released",
                access_level=STANDARD,
                previous_revision_id=rev_b.id,
                company_id=1,
            )
            db.add(rev_c)
            db.flush()
            inserted["id"] = rev_c.id
        return ids

    monkeypatch.setattr(document_access_service, "revision_chain_ids", interleaved)
    response = w.post(
        f"/documents/{rev_a.id}/access-level",
        "quality",
        json={"access_level": "restricted", "reason": "Program drawing"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert calls["n"] >= 2  # re-walked after the locks
    assert sorted(body["changed_document_ids"]) == sorted([rev_a.id, rev_b.id, inserted["id"]])
    assert body["chain_size"] == 3
    assert w.fresh(Document, inserted["id"]).access_level == "restricted"


def test_a_chain_that_never_settles_is_a_409_and_changes_nothing(w: World, monkeypatch):
    rev_a = w.document(STANDARD)

    def growing(db, company_id, document_id):
        tip = Document(
            document_number=f"GROW-{uuid4().hex[:8]}",
            title="Ever-growing",
            revision=uuid4().hex[:6],
            document_type=rev_a.document_type,
            status="released",
            access_level=STANDARD,
            company_id=1,
        )
        db.add(tip)
        db.flush()
        return [rev_a.id, tip.id] + [row.id for row in db.query(Document.id).filter(Document.title == "Ever-growing")]

    monkeypatch.setattr(document_access_service, "revision_chain_ids", growing)
    response = w.post(
        f"/documents/{rev_a.id}/access-level", "quality", json={"access_level": "restricted", "reason": "x"}
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == document_access_service.CHAIN_CHANGED_DETAIL
    assert w.fresh(Document, rev_a.id).access_level == STANDARD


# --------------------------------------------------------------------------- 4. attach raise rule


def test_attaching_to_an_export_controlled_customers_work_order_raises_the_document(w: World):
    work_order = _controlled_customer_work_order(w)
    document = w.document(STANDARD)
    attached = w.post(f"/documents/{document.id}/attach-work-order", "quality", json={"work_order_id": work_order.id})
    assert attached.status_code == 200, attached.text
    assert attached.json()["access_level"] == EXPORT  # echoed even though Quality may no longer open it
    assert w.fresh(Document, document.id).access_level == EXPORT
    row = (
        w.db.query(AuditLog).filter(AuditLog.action == "ACCESS_LEVEL_CHANGE", AuditLog.resource_id == document.id).one()
    )
    assert row.extra_data["reason"] == "work order customer is export-controlled"
    assert row.extra_data["chain_change"]["work_order_id"] == work_order.id
    assert (row.old_values, row.new_values) == ({"access_level": STANDARD}, {"access_level": EXPORT})
    # Quality may no longer open it; the access review neither lists it nor counts it as unlinked.
    assert w.get(f"/documents/{document.id}", "quality").status_code == 404
    review = w.get("/documents/access-review", "quality").json()
    assert document.id not in [r["document_id"] for r in review["documents"]]
    assert review["unlinked_document_count"] == 0


def test_attaching_to_an_ordinary_work_order_changes_no_level(w: World):
    document = w.document(STANDARD)
    attached = w.post(f"/documents/{document.id}/attach-work-order", "quality", json={"work_order_id": w.work_order.id})
    assert attached.status_code == 200, attached.text
    assert attached.json()["access_level"] == STANDARD
    assert w.db.query(AuditLog).filter(AuditLog.action == "ACCESS_LEVEL_CHANGE").count() == 0


def test_the_hank_attach_task_applies_the_same_raise(w: World):
    work_order = _controlled_customer_work_order(w)
    document = w.document(STANDARD)
    proposed = _propose_attach(w, "cleared_admin", document.id, work_order.id)
    assert proposed.status_code == 200, proposed.text
    task = proposed.json()
    executed = w.post(
        f"/hank/tasks/{task['id']}/execute",
        "cleared_admin",
        json={"expected_company_id": 1, "expected_version": task["version"]},
    )
    assert executed.status_code == 200, executed.text
    assert w.fresh(Document, document.id).access_level == EXPORT
    assert (
        w.db.query(AuditLog)
        .filter(AuditLog.action == "ACCESS_LEVEL_CHANGE", AuditLog.resource_id == document.id)
        .count()
        == 1
    )


# --------------------------------------------------------------------------- 5. chat intake files


def test_chat_treats_an_intake_file_filed_export_controlled_as_missing(w: World):
    hidden = w.document(EXPORT)
    shown = w.document(STANDARD)
    hidden_file = _intake_file(w, "cleared_admin", document=hidden)
    shown_file = _intake_file(w, "cleared_admin", document=shown)
    user = w.users["cleared_admin"]
    with pytest.raises(HTTPException) as caught:
        attachment_manifest(w.db, 1, user, [hidden_file.id])
    assert (caught.value.status_code, caught.value.detail) == (404, "Intake file not found")
    evidence = document_evidence(db=w.db, company_id=1, user=user, file_id=hidden_file.id, section="fields")
    assert evidence["is_error"] is True and "SECRET" not in json.dumps(evidence)
    manifest, _refs = attachment_manifest(w.db, 1, user, [shown_file.id])
    assert manifest[0]["file_id"] == shown_file.id


# --------------------------------------------------------------------------- 6. nest responses


def _nest_rows(payload: Any) -> List[Dict[str, Any]]:
    operations = payload.get("operations") if isinstance(payload, dict) and "operations" in payload else [payload]
    return [op["laser_nest"] for op in operations if op.get("laser_nest")]


def _assert_nest_hidden(payload: Any) -> None:
    rows = _nest_rows(payload)
    assert rows, payload
    for nest in rows:
        assert (nest["document_id"], nest["has_document"], nest["document_file_name"]) == (None, False, None), nest


def test_work_order_put_release_and_operation_put_hide_a_hidden_nest_pdf(w: World):
    document = w.document(EXPORT)
    w.nest(document_id=document.id)
    work_order = w.fresh(WorkOrder, w.work_order.id)
    put = w.put(f"/work-orders/{work_order.id}", "admin", json={"version": work_order.version, "notes": "re-saved"})
    assert put.status_code == 200, put.text
    _assert_nest_hidden(put.json())
    operation_put = w.put(
        f"/work-orders/operations/{w.operation.id}",
        "admin",
        json={"version": w.fresh(type(w.operation), w.operation.id).version, "name": "Laser cut"},
    )
    assert operation_put.status_code == 200, operation_put.text
    _assert_nest_hidden(operation_put.json())
    # The authorized Admin still sees it, and nothing was written.
    shown = w.put(
        f"/work-orders/{work_order.id}",
        "cleared_admin",
        json={"version": w.fresh(WorkOrder, work_order.id).version, "notes": "again"},
    )
    assert _nest_rows(shown.json())[0]["document_id"] == document.id
    assert w.fresh(type(w.operation), w.operation.id).laser_nest.document_id == document.id

    draft, op = make_wo_with_operation(
        w.db, work_center=w.work_center, wo_status=WorkOrderStatus.DRAFT, op_status=OperationStatus.PENDING
    )
    w.nest(document_id=document.id, operation=op)
    released = w.post(f"/work-orders/{draft.id}/release", "admin")
    assert released.status_code == 200, released.text
    _assert_nest_hidden(released.json())


# --------------------------------------------------------------------------- 7. audit search


def test_audit_search_filters_hidden_rows_before_paginating(w: World):
    document = w.document(EXPORT)
    # Hidden rows whose stored text contains the number, then a visible row that also does.
    audit = AuditService(w.db, w.users["cleared_admin"], company_id=1)
    for _ in range(3):
        audit.log_required("UPDATE", "document", resource_id=document.id, resource_identifier=document.document_number)
    audit.log_required("UPDATE", "part", resource_id=w.part.id, resource_identifier=f"{document.document_number}-PART")
    w.db.commit()
    # limit=1 at offset 0 is the visible row: no empty page reveals that hidden rows matched.
    first = _audit(w, "manager", search=document.document_number, limit=1, offset=0)
    assert [row["resource_type"] for row in first] == ["part"]
    assert _audit(w, "manager", search=document.document_number, limit=1, offset=1) == []
    assert len(_audit(w, "manager", search=document.document_number)) == 1
    # A search on what the reader is shown (the actor) still returns the hidden rows, redacted.
    actor = _audit(w, "manager", search=w.users["cleared_admin"].last_name, resource_type="document")
    assert actor and all(row["resource_identifier"] == REDACTED_IDENTIFIER for row in actor)
    # The authorized reader's search sees all four.
    assert len(_audit(w, "cleared_admin", search=document.document_number)) == 4
