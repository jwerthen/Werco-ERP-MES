"""Parity of the document access rule's two forms (``app/services/document_access.py``).

``visible_documents_clause`` (SQL) and ``can_view_document`` (Python) must agree for every
role x superuser x export flag x home company x access level, and both must match the rule as
the PR contract states it, written out here independently of the module:

* ``standard`` -- everyone;
* ``restricted`` -- Admin, Manager, Quality, Supervisor, a superuser, a Platform Admin;
* ``export_controlled`` -- ONLY ``export_authorized``, and only in the user's OWN company: no
  role bypasses it, and a platform account context-switched into another tenant carries no
  export authorization there.

An AI-external API token and "no person" (``None``) see ``standard`` only.
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
    model_visible_documents_clause,
    visible_access_levels,
    visible_document_or_404,
    visible_documents_clause,
)

pytestmark = pytest.mark.unit

COMPANY_ID = 1
OTHER_COMPANY_ID = 2
LEVELS = [level.value for level in DocumentAccessLevel]
RESTRICTED_ROLES = {UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY, UserRole.SUPERVISOR, UserRole.PLATFORM_ADMIN}
HOMES = (COMPANY_ID, OTHER_COMPANY_ID)  # OTHER = a platform account context-switched into COMPANY_ID


def _expected(role, is_superuser, export_authorized, home, level) -> bool:
    if level == "standard":
        return True
    if level == "restricted":
        return is_superuser or role in RESTRICTED_ROLES
    if level == "export_controlled":
        return export_authorized and home == COMPANY_ID
    return False


def _persona(role, is_superuser, export_authorized, home=COMPANY_ID) -> User:
    # Transient: the rule reads only the user's attributes, never the user's row.
    return User(role=role, is_superuser=is_superuser, export_authorized=export_authorized, company_id=home)


MATRIX = list(itertools.product(list(UserRole), (False, True), (False, True), HOMES))


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


@pytest.mark.parametrize("role,is_superuser,export_authorized,home", MATRIX)
def test_clause_and_twin_match_the_rule(db_session, documents, role, is_superuser, export_authorized, home):
    user = _persona(role, is_superuser, export_authorized, home)
    sql_visible = {
        document.access_level
        for document in db_session.query(Document)
        .filter(Document.company_id == COMPANY_ID, visible_documents_clause(user, COMPANY_ID))
        .all()
    }
    for level in LEVELS:
        expected = _expected(role, is_superuser, export_authorized, home, level)
        assert can_view_document(user, documents[(COMPANY_ID, level)]) is expected, level
        assert (level in sql_visible) is expected, level


def test_parity_helper_reports_no_mismatch_over_the_whole_matrix(db_session, documents):
    users = [_persona(*persona) for persona in MATRIX]
    assert clause_parity_mismatches(db_session, COMPANY_ID, users + [None]) == []
    assert clause_parity_mismatches(db_session, OTHER_COMPANY_ID, users + [None]) == []


def test_a_context_switched_platform_account_carries_no_export_authorization(db_session, documents):
    """Granted in its home company, a platform account opens that company's export-controlled
    documents and NOT the tenant it switched into -- in SQL and in Python."""
    platform = _persona(UserRole.PLATFORM_ADMIN, False, True, home=OTHER_COMPANY_ID)
    assert "export_controlled" in visible_access_levels(platform, OTHER_COMPANY_ID)
    assert "export_controlled" not in visible_access_levels(platform, COMPANY_ID)
    assert can_view_document(platform, documents[(OTHER_COMPANY_ID, "export_controlled")])
    assert not can_view_document(platform, documents[(COMPANY_ID, "export_controlled")])
    with pytest.raises(HTTPException):
        visible_document_or_404(db_session, COMPANY_ID, platform, documents[(COMPANY_ID, "export_controlled")].id)


def test_no_role_bypasses_export_control():
    document = Document(access_level="export_controlled", company_id=COMPANY_ID)
    for role in UserRole:
        assert not can_view_document(_persona(role, True, False), document), role
        assert can_view_document(_persona(role, False, True), document), role


def test_no_person_and_ai_external_tokens_see_standard_only():
    assert can_view_document(None, Document(access_level="standard", company_id=COMPANY_ID))
    assert not can_view_document(None, Document(access_level="restricted", company_id=COMPANY_ID))
    assert not can_view_document(None, Document(access_level="export_controlled", company_id=COMPANY_ID))
    connector = _persona(UserRole.ADMIN, True, True)
    connector._api_token_id = 7
    connector._api_token_ai_external = True
    assert visible_access_levels(connector, COMPANY_ID) == ("standard",)
    connector._api_token_ai_external = False  # an ordinary full token acts as its user
    assert visible_access_levels(connector, COMPANY_ID) == ("standard", "restricted", "export_controlled")


def test_unknown_levels_fail_closed():
    admin = _persona(UserRole.ADMIN, True, True)
    assert not can_view_document(admin, Document(access_level=None, company_id=COMPANY_ID))
    assert not can_view_document(admin, Document(access_level="confidential", company_id=COMPANY_ID))
    assert not can_view_document(admin, None)


def test_model_bound_reads_never_include_export_controlled(db_session, documents):
    authorized = _persona(UserRole.ADMIN, False, True)
    levels = {
        document.access_level
        for document in db_session.query(Document)
        .filter(Document.company_id == COMPANY_ID, model_visible_documents_clause(authorized, COMPANY_ID))
        .all()
    }
    assert levels == {"standard", "restricted"}


def test_visible_document_or_404_hides_invisible_missing_and_foreign_alike(db_session, documents):
    operator = _persona(UserRole.OPERATOR, False, False)
    quality = _persona(UserRole.QUALITY, False, False)
    restricted = documents[(COMPANY_ID, "restricted")]
    assert visible_document_or_404(db_session, COMPANY_ID, quality, restricted.id).id == restricted.id
    for document_id in (restricted.id, 999_999, documents[(OTHER_COMPANY_ID, "standard")].id):
        with pytest.raises(HTTPException) as caught:
            visible_document_or_404(db_session, COMPANY_ID, operator, document_id)
        assert (caught.value.status_code, caught.value.detail) == (404, "Document not found")
