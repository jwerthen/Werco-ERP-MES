"""Manual document release requires a writer and durable audit evidence."""

from pathlib import Path

import pytest

from app.api.endpoints import documents
from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.document import Document
from app.models.part import Part, PartType
from app.models.user import UserRole
from app.services.audit_service import AuditService
from app.services.storage_service import LocalStorageBackend

pytestmark = [pytest.mark.api, pytest.mark.requires_db]


@pytest.fixture(autouse=True)
def local_files(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(documents, "get_storage", lambda: LocalStorageBackend())
    return tmp_path


def upload(client, headers, **values):
    return client.post(
        "/api/v1/documents/upload",
        headers=headers,
        data={"title": "Controlled instruction", "document_type": "work_instruction", **values},
        files={"file": ("instruction.pdf", b"synthetic-controlled-evidence", "application/pdf")},
    )


@pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.OPERATOR, UserRole.SUPERVISOR, UserRole.SHIPPING])
def test_non_releasers_cannot_publish_documents(client, db_session, test_user, local_files, role):
    test_user.role = role
    db_session.commit()
    headers = {"Authorization": f"Bearer {create_access_token(test_user.id, company_id=1)}"}
    before = db_session.query(AuditLog).count()
    response = upload(client, headers)
    assert response.status_code == 403
    assert db_session.query(Document).count() == 0
    assert db_session.query(AuditLog).count() == before
    assert list(local_files.iterdir()) == []


def test_release_has_actor_time_and_committed_audit(client, db_session, admin_user, admin_headers):
    response = upload(client, admin_headers)
    assert response.status_code == 200
    document_id = response.json()["id"]
    db_session.rollback()
    document = db_session.get(Document, document_id)
    assert document.released_by == admin_user.id
    assert document.released_at is not None
    entry = (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type == "document", AuditLog.resource_id == document_id)
        .one()
    )
    assert entry.company_id == 1 and entry.user_id == admin_user.id
    assert entry.new_values["status"] == "released"


@pytest.mark.parametrize('role', [UserRole.QUALITY, UserRole.MANAGER])
def test_office_release_roles_can_publish_and_attach_but_quality_cannot_delete(
    client, db_session, test_user, test_work_order, local_files, role
):
    test_user.role = role
    db_session.commit()
    headers = {'Authorization': f'Bearer {create_access_token(test_user.id, company_id=1)}'}
    response = upload(client, headers)
    assert response.status_code == 200
    document_id = response.json()['id']
    response = client.post(
        f'/api/v1/documents/{document_id}/attach-work-order',
        headers=headers,
        json={'work_order_id': test_work_order.id},
    )
    assert response.status_code == 200
    assert response.json()['work_order_id'] == test_work_order.id
    db_session.rollback()
    assert db_session.get(Document, document_id).released_by == test_user.id
    before = db_session.query(AuditLog).count()
    response = client.delete(f'/api/v1/documents/{document_id}', headers=headers)
    db_session.rollback()
    if role == UserRole.QUALITY:
        assert response.status_code == 403
        assert db_session.get(Document, document_id) is not None
        assert db_session.query(AuditLog).count() == before
        assert len(list(local_files.iterdir())) == 1
    else:
        assert response.status_code == 200
        assert db_session.get(Document, document_id) is None
        assert db_session.query(AuditLog).count() == before + 1
        assert list(local_files.iterdir()) == []


def test_missing_audit_rolls_back_upload_and_removes_new_file(
    client, db_session, admin_headers, local_files, monkeypatch
):
    monkeypatch.setattr(AuditService, "log", lambda *args, **kwargs: None)
    response = upload(client, admin_headers)
    assert response.status_code == 503
    db_session.rollback()
    assert db_session.query(Document).count() == 0
    assert list(local_files.iterdir()) == []


@pytest.mark.parametrize('mutation', ['attach', 'delete', 'revision'])
@pytest.mark.parametrize('role', [UserRole.VIEWER, UserRole.OPERATOR, UserRole.SUPERVISOR, UserRole.SHIPPING])
def test_non_releasers_cannot_change_existing_document(
    client, db_session, admin_headers, test_user, test_work_order, role, mutation
):
    document_id = upload(client, admin_headers).json()['id']
    original = db_session.get(Document, document_id)
    file_path = original.file_path
    before = db_session.query(AuditLog).count()
    test_user.role = role
    db_session.commit()
    headers = {'Authorization': f'Bearer {create_access_token(test_user.id, company_id=1)}'}
    if mutation == 'attach':
        response = client.post(
            f'/api/v1/documents/{document_id}/attach-work-order',
            headers=headers,
            json={'work_order_id': test_work_order.id},
        )
    elif mutation == 'delete':
        response = client.delete(f'/api/v1/documents/{document_id}', headers=headers)
    else:
        response = upload(client, headers, previous_revision_id=document_id, revision='B', revision_notes='Revised')
    assert response.status_code == 403
    db_session.rollback()
    assert db_session.get(Document, document_id).work_order_id is None
    assert db_session.query(Document).count() == 1
    assert db_session.query(AuditLog).count() == before
    assert Path(file_path).read_bytes() == b'synthetic-controlled-evidence'


