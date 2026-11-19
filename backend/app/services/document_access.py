"""Who may open a document: the one rule (AS9100D 7.5.3.1 / 7.5.3.2, migration 131).

Every controlled document carries ``documents.access_level`` (``DocumentAccessLevel``):

* ``standard`` -- anyone who could read documents before migration 131.
* ``restricted`` -- Admin, Manager, Quality and Supervisor, plus a superuser and a Platform
  Admin (the same pass-through ``require_role`` gives them).
* ``export_controlled`` -- ONLY a user whose ``users.export_authorized`` is true, and only in
  the company that user belongs to (``users.company_id``). No role bypasses it: not Admin, not
  a superuser, not a Platform Admin. The authorization is granted per company by that
  company's Admin, so a platform account that switches into another tenant carries no export
  authorization there, whatever its home company granted. An API token and a crew-station
  badge (kiosk operator) token act as the user they are bound to, so they carry that user's
  flag.

Two callers see ``standard`` only, whoever they are: an unattended station with no person
behind it (``user=None``, a crew-station tablet's own token), and an AI-external API token
(``credentials.is_ai_external_credential``), whose answers go to an outside AI service.

The rule exists twice, on purpose, and nowhere else:

* :func:`visible_documents_clause` -- the SQL form, for queries that list or look up
  documents (it keeps the filtering in the database, so a limit/offset page is a page of
  VISIBLE documents);
* :func:`can_view_document` -- the Python form, for a document already loaded through a
  relationship (a laser nest's PDF, a step record's attachment). It judges the export half
  against the document's own ``company_id``, which on every such path is the active company.

Both are built from :func:`visible_access_levels`, and ``tests/test_document_access.py``
holds them together over every role x export flag x level x company combination through
:func:`clause_parity_mismatches`, so they cannot drift.

:func:`model_visible_documents_clause` is the narrower form for reads whose result is handed to
an AI model (copilot document search, Hank's job documents): it never includes
``export_controlled``, even for an authorized user.

The policy everywhere is **404, before any other check** (no oracle): a document the caller
may not view is indistinguishable from one that does not exist. :func:`visible_document_or_404`
is the by-id resolve for that. Ids that merely POINT at a document (a receipt's certificate,
a shipment's label) are not hidden -- the file itself is gated where its bytes are served.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.core.credentials import is_ai_external_credential
from app.db.tenant_filter import tenant_query
from app.models.audit_log import AuditLog
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


def is_export_authorized(user: Optional[Any], company_id: Optional[int]) -> bool:
    """True only when ``user.export_authorized`` is exactly True AND ``company_id`` is the user's own company.

    The grant is made by a company's Admin for that company's documents, so it never travels
    with a platform account into another tenant. Nothing else counts, no role included.
    """
    if user is None or company_id is None or getattr(user, "export_authorized", False) is not True:
        return False
    home = getattr(user, "company_id", None)
    return home is not None and home == company_id


def visible_access_levels(user: Optional[Any], company_id: Optional[int]) -> Tuple[str, ...]:
    """The access levels ``user`` may open in ``company_id``, in severity order.

    ``None`` (no person) and an AI-external API token get ``standard`` only.
    """
    levels: List[str] = [DocumentAccessLevel.STANDARD.value]
    if user is None or is_ai_external_credential(user):
        return tuple(levels)
    if sees_restricted_documents(user):
        levels.append(DocumentAccessLevel.RESTRICTED.value)
    if is_export_authorized(user, company_id):
        levels.append(DocumentAccessLevel.EXPORT_CONTROLLED.value)
    return tuple(levels)


def visible_documents_clause(user: Optional[Any], company_id: Optional[int]) -> ColumnElement[bool]:
    """SQL: the documents ``user`` may open in the active company ``company_id``. AND it into any query.

    A NULL or unknown ``access_level`` (impossible under the NOT NULL + CHECK, but SQLite tests
    do not enforce every CHECK) matches nothing: fail closed, the same answer as
    :func:`can_view_document`.
    """
    return Document.access_level.in_(visible_access_levels(user, company_id))


def model_visible_documents_clause(user: Optional[Any], company_id: Optional[int]) -> ColumnElement[bool]:
    """SQL: the documents ``user`` may open whose metadata may be handed to an AI model.

    :func:`visible_documents_clause` minus ``export_controlled``, for everyone: an export
    authorization lets a person open the document, never send it to an outside model.
    """
    levels = tuple(
        level
        for level in visible_access_levels(user, company_id)
        if level != DocumentAccessLevel.EXPORT_CONTROLLED.value
    )
    return Document.access_level.in_(levels)


def can_view_document(user: Optional[Any], document: Optional[Any]) -> bool:
    """Python twin of :func:`visible_documents_clause` for an already-loaded document.

    ``None`` for the document is False (nothing to view). The export half is judged against
    ``document.company_id`` -- on every caller's path the active company, because the document
    was loaded through a tenant-scoped query or relationship. Tenancy itself is NOT checked here.
    """
    if document is None:
        return False
    return getattr(document, "access_level", None) in visible_access_levels(user, getattr(document, "company_id", None))


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
    query = tenant_query(db, Document, company_id).filter(
        Document.id == document_id, visible_documents_clause(user, company_id)
    )
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
        .filter(Document.id.in_(sorted(ids)), visible_documents_clause(user, company_id))
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


def access_level_rank(level: Optional[str]) -> int:
    """The severity of ``level`` (standard 0, restricted 1, export_controlled 2); unknown counts as the highest."""
    if level in ACCESS_LEVELS:
        return ACCESS_LEVEL_ORDER.index(level)
    return len(ACCESS_LEVEL_ORDER) - 1


def revision_chain_ids(db: Session, company_id: int, document_id: int) -> List[int]:
    """Every document id in ``document_id``'s revision chain (``previous_revision_id`` walked both
    ways, tenant-scoped), in ascending id order -- the order chain rows are locked in. Pure read."""
    rows = tenant_query(db, Document, company_id).with_entities(Document.id, Document.previous_revision_id).all()
    previous_of: Dict[int, Optional[int]] = {row.id: row.previous_revision_id for row in rows}
    if document_id not in previous_of:
        return []
    root = document_id
    seen: Set[int] = set()
    while root not in seen and previous_of.get(root) in previous_of:
        seen.add(root)
        root = previous_of[root]  # type: ignore[assignment]
    linked = {root}
    while True:
        children = {doc_id for doc_id, previous_id in previous_of.items() if previous_id in linked}
        if children.issubset(linked):
            break
        linked.update(children)
    return sorted(linked)


def export_authorized_user_count(db: Session, company_id: int) -> int:
    """Active users of the company -- its own users, never a visiting platform account -- who may
    open an ``export_controlled`` document there."""
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
    export flag x home company, then asserts this is empty. Reads only the active company's
    documents.
    """
    documents = tenant_query(db, Document, company_id).all()
    mismatches: List[Tuple[Any, Set[int], Set[int]]] = []
    for user in users:
        sql_ids = {
            row.id
            for row in tenant_query(db, Document, company_id)
            .filter(visible_documents_clause(user, company_id))
            .with_entities(Document.id)
            .all()
        }
        python_ids = {document.id for document in documents if can_view_document(user, document)}
        if sql_ids != python_ids:
            mismatches.append((user, sql_ids, python_ids))
    return mismatches


