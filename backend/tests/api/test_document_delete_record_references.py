"""``DELETE /documents/{id}`` refuses a document that another record still references.

Every column below is a plain foreign key to ``documents.id`` with no ``ON DELETE``.
On Postgres an unguarded physical delete fails at flush, after the document row lock
and the global audit-chain lock are taken: a 500 with nothing written and no reason.
The tests run on SQLite, which does not enforce those keys, so without the guard each
refused case here answers 200 and orphans the referencing row. That is what they catch.

- ``shipments.label_document_id`` / ``bol_document_id``: retained, voided or removed.
- ``po_receipts.label_document_id``: retained, voided included.
- ``operation_step_records.attachment_document_id``: retained, superseded included.
- ``document_files.document_id``: retained.
- ``laser_nests.document_id``: a live nest sends the user to Detach; a removed nest
  keeps its PDF so a restore brings it back.
- ``engineering_change_orders.affected_documents`` (a JSON id list, not a foreign key, so
  even Postgres would let the delete through and the ECO would silently lose the item):
  an ECO not yet approved sends the user to edit it; from approval on it is the record.

The QMS links are covered by ``test_document_delete_qms_links.py``.
"""

from datetime import date, datetime

import pytest
from sqlalchemy import Select, Update, event
from sqlalchemy.dialects import postgresql

from app.api.endpoints import documents
from app.core.config import settings
from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.document import Document
from app.models.engineering_change import ECOStatus, ECOType, EngineeringChangeOrder
from app.models.governance import DocumentFile
from app.models.laser_nest import LaserNest, LaserNestPackage
from app.models.part import Part
from app.models.process_sheet import OperationStepRecord, ProcessSheet, WOOperationStep
from app.models.purchasing import POReceipt, POStatus, PurchaseOrder, PurchaseOrderLine, ReceiptStatus, Vendor
from app.models.qms_standard import QMSStandard
from app.models.shipping import Shipment, ShipmentStatus
from app.models.user import UserRole
from app.models.work_order import OperationStatus
from app.services.audit_service import AuditService
from app.services.storage_service import LocalStorageBackend
from tests.api.kiosk_test_helpers import _next, ensure_company, make_user, make_wo_with_operation, make_work_center

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

KINDS = ["shipment_label", "shipment_bol", "receipt_label", "step_evidence", "governed_file", "laser_nest"]


