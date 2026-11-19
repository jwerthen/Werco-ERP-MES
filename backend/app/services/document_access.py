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
from sqlalchemy import and_, false, func, not_, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.core.credentials import is_ai_external_credential
from app.db.tenant_filter import tenant_query
from app.models.audit_log import AuditLog
from app.models.document import Document, DocumentAccessLevel
from app.models.hank import HankTask
from app.models.hank_intake import HankIntakeFile
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
    return Document.access_level.in_(model_visible_access_levels(user, company_id))


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

# Audit resource types whose rows carry a document's metadata. A ``document`` row is about the
# document its ``resource_id`` names. A ``hank_intake_file`` row can carry the filing plan's title,
# description and reviewed fields and the filed document's number; a ``hank_task`` row can carry
# an attach task's document number and title, or a watch's arrived document. Those two are judged
# by the documents their source record and payload reference (:func:`_record_document_ids`).
DOCUMENT_AUDIT_RESOURCE_TYPES = ("document", "hank_intake_file", "hank_task")

# Payload keys that name the document a Hank row is about. ``certificate_document_id`` and other
# pointer keys are deliberately absent: a pointer id stays visible (module docstring).
_PAYLOAD_DOCUMENT_ID_KEYS = ("document_id",)


def _level_in(values: Any) -> Optional[str]:
    if isinstance(values, dict):
        level = values.get("access_level")
        if isinstance(level, str):
            return level
    return None


