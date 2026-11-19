"""Purchase-order quality flow-down (AS9100D 8.4.3): the block, its freeze, and its catalog.

The block a purchase order prints is ONE shape, built by ONE pure function,
:func:`build_flowdown_block`, from plain rows: the company's ``POFlowdownProfile``, its active
``POQualityClause`` rows and the PO's own ``quality_clause_codes``::

    {"enabled": false}                                    # disabled or no profile
    {"enabled": true, "heading": str, "document_reference": str | None, "body": str | None,
     "clauses": [{"code": str, "title": str, "body": str | None}, ...]}

Two readers call it:

* the freeze listener on ``PurchaseOrder`` (``app.models.purchasing``), which runs inside the
  flush that moves a PO out of DRAFT (or inserts one already issued) and stores the result on
  ``purchase_orders.flowdown_snapshot``. It reads through :func:`compute_flowdown_block` with
  the FLUSH CONNECTION -- Core selects only, never an ORM query on the session mid-flush;
* the print-data render (:func:`print_flowdown_block`), which shows a DRAFT's LIVE block and an
  issued PO's frozen snapshot -- and nothing for an issued PO with no snapshot (issued before
  migration 128). That last case is never back-filled: the catalog today is not what the
  supplier was sent.

Selection semantics: ``quality_clause_codes`` NULL means "the catalog's active
``default_selected`` clauses", so a PO created by a door that knows nothing of flow-down (MRP
drafts, the loaders, PO upload) needs no change. An explicit list, empty included, is the
buyer's choice. Only ACTIVE clauses print; they print in ``sort_order``, then ``code`` order.
"""

from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.locks import acquire_generator_lock
from app.db.tenant_filter import tenant_query
from app.models.po_flowdown import DEFAULT_FLOWDOWN_HEADING, POFlowdownProfile, POQualityClause
from app.models.purchasing import POStatus, PurchaseOrder, po_status_member
from app.services.audit_service import AuditService

PROFILE_RESOURCE_TYPE = "po_flowdown_profile"
CLAUSE_RESOURCE_TYPE = "po_quality_clause"

PROFILE_STALE_DETAIL = (
    "The quality flow-down settings were changed by someone else since you loaded them. Reload them and save again."
)
PROFILE_AUDIT_UNAVAILABLE = (
    "The audit record for the quality flow-down settings could not be saved; nothing was changed. Retry."
)
CLAUSE_AUDIT_UNAVAILABLE = "The audit record for the quality clause could not be saved; nothing was changed. Retry."
CLAUSE_CODE_IMMUTABLE_DETAIL = (
    "A quality clause's code cannot be changed once it exists. Retire this clause (is_active false) "
    "and add a new one under the new code."
)

_PROFILE_FIELDS = ("enabled", "heading", "document_reference", "body")
_CLAUSE_FIELDS = ("code", "title", "body", "default_selected", "is_active", "sort_order")
_CLAUSE_EDITABLE_FIELDS = ("title", "body", "default_selected", "is_active", "sort_order")


def flowdown_frozen_detail(po_number: str) -> str:
    """The 409 for changing the clause selection of a PO that is no longer a draft."""
    return f"PO {po_number} has been issued; its quality flow-down is frozen."


# ── The block ───────────────────────────────────────────────────────────────


def normalize_clause_codes(codes: Optional[Iterable[str]]) -> Optional[List[str]]:
    """Strip, drop blanks and de-duplicate, keeping first-seen order. ``None`` stays ``None``."""
    if codes is None:
        return None
    seen: List[str] = []
    for code in codes:
        text = str(code).strip()
        if text and text not in seen:
            seen.append(text)
    return seen


