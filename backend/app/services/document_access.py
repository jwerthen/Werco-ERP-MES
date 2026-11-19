"""Who may open a document: the one rule (AS9100D 7.5.3.1 / 7.5.3.2, migration 131).

Every controlled document carries ``documents.access_level`` (``DocumentAccessLevel``):

* ``standard`` -- anyone who could read documents before migration 131.
* ``restricted`` -- Admin, Manager, Quality and Supervisor, plus a superuser and a Platform
  Admin (the same pass-through ``require_role`` gives them).
* ``export_controlled`` -- ONLY a user whose ``users.export_authorized`` is true. No role
  bypasses it: not Admin, not a superuser, not a Platform Admin. An API token and a
  crew-station badge (kiosk operator) token act as the user they are bound to, so they carry
  that user's flag. An unattended station with no person behind it (a crew-station tablet's
  own token) sees ``standard`` only. AI-external API tokens reach no document route at all.

The rule exists twice, on purpose, and nowhere else:

* :func:`visible_documents_clause` -- the SQL form, for queries that list or look up
  documents (it keeps the filtering in the database, so a limit/offset page is a page of
  VISIBLE documents);
* :func:`can_view_document` -- the Python form, for a document already loaded through a
  relationship (a laser nest's PDF, a step record's attachment).

Both are built from :func:`visible_access_levels`, and ``tests/test_document_access.py``
holds them together over every role x export flag x level combination through
:func:`clause_parity_mismatches`, so they cannot drift.

The policy everywhere is **404, before any other check** (no oracle): a document the caller
may not view is indistinguishable from one that does not exist. :func:`visible_document_or_404`
is the by-id resolve for that. Ids that merely POINT at a document (a receipt's certificate,
a shipment's label) are not hidden -- the file itself is gated where its bytes are served.
"""

from __future__ import annotations

from typing import Any, Iterable, List, Optional, Sequence, Set, Tuple

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.db.tenant_filter import tenant_query
from app.models.document import Document, DocumentAccessLevel
from app.models.user import User, UserRole

# Roles that open a ``restricted`` document. A superuser and a Platform Admin pass too, as they
# pass every ``require_role`` gate (``app.api.deps.require_role``).
RESTRICTED_DOCUMENT_ROLES = frozenset({UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY, UserRole.SUPERVISOR})

# Severity order: a revision inherits at least its predecessor's level, and the upload raise
# rule never lowers.
ACCESS_LEVEL_ORDER: Tuple[str, ...] = (
    DocumentAccessLevel.STANDARD.value,
    DocumentAccessLevel.RESTRICTED.value,
    DocumentAccessLevel.EXPORT_CONTROLLED.value,
)
ACCESS_LEVELS = frozenset(ACCESS_LEVEL_ORDER)

DOCUMENT_NOT_FOUND = "Document not found"


def _role(user: Any) -> Optional[UserRole]:
    role = getattr(user, "role", None)
    if role is None or isinstance(role, UserRole):
        return role
    try:
        return UserRole(getattr(role, "value", role))
    except ValueError:
        return None


def sees_restricted_documents(user: Optional[Any]) -> bool:
    """True when ``user`` may open a ``restricted`` document (role, superuser or Platform Admin)."""
    if user is None:
        return False
    if getattr(user, "is_superuser", False) is True:
        return True
    role = _role(user)
    return role == UserRole.PLATFORM_ADMIN or role in RESTRICTED_DOCUMENT_ROLES


def is_export_authorized(user: Optional[Any]) -> bool:
    """True only when ``user.export_authorized`` is exactly True. Nothing else counts, no role included."""
    return user is not None and getattr(user, "export_authorized", False) is True


def visible_access_levels(user: Optional[Any]) -> Tuple[str, ...]:
    """The access levels ``user`` may open, in severity order. ``None`` (no person) gets ``standard``."""
    levels: List[str] = [DocumentAccessLevel.STANDARD.value]
    if sees_restricted_documents(user):
        levels.append(DocumentAccessLevel.RESTRICTED.value)
    if is_export_authorized(user):
        levels.append(DocumentAccessLevel.EXPORT_CONTROLLED.value)
    return tuple(levels)


