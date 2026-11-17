"""``DELETE /documents/{id}`` refuses a document that QMS records still link.

``qms_standards.document_id`` and ``qms_clause_evidence.document_id`` are plain
foreign keys with no ``ON DELETE``. On Postgres an unguarded physical delete fails
at flush, after the document row lock and the global audit-chain lock are taken:
a 500, or a 40P01 against an evidence edit holding the referencing row. The tests
run on SQLite, which does not enforce those keys, so without the guard each refused
case here would answer 200 and orphan the QMS row. That is what they catch.
"""

import pytest
from sqlalchemy import Update, event

from app.api.endpoints import documents
from app.core.config import settings
from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.document import Document
from app.models.qms_standard import QMSClause, QMSClauseEvidence, QMSStandard
from app.models.user import User, UserRole
from app.services.audit_service import AuditService
from app.services.storage_service import LocalStorageBackend

pytestmark = [pytest.mark.api, pytest.mark.requires_db]


@pytest.fixture(autouse=True)
def local_files(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(documents, "get_storage", lambda: LocalStorageBackend())
    return tmp_path


def _upload(client, headers) -> int:
    response = client.post(
        "/api/v1/documents/upload",
        headers=headers,
        data={"title": "Quality manual", "document_type": "procedure"},
        files={"file": ("manual.pdf", b"synthetic-quality-manual", "application/pdf")},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _standard(db, *, company_id=1, name="AS9100D", document_id=None, is_deleted=False) -> QMSStandard:
    standard = QMSStandard(name=name, company_id=company_id, document_id=document_id, is_deleted=is_deleted)
    db.add(standard)
    db.flush()
    return standard


def _evidence(db, document_id, *, company_id=1, standard=None, clause_deleted=False, is_deleted=False):
    standard = standard or _standard(db, company_id=company_id)
    clause = QMSClause(
        standard_id=standard.id,
        clause_number="7.5.3",
        title="Control of documented information",
        company_id=company_id,
        is_deleted=clause_deleted,
    )
    db.add(clause)
    db.flush()
    evidence = QMSClauseEvidence(
        clause_id=clause.id,
        evidence_type="document",
        title="Quality manual",
        document_id=document_id,
        company_id=company_id,
        is_deleted=is_deleted,
    )
    db.add(evidence)
    db.commit()
    return evidence


def _delete(client, headers, document_id):
    return client.delete(f"/api/v1/documents/{document_id}", headers=headers)


def _assert_untouched(db, document_id, audit_before, local_files):
    db.rollback()
    assert db.get(Document, document_id) is not None
    assert db.query(AuditLog).count() == audit_before
    assert len(list(local_files.iterdir())) == 1


def test_document_linked_as_qms_evidence_is_refused_before_any_write(client, db_session, admin_headers, local_files):
    document_id = _upload(client, admin_headers)
    _evidence(db_session, document_id)
    audit_before = db_session.query(AuditLog).count()

    response = _delete(client, admin_headers, document_id)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "linked as QMS evidence on AS9100D clause 7.5.3" in detail
    assert "Unlink it first" in detail and "document_id to null" in detail
    _assert_untouched(db_session, document_id, audit_before, local_files)


def test_document_linked_to_qms_standard_is_refused_before_any_write(client, db_session, admin_headers, local_files):
    document_id = _upload(client, admin_headers)
    _standard(db_session, name="ISO 9001:2015", document_id=document_id)
    db_session.commit()
    audit_before = db_session.query(AuditLog).count()

    response = _delete(client, admin_headers, document_id)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "linked to QMS standard ISO 9001:2015" in detail
    assert "Unlink it first" in detail
    _assert_untouched(db_session, document_id, audit_before, local_files)


def test_refusal_counts_the_other_live_links(client, db_session, admin_headers, local_files):
    document_id = _upload(client, admin_headers)
    standard = _standard(db_session, document_id=document_id)
    _evidence(db_session, document_id, standard=standard)
    _evidence(db_session, document_id, standard=standard)

    response = _delete(client, admin_headers, document_id)

    assert response.status_code == 409
    assert "linked to QMS standard AS9100D and 2 other QMS links." in response.json()["detail"]


@pytest.mark.parametrize("removed", ["evidence", "clause", "standard", "standard_link"])
def test_removed_qms_links_keep_the_document(client, db_session, admin_headers, local_files, removed):
    """A soft-deleted QMS row still holds the key and still records what was assessed.

    Evidence under a removed clause or standard counts as removed even with its own
    flag clear, so the message does not send the user to unlink a row no screen shows.
    """
    document_id = _upload(client, admin_headers)
    if removed == "standard_link":
        _standard(db_session, document_id=document_id, is_deleted=True)
        db_session.commit()
    else:
        _evidence(
            db_session,
            document_id,
            standard=_standard(db_session, is_deleted=removed == "standard"),
            clause_deleted=removed == "clause",
            is_deleted=removed == "evidence",
        )
    audit_before = db_session.query(AuditLog).count()

    response = _delete(client, admin_headers, document_id)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "removed QMS standard, clause or evidence link" in detail
    assert "Unlink it first" not in detail
    _assert_untouched(db_session, document_id, audit_before, local_files)


def test_live_link_is_named_ahead_of_removed_ones(client, db_session, admin_headers, local_files):
    document_id = _upload(client, admin_headers)
    _evidence(db_session, document_id, is_deleted=True)
    _standard(db_session, name="Live manual", document_id=document_id)
    db_session.commit()

    response = _delete(client, admin_headers, document_id)

    assert response.status_code == 409
    assert "linked to QMS standard Live manual." in response.json()["detail"]


def test_unlinking_the_evidence_releases_the_document(client, db_session, admin_headers, local_files):
    document_id = _upload(client, admin_headers)
    evidence = _evidence(db_session, document_id)
    assert _delete(client, admin_headers, document_id).status_code == 409

    unlinked = client.put(
        f"/api/v1/qms-standards/evidence/{evidence.id}", headers=admin_headers, json={"document_id": None}
    )
    assert unlinked.status_code == 200, unlinked.text

    response = _delete(client, admin_headers, document_id)
    assert response.status_code == 200, response.text
    db_session.rollback()
    assert db_session.get(Document, document_id) is None


def test_unreferenced_document_still_deletes_with_audit(client, db_session, admin_headers, local_files):
    document_id = _upload(client, admin_headers)
    # A QMS link to a DIFFERENT document of this company is not a reason to refuse.
    other_id = _upload(client, admin_headers)
    _evidence(db_session, other_id)
    _standard(db_session, document_id=other_id)
    db_session.commit()

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


def test_another_companys_link_is_not_disclosed(client, db_session, admin_headers, local_files):
    """The check reads only the active company's QMS rows.

    A cross-company link can exist only through the QMS routes' old unchecked
    ``document_id`` writes. Naming it would disclose the other tenant's standard,
    so the refusal never mentions it. (On Postgres the foreign key still stops
    that delete; SQLite does not enforce it, so here the delete goes through.)
    """
    db_session.add(Company(id=2, name="Other tenant", slug="other-tenant", is_active=True))
    db_session.commit()
    document_id = _upload(client, admin_headers)
    _standard(db_session, company_id=2, name="Other tenant private standard", document_id=document_id)
    _evidence(db_session, document_id, company_id=2)

    response = _delete(client, admin_headers, document_id)

    assert response.status_code != 409
    assert "Other tenant private standard" not in response.text
    assert "QMS" not in response.text


def test_quality_role_still_cannot_delete_a_linked_document(client, db_session, admin_headers, local_files):
    """The role gate answers first: a caller who may not delete learns nothing about QMS links."""
    document_id = _upload(client, admin_headers)
    _evidence(db_session, document_id)
    quality = User(
        email="qms-delete-quality@werco.test",
        employee_id="QMS-DEL-Q1",
        first_name="Quality",
        last_name="Reviewer",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",
        role=UserRole.QUALITY,
        is_active=True,
        company_id=1,
    )
    db_session.add(quality)
    db_session.commit()
    headers = {"Authorization": f"Bearer {create_access_token(quality.id, company_id=1)}"}

    response = _delete(client, headers, document_id)

    assert response.status_code == 403
    assert "QMS" not in response.text


@pytest.mark.parametrize("target", ["standard", "evidence"])
def test_link_write_reaches_the_document_before_the_audit_chain_lock(
    client, db_session, admin_headers, local_files, monkeypatch, target
):
    """A new ``document_id`` is flushed before the audit row, so its key-share lock on the
    document row comes before the global audit-chain lock: the order the delete takes them in.

    ``AuditService.log`` takes the chain lock with a raw statement that does not autoflush, so
    an unflushed link would reach the database only after the chain lock, and a link racing a
    delete of the same document could deadlock against it on Postgres.
    """
    document_id = _upload(client, admin_headers)
    standard = _standard(db_session)
    evidence = _evidence(db_session, None, standard=standard)
    table = "qms_standards" if target == "standard" else "qms_clause_evidence"
    url = (
        f"/api/v1/qms-standards/{standard.id}"
        if target == "standard"
        else f"/api/v1/qms-standards/evidence/{evidence.id}"
    )

    events = []

    def before_execute(conn, clauseelement, multiparams, params, execution_options):  # noqa: ARG001
        if isinstance(clauseelement, Update) and clauseelement.table.name == table:
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
        response = client.put(url, headers=admin_headers, json={"document_id": document_id})
    finally:
        event.remove(engine, "before_execute", before_execute)

    assert response.status_code == 200, response.text
    assert "link_update" in events and "chain_lock" in events, events
    assert events.index("link_update") < events.index("chain_lock"), events
    assert "link_update" not in events[events.index("chain_lock") :], events