@pytest.fixture(autouse=True)
def local_files(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(documents, "get_storage", lambda: LocalStorageBackend())
    return tmp_path


def _upload(client, headers) -> int:
    response = client.post(
        "/api/v1/documents/upload",
        headers=headers,
        data={"title": "Referenced file", "document_type": "quality_record"},
        files={"file": ("record.pdf", b"synthetic-referenced-record", "application/pdf")},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _delete(client, headers, document_id):
    return client.delete(f"/api/v1/documents/{document_id}", headers=headers)


def _assert_untouched(db, document_id, audit_before, local_files):
    db.rollback()
    assert db.get(Document, document_id) is not None
    assert db.query(AuditLog).count() == audit_before
    assert len(list(local_files.iterdir())) == 1


def _work_order(db, company_id, op_status=OperationStatus.READY):
    return make_wo_with_operation(
        db, company_id=company_id, work_center=make_work_center(db, company_id=company_id), op_status=op_status
    )


def _shipment(db, document_id, *, company_id, field, removed):
    work_order, _ = _work_order(db, company_id)
    shipment = Shipment(
        shipment_number=f"SHP-DOCREF-{_next():05d}",
        work_order_id=work_order.id,
        status=ShipmentStatus.SHIPPED,
        company_id=company_id,
        **{field: document_id},
    )
    if removed:
        shipment.voided_at = datetime.utcnow()
        shipment.is_deleted = True
        shipment.deleted_at = datetime.utcnow()
    db.add(shipment)
    db.commit()
    return shipment


def _receipt(db, document_id, *, company_id, removed):
    ensure_company(db, company_id)
    n = _next()
    vendor = Vendor(code=f"VDR{n:05d}", name=f"Vendor {n}", is_active=True, company_id=company_id)
    part = Part(
        part_number=f"DR-{n:05d}",
        name=f"Part {n}",
        part_type="purchased",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add_all([vendor, part])
    db.flush()
    po = PurchaseOrder(
        po_number=f"PO-DR-{n:05d}",
        vendor_id=vendor.id,
        status=POStatus.PARTIAL,
        order_date=date.today(),
        company_id=company_id,
    )
    db.add(po)
    db.flush()
    line = PurchaseOrderLine(
        purchase_order_id=po.id,
        line_number=1,
        part_id=part.id,
        quantity_ordered=10,
        quantity_received=5,
        unit_price=5.0,
        company_id=company_id,
    )
    db.add(line)
    db.flush()
    receiver = make_user(db, company_id=company_id, role=UserRole.MANAGER)
    receipt = POReceipt(
        receipt_number=f"RCV-DR-{n:05d}",
        po_line_id=line.id,
        quantity_received=5,
        lot_number=f"LOT-DR-{n:05d}",
        status=ReceiptStatus.ACCEPTED,
        received_by=receiver.id,
        received_at=datetime.utcnow(),
        label_document_id=document_id,
        company_id=company_id,
        is_deleted=removed,
    )
    db.add(receipt)
    db.commit()
    return receipt


def _step_record(db, document_id, *, company_id, removed):
    work_order, operation = _work_order(db, company_id)
    recorder = make_user(db, company_id=company_id, role=UserRole.OPERATOR)
    sheet = ProcessSheet(company_id=company_id, sheet_number=f"PS-DR-{_next():05d}", title="Weld photo plan")
    db.add(sheet)
    db.flush()
    step = WOOperationStep(
        company_id=company_id,
        work_order_operation_id=operation.id,
        source_sheet_id=sheet.id,
        source_sheet_revision="A",
        sequence=10,
        label="Photo of weld bead",
        step_type="photo",
        config={},
    )
    db.add(step)
    db.flush()
    record = OperationStepRecord(
        company_id=company_id,
        wo_operation_step_id=step.id,
        work_order_operation_id=operation.id,
        recorded_by=recorder.id,
        attachment_document_id=document_id,
    )
    db.add(record)
    db.flush()
    if removed:
        # Superseded: a correcting record replaced it. The original keeps its evidence.
        replacement = OperationStepRecord(
            company_id=company_id,
            wo_operation_step_id=step.id,
            work_order_operation_id=operation.id,
            recorded_by=recorder.id,
            attachment_document_id=None,
        )
        db.add(replacement)
        db.flush()
        record.superseded_by_id = replacement.id
        record.supersede_reason = "Wrong photo"
    db.commit()
    return work_order


def _governed_file(db, document_id, *, company_id):
    ensure_company(db, company_id)
    db.add(
        DocumentFile(
            company_id=company_id,
            parent_record_type="documents",
            parent_record_id=document_id,
            document_id=document_id,
            storage_key=f"governed/{_next():05d}.pdf",
            original_file_name="record.pdf",
            legal_hold_active=True,
        )
    )
    db.commit()


def _laser_nest(db, document_id, *, company_id, removed=False, operation=None):
    if operation is None:
        _, operation = _work_order(db, company_id)
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
        nest_name=f"Nest {n}",
        cnc_number=f"CNC-DR-{n:04d}",
        document_id=document_id,
        planned_runs=2,
        is_deleted=removed,
    )
    if removed:
        nest.deleted_at = datetime.utcnow()
    db.add(nest)
    db.commit()
    return nest


def _refer(db, kind, document_id, *, company_id=1, removed=False):
    """Create the ``kind`` row that references ``document_id``; return the words its refusal names."""
    if kind in ("shipment_label", "shipment_bol"):
        field = "label_document_id" if kind == "shipment_label" else "bol_document_id"
        shipment = _shipment(db, document_id, company_id=company_id, field=field, removed=removed)
        artifact = "carrier label" if kind == "shipment_label" else "bill of lading"
        return f"the {artifact} for shipment {shipment.shipment_number}"
    if kind == "receipt_label":
        receipt = _receipt(db, document_id, company_id=company_id, removed=removed)
        return f"the receiving label for receipt {receipt.receipt_number}"
    if kind == "step_evidence":
        work_order = _step_record(db, document_id, company_id=company_id, removed=removed)
        return f"evidence captured at a process-sheet step on work order {work_order.work_order_number}"
    if kind == "governed_file":
        _governed_file(db, document_id, company_id=company_id)
        return "governed file record"
    nest = _laser_nest(db, document_id, company_id=company_id, removed=removed)
    return "a removed laser nest" if removed else f"laser nest {nest.cnc_number}"


@pytest.mark.parametrize("kind", KINDS)
def test_referenced_document_is_refused_before_any_write(client, db_session, admin_headers, local_files, kind):
    document_id = _upload(client, admin_headers)
    named = _refer(db_session, kind, document_id)
    audit_before = db_session.query(AuditLog).count()

    response = _delete(client, admin_headers, document_id)

    assert response.status_code == 409, response.text
    assert named in response.json()["detail"]
    _assert_untouched(db_session, document_id, audit_before, local_files)


@pytest.mark.parametrize("kind", ["shipment_label", "shipment_bol", "receipt_label", "step_evidence", "laser_nest"])
def test_removed_referrer_still_keeps_the_document(client, db_session, admin_headers, local_files, kind):
    """A voided or removed shipment, a voided receipt, a superseded step record and a
    removed laser nest each still hold the key and still need the document."""
    document_id = _upload(client, admin_headers)
    named = _refer(db_session, kind, document_id, removed=True)
    audit_before = db_session.query(AuditLog).count()

    response = _delete(client, admin_headers, document_id)

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert named in detail
    assert "Detach it" not in detail
    _assert_untouched(db_session, document_id, audit_before, local_files)


def test_live_laser_nest_points_at_detach_and_counts_the_others(client, db_session, admin_headers, local_files):
    """A duplicated work order copies the nest's document reference, so one PDF can sit on several nests."""
    document_id = _upload(client, admin_headers)
    work_order, operation = _work_order(db_session, 1)
    first = _laser_nest(db_session, document_id, company_id=1, operation=operation)
    _laser_nest(db_session, document_id, company_id=1)
    _laser_nest(db_session, document_id, company_id=1)

    response = _delete(client, admin_headers, document_id)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert (
        f"the PDF attached to laser nest {first.cnc_number} on work order {work_order.work_order_number} "
        "and 2 other laser nests." in detail
    )
    assert "Detach it from the nest first" in detail


@pytest.mark.parametrize("worked_by", ["status", "actual_start"])
def test_a_nest_with_recorded_work_keeps_its_pdf(client, db_session, admin_headers, local_files, worked_by):
    """Once its operation has recorded work, the PDF is the layout the sheet was cut from:
    the refusal does not send the user to Detach it."""
    document_id = _upload(client, admin_headers)
    status = OperationStatus.COMPLETE if worked_by == "status" else OperationStatus.PENDING
    work_order, operation = _work_order(db_session, 1, op_status=status)
    if worked_by == "actual_start":
        operation.actual_start = datetime.utcnow()
        db_session.commit()
    nest = _laser_nest(db_session, document_id, company_id=1, operation=operation)
    _laser_nest(db_session, document_id, company_id=1)  # an un-worked nest sharing the PDF
    audit_before = db_session.query(AuditLog).count()

    response = _delete(client, admin_headers, document_id)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert f"the PDF of laser nest {nest.cnc_number} on work order {work_order.work_order_number}" in detail
    assert "has recorded work" in detail
    assert "Detach" not in detail
    _assert_untouched(db_session, document_id, audit_before, local_files)


def test_a_removed_nest_is_reported_before_a_live_nest_to_detach(client, db_session, admin_headers, local_files):
    """Detaching the live nest would not free a PDF a removed nest keeps, so the user hears that first."""
    document_id = _upload(client, admin_headers)
    _laser_nest(db_session, document_id, company_id=1)
    _laser_nest(db_session, document_id, company_id=1, removed=True)

    response = _delete(client, admin_headers, document_id)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "a removed laser nest" in detail
    assert "Detach" not in detail


def test_a_removed_nest_is_reported_before_a_live_qms_link_to_unlink(client, db_session, admin_headers, local_files):
    document_id = _upload(client, admin_headers)
    db_session.add(QMSStandard(name="AS9100D", company_id=1, document_id=document_id))
    db_session.commit()
    _laser_nest(db_session, document_id, company_id=1, removed=True)

    response = _delete(client, admin_headers, document_id)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "a removed laser nest" in detail
    assert "Unlink it first" not in detail


def test_a_removed_qms_link_is_reported_before_a_live_nest_to_detach(client, db_session, admin_headers, local_files):
    document_id = _upload(client, admin_headers)
    db_session.add(QMSStandard(name="Retired manual", company_id=1, document_id=document_id, is_deleted=True))
    db_session.commit()
    _laser_nest(db_session, document_id, company_id=1)

    response = _delete(client, admin_headers, document_id)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "removed QMS standard, clause or evidence link" in detail
    assert "Detach" not in detail


def test_qms_and_nest_instructions_are_given_together(client, db_session, admin_headers, local_files):
    """When every blocker can be released, one refusal names them all, so one round of unlinking frees it."""
    document_id = _upload(client, admin_headers)
    db_session.add(QMSStandard(name="AS9100D", company_id=1, document_id=document_id))
    db_session.commit()
    nest = _laser_nest(db_session, document_id, company_id=1)

    response = _delete(client, admin_headers, document_id)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "linked to QMS standard AS9100D" in detail and "Unlink it first" in detail
    assert f"laser nest {nest.cnc_number}" in detail and "Detach it from the nest first" in detail


def test_detaching_the_nest_pdf_releases_the_document(client, db_session, admin_headers, local_files):
    document_id = _upload(client, admin_headers)
    nest = _laser_nest(db_session, document_id, company_id=1)
    assert _delete(client, admin_headers, document_id).status_code == 409

    detached = client.delete(f"/api/v1/laser-nests/{nest.id}/document", headers=admin_headers)
    assert detached.status_code == 200, detached.text

    response = _delete(client, admin_headers, document_id)
    assert response.status_code == 200, response.text
    db_session.rollback()
    assert db_session.get(Document, document_id) is None
    assert db_session.get(LaserNest, nest.id).document_id is None


def test_a_retained_record_is_named_ahead_of_a_nest_to_detach(client, db_session, admin_headers, local_files):
    """Detaching the nest would not free a document a shipment keeps, so the shipment is what the user hears."""
    document_id = _upload(client, admin_headers)
    _laser_nest(db_session, document_id, company_id=1)
    named = _refer(db_session, "shipment_label", document_id)

    response = _delete(client, admin_headers, document_id)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert named in detail
    assert "laser nest" not in detail


@pytest.mark.parametrize("kind", KINDS)
def test_a_reference_to_another_document_does_not_block(client, db_session, admin_headers, local_files, kind):
    document_id = _upload(client, admin_headers)
    other_id = _upload(client, admin_headers)
    _refer(db_session, kind, other_id)

    response = _delete(client, admin_headers, document_id)

    assert response.status_code == 200, response.text
    db_session.rollback()
    assert db_session.get(Document, document_id) is None
    assert db_session.get(Document, other_id) is not None
    entry = (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type == "document", AuditLog.resource_id == document_id, AuditLog.action == "DELETE")
        .one()
    )
    assert entry.company_id == 1


@pytest.mark.parametrize("kind", KINDS)
def test_another_companys_reference_is_not_disclosed(client, db_session, admin_headers, local_files, kind):
    """The checks read only the active company's rows.

    No writer of these columns accepts another company's document, so such a row can
    only come from outside the app. Naming it would disclose the other tenant's record,
    so the refusal never does. (On Postgres the foreign key still stops that delete;
    SQLite does not enforce it, so here the delete goes through.)
    """
    document_id = _upload(client, admin_headers)
    named = _refer(db_session, kind, document_id, company_id=2)

    response = _delete(client, admin_headers, document_id)

    assert response.status_code == 200, response.text
    assert named not in response.text


def test_supervisor_cannot_delete_a_referenced_document(client, db_session, admin_headers, local_files):
    """The role gate answers first: a caller who may not delete learns nothing about the reference."""
    document_id = _upload(client, admin_headers)
    named = _refer(db_session, "shipment_label", document_id)
    supervisor = make_user(db_session, company_id=1, role=UserRole.SUPERVISOR)
    headers = {"Authorization": f"Bearer {create_access_token(supervisor.id, company_id=1)}"}

    response = _delete(client, headers, document_id)

    assert response.status_code == 403
    assert named not in response.text


def test_nest_attach_reaches_the_document_before_the_audit_chain_lock(
    client, db_session, admin_headers, local_files, monkeypatch
):
    """The new ``document_id`` is flushed before the audit row, so its key-share lock on the
    document row comes before the global audit-chain lock: the order the delete takes them in.

    ``AuditService.log`` takes the chain lock with a raw statement that does not autoflush, so
    an unflushed link would reach the database only after the chain lock, and an attach racing
    a delete of the same document could deadlock against it on Postgres.
    """
    document_id = _upload(client, admin_headers)
    nest = _laser_nest(db_session, None, company_id=1)
    events = []

    def before_execute(conn, clauseelement, multiparams, params, execution_options):  # noqa: ARG001
        if isinstance(clauseelement, Update) and clauseelement.table.name == "laser_nests":
            events.append("link_update")

    monkeypatch.setattr(settings, "AUDIT_HASH_CHAIN_ENABLED", True)
    original_chain_lock = AuditService._acquire_chain_lock

    def spy_chain_lock(self):
        events.append("chain_lock")
        original_chain_lock(self)

    monkeypatch.setattr(AuditService, "_acquire_chain_lock", spy_chain_lock)
    engine = db_session.get_bind()
    event.listen(engine, "before_execute", before_execute)
    try:
        response = client.post(
            f"/api/v1/laser-nests/{nest.id}/attach-document",
            headers=admin_headers,
            json={"document_id": document_id},
        )
    finally:
        event.remove(engine, "before_execute", before_execute)

    assert response.status_code == 200, response.text
    assert "link_update" in events and "chain_lock" in events, events
    assert events.index("link_update") < events.index("chain_lock"), events
    assert "link_update" not in events[events.index("chain_lock") :], events


# --------------------------------------------------------------------------- ECOs

ECO_EDITABLE = ["draft", "submitted"]
ECO_RECORDED = ["under_review", "approved", "in_implementation", "completed", "rejected", "cancelled"]


def _eco(db, affected_documents, *, company_id=1, status="draft"):
    requester = make_user(db, company_id=company_id, role=UserRole.MANAGER)
    eco = EngineeringChangeOrder(
        eco_number=f"ECO-DOCREF-{_next():05d}",
        title="Revise weld procedure",
        description="Document change",
        eco_type=ECOType.DOCUMENTATION,
        status=ECOStatus(status),
        reason_for_change="Customer requirement",
        requested_by=requester.id,
        affected_documents=affected_documents,
        company_id=company_id,
    )
    db.add(eco)
    db.commit()
    return eco


@pytest.mark.parametrize("status", ECO_EDITABLE)
def test_an_undecided_eco_sends_the_user_to_edit_it(client, db_session, admin_headers, local_files, status):
    document_id = _upload(client, admin_headers)
    eco = _eco(db_session, f"[{document_id}]", status=status)
    audit_before = db_session.query(AuditLog).count()

    response = _delete(client, admin_headers, document_id)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert (
        f"listed as affected by engineering change {eco.eco_number} (PUT /eco/eco/{eco.id}). "
        "Take it out of that change's affected_documents first" in detail
    )
    _assert_untouched(db_session, document_id, audit_before, local_files)


def test_every_undecided_eco_is_named_with_its_edit_path(client, db_session, admin_headers, local_files):
    """The ECO list can't be searched by document, so "and N others" would leave the user stuck."""
    document_id = _upload(client, admin_headers)
    ecos = [_eco(db_session, f"[{document_id}]", status=status) for status in ("draft", "submitted", "draft")]

    response = _delete(client, admin_headers, document_id)

    assert response.status_code == 409
    detail = response.json()["detail"]
    first, second, third = (f"{eco.eco_number} (PUT /eco/eco/{eco.id})" for eco in ecos)
    assert f"engineering changes {first}, {second} and {third}." in detail
    assert "each of those changes' affected_documents" in detail


@pytest.mark.parametrize("status", ECO_RECORDED)
def test_an_approved_or_closed_eco_keeps_the_document(client, db_session, admin_headers, local_files, status):
    """Once an approver has signed (under review) or the change is approved, the affected list is
    the record of what was authorized, and a rejected or cancelled change records what was
    decided, so the refusal does not suggest editing it."""
    document_id = _upload(client, admin_headers)
    _eco(db_session, f"[{document_id}]", status="draft")
    eco = _eco(db_session, f"[{document_id}]", status=status)
    audit_before = db_session.query(AuditLog).count()

    response = _delete(client, admin_headers, document_id)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert f"engineering change {eco.eco_number} ({status.replace('_', ' ')})" in detail
    assert "keeps the record of what the change covered" in detail
    assert "Take it out" not in detail
    _assert_untouched(db_session, document_id, audit_before, local_files)


def test_taking_the_document_off_the_eco_releases_it(client, db_session, admin_headers, local_files):
    document_id = _upload(client, admin_headers)
    eco = _eco(db_session, f"[{document_id}]")
    assert _delete(client, admin_headers, document_id).status_code == 409

    edited = client.put(f"/api/v1/eco/eco/{eco.id}", headers=admin_headers, json={"affected_documents": []})
    assert edited.status_code == 200, edited.text

    response = _delete(client, admin_headers, document_id)
    assert response.status_code == 200, response.text
    db_session.rollback()
    assert db_session.get(Document, document_id) is None


def test_an_eco_naming_a_different_id_with_the_same_digits_does_not_block(
    client, db_session, admin_headers, local_files
):
    """The text column is only a prefilter: ids 1 and 12 are different documents."""
    document_id = _upload(client, admin_headers)
    _eco(db_session, f"[{document_id}1, 1{document_id}]")
    _eco(db_session, f'["{document_id}0"]')

    response = _delete(client, admin_headers, document_id)

    assert response.status_code == 200, response.text


def test_an_unreadable_affected_documents_value_does_not_block(client, db_session, admin_headers, local_files):
    """``GET /eco/eco/affected-items`` skips a value it cannot parse; one bad row must not block every delete."""
    document_id = _upload(client, admin_headers)
    _eco(db_session, f"not json {document_id}")
    _eco(db_session, f'{{"id": {document_id}}}')

    response = _delete(client, admin_headers, document_id)

    assert response.status_code == 200, response.text


def test_a_string_id_in_the_list_still_counts(client, db_session, admin_headers, local_files):
    document_id = _upload(client, admin_headers)
    eco = _eco(db_session, f'["{document_id}"]')

    response = _delete(client, admin_headers, document_id)

    assert response.status_code == 409
    assert eco.eco_number in response.json()["detail"]


def test_an_approved_eco_is_reported_before_a_nest_to_detach(client, db_session, admin_headers, local_files):
    document_id = _upload(client, admin_headers)
    _laser_nest(db_session, document_id, company_id=1)
    eco = _eco(db_session, f"[{document_id}]", status="approved")

    response = _delete(client, admin_headers, document_id)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert eco.eco_number in detail
    assert "Detach" not in detail


def test_eco_qms_and_nest_instructions_are_given_together(client, db_session, admin_headers, local_files):
    document_id = _upload(client, admin_headers)
    db_session.add(QMSStandard(name="AS9100D", company_id=1, document_id=document_id))
    db_session.commit()
    nest = _laser_nest(db_session, document_id, company_id=1)
    eco = _eco(db_session, f"[{document_id}]", status="submitted")

    response = _delete(client, admin_headers, document_id)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "Unlink it first" in detail
    assert f"laser nest {nest.cnc_number}" in detail
    assert f"engineering change {eco.eco_number}" in detail


def test_another_companys_eco_is_not_read(client, db_session, admin_headers, local_files):
    """ECO validation accepts only the company's own document ids, so such a list can only come from
    outside the app. It is not named, and since there is no foreign key the delete goes through."""
    ensure_company(db_session, 2)
    document_id = _upload(client, admin_headers)
    eco = _eco(db_session, f"[{document_id}]", company_id=2, status="approved")

    response = _delete(client, admin_headers, document_id)

    assert response.status_code == 200, response.text
    assert eco.eco_number not in response.text


def test_eco_update_without_affected_documents_locks_no_document(client, db_session, admin_headers, local_files):
    """Only a write that names documents locks them; a title edit takes no document lock."""
    document_id = _upload(client, admin_headers)
    eco = _eco(db_session, f"[{document_id}]")
    locked = []

    def before_execute(conn, clauseelement, multiparams, params, execution_options):  # noqa: ARG001
        if isinstance(clauseelement, Select) and "FOR KEY SHARE" in str(
            clauseelement.compile(dialect=postgresql.dialect())
        ):
            locked.append(clauseelement)

    engine = db_session.get_bind()
    event.listen(engine, "before_execute", before_execute)
    try:
        response = client.put(f"/api/v1/eco/eco/{eco.id}", headers=admin_headers, json={"title": "Retitled"})
    finally:
        event.remove(engine, "before_execute", before_execute)

    assert response.status_code == 200, response.text
    assert locked == []
    db_session.rollback()
    assert db_session.get(EngineeringChangeOrder, eco.id).affected_documents == f"[{document_id}]"


@pytest.mark.parametrize("verb", ["create", "update"])
def test_eco_write_locks_the_named_documents_before_the_audit_chain_lock(
    client, db_session, admin_headers, local_files, monkeypatch, verb
):
    """With no foreign key, ECO create / update take the lock one would: FOR KEY SHARE on each
    named document, before the audit row takes the global chain lock, the order the delete takes
    them in. A concurrent delete is then either seen by the delete's ECO read or makes this
    write's validation fail. (SQLite ignores the clause; the statement is compiled for Postgres.)"""
    document_id = _upload(client, admin_headers)
    eco = _eco(db_session, None)
    events = []

    def before_execute(conn, clauseelement, multiparams, params, execution_options):  # noqa: ARG001
        if isinstance(clauseelement, Select) and "documents" in {
            getattr(table, "name", None) for table in clauseelement.get_final_froms()
        }:
            compiled = str(clauseelement.compile(dialect=postgresql.dialect()))
            if "FOR KEY SHARE" in compiled:
                events.append("document_key_share")

    monkeypatch.setattr(settings, "AUDIT_HASH_CHAIN_ENABLED", True)
    original_chain_lock = AuditService._acquire_chain_lock

    def spy_chain_lock(self):
        events.append("chain_lock")
        original_chain_lock(self)

    monkeypatch.setattr(AuditService, "_acquire_chain_lock", spy_chain_lock)
    engine = db_session.get_bind()
    event.listen(engine, "before_execute", before_execute)
    try:
        if verb == "create":
            response = client.post(
                "/api/v1/eco/eco/",
                headers=admin_headers,
                json={
                    "title": "Revise weld procedure",
                    "description": "Document change",
                    "eco_type": "documentation",
                    "reason_for_change": "Customer requirement",
                    "affected_documents": [document_id],
                },
            )
        else:
            response = client.put(
                f"/api/v1/eco/eco/{eco.id}",
                headers=admin_headers,
                json={"affected_documents": [document_id]},
            )
    finally:
        event.remove(engine, "before_execute", before_execute)

    assert response.status_code == 200, response.text
    assert "document_key_share" in events and "chain_lock" in events, events
    assert events.index("document_key_share") < events.index("chain_lock"), events