def visible_documents_clause(user: Optional[Any]) -> ColumnElement[bool]:
    """SQL: the documents ``user`` may open. AND it into any query over ``Document``.

    A NULL or unknown ``access_level`` (impossible under the NOT NULL + CHECK, but SQLite tests
    do not enforce every CHECK) matches nothing: fail closed, the same answer as
    :func:`can_view_document`.
    """
    return Document.access_level.in_(visible_access_levels(user))


def can_view_document(user: Optional[Any], document: Optional[Any]) -> bool:
    """Python twin of :func:`visible_documents_clause` for an already-loaded document.

    ``None`` for the document is False (nothing to view). Tenancy is NOT checked here -- the
    caller loaded the document through a tenant-scoped query or relationship.
    """
    if document is None:
        return False
    return getattr(document, "access_level", None) in visible_access_levels(user)


def visible_document_or_404(
    db: Session,
    company_id: int,
    user: Optional[Any],
    document_id: Any,
    *,
    for_update: bool = False,
) -> Document:
    """The active company's document ``document_id`` if ``user`` may open it, else **404**.

    The same ``Document not found`` for an id that does not exist, belongs to another company,
    or is above the caller's access -- so call this FIRST, before any other check on the
    document, and nothing about an invisible document leaks through a later 409/422.
    ``for_update`` takes the row lock (Postgres re-checks the WHERE on the locked row).
    """
    query = tenant_query(db, Document, company_id).filter(Document.id == document_id, visible_documents_clause(user))
    if for_update:
        query = query.with_for_update()
    document = query.first()
    if document is None:
        raise HTTPException(status_code=404, detail=DOCUMENT_NOT_FOUND)
    return document


def visible_document_ids(db: Session, company_id: int, user: Optional[Any], document_ids: Iterable[Any]) -> Set[int]:
    """The subset of ``document_ids`` that are the active company's and that ``user`` may open."""
    ids = {int(value) for value in document_ids if value is not None and not isinstance(value, bool)}
    if not ids:
        return set()
    rows = (
        tenant_query(db, Document, company_id)
        .filter(Document.id.in_(sorted(ids)), visible_documents_clause(user))
        .with_entities(Document.id)
        .all()
    )
    return {row.id for row in rows}


def higher_access_level(*levels: Optional[str]) -> str:
    """The most restrictive of ``levels`` (unknown / None ignored); ``standard`` when none."""
    best = 0
    for level in levels:
        if level in ACCESS_LEVELS:
            best = max(best, ACCESS_LEVEL_ORDER.index(level))
    return ACCESS_LEVEL_ORDER[best]


def export_authorized_user_count(db: Session, company_id: int) -> int:
    """Active users of the company who may open an ``export_controlled`` document."""
    return (
        db.query(func.count(User.id))
        .filter(
            User.company_id == company_id,
            User.is_active == True,  # noqa: E712
            User.export_authorized == True,  # noqa: E712
        )
        .scalar()
        or 0
    )


def clause_parity_mismatches(
    db: Session, company_id: int, users: Sequence[Any]
) -> List[Tuple[Any, Set[int], Set[int]]]:
    """Every ``(user, sql_visible_ids, python_visible_ids)`` where the two forms disagree.

    Test helper: the parity test seeds documents at every level and users across every role x
    export flag, then asserts this is empty. Reads only the active company's documents.
    """
    documents = tenant_query(db, Document, company_id).all()
    mismatches: List[Tuple[Any, Set[int], Set[int]]] = []
    for user in users:
        sql_ids = {
            row.id
            for row in tenant_query(db, Document, company_id)
            .filter(visible_documents_clause(user))
            .with_entities(Document.id)
            .all()
        }
        python_ids = {document.id for document in documents if can_view_document(user, document)}
        if sql_ids != python_ids:
            mismatches.append((user, sql_ids, python_ids))
    return mismatches
