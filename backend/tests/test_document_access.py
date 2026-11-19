"""Parity of the document access rule's two forms (``app/services/document_access.py``).

``visible_documents_clause`` (SQL) and ``can_view_document`` (Python) must agree for every
role x superuser x export flag x access level, and both must match the rule as the PR contract
states it, written out here independently of the module:

* ``standard`` -- everyone;
* ``restricted`` -- Admin, Manager, Quality, Supervisor, a superuser, a Platform Admin;
* ``export_controlled`` -- ONLY ``export_authorized``; no role bypasses it.
"""

import itertools

import pytest
from fastapi import HTTPException

from app.models.company import Company
from app.models.document import Document, DocumentAccessLevel, DocumentType
from app.models.user import User, UserRole
from app.services.document_access import (
    can_view_document,
    clause_parity_mismatches,
    visible_document_or_404,
    visible_documents_clause,
)

pytestmark = pytest.mark.unit

COMPANY_ID = 1
OTHER_COMPANY_ID = 2
LEVELS = [level.value for level in DocumentAccessLevel]
RESTRICTED_ROLES = {UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY, UserRole.SUPERVISOR, UserRole.PLATFORM_ADMIN}


def _expected(role, is_superuser, export_authorized, level) -> bool:
    if level == "standard":
        return True
    if level == "restricted":
        return is_superuser or role in RESTRICTED_ROLES
    if level == "export_controlled":
        return export_authorized
    return False


def _persona(role, is_superuser, export_authorized) -> User:
    # Transient: the rule reads only the user's attributes, never the user's row.
    return User(role=role, is_superuser=is_superuser, export_authorized=export_authorized, company_id=COMPANY_ID)


MATRIX = list(itertools.product(list(UserRole), (False, True), (False, True)))


@pytest.fixture
def documents(db_session):
    if not db_session.query(Company).filter(Company.id == OTHER_COMPANY_ID).first():
        db_session.add(Company(id=OTHER_COMPANY_ID, name="Other tenant", slug="other-tenant", is_active=True))
        db_session.flush()
    rows = {}
    for company_id in (COMPANY_ID, OTHER_COMPANY_ID):
        for level in LEVELS:
            document = Document(
                document_number=f"DOC-{company_id}-{level}",
                title=f"{level} drawing",
                document_type=DocumentType.DRAWING,
                status="released",
                access_level=level,
                company_id=company_id,
            )
            db_session.add(document)
            rows[(company_id, level)] = document
    db_session.commit()
    return rows


@pytest.mark.parametrize("role,is_superuser,export_authorized", MATRIX)
def test_clause_and_twin_match_the_rule(db_session, documents, role, is_superuser, export_authorized):
    user = _persona(role, is_superuser, export_authorized)
    sql_visible = {
        document.access_level
        for document in db_session.query(Document)
        .filter(Document.company_id == COMPANY_ID, visible_documents_clause(user))
        .all()
    }
    for level in LEVELS:
        expected = _expected(role, is_superuser, export_authorized, level)
        assert can_view_document(user, documents[(COMPANY_ID, level)]) is expected, level
        assert (level in sql_visible) is expected, level


def test_parity_helper_reports_no_mismatch_over_the_whole_matrix(db_session, documents):
    users = [_persona(*persona) for persona in MATRIX]
    assert clause_parity_mismatches(db_session, COMPANY_ID, users + [None]) == []


def test_no_role_bypasses_export_control():
    document = Document(access_level="export_controlled")
    for role in UserRole:
        assert not can_view_document(_persona(role, True, False), document), role
        assert can_view_document(_persona(role, False, True), document), role


def test_no_person_sees_standard_only_and_unknown_levels_fail_closed():
    assert can_view_document(None, Document(access_level="standard"))
    assert not can_view_document(None, Document(access_level="restricted"))
    assert not can_view_document(None, Document(access_level="export_controlled"))
    admin = _persona(UserRole.ADMIN, True, True)
    assert not can_view_document(admin, Document(access_level=None))
    assert not can_view_document(admin, Document(access_level="confidential"))
    assert not can_view_document(admin, None)


def test_visible_document_or_404_hides_invisible_missing_and_foreign_alike(db_session, documents):
    operator = _persona(UserRole.OPERATOR, False, False)
    quality = _persona(UserRole.QUALITY, False, False)
    restricted = documents[(COMPANY_ID, "restricted")]
    assert visible_document_or_404(db_session, COMPANY_ID, quality, restricted.id).id == restricted.id
    for document_id in (restricted.id, 999_999, documents[(OTHER_COMPANY_ID, "standard")].id):
        with pytest.raises(HTTPException) as caught:
            visible_document_or_404(db_session, COMPANY_ID, operator, document_id)
        assert (caught.value.status_code, caught.value.detail) == (404, "Document not found")