def build_flowdown_block(
    profile: Optional[Mapping[str, Any]],
    clauses: Iterable[Mapping[str, Any]],
    selected_codes: Optional[Sequence[str]],
) -> Dict[str, Any]:
    """The flow-down block for one purchase order. Pure: plain mappings in, a JSON dict out.

    ``profile`` is the company's profile row (``None`` when it has none); ``clauses`` its
    catalog rows (inactive ones are skipped here too, so a caller may pass the whole catalog);
    ``selected_codes`` the PO's ``quality_clause_codes`` (``None`` = the default-selected set).
    """
    if not profile or not profile.get("enabled"):
        return {"enabled": False}
    active = [clause for clause in clauses if clause.get("is_active")]
    if selected_codes is None:
        chosen = [clause for clause in active if clause.get("default_selected")]
    else:
        wanted = set(selected_codes)
        chosen = [clause for clause in active if clause.get("code") in wanted]
    chosen.sort(key=lambda clause: (int(clause.get("sort_order") or 0), str(clause.get("code") or "")))
    return {
        "enabled": True,
        "heading": profile.get("heading") or DEFAULT_FLOWDOWN_HEADING,
        "document_reference": profile.get("document_reference") or None,
        "body": profile.get("body") or None,
        "clauses": [
            {"code": clause.get("code"), "title": clause.get("title"), "body": clause.get("body") or None}
            for clause in chosen
        ],
    }


def load_flowdown_inputs(executor: Any, company_id: int) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """The company's profile and ACTIVE clause rows, read with Core selects.

    ``executor`` is either the flush ``Connection`` (the freeze listener -- an ORM query on the
    session is not allowed mid-flush) or a ``Session`` (the live draft render). Both selects are
    scoped to ``company_id``.
    """
    profiles = POFlowdownProfile.__table__
    catalog = POQualityClause.__table__
    profile_row = (
        executor.execute(
            select(profiles.c.enabled, profiles.c.heading, profiles.c.document_reference, profiles.c.body).where(
                profiles.c.company_id == company_id
            )
        )
        .mappings()
        .first()
    )
    clause_rows = (
        executor.execute(
            select(
                catalog.c.code,
                catalog.c.title,
                catalog.c.body,
                catalog.c.default_selected,
                catalog.c.is_active,
                catalog.c.sort_order,
            ).where(
                catalog.c.company_id == company_id,
                catalog.c.is_active == True,  # noqa: E712
            )
        )
        .mappings()
        .all()
    )
    return (dict(profile_row) if profile_row is not None else None), [dict(row) for row in clause_rows]


def compute_flowdown_block(executor: Any, company_id: int, selected_codes: Optional[Sequence[str]]) -> Dict[str, Any]:
    """:func:`build_flowdown_block` over the company's current profile and catalog."""
    profile, clauses = load_flowdown_inputs(executor, company_id)
    return build_flowdown_block(profile, clauses, selected_codes)


def is_flowdown_frozen(status: Any) -> bool:
    """A PO's clause selection is frozen once it is no longer a draft."""
    member = po_status_member(status)
    return member is not None and member != POStatus.DRAFT


def print_flowdown_block(db: Session, po: PurchaseOrder, company_id: int) -> Optional[Dict[str, Any]]:
    """The block the PO's printed document carries, or ``None`` for no block.

    * DRAFT: the LIVE block from today's profile and catalog (``company_id`` is the active
      company, which the caller has already matched against the PO).
    * Not DRAFT, snapshot present: the snapshot frozen when it left DRAFT.
    * Not DRAFT, no snapshot (issued before migration 128): no block. Never back-filled.

    A disabled block (``{"enabled": false}``) is also ``None``.
    """
    if is_flowdown_frozen(po.status):
        block = po.flowdown_snapshot
    else:
        block = compute_flowdown_block(db, company_id, po.quality_clause_codes)
    if not isinstance(block, dict) or not block.get("enabled"):
        return None
    return block


# ── A purchase order's clause selection ─────────────────────────────────────