def _as_id(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _payload_document_ids(value: Any, found: Optional[Set[int]] = None) -> Set[int]:
    """Every document id a Hank payload names: a ``document_id`` key, or a ``{"type": "document",
    "id": ...}`` reference, at any depth."""
    found = set() if found is None else found
    if isinstance(value, dict):
        if value.get("type") == "document":
            ref = _as_id(value.get("id"))
            if ref is not None:
                found.add(ref)
        for key, item in value.items():
            if key in _PAYLOAD_DOCUMENT_ID_KEYS:
                ref = _as_id(item)
                if ref is not None:
                    found.add(ref)
            else:
                _payload_document_ids(item, found)
    elif isinstance(value, list):
        for item in value:
            _payload_document_ids(item, found)
    return found


def _record_document_ids(
    db: Session, company_id: int, resource_type: str, record_ids: Iterable[int]
) -> Dict[int, Set[int]]:
    """resource id -> the document ids its Hank source record names (an intake file's filed
    document, an attach task's document, a watch's arrived document)."""
    ids = sorted({record_id for record_id in record_ids if record_id is not None})
    if not ids:
        return {}
    result: Dict[int, Set[int]] = {}
    if resource_type == "hank_intake_file":
        for row in (
            tenant_query(db, HankIntakeFile, company_id)
            .filter(HankIntakeFile.id.in_(ids))
            .with_entities(HankIntakeFile.id, HankIntakeFile.result_json)
            .all()
        ):
            result[row.id] = _payload_document_ids(row.result_json)
    elif resource_type == "hank_task":
        for row in (
            tenant_query(db, HankTask, company_id)
            .filter(HankTask.id.in_(ids))
            .with_entities(HankTask.id, HankTask.input_json, HankTask.result_json)
            .all()
        ):
            result[row.id] = _payload_document_ids(row.input_json) | _payload_document_ids(row.result_json)
    return result


def _document_levels(db: Session, company_id: int, document_ids: Iterable[int]) -> Dict[int, str]:
    """document id -> its level now or, for a deleted document, its LAST KNOWN level.

    Every document audit row written since migration 131 carries ``access_level``; a deleted
    document whose trail never names a level was deleted before 131, when every document was
    ``standard``.
    """
    ids = sorted({doc_id for doc_id in document_ids if doc_id is not None})
    if not ids:
        return {}
    level_of: Dict[int, str] = {
        row.id: row.access_level
        for row in tenant_query(db, Document, company_id)
        .filter(Document.id.in_(ids))
        .with_entities(Document.id, Document.access_level)
        .all()
    }
    missing = [doc_id for doc_id in ids if doc_id not in level_of]
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
    return level_of


def redacted_document_audit_ids(db: Session, company_id: int, user: Optional[Any], rows: Sequence[Any]) -> Set[int]:
    """The ids of the audit ``rows`` the reader must see redacted.

    Audit rows are immutable, so redaction happens on the way OUT, never by writing. A row is
    redacted when ANY document it is about is one the reader may not open now:

    * a ``document`` row: the document its ``resource_id`` names (a row with no resource id
      cannot be resolved and is redacted);
    * a ``hank_intake_file`` / ``hank_task`` row: the documents its source record names (the
      intake file's filed document; an attach task's document; a watch's arrived document) and
      any its own old / new values name.

    A deleted document is judged by its LAST KNOWN level (:func:`_document_levels`).
    """
    relevant = [row for row in rows if getattr(row, "resource_type", None) in DOCUMENT_AUDIT_RESOURCE_TYPES]
    if not relevant:
        return set()
    refs: Dict[int, Optional[Set[int]]] = {}
    by_type: Dict[str, List[Any]] = {}
    for row in relevant:
        by_type.setdefault(row.resource_type, []).append(row)
    for resource_type, typed in by_type.items():
        if resource_type == "document":
            for row in typed:
                refs[row.id] = None if row.resource_id is None else {row.resource_id}
            continue
        sources = _record_document_ids(db, company_id, resource_type, (row.resource_id for row in typed))
        for row in typed:
            named = set(sources.get(row.resource_id, set()))
            named |= _payload_document_ids(row.old_values) | _payload_document_ids(row.new_values)
            refs[row.id] = named
    levels = _document_levels(db, company_id, {doc_id for named in refs.values() if named for doc_id in named})
    visible_levels = set(visible_access_levels(user, company_id))
    return {
        row_id
        for row_id, named in refs.items()
        if named is None or any(levels.get(doc_id) not in visible_levels for doc_id in named)
    }


def model_visible_access_levels(user: Optional[Any], company_id: Optional[int]) -> Tuple[str, ...]:
    """:func:`visible_access_levels` minus ``export_controlled``: what may reach an AI model."""
    return tuple(
        level
        for level in visible_access_levels(user, company_id)
        if level != DocumentAccessLevel.EXPORT_CONTROLLED.value
    )


def model_hidden_record_ids(
    db: Session, company_id: int, user: Optional[Any], resource_type: str, record_ids: Iterable[int]
) -> Set[int]:
    """The Hank records (``"hank_intake_file"`` / ``"hank_task"``) whose details must not reach an
    AI model for ``user``: any document the record names is ``export_controlled`` or one the user
    may not open (a deleted one by its last known level). Chat treats such a record as missing."""
    named = _record_document_ids(db, company_id, resource_type, record_ids)
    levels = _document_levels(db, company_id, {doc_id for ids in named.values() for doc_id in ids})
    allowed = set(model_visible_access_levels(user, company_id))
    return {record_id for record_id, ids in named.items() if any(levels.get(doc_id) not in allowed for doc_id in ids)}


def audit_search_visibility_clause(db: Session, company_id: int, user: Optional[Any]) -> Optional[ColumnElement[bool]]:
    """SQL: the audit rows a ``search`` may return, or None when the reader may open every level.

    A free-text search runs over the stored text, so it must never match -- or count, or page
    past -- a row the reader would see redacted: that would confirm what a hidden document's
    number or title contains. This excludes, BEFORE pagination, every ``document`` row whose
    document the reader may not open (a deleted one by its last known level) and every Hank
    intake / task row whose source record names such a document. Without a search the list
    keeps those rows and redacts them (:func:`redacted_document_audit_ids`).
    """
    levels = visible_access_levels(user, company_id)
    if set(levels) == ACCESS_LEVELS:
        return None
    visible_ids = select(Document.id).where(Document.company_id == company_id, Document.access_level.in_(levels))
    deleted_ids = [
        row.resource_id
        for row in db.query(AuditLog.resource_id)
        .filter(
            AuditLog.company_id == company_id,
            AuditLog.resource_type == "document",
            AuditLog.resource_id.isnot(None),
            AuditLog.resource_id.notin_(select(Document.id).where(Document.company_id == company_id)),
        )
        .distinct()
        .all()
    ]
    deleted_levels = _document_levels(db, company_id, deleted_ids)
    visible_deleted = sorted(doc_id for doc_id, level in deleted_levels.items() if level in levels)

    hidden_sources: Dict[str, List[int]] = {"hank_intake_file": [], "hank_task": []}
    named_by: Dict[str, Dict[int, Set[int]]] = {
        "hank_intake_file": {
            row.id: _payload_document_ids(row.result_json)
            for row in tenant_query(db, HankIntakeFile, company_id)
            .filter(HankIntakeFile.result_json.isnot(None))
            .with_entities(HankIntakeFile.id, HankIntakeFile.result_json)
            .all()
        },
        "hank_task": {
            row.id: _payload_document_ids(row.input_json) | _payload_document_ids(row.result_json)
            for row in tenant_query(db, HankTask, company_id)
            .with_entities(HankTask.id, HankTask.input_json, HankTask.result_json)
            .all()
        },
    }
    named_ids = {doc_id for records in named_by.values() for named in records.values() for doc_id in named}
    named_levels = _document_levels(db, company_id, named_ids)
    for resource_type, records in named_by.items():
        hidden_sources[resource_type] = sorted(
            record_id
            for record_id, named in records.items()
            if any(named_levels.get(doc_id) not in levels for doc_id in named)
        )

    document_ok = or_(
        AuditLog.resource_id.in_(visible_ids),
        AuditLog.resource_id.in_(visible_deleted) if visible_deleted else false(),
    )
    clauses = [not_(and_(AuditLog.resource_type == "document", or_(AuditLog.resource_id.is_(None), not_(document_ok))))]
    for resource_type, hidden in hidden_sources.items():
        if hidden:
            clauses.append(
                not_(
                    and_(
                        AuditLog.resource_type == resource_type,
                        AuditLog.resource_id.isnot(None),
                        AuditLog.resource_id.in_(hidden),
                    )
                )
            )
    return and_(*clauses)