@pytest.mark.parametrize('mutation', ['attach', 'delete', 'revision'])
def test_audit_failure_preserves_existing_document_and_bytes(
    client, db_session, admin_headers, test_work_order, monkeypatch, local_files, mutation
):
    document_id = upload(client, admin_headers).json()['id']
    before = db_session.query(AuditLog).count()
    original_path = db_session.get(Document, document_id).file_path
    monkeypatch.setattr(AuditService, 'log', lambda *args, **kwargs: None)
    if mutation == 'attach':
        response = client.post(
            f'/api/v1/documents/{document_id}/attach-work-order',
            headers=admin_headers,
            json={'work_order_id': test_work_order.id},
        )
    elif mutation == 'delete':
        response = client.delete(f'/api/v1/documents/{document_id}', headers=admin_headers)
    else:
        response = upload(
            client, admin_headers, previous_revision_id=document_id, revision='B', revision_notes='Revised'
        )
    assert response.status_code == 503
    db_session.rollback()
    assert db_session.get(Document, document_id).work_order_id is None
    assert db_session.query(Document).count() == 1
    assert db_session.query(AuditLog).count() == before
    assert Path(original_path).read_bytes() == b'synthetic-controlled-evidence'
    assert len(list(local_files.iterdir())) == 1


@pytest.mark.parametrize('mutation', ['attach', 'delete', 'revision'])
def test_foreign_document_mutators_leave_both_tenants_unchanged(
    client, db_session, admin_headers, test_work_order, mutation
):
    document_id = upload(client, admin_headers).json()['id']
    db_session.add(Company(id=2, name='Foreign', slug='foreign', is_active=True))
    document = db_session.get(Document, document_id)
    document.company_id = 2
    db_session.commit()
    before = db_session.query(AuditLog).count()
    if mutation == 'attach':
        response = client.post(
            f'/api/v1/documents/{document_id}/attach-work-order',
            headers=admin_headers,
            json={'work_order_id': test_work_order.id},
        )
    elif mutation == 'delete':
        response = client.delete(f'/api/v1/documents/{document_id}', headers=admin_headers)
    else:
        response = upload(
            client, admin_headers, previous_revision_id=document_id, revision='B', revision_notes='Revised'
        )
    assert response.status_code == 404
    db_session.rollback()
    assert db_session.get(Document, document_id).company_id == 2
    assert db_session.query(Document).count() == 1
    assert db_session.query(AuditLog).count() == before


def test_revision_keeps_prior_file_and_audits_link(client, db_session, admin_headers, local_files):
    original = upload(client, admin_headers).json()
    revision = upload(
        client, admin_headers, previous_revision_id=original['id'], revision='B', revision_notes='Revised tolerance'
    )
    assert revision.status_code == 200
    db_session.rollback()
    rows = (
        db_session.query(AuditLog).filter(AuditLog.resource_type == 'document').order_by(AuditLog.sequence_number).all()
    )
    assert rows[-1].new_values['previous_revision_id'] == original['id']
    assert rows[-1].new_values['revision_notes'] == 'Revised tolerance'
    assert len(list(local_files.iterdir())) == 2
    assert client.delete(f"/api/v1/documents/{original['id']}", headers=admin_headers).status_code == 409


def test_document_responses_do_not_expand_foreign_part(client, db_session, admin_headers):
    document_id = upload(client, admin_headers).json()['id']
    db_session.add(Company(id=2, name='Foreign', slug='foreign', is_active=True))
    foreign = Part(
        part_number='PRIVATE-PART', name='Private customer part', part_type=PartType.MANUFACTURED, company_id=2
    )
    db_session.add(foreign)
    db_session.flush()
    document = db_session.get(Document, document_id)
    document.part_id = foreign.id
    db_session.commit()
    # Even a relationship loaded into the identity map must be refreshed safely.
    assert document.part.name == 'Private customer part'
    for url in [f'/api/v1/documents/{document_id}', '/api/v1/documents/', f'/api/v1/documents/{document_id}/revisions']:
        response = client.get(url, headers=admin_headers)
        assert response.status_code == 200
        records = response.json() if isinstance(response.json(), list) else [response.json()]
        assert all(row['part'] is None for row in records)
        assert 'Private customer part' not in response.text


@pytest.mark.parametrize('reference', ['part', 'work_order'])
def test_revisions_preserve_historical_links_but_new_uploads_cannot_select_deleted_records(
    client, db_session, admin_headers, test_part, test_work_order, local_files, reference
):
    record = test_part if reference == 'part' else test_work_order
    links = {f'{reference}_id': record.id}
    original = upload(client, admin_headers, **links).json()
    original_path = db_session.get(Document, original['id']).file_path
    record.is_deleted = True
    db_session.commit()
    before = db_session.query(AuditLog).count()

    assert upload(client, admin_headers, **links).status_code == 404
    assert db_session.query(AuditLog).count() == before
    assert db_session.query(Document).count() == 1

    response = upload(
        client,
        admin_headers,
        previous_revision_id=original['id'],
        revision='B',
        revision_notes='Correct historical instruction',
        **links,
    )
    assert response.status_code == 200
    assert response.json()[f'{reference}_id'] == record.id
    if reference == 'part':
        assert response.json()['part']['part_number'] == test_part.part_number
    db_session.rollback()
    assert db_session.query(Document).count() == 2
    assert db_session.query(AuditLog).count() == before + 1
    assert Path(original_path).read_bytes() == b'synthetic-controlled-evidence'
    assert len(list(local_files.iterdir())) == 2