def resolve_clause_codes(db: Session, company_id: int, codes: Optional[Iterable[str]]) -> Optional[List[str]]:
    """Normalize ``codes`` and require each to name an ACTIVE clause of the company (422).

    ``None`` stays ``None`` (follow the catalog defaults); ``[]`` stays ``[]`` (no clauses).
    """
    normalized = normalize_clause_codes(codes)
    if not normalized:
        return normalized
    known = {
        row[0]
        for row in tenant_query(db, POQualityClause, company_id)
        .with_entities(POQualityClause.code)
        .filter(POQualityClause.code.in_(normalized), POQualityClause.is_active == True)  # noqa: E712
        .all()
    }
    unknown = [code for code in normalized if code not in known]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown or retired quality clause code(s): {', '.join(unknown)}. "
                "Use the codes of active clauses from GET /purchasing/quality-clauses."
            ),
        )
    return normalized


def apply_clause_codes_change(db: Session, po: PurchaseOrder, company_id: int, requested: Optional[List[str]]) -> bool:
    """Set ``po.quality_clause_codes`` from an edit; ``True`` when it changed.

    An unchanged value (a client echoing the record back) is a no-op on any PO, so it is never
    refused and never re-validated (a since-retired code it still carries does not 422). A
    change is refused 409 once the PO is no longer a DRAFT -- its block was frozen when it left
    DRAFT -- and otherwise validated (422 naming unknown or retired codes). Call it before the
    first mutation of the edit so a refusal leaves the row untouched.
    """
    normalized = normalize_clause_codes(requested)
    if normalized == po.quality_clause_codes:
        return False
    if is_flowdown_frozen(po.status):
        raise HTTPException(status_code=409, detail=flowdown_frozen_detail(po.po_number))
    po.quality_clause_codes = resolve_clause_codes(db, company_id, normalized)
    return True


# ── Profile ─────────────────────────────────────────────────────────────────


def get_profile(db: Session, company_id: int, *, lock: bool = False) -> Optional[POFlowdownProfile]:
    query = tenant_query(db, POFlowdownProfile, company_id)
    if lock:
        query = query.with_for_update().populate_existing()
    return query.first()


def profile_values(profile: Optional[POFlowdownProfile]) -> Dict[str, Any]:
    """The editable fields, or the unsaved default when the company has no row."""
    if profile is None:
        return {"enabled": False, "heading": DEFAULT_FLOWDOWN_HEADING, "document_reference": None, "body": None}
    return {field: getattr(profile, field) for field in _PROFILE_FIELDS}


def save_profile(
    db: Session, company_id: int, user_id: int, values: Mapping[str, Any], version: int, audit: AuditService
) -> POFlowdownProfile:
    """Create or update the company's profile under a version compare-and-swap.

    ``version`` must be the stored version (0 when the company has no row yet), else 409 and
    nothing changes. A save that changes nothing returns the row untouched (no version bump, no
    audit row). Otherwise the change and its REQUIRED audit row are flushed together; the caller
    owns the commit and translates ``AuditWriteError`` (``app.api.audit_guard.required_audit``).
    """
    acquire_generator_lock(db, "po_flowdown_profile", company_id)
    row = get_profile(db, company_id, lock=True)
    current_version = row.version if row is not None else 0
    if version != current_version:
        raise HTTPException(status_code=409, detail=PROFILE_STALE_DETAIL)
    new_values = {field: values.get(field) for field in _PROFILE_FIELDS}
    if row is None:
        row = POFlowdownProfile(company_id=company_id, version=1, updated_by=user_id, **new_values)
        db.add(row)
        try:
            db.flush()
        except IntegrityError as exc:
            # A concurrent first save (SQLite has no advisory lock to serialize it).
            db.rollback()
            raise HTTPException(status_code=409, detail=PROFILE_STALE_DETAIL) from exc
        audit.log_required(
            AuditService.ACTIONS["CREATE"],
            PROFILE_RESOURCE_TYPE,
            resource_id=row.id,
            resource_identifier="PO quality flow-down",
            description="Created the purchase-order quality flow-down settings",
            new_values={**new_values, "version": row.version},
        )
        return row
    old_values = profile_values(row)
    if old_values == new_values:
        return row
    for field, value in new_values.items():
        setattr(row, field, value)
    row.version = current_version + 1
    row.updated_by = user_id
    db.flush()
    audit.log_update(
        PROFILE_RESOURCE_TYPE,
        row.id,
        "PO quality flow-down",
        old_values={**old_values, "version": current_version},
        new_values={**new_values, "version": row.version},
        description="Updated the purchase-order quality flow-down settings",
        required=True,
    )
    return row