# --------------------------------------------------------------------------- audit-log reads

REDACTED_AUDIT_VALUES = {"redacted": "document not available to you"}
REDACTED_AUDIT_DESCRIPTION = "Document change (restricted)"
REDACTED_AUDIT_IDENTIFIER = "Restricted document"


def _level_in(values: Any) -> Optional[str]:
    if isinstance(values, dict):
        level = values.get("access_level")
        if isinstance(level, str):
            return level
    return None


def redacted_document_audit_ids(db: Session, company_id: int, user: Optional[Any], rows: Sequence[Any]) -> Set[int]:
    """The ids of the audit ``rows`` (``resource_type == "document"``) the reader must see redacted.

    Audit rows are immutable, so redaction happens on the way OUT, never by writing. A row is
    redacted when its document is the company's and the reader may not open it now. For a
    document that no longer exists (deleted), its LAST KNOWN level is read from the company's
    own audit trail -- every document audit row written since migration 131 carries
    ``access_level`` -- and the row is redacted unless that level is one the reader may open; a
    document whose trail never names a level was deleted before 131, when every document was
    ``standard``. A document row with no resource id cannot be resolved and is redacted.
    """
    document_rows = [row for row in rows if getattr(row, "resource_type", None) == "document"]
    if not document_rows:
        return set()
    ids = {row.resource_id for row in document_rows if row.resource_id is not None}
    existing = (
        {
            row.id: row.access_level
            for row in tenant_query(db, Document, company_id)
            .filter(Document.id.in_(sorted(ids)))
            .with_entities(Document.id, Document.access_level)
            .all()
        }
        if ids
        else {}
    )
    visible_levels = set(visible_access_levels(user, company_id))
    level_of: Dict[int, str] = {doc_id: level for doc_id, level in existing.items()}
    missing = sorted(ids - set(existing))
    if missing:
        trail = (
            db.query(AuditLog.resource_id, AuditLog.old_values, AuditLog.new_values)
            .filter(
                AuditLog.company_id == company_id,
                AuditLog.resource_type == "document",
                AuditLog.resource_id.in_(missing),
            )
            .order_by(AuditLog.id.desc())
            .all()
        )
        for entry in trail:
            if entry.resource_id in level_of:
                continue
            # The newest row that names a level wins: its new value, else (a DELETE) its old one.
            level = _level_in(entry.new_values) or _level_in(entry.old_values)
            if level is not None:
                level_of[entry.resource_id] = level
        for doc_id in missing:
            level_of.setdefault(doc_id, DocumentAccessLevel.STANDARD.value)
    return {
        row.id
        for row in document_rows
        if row.resource_id is None or level_of.get(row.resource_id) not in visible_levels
    }