# ── Clause catalog ──────────────────────────────────────────────────────────


def list_clauses(db: Session, company_id: int, *, include_inactive: bool = False) -> List[POQualityClause]:
    query = tenant_query(db, POQualityClause, company_id)
    if not include_inactive:
        query = query.filter(POQualityClause.is_active == True)  # noqa: E712
    return query.order_by(POQualityClause.sort_order, POQualityClause.code, POQualityClause.id).all()


def clause_values(clause: POQualityClause) -> Dict[str, Any]:
    return {field: getattr(clause, field) for field in _CLAUSE_FIELDS}


def _duplicate_code_detail(code: str) -> str:
    return f"A quality clause with code '{code}' already exists. Codes are unique (ignoring case); edit that clause."


def create_clause(
    db: Session, company_id: int, user_id: int, values: Mapping[str, Any], audit: AuditService
) -> POQualityClause:
    """Add a clause to the catalog (409 on a code already used, ignoring case).

    Flushes the row and its REQUIRED audit row; the caller owns the commit.
    """
    code = str(values["code"]).strip()
    duplicate = (
        tenant_query(db, POQualityClause, company_id)
        .with_entities(POQualityClause.id)
        .filter(func.lower(POQualityClause.code) == code.lower())
        .first()
    )
    if duplicate is not None:
        raise HTTPException(status_code=409, detail=_duplicate_code_detail(code))
    now = datetime.utcnow()
    clause = POQualityClause(
        company_id=company_id,
        code=code,
        title=values["title"],
        body=values.get("body"),
        default_selected=bool(values.get("default_selected", False)),
        is_active=bool(values.get("is_active", True)),
        sort_order=int(values.get("sort_order") or 0),
        created_by=user_id,
        updated_by=user_id,
        created_at=now,
        updated_at=now,
    )
    db.add(clause)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=_duplicate_code_detail(code)) from exc
    audit.log_required(
        AuditService.ACTIONS["CREATE"],
        CLAUSE_RESOURCE_TYPE,
        resource_id=clause.id,
        resource_identifier=clause.code,
        description=f"Created quality clause {clause.code}",
        new_values=clause_values(clause),
    )
    return clause


def update_clause(
    db: Session, company_id: int, clause_id: int, user_id: int, changes: Mapping[str, Any], audit: AuditService
) -> POQualityClause:
    """Edit a clause's title, body, default, active flag or sort order (404 if not this company's).

    ``code`` is immutable: echoing the stored code is accepted, a different one is 409. A
    change writes a REQUIRED audit row; an edit that changes nothing writes none. The caller
    owns the commit. Editing a clause never changes a purchase order already issued -- each
    one printed from the snapshot frozen when it left DRAFT.
    """
    clause = (
        tenant_query(db, POQualityClause, company_id)
        .filter(POQualityClause.id == clause_id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if clause is None:
        raise HTTPException(status_code=404, detail="Quality clause not found")
    if "code" in changes and changes["code"] is not None and str(changes["code"]).strip() != clause.code:
        raise HTTPException(status_code=409, detail=CLAUSE_CODE_IMMUTABLE_DETAIL)
    old_values = clause_values(clause)
    for field in _CLAUSE_EDITABLE_FIELDS:
        if field in changes:
            setattr(clause, field, changes[field])
    new_values = clause_values(clause)
    if new_values == old_values:
        return clause
    clause.updated_by = user_id
    clause.updated_at = datetime.utcnow()
    db.flush()
    audit.log_update(
        CLAUSE_RESOURCE_TYPE,
        clause.id,
        clause.code,
        old_values=old_values,
        new_values=new_values,
        description=f"Updated quality clause {clause.code}",
        required=True,
    )
    return clause
