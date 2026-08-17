from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ValidationError
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.db.database import get_db
from app.db.locks import acquire_generator_lock
from app.models.part import Part
from app.models.purchasing import (
    POStatus,
    PurchaseOrder,
    PurchaseOrderLine,
    Vendor,
)
from app.models.user import User, UserRole
from app.schemas.import_kit import PurchaseOrderImportResponse
from app.schemas.purchasing import (
    POCreate,
    POLineCreate,
    POLineResponse,
    POListResponse,
    POResponse,
    POUpdate,
    VendorCreate,
    VendorResponse,
    VendorUpdate,
)
from app.services.audit_service import AuditService
from app.services.import_service import ImportFileError, parse_import_file
from app.services.migration_import_service import import_open_purchase_orders
from app.services.operational_event_service import OperationalEventService

router = APIRouter()

# Ceiling on the rows ``GET /purchasing/purchase-orders`` will materialize. A
# generous bound, not a page size: the default filter already excludes
# closed/cancelled POs, so the live set is the shop's OPEN book -- far below this
# for every tenant. It exists so a pathological caller cannot read the table.
MAX_PO_ROWS = 5_000


class VendorCsvImportError(BaseModel):
    row: int
    code: Optional[str] = None
    name: Optional[str] = None
    reason: str


class VendorCsvImportResponse(BaseModel):
    imported_count: int
    skipped_count: int
    total_rows: int
    created_ids: List[int]
    errors: List[VendorCsvImportError]
    dry_run: bool = False


def _parse_bool(value: str, default: bool = False) -> bool:
    if value == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "y", "approved"}:
        return True
    if normalized in {"false", "0", "no", "n"}:
        return False
    raise ValueError(f"Invalid boolean value '{value}'")


def _parse_int(value: str, field_name: str, default: int = 0) -> int:
    if value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an integer") from exc


def _generate_vendor_code(db: Session, name: str, company_id: int) -> str:
    base = "".join(c for c in name.upper() if c.isalnum())[:3]
    if len(base) < 3:
        base = base.ljust(3, "X")
    # DELIBERATELY counts soft-deleted vendors too -- do not add ``is_deleted == False``.
    # ``uq_vendors_company_code`` is a plain UniqueConstraint on (company_id, code) with no
    # partial predicate, so a deleted vendor still OWNS its code: skipping the tombstones
    # here would mint a code that is already taken and turn a clean insert into an
    # IntegrityError. It would also collide with a later restore, which has no way to
    # renumber. Same reasoning as the duplicate probes in create/update below.
    existing = db.query(Vendor).filter(Vendor.company_id == company_id, Vendor.code.like(f"{base}%")).count()
    return f"{base}{existing + 1:03d}"


# ============ VENDORS ============


def _live_vendor_or_404(db: Session, vendor_id: int, company_id: int) -> Vendor:
    """Resolve a vendor that is workable: this company's, and NOT soft-deleted.

    The vendor twin of ``_live_po_or_404``, and it exists for the same reason: ``PUT`` runs
    a blind setattr loop over the request body, and ``VendorUpdate`` exposes ``is_active``.
    Resolving with ``company_id`` alone therefore let one ``PUT {"is_active": true}`` on a
    deleted vendor produce ``is_deleted=True`` + ``is_active=True`` -- a state nothing in the
    app produces deliberately, and the state that unmasks every read which filters
    ``is_active`` as a PROXY for "removed" (global search, the PO-review typeahead, PO-
    extraction vendor matching, and the MRP auto-PO vendor picker). Those reads now carry a
    real ``is_deleted`` filter of their own, but the reanimation path had to close too: it
    also bypassed ``/restore``, so the audit log recorded an ordinary update rather than the
    ``action="restore"`` row. Nor did it need a crafted request: ``Vendor`` has NO ``version``
    column (``VendorUpdate.version`` is vestigial), so this ``PUT`` carried no optimistic lock
    -- a stale edit tab was sufficient. Admin A opens the edit form, admin B deletes the
    vendor, admin A saves.

    ``DELETE`` and ``POST .../restore`` deliberately do NOT use this -- they need to SEE the
    deleted row, one to refuse a double delete (400 "already deleted"), the other to undo
    one. Neither do the vendor-code duplicate probes: ``uq_vendors_company_code`` spans
    soft-deleted rows, so a deleted vendor must keep blocking its own code (see
    ``_generate_vendor_code``).
    """
    vendor = (
        db.query(Vendor)
        .filter(
            Vendor.id == vendor_id,
            Vendor.company_id == company_id,
            Vendor.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    return vendor


def _vendor_response(
    vendor: Vendor,
    *,
    deleted_view: bool = False,
    deleted_by_name: Optional[str] = None,
) -> VendorResponse:
    """Serialize a Vendor while enforcing the soft-delete provenance tri-state.

    EVERY endpoint with ``response_model=VendorResponse`` must go through here rather than
    returning the ORM row, and the reason is specific to vendors. ``is_deleted`` and
    ``deleted_at`` are REAL COLUMNS on Vendor, and VendorResponse sets ``from_attributes``
    -- so a returned ORM row populates both automatically on every path, and
    ``GET /vendors/{id}`` would answer ``is_deleted: false`` while the default list answers
    ``is_deleted: null``. A shared row renderer that reads "non-null is_deleted" as "this
    came from the restore view" would then offer a Restore control on a LIVE vendor. (The
    PO twin never had to solve this: POListResponse is used by exactly one handler, which
    hand-builds its rows, and PO detail responses use a different schema entirely.)

    So: non-null on the ``deleted_only=true`` view of the list, null everywhere else --
    create, get, update, and the default list alike.

    ``is_active_before_delete`` rides the same rule for the same reason (it is a real
    column too), but note it is NOT a fact about the delete: it is what restore will put
    ``is_active`` back to, which is the one thing a reader needs BEFORE clicking Restore
    and which the row's own ``is_active`` cannot tell them -- the delete forces that False
    on every deleted row. Its None on the deleted view means "deleted before 082", which
    restore resolves as INACTIVE; see restore_vendor.
    """
    row = VendorResponse.model_validate(vendor)
    row.is_deleted = vendor.is_deleted if deleted_view else None
    row.deleted_at = vendor.deleted_at if deleted_view else None
    row.deleted_by_name = deleted_by_name if deleted_view else None
    row.is_active_before_delete = vendor.is_active_before_delete if deleted_view else None
    return row


@router.get("/vendors", response_model=List[VendorResponse])
def list_vendors(
    active_only: bool = True,
    approved_only: bool = False,
    deleted_only: bool = Query(
        False,
        description="Return ONLY soft-deleted vendors (the restore view). Default false = live vendors only.",
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List vendors by name.

    ``deleted_only=true`` inverts the soft-delete filter and returns ONLY the
    company's soft-deleted vendors, each carrying ``is_deleted`` / ``deleted_at`` /
    ``deleted_by_name`` / ``is_active_before_delete`` so the caller can decide whether to
    restore it (via ``POST /vendors/{id}/restore``, ADMIN/MANAGER) AND can see, before
    clicking, whether it will come back active or switched off. Without this a deleted vendor is
    invisible to every API caller and nothing can be restored -- which is exactly the
    gap the vendor-read tightening left behind, since it made a soft-deleted vendor
    unresolvable on the write paths with no way to undo the delete from the UI.

    No extra role gate: this returns rows the same reader could already see before
    they were deleted. Restoring one is the privileged act, and that gate lives on
    the restore verb.
    """
    # Tenancy is unconditional and unchanged -- company_id from get_current_company_id
    # scopes BOTH views. ``deleted_only`` only ever flips the is_deleted predicate; it
    # can never widen the tenant scope. With deleted_only=False this compiles to the
    # same ``vendors.is_deleted = false`` this query has always emitted.
    query = db.query(Vendor).filter(
        Vendor.company_id == company_id,
        Vendor.is_deleted == (True if deleted_only else False),  # noqa: E712
    )

    # ``active_only`` DEFAULTS TO TRUE, and delete_vendor sets ``is_active = False`` on its
    # way out -- so ANDing the two would make the deleted view return an empty list for
    # every caller who did not think to also pass ``active_only=false``, i.e. the restore
    # screen would read "no deleted vendors" no matter how many exist. Do not "tidy" this
    # carve-out away; it is the same species as the closed/cancelled status carve-out on
    # the deleted PO list. There is nothing to preserve here either: while a vendor is
    # deleted its ``is_active`` is False by definition of the delete, so an is_active
    # filter over the deleted set is not merely unhelpful, it is empty by construction.
    # (What the vendor WAS is remembered in ``is_active_before_delete`` and is restore's
    # business, not a filter -- see delete_vendor/restore_vendor.)
    if active_only and not deleted_only:
        query = query.filter(Vendor.is_active == True)

    # ``approved_only`` deliberately KEEPS applying to the deleted view, unlike active_only.
    # It is neither half of the trap: it defaults to False (so it is never silently on) and
    # delete_vendor never touches ``is_approved`` (so it still means what it says on a
    # deleted row, and cannot empty the view behind the caller's back). An explicit
    # ``approved_only=true`` narrowing the restore view is a real question with a real
    # answer -- the same reason ``?status=`` is shared by both views of the PO list.
    if approved_only:
        query = query.filter(Vendor.is_approved == True)

    vendors = query.order_by(Vendor.name).all()

    # Resolve deleted_by -> display name in ONE batched query, and ONLY for the deleted
    # view. SoftDeleteMixin.deleted_by is a bare Integer column with no FK/relationship,
    # so there is nothing to joinedload and the alternative is a lookup per row. On the
    # default path this dict stays empty, NO query is emitted at all, and the loop below
    # reads None out of it -- that is what keeps the unset parameter inert.
    #
    # Not company-scoped, on purpose: deleted_by is whoever's session performed the
    # delete, and a platform admin acting inside this company is not a user row of it, so
    # scoping would blank exactly the name a reader most needs. The id is read off our
    # own already-tenant-scoped row and never comes from the caller, so this cannot be
    # steered into enumerating another tenant's users. It discloses nothing new either:
    # AuditService already snapshots the same actor's full_name onto this tenant's
    # audit_log row for the very same delete.
    #
    # It also deliberately applies NO ``is_active`` / ``is_deleted`` filter to User:
    # provenance must survive the deleter's own departure. Adding one would silently
    # regress the name to "Unknown" for exactly the deletes people ask about most -- the
    # ones done by someone who has since left.
    #
    # User.full_name is a plain Python property, not a mapped column, so it cannot be
    # SELECTed -- pull first_name/last_name and join them here.
    deleted_by_names: dict[int, str] = {}
    if deleted_only:
        deleter_ids = {v.deleted_by for v in vendors if v.deleted_by is not None}
        if deleter_ids:
            for user_id, first_name, last_name in db.query(User.id, User.first_name, User.last_name).filter(
                User.id.in_(deleter_ids)
            ):
                name = f"{first_name or ''} {last_name or ''}".strip()
                if name:
                    deleted_by_names[user_id] = name

    # Serialize through _vendor_response rather than returning the ORM rows: it is what
    # keeps the four provenance fields null on the default path (see its docstring).
    # ``deleted_by_names`` is empty there, so the .get() below is a dict miss, not a query.
    return [
        _vendor_response(
            vendor,
            deleted_view=deleted_only,
            deleted_by_name=deleted_by_names.get(vendor.deleted_by),
        )
        for vendor in vendors
    ]


@router.post("/vendors", response_model=VendorResponse)
def create_vendor(
    vendor_in: VendorCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create a vendor. `code` must be unique within the company (400 "Vendor code already exists").
    Writes a tamper-evident audit_log CREATE row for the new vendor."""
    # DELIBERATELY sees soft-deleted vendors -- do not add ``is_deleted == False``, for two
    # independent reasons. (1) ``uq_vendors_company_code`` has no partial predicate, so it
    # spans deleted rows: filtering here would push a clean 400 down into the IntegrityError
    # backstop below (a rollback, and a 500 the day anyone removes that backstop). (2) If a
    # deleted vendor's code were reusable, someone takes it, and ``POST /vendors/{id}/restore``
    # then resurrects a row that violates the constraint -- restore has no collision check and
    # cannot be given one after the fact.
    existing = db.query(Vendor).filter(Vendor.code == vendor_in.code, Vendor.company_id == company_id).first()
    if existing:
        raise HTTPException(status_code=400, detail="Vendor code already exists")

    vendor = Vendor(**vendor_in.model_dump())
    vendor.company_id = company_id
    if vendor.is_approved:
        vendor.approval_date = date.today()
    db.add(vendor)
    try:
        db.flush()
        audit.log_create("vendor", vendor.id, vendor.code, new_values=vendor)
        db.commit()
    except IntegrityError as exc:
        # TOCTOU backstop: a concurrent create can slip past the pre-insert probe;
        # uq_vendors_company_code catches it at commit -- surface the same 400.
        db.rollback()
        raise HTTPException(status_code=400, detail="Vendor code already exists") from exc
    db.refresh(vendor)
    # Through _vendor_response, not the bare ORM row -- see its docstring: returning
    # ``vendor`` here would ship ``is_deleted: false`` where the list ships ``null``.
    return _vendor_response(vendor)


@router.post("/vendors/import-csv", response_model=VendorCsvImportResponse)
async def import_vendors_csv(
    request: Request,
    file: UploadFile = File(...),
    dry_run: bool = Query(False, description="Validate only; no rows are written"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
):
    """Import vendor master records from CSV or XLSX with row-level errors."""
    content = await file.read()
    # Parse + import are CPU/DB-bound sync work; run them in the threadpool so a
    # large upload can't stall the event loop (the request-scoped Session/audit
    # are used sequentially from one worker thread — same as a sync endpoint).
    try:
        table = await run_in_threadpool(parse_import_file, file.filename, content, required_columns={"name"})
    except ImportFileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _run_import() -> VendorCsvImportResponse:
        # DELIBERATELY includes soft-deleted vendors' codes: the bulk form of the create
        # probe above, against the same tombstone-spanning unique constraint. An import row
        # reusing a deleted vendor's code must fail as a duplicate, or it writes a row that
        # cannot coexist with a restore of that vendor.
        existing_codes = {
            (value or "").strip().upper()
            for (value,) in db.query(Vendor.code).filter(Vendor.company_id == company_id).all()
        }

        audit = AuditService(db, current_user, request)
        errors: List[VendorCsvImportError] = []
        created_ids: List[int] = []
        total_rows = 0
        accepted_count = 0

        for row_number, row in table.iter_rows():
            total_rows += 1
            name = row.get("name", "")
            code = (row.get("code") or "").upper()
            code_was_provided = bool(code)

            try:
                if not name:
                    raise ValueError("name is required")
                if not code:
                    code = _generate_vendor_code(db, name, company_id)
                if code in existing_codes:
                    raise ValueError("Vendor code already exists")

                vendor_in = VendorCreate(
                    code=code,
                    name=name,
                    contact_name=row.get("contact_name") or None,
                    email=row.get("email") or None,
                    phone=row.get("phone") or None,
                    address_line1=row.get("address_line1") or None,
                    address_line2=row.get("address_line2") or None,
                    city=row.get("city") or None,
                    state=(row.get("state") or "").upper() or None,
                    postal_code=row.get("postal_code") or row.get("zip_code") or None,
                    country=row.get("country") or "US",
                    payment_terms=row.get("payment_terms") or None,
                    lead_time_days=_parse_int(row.get("lead_time_days", ""), "lead_time_days", 14),
                    is_approved=_parse_bool(row.get("is_approved", ""), False),
                    is_as9100_certified=_parse_bool(row.get("is_as9100_certified", ""), False),
                    is_iso9001_certified=_parse_bool(row.get("is_iso9001_certified", ""), False),
                    notes=row.get("notes") or None,
                )
                is_active = _parse_bool(row.get("is_active", ""), True)
            except (ValueError, ValidationError) as exc:
                errors.append(
                    VendorCsvImportError(
                        row=row_number,
                        code=code or None,
                        name=name or None,
                        reason=str(exc),
                    )
                )
                continue

            if dry_run:
                # Generated codes are only reserved at commit; don't let a
                # would-be-generated code trip the in-file duplicate check.
                if code_was_provided:
                    existing_codes.add(code.upper())
                accepted_count += 1
                continue

            try:
                vendor = Vendor(**vendor_in.model_dump())
                vendor.company_id = company_id
                vendor.is_active = is_active
                if vendor.is_approved:
                    vendor.approval_date = date.today()
                db.add(vendor)
                db.flush()
                audit.log_create("vendor", vendor.id, vendor.code, new_values=vendor, extra_data={"source": "import"})
                db.commit()
                db.refresh(vendor)
            except Exception as exc:
                db.rollback()
                errors.append(VendorCsvImportError(row=row_number, code=code, name=name, reason=str(exc)))
                continue

            existing_codes.add(vendor.code.upper())
            created_ids.append(vendor.id)
            accepted_count += 1

        return VendorCsvImportResponse(
            imported_count=accepted_count,
            skipped_count=len(errors),
            total_rows=total_rows,
            created_ids=created_ids,
            errors=errors,
            dry_run=dry_run,
        )

    return await run_in_threadpool(_run_import)


@router.get("/vendors/{vendor_id}", response_model=VendorResponse)
def get_vendor(
    vendor_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    # Through _vendor_response, not the bare ORM row -- see its docstring.
    return _vendor_response(_live_vendor_or_404(db, vendor_id, company_id))


@router.put("/vendors/{vendor_id}", response_model=VendorResponse)
def update_vendor(
    vendor_id: int,
    vendor_in: VendorUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Update a vendor. `code` is editable: normalized to uppercase, must stay unique within the
    company (400 "Vendor code already exists"), and cannot be blanked (explicit JSON null -> 400,
    empty/whitespace string -> 422 at the schema). An update that changes fields writes an
    audit_log row; a no-change PUT writes none. 404s on a soft-deleted vendor -- see
    ``_live_vendor_or_404``: editing one (this setattr loop includes ``is_active``) is how a
    deleted vendor came back to life outside ``/restore``."""
    vendor = _live_vendor_or_404(db, vendor_id, company_id)

    update_data = vendor_in.model_dump(exclude_unset=True)

    # Vendor code is editable but never blankable: normalize like the CSV import path,
    # reject an explicit null/blank, and enforce per-company uniqueness on change.
    if "code" in update_data:
        new_code = (update_data["code"] or "").strip().upper()
        if not new_code:
            raise HTTPException(status_code=400, detail="Vendor code cannot be blank")
        update_data["code"] = new_code
        if new_code != vendor.code:
            # Case-insensitive probe: a legacy lowercase row must also block a rename to its
            # uppercase twin (the PO-import vendor matcher resolves codes case-insensitively).
            #
            # DELIBERATELY sees soft-deleted vendors, unlike the row resolution above -- same
            # tombstone-spanning constraint as the create probe: a rename must not take a code
            # a deleted vendor still holds, or restoring that vendor violates
            # ``uq_vendors_company_code``. Resolution filters, the duplicate probe does not;
            # fixing "both" is the trap here.
            duplicate = (
                db.query(Vendor)
                .filter(
                    Vendor.company_id == company_id,
                    func.upper(Vendor.code) == new_code,
                    Vendor.id != vendor_id,
                )
                .first()
            )
            if duplicate:
                raise HTTPException(status_code=400, detail="Vendor code already exists")

    # Snapshot BEFORE mutating; column-only, so the vestigial VendorUpdate.version
    # (Vendor has no version column) never enters the audited changes diff.
    old_values = {c.key: getattr(vendor, c.key) for c in vendor.__table__.columns}

    # Set approval date if being approved
    if update_data.get("is_approved") and not vendor.is_approved:
        vendor.approval_date = date.today()

    for field, value in update_data.items():
        setattr(vendor, field, value)

    audit.log_update("vendor", vendor.id, vendor.code, old_values=old_values, new_values=vendor)
    try:
        db.commit()
    except IntegrityError as exc:
        # TOCTOU backstop: a concurrent writer can slip a duplicate code past the probe;
        # uq_vendors_company_code catches it at commit -- surface the same 400 as the probe.
        db.rollback()
        raise HTTPException(status_code=400, detail="Vendor code already exists") from exc
    db.refresh(vendor)
    # Through _vendor_response, not the bare ORM row -- see its docstring.
    return _vendor_response(vendor)


@router.delete("/vendors/{vendor_id}")
def delete_vendor(
    vendor_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
):
    """Soft delete a vendor (compliance invariant #3 -- no hard delete). Refuses while any
    live (not closed/cancelled) purchase order still references the vendor."""
    # Raw lookup (NOT ``_live_vendor_or_404``): this verb must SEE an already-deleted row so
    # the re-delete guard below can answer 400 "already deleted". Filtering here would turn
    # that guard into a bare 404 and let a double DELETE reset deleted_at/deleted_by.
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id, Vendor.company_id == company_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    # Re-delete guard (symmetric with restore_vendor's "not deleted" check): a double
    # DELETE must not reset deleted_at/deleted_by or write a duplicate audit row.
    if vendor.is_deleted:
        raise HTTPException(status_code=400, detail=f"Vendor {vendor.name} is already deleted")

    # Guardrail: don't orphan open purchasing activity behind a deleted vendor.
    active_po_count = (
        db.query(PurchaseOrder)
        .filter(
            PurchaseOrder.vendor_id == vendor.id,
            PurchaseOrder.company_id == company_id,
            PurchaseOrder.is_deleted == False,  # noqa: E712
            PurchaseOrder.status.not_in([POStatus.CLOSED, POStatus.CANCELLED]),
        )
        .count()
    )
    if active_po_count > 0:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot delete vendor {vendor.name}: {active_po_count} active purchase order(s) "
                "reference it. Close or cancel them first."
            ),
        )

    audit = AuditService(db, current_user, request)

    vendor.soft_delete(current_user.id)

    # ORDER IS LOAD-BEARING: capture the CURRENT is_active FIRST, then force it False.
    # Reversed, the sidecar records the value the very next line is about to write and
    # every restore reactivates -- the bug this column exists to prevent. See
    # Vendor.is_active_before_delete and restore_vendor.
    #
    # The ``is_active = False`` write STAYS. Not writing it (so restore would have nothing
    # to put back) is the tempting shortcut and is a security regression: six read paths
    # filter is_active, it is a deliberate second layer behind the is_deleted filters, and
    # dropping it would silently change what "deleted" means to any query added later.
    #
    # Known, accepted imprecision: ``Vendor.is_active`` is itself NULLABLE, so a row whose
    # is_active is NULL records NULL here -- which restore cannot tell from "we never
    # recorded one" and therefore restores as INACTIVE. That errs the SAFE way (off, not on),
    # and the row is hard to reach besides: the ORM re-applies the column default even when
    # is_active is explicitly assigned None, so only a raw/Core insert passing an explicit
    # NULL produces one. It is stated here because it is the reason this sentinel must never
    # be "tidied" into a NOT NULL column: NULL has to stay reachable to mean "deleted before
    # 082 shipped".
    vendor.is_active_before_delete = vendor.is_active
    vendor.is_active = False

    # Log BEFORE the terminal commit so the audit row commits atomically with the
    # delete (AuditService.log only flushes; get_db never commits).
    audit.log_delete("vendor", vendor.id, vendor.name, soft_delete=True)
    db.commit()
    return {"message": f"Vendor {vendor.name} deleted", "can_restore": True}


@router.post("/vendors/{vendor_id}/restore", summary="Restore a soft-deleted vendor")
def restore_vendor(
    vendor_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
):
    """Restore a soft-deleted vendor, PRESERVING the is_active state it had when it was
    deleted. Raw lookup (NOT ``_live_vendor_or_404``) so it can see the soft-deleted row --
    seeing the tombstone is this verb's entire job.

    Why the preserve, rather than the unconditional ``is_active = True`` this used to do:
    an approved-supplier list is an AS9100D-controlled artifact. A supplier the shop
    deliberately DEACTIVATED -- lapsed certification, a quality escape, a commercial
    dispute -- and then deleted must not come back looking like an active, selectable
    supplier just because somebody undid the delete. Undoing a delete restores a RECORD; it
    is not an approval decision, and it must not silently make one. Re-activating a
    recovered supplier stays a deliberate, separately audited ``PUT /vendors/{id}``.

    ``delete_vendor`` records the pre-delete value in ``is_active_before_delete``; NULL means
    the delete predates that column (migration 082), and by OWNER DECISION that unknown
    restores INACTIVE -- it falls back to False, NOT to the pre-082 unconditional True. On an
    AS9100D-controlled approved-supplier list the safe unknown is OFF: for a legacy row the
    system genuinely does not know whether the shop had switched the vendor off before
    deleting it, so it comes back switched off and a human reactivates it deliberately,
    through the separately audited ``PUT /vendors/{id}``. That is a DELIBERATE BREAK from the
    old behavior -- do not "preserve backward compatibility" by flipping it back. Coming back
    inactive is recoverable by an explicit, audited decision; coming back wrongly active is
    not detectable at all. The sidecar is CLEARED here so a later delete/restore cycle can
    never read this one's stale value.

    Refuses nothing beyond the "not deleted" guard, deliberately -- see the comment at the
    guard below.
    """
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id, Vendor.company_id == company_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    if not vendor.is_deleted:
        raise HTTPException(status_code=400, detail="Vendor is not deleted")

    # No dependency refusal here, unlike restore_purchase_order (which refuses a PO whose
    # vendor is itself deleted). A vendor is a ROOT record: it references nothing but its
    # company, so there is no parent that could have gone away underneath it, and nothing
    # it points at can be in a state that makes it invalid to bring back. The one collision
    # a restore could plausibly hit -- another vendor having taken its ``code`` while it was
    # deleted -- is already impossible upstream by design, not by luck: create's probe,
    # update's rename probe and ``_generate_vendor_code`` all DELIBERATELY count
    # soft-deleted rows, because ``uq_vendors_company_code`` has no partial predicate and a
    # deleted vendor still owns its code. Keep those three that way and this verb needs no
    # collision check (it could not be given a sensible one anyway -- it has no way to
    # renumber). A restore whose vendor was deactivated is likewise not a refusal: it is
    # exactly the case the is_active preservation above handles.
    audit = AuditService(db, current_user, request)

    # COALESCE(is_active_before_delete, False) -- see the docstring for why the NULL branch
    # is False and not the pre-082 True. The prior is_active of a vendor deleted before 082
    # is genuinely unknown: the delete overwrote it in place, and the audit_log delete row
    # records the deletion, not the flag. Resolving that unknown to ON would fabricate
    # supplier-approval state; resolving it to OFF asks a human to make the call.
    restored_is_active = False if vendor.is_active_before_delete is None else vendor.is_active_before_delete

    # SNAPSHOT the prior is_active; do NOT hard-code False here even though delete_vendor
    # always writes False. Pre-#231 a single ``PUT {"is_active": true}`` on a soft-deleted
    # vendor produced ``is_deleted=True`` + ``is_active=True`` (no optimistic lock, so a
    # stale edit tab sufficed -- see _live_vendor_or_404). That path is closed, but rows
    # already in that state were NOT repaired, and restoring one really does move
    # is_active True -> False. Asserting False would record that move as a no-op in the
    # tamper-evident audit_log, and AuditService._get_changes would omit is_active from
    # the diff entirely -- the one column a reader would ask about. One local is cheaper
    # than an audit row that can lie. (``is_deleted: True`` stays hard-coded: the
    # "not deleted -> 400" guard at the top of this verb proves it.)
    previous_is_active = vendor.is_active

    vendor.restore()
    vendor.is_active = restored_is_active
    # Clear the sidecar: it is only meaningful while the row is deleted. Left set, a vendor
    # that is deleted again and restored again would read THIS cycle's value if some future
    # edit ever skipped the delete-side capture.
    vendor.is_active_before_delete = None

    # Log BEFORE the terminal commit so the audit row commits atomically with the
    # restore (AuditService.log only flushes; get_db never commits).
    #
    # ``is_active`` is in the diff on purpose (the PO twin logs is_deleted alone, because
    # its restore touches nothing else): this verb DOES write an approval-relevant flag, and
    # the whole point of preserving it is that the value matters. Leaving it out would make
    # the one column a reader would ask about the one column the restore row does not
    # record -- and the OLD half is READ off the row (``previous_is_active`` above) rather
    # than assumed to be False, so the diff cannot understate a real change.
    audit.log_update(
        "vendor",
        vendor.id,
        vendor.name,
        old_values={"is_deleted": True, "is_active": previous_is_active},
        new_values={"is_deleted": False, "is_active": restored_is_active},
        action="restore",
    )
    db.commit()

    return {"message": f"Vendor {vendor.name} restored"}


# ============ PURCHASE ORDERS ============


def generate_po_number(db: Session, company_id: int = None) -> str:
    """Generate next PO number (PO-YYYYMMDD-XXX).

    Holds an advisory lock so concurrent creates can't collide.
    """
    acquire_generator_lock(db, "po_number", company_id)

    today = datetime.now().strftime("%Y%m%d")
    prefix = f"PO-{today}-"

    query = db.query(PurchaseOrder).filter(PurchaseOrder.po_number.like(f"{prefix}%"))
    if company_id is not None:
        query = query.filter(PurchaseOrder.company_id == company_id)
    last_po = query.order_by(PurchaseOrder.po_number.desc()).first()

    if last_po:
        last_num = int(last_po.po_number.split("-")[-1])
        new_num = last_num + 1
    else:
        new_num = 1

    return f"{prefix}{new_num:03d}"


def _live_po_or_404(db: Session, po_id: int, company_id: int) -> PurchaseOrder:
    """Resolve a purchase order that is workable: this company's, and NOT soft-deleted.

    Every PO *write* verb except delete/restore resolves through here, so a soft-deleted
    PO 404s on all of them. That is what makes the archive an archive: a deleted PO is a
    RECORD, not a workable order, and the two ways it could stop being one are editing it
    (``PUT``, whose blind setattr loop includes ``status``) and issuing it to a vendor
    (``/send``, ``/lines``). Until the Deleted view shipped, those ids were effectively
    unobtainable -- the reads all filter ``is_deleted`` -- so the gap was theoretical; the
    restore view hands the ids to any authenticated reader, including roles deliberately
    kept below ``require_role([ADMIN, MANAGER])`` on restore, which is what makes it real.

    ``DELETE`` and ``POST .../restore`` deliberately do NOT use this (they need to see the
    deleted row: one to refuse a double delete, the other to undo one), and neither does
    ``GET .../{po_id}``, which already carries its own identical filter.
    """
    po = (
        db.query(PurchaseOrder)
        .filter(
            PurchaseOrder.id == po_id,
            PurchaseOrder.company_id == company_id,
            PurchaseOrder.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    return po


@router.get("/purchase-orders", response_model=List[POListResponse])
def list_purchase_orders(
    status: Optional[str] = None,
    vendor_id: Optional[int] = None,
    deleted_only: bool = Query(
        False,
        description="Return ONLY soft-deleted purchase orders (the restore view). Default false = live POs only.",
    ),
    limit: int = Query(MAX_PO_ROWS, ge=1, le=MAX_PO_ROWS),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List purchase orders, newest first.

    Open POs only (closed and cancelled are excluded) unless ``status`` is
    given. Returns at most ``limit`` POs starting at ``offset``, each with a
    ``line_count``; rows past the limit are not returned, so page through them
    with ``offset``.

    ``deleted_only=true`` inverts the soft-delete filter and returns ONLY the
    company's soft-deleted POs, each carrying ``is_deleted`` / ``deleted_at`` /
    ``deleted_by_name`` so the caller can decide whether to restore it (via
    ``POST /purchase-orders/{id}/restore``, ADMIN/MANAGER). Without this a deleted
    PO is invisible to every API caller and nothing can be restored.

    No extra role gate: this returns rows the same reader could already see before
    they were deleted. Restoring one is the privileged act, and that gate lives on
    the restore verb.
    """
    # The default IS the ceiling (MAX_PO_ROWS): a zero-argument caller receives
    # the whole set exactly as it did before the cap existed.
    #
    # `ge=1` so a negative value cannot reach `.limit()` -- PostgreSQL rejects a
    # negative LIMIT and SQLite silently treats it as "unbounded"; `ge=0` on the
    # offset because PostgreSQL likewise rejects a negative OFFSET.
    # ``line_count`` is the only thing this response needs from the lines, so it
    # comes back as a correlated COUNT rather than a ``selectinload``. The eager
    # load hydrated every line ORM object of every PO purely to call ``len()`` on
    # them -- 5k open POs x 8 lines is 40k objects to emit 5k integers.
    # PurchaseOrderLine carries TenantMixin but NOT SoftDeleteMixin (see
    # app/models/purchasing.py), so COUNT(*) and len(po.lines) are exactly equal;
    # there is no soft-deleted line for the count to disagree about.
    line_count_sq = (
        db.query(func.count(PurchaseOrderLine.id))
        .filter(PurchaseOrderLine.purchase_order_id == PurchaseOrder.id)
        .correlate(PurchaseOrder)
        .scalar_subquery()
        .label("line_count")
    )

    # Tenancy is unconditional and unchanged -- company_id from get_current_company_id
    # scopes BOTH views. ``deleted_only`` only ever flips the is_deleted predicate; it
    # can never widen the tenant scope.
    query = (
        db.query(PurchaseOrder, line_count_sq)
        .filter(
            PurchaseOrder.company_id == company_id,
            PurchaseOrder.is_deleted == (True if deleted_only else False),  # noqa: E712
        )
        .options(
            # The joined vendor deliberately carries NO ``is_deleted`` predicate: a PO's
            # vendor is the historical record of who the order was placed with, and
            # delete_vendor permits deleting a vendor once its POs are closed/cancelled -- so
            # a closed PO legitimately points at a deleted vendor and must still render its
            # name. Blanking it would erase the traceability the record exists for. The
            # live-vendor gate belongs on the WRITE verbs (create_purchase_order, and
            # _live_vendor_or_404 on the vendor itself).
            joinedload(PurchaseOrder.vendor)
        )
    )

    if status:
        query = query.filter(PurchaseOrder.status == status)
    elif not deleted_only:
        # Default: exclude closed/cancelled
        #
        # DELIBERATELY SKIPPED on the deleted view -- do not "tidy" this back into a
        # plain ``else``. A soft-deleted PO can sit in ANY status, and a CANCELLED-then-
        # deleted PO is one of the likeliest things somebody wants back. Applying this
        # exclusion here would hide those rows from the ONLY list that can see them, so
        # the restore control could never be offered for them and nothing else in the
        # API would reach them either. An explicit ``?status=`` still narrows the
        # deleted view -- that is the branch above, which both views share.
        query = query.filter(PurchaseOrder.status.not_in([POStatus.CLOSED, POStatus.CANCELLED]))

    if vendor_id:
        query = query.filter(PurchaseOrder.vendor_id == vendor_id)

    rows = query.order_by(PurchaseOrder.created_at.desc()).offset(offset).limit(limit).all()

    # Resolve deleted_by -> display name in ONE batched query, and ONLY for the deleted
    # view. SoftDeleteMixin.deleted_by is a bare Integer column with no FK/relationship,
    # so there is nothing to joinedload and the alternative is a lookup per row. On the
    # default path this dict stays empty, no query is emitted, and the loop below reads
    # None out of it -- that is what keeps the unset parameter inert.
    #
    # Not company-scoped, on purpose: deleted_by is whoever's session performed the
    # delete, and a platform admin acting inside this company is not a user row of it, so
    # scoping would blank exactly the name a reader most needs. The id is read off our
    # own already-tenant-scoped row and never comes from the caller, so this cannot be
    # steered into enumerating another tenant's users. It discloses nothing new either:
    # AuditService already snapshots the same actor's full_name onto this tenant's
    # audit_log row for the very same delete.
    #
    # It also deliberately applies NO ``is_active`` / ``is_deleted`` filter to User:
    # provenance must survive the deleter's own departure. Adding one would silently
    # regress the name to "Unknown" for exactly the deletes people ask about most -- the
    # ones done by someone who has since left.
    deleted_by_names: dict[int, str] = {}
    if deleted_only:
        deleter_ids = {po.deleted_by for po, _ in rows if po.deleted_by is not None}
        if deleter_ids:
            for user_id, first_name, last_name in db.query(User.id, User.first_name, User.last_name).filter(
                User.id.in_(deleter_ids)
            ):
                name = f"{first_name or ''} {last_name or ''}".strip()
                if name:
                    deleted_by_names[user_id] = name

    result = []
    for po, line_count in rows:
        result.append(
            POListResponse(
                id=po.id,
                po_number=po.po_number,
                vendor_id=po.vendor_id,
                vendor_name=po.vendor.name if po.vendor else None,
                status=po.status.value if hasattr(po.status, "value") else po.status,
                order_date=po.order_date,
                required_date=po.required_date,
                total=po.total,
                line_count=line_count,
                created_at=po.created_at,
                # Left at their None defaults on the default path -- see POListResponse.
                is_deleted=po.is_deleted if deleted_only else None,
                deleted_at=po.deleted_at if deleted_only else None,
                deleted_by_name=(deleted_by_names.get(po.deleted_by) if deleted_only else None),
            )
        )
    return result


@router.post("/purchase-orders", response_model=POResponse)
def create_purchase_order(
    po_in: POCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create a purchase order with its lines. Writes one tamper-evident audit_log CREATE
    row for the PO (line_count in extra_data; no per-line rows for document creation)."""
    # Verify vendor -- must be a live, active vendor (can't open a PO against a
    # deleted or deactivated supplier).
    vendor = (
        db.query(Vendor)
        .filter(
            Vendor.id == po_in.vendor_id,
            Vendor.company_id == company_id,
            Vendor.is_deleted == False,  # noqa: E712
            Vendor.is_active == True,  # noqa: E712
        )
        .first()
    )
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    po_number = generate_po_number(db, company_id)

    po = PurchaseOrder(
        po_number=po_number,
        vendor_id=po_in.vendor_id,
        required_date=po_in.required_date,
        expected_date=po_in.expected_date,
        ship_to=po_in.ship_to,
        shipping_method=po_in.shipping_method,
        notes=po_in.notes,
        created_by=current_user.id,
    )
    po.company_id = company_id
    db.add(po)
    try:
        db.flush()
    except IntegrityError as exc:
        # Backstop for a duplicate po_number surfacing at the header flush. The
        # advisory lock in generate_po_number already serializes same-company
        # creates on Postgres — this is the SQLite/edge backstop (400, not 500).
        db.rollback()
        raise HTTPException(status_code=400, detail=f"PO number '{po_number}' already exists") from exc

    # Add lines
    subtotal = 0.0
    for idx, line_data in enumerate(po_in.lines, 1):
        part = db.query(Part).filter(Part.id == line_data.part_id, Part.company_id == company_id).first()
        if not part:
            raise HTTPException(status_code=404, detail=f"Part {line_data.part_id} not found")

        # quantity_ordered/unit_price parse as Decimal (Money schema types) but the
        # PO money columns are Float — coerce so `subtotal += line_total` and the
        # `subtotal + po.tax + po.shipping` total below don't mix Decimal with float.
        line_total = float(line_data.quantity_ordered) * float(line_data.unit_price)
        line = PurchaseOrderLine(
            purchase_order_id=po.id,
            line_number=idx,
            part_id=line_data.part_id,
            quantity_ordered=line_data.quantity_ordered,
            unit_price=line_data.unit_price,
            line_total=line_total,
            required_date=line_data.required_date or po_in.required_date,
            notes=line_data.notes,
        )
        line.company_id = company_id
        db.add(line)
        subtotal += line_total

    po.subtotal = subtotal
    po.total = subtotal + po.tax + po.shipping

    db.flush()
    audit.log_create(
        "purchase_order",
        po.id,
        po.po_number,
        new_values=po,
        extra_data={"vendor_code": vendor.code, "line_count": len(po_in.lines)},
    )
    OperationalEventService(db).emit_best_effort(
        company_id=company_id,
        event_type="purchase_order_created",
        source_module="purchasing",
        entity_type="purchase_order",
        entity_id=po.id,
        user_id=current_user.id,
        severity="info",
        event_payload={
            "po_number": po.po_number,
            "vendor_id": po.vendor_id,
            "vendor_name": vendor.name,
            "line_count": len(po_in.lines),
            "required_date": po.required_date.isoformat() if po.required_date else None,
            "total": float(po.total or 0),
        },
    )
    db.commit()
    db.refresh(po)
    return po


@router.post(
    "/purchase-orders/import",
    response_model=PurchaseOrderImportResponse,
    summary="Import open purchase orders (CSV/XLSX)",
)
async def import_open_purchase_orders_endpoint(
    file: UploadFile = File(...),
    dry_run: bool = Query(False, description="Validate and preview only; guarantees no rows are written"),
    db: Session = Depends(get_db),
    # ADMIN/MANAGER only: imported POs land directly in SENT (issued), and the
    # interactive /send transition is ADMIN/MANAGER-only — allowing SUPERVISOR
    # here would let them issue POs via spreadsheet that they cannot issue in
    # the UI (privilege escalation).
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Import OPEN (issued, not yet received) purchase orders for the Excel go-live migration.

    Columns: ``po_number`` (optional — rows sharing one become lines of a
    single PO; generated when blank), ``vendor_code`` (must exist),
    ``part_number`` (must exist), ``quantity``, ``unit_price``,
    ``promised_date`` (optional). POs are created in ``sent`` (issued) status
    so receiving can act on them immediately. Use ``dry_run=true`` to preview
    without writing.
    """
    content = await file.read()
    # Parse + import are CPU/DB-bound sync work; run them in the threadpool so a
    # large upload can't stall the event loop (the request-scoped Session/audit
    # are used sequentially from one worker thread — same as a sync endpoint).
    try:
        table = await run_in_threadpool(
            parse_import_file,
            file.filename,
            content,
            required_columns={"vendor_code", "part_number", "quantity", "unit_price"},
        )
    except ImportFileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return await run_in_threadpool(
        import_open_purchase_orders,
        db,
        table=table,
        current_user=current_user,
        company_id=company_id,
        audit=audit,
        dry_run=dry_run,
    )


@router.get("/purchase-orders/{po_id}", response_model=POResponse)
def get_purchase_order(
    po_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    po = (
        db.query(PurchaseOrder)
        .options(
            # Vendor deliberately unfiltered on soft delete -- see list_purchase_orders: the
            # order names the supplier it was placed with, deleted or not.
            joinedload(PurchaseOrder.vendor),
            joinedload(PurchaseOrder.lines).joinedload(PurchaseOrderLine.part),
        )
        .filter(
            PurchaseOrder.id == po_id,
            PurchaseOrder.company_id == company_id,
            PurchaseOrder.is_deleted == False,  # noqa: E712
        )
        .first()
    )

    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    return po


@router.put("/purchase-orders/{po_id}", response_model=POResponse)
def update_purchase_order(
    po_id: int,
    po_in: POUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Update a purchase order. Writes a tamper-evident audit_log UPDATE row with the
    changes diff (a status change shows up in the diff; no row when nothing changed).

    404s on a soft-deleted PO -- see ``_live_po_or_404``. Also refuses (400) a ``status``
    change that would make the order LIVE again while its vendor is soft-deleted -- see the
    guard below."""
    po = _live_po_or_404(db, po_id, company_id)

    # The third door into "a live PO against a removed supplier". ``create_purchase_order``
    # and ``create_po_from_upload`` both refuse to CREATE that state, and
    # ``restore_purchase_order`` refuses to bring it back -- but this verb can REVIVE it, and
    # nothing here validates the transition at all: ``POStatus(value)`` only checks enum
    # membership, so a CLOSED PO takes ``{"status": "sent"}`` and lands straight back in
    # ``GET /receiving/open-pos`` (which filters status and PO is_deleted, and deliberately
    # never checks the vendor). ``delete_vendor``'s "no live POs" check is point-in-time:
    # it passed when every PO was closed, and nothing re-evaluates it afterwards.
    #
    # Only revivals are refused. CLOSED/CANCELLED are terminal, so moving a PO INTO one of
    # them stays allowed -- closing out a removed supplier's paperwork must never be blocked.
    # Message mirrors ``restore_purchase_order`` so the two read identically, and it is
    # raised before the first setattr so a refusal leaves the row untouched.
    new_status = po_in.model_dump(exclude_unset=True).get("status")
    if new_status is not None and POStatus(new_status) not in (POStatus.CLOSED, POStatus.CANCELLED):
        if po.vendor is not None and po.vendor.is_deleted:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Cannot set purchase order {po.po_number} to '{POStatus(new_status).value}': its vendor "
                    f"{po.vendor.name} is deleted. Restore the vendor first."
                ),
            )

    # Snapshot BEFORE mutating; column-only, so the vestigial POUpdate.version
    # (PurchaseOrder has no version column) never enters the audited changes diff.
    old_values = {c.key: getattr(po, c.key) for c in po.__table__.columns}

    previous_status = po.status
    update_data = po_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        if field == "status":
            setattr(po, field, POStatus(value))
        else:
            setattr(po, field, value)

    OperationalEventService(db).emit_best_effort(
        company_id=company_id,
        event_type="purchase_order_updated",
        source_module="purchasing",
        entity_type="purchase_order",
        entity_id=po.id,
        user_id=current_user.id,
        severity="info" if po.status == previous_status else "medium",
        event_payload={
            "po_number": po.po_number,
            "changed_fields": [field for field in update_data.keys() if field != "version"],
            "previous_status": (previous_status.value if hasattr(previous_status, "value") else previous_status),
            "status": po.status.value if hasattr(po.status, "value") else po.status,
            "required_date": po.required_date.isoformat() if po.required_date else None,
            "expected_date": po.expected_date.isoformat() if po.expected_date else None,
        },
    )
    # Audit BEFORE the terminal commit so the audit row commits atomically with the
    # change -- AuditService.log() only flushes; the session never commits on teardown.
    db.flush()
    audit.log_update("purchase_order", po.id, po.po_number, old_values=old_values, new_values=po)
    db.commit()
    db.refresh(po)
    return po


@router.post("/purchase-orders/{po_id}/send")
def send_purchase_order(
    po_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Issue a draft/approved PO to the vendor (status -> sent, stamps order_date).
    Writes a tamper-evident audit_log STATUS_CHANGE row.

    404s on a soft-deleted PO -- see ``_live_po_or_404``. Mailing a deleted order to a
    vendor is the worst thing this router could do."""
    po = _live_po_or_404(db, po_id, company_id)

    if po.status not in [POStatus.DRAFT, POStatus.APPROVED]:
        raise HTTPException(status_code=400, detail="Can only send draft or approved POs")

    old_status = po.status.value if hasattr(po.status, "value") else po.status
    po.status = POStatus.SENT
    po.order_date = date.today()
    OperationalEventService(db).emit_best_effort(
        company_id=company_id,
        event_type="purchase_order_sent",
        source_module="purchasing",
        entity_type="purchase_order",
        entity_id=po.id,
        user_id=current_user.id,
        severity="info",
        event_payload={
            "po_number": po.po_number,
            "vendor_id": po.vendor_id,
            "order_date": po.order_date.isoformat() if po.order_date else None,
            "required_date": po.required_date.isoformat() if po.required_date else None,
        },
    )
    db.flush()
    audit.log_status_change(
        "purchase_order",
        po.id,
        po.po_number,
        old_status=old_status,
        new_status=POStatus.SENT.value,
        extra_data={"order_date": po.order_date.isoformat()},
    )
    db.commit()

    return {"message": "PO sent", "po_number": po.po_number}


@router.post("/purchase-orders/{po_id}/lines", response_model=POLineResponse)
def add_po_line(
    po_id: int,
    line_in: POLineCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Add a line to a draft PO and roll the PO subtotal/total. Writes two tamper-evident
    audit_log rows: a CREATE for the new line and an UPDATE for the PO-totals change.

    404s on a soft-deleted PO -- see ``_live_po_or_404``."""
    po = _live_po_or_404(db, po_id, company_id)

    if po.status not in [POStatus.DRAFT]:
        raise HTTPException(status_code=400, detail="Can only add lines to draft POs")

    part = db.query(Part).filter(Part.id == line_in.part_id, Part.company_id == company_id).first()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")

    # Get next line number
    max_line = (
        db.query(func.max(PurchaseOrderLine.line_number)).filter(PurchaseOrderLine.purchase_order_id == po_id).scalar()
        or 0
    )

    # quantity_ordered/unit_price parse as Decimal (Money schema types) but the PO
    # money columns are Float — coerce so the `po.subtotal += line_total` roll below
    # doesn't mix Decimal with float (mirrors create_purchase_order).
    line_total = float(line_in.quantity_ordered) * float(line_in.unit_price)
    line = PurchaseOrderLine(
        purchase_order_id=po_id,
        line_number=max_line + 1,
        part_id=line_in.part_id,
        quantity_ordered=line_in.quantity_ordered,
        unit_price=line_in.unit_price,
        line_total=line_total,
        required_date=line_in.required_date,
        notes=line_in.notes,
    )
    line.company_id = company_id
    db.add(line)

    # Snapshot the PO columns BEFORE the totals mutation so the audited UPDATE diff
    # below captures the subtotal/total roll.
    old_po_values = {c.key: getattr(po, c.key) for c in po.__table__.columns}

    # Update PO totals
    po.subtotal += line_total
    po.total = po.subtotal + po.tax + po.shipping

    db.flush()
    audit.log_create(
        "purchase_order_line",
        line.id,
        f"{po.po_number}-L{line.line_number}",
        new_values=line,
        extra_data={"po_id": po.id, "po_number": po.po_number},
    )
    audit.log_update(
        "purchase_order",
        po.id,
        po.po_number,
        old_values=old_po_values,
        new_values=po,
        extra_data={"cause": "po_line_added", "line_id": line.id, "line_number": line.line_number},
    )
    OperationalEventService(db).emit_best_effort(
        company_id=company_id,
        event_type="purchase_order_line_added",
        source_module="purchasing",
        entity_type="purchase_order_line",
        entity_id=line.id,
        user_id=current_user.id,
        severity="info",
        event_payload={
            "po_id": po.id,
            "po_number": po.po_number,
            "line_number": line.line_number,
            "part_id": line.part_id,
            "quantity_ordered": float(line.quantity_ordered or 0),
            "unit_price": float(line.unit_price or 0),
            "required_date": (line.required_date.isoformat() if line.required_date else None),
        },
    )
    db.commit()
    db.refresh(line)
    return line


@router.delete("/purchase-orders/{po_id}")
def delete_purchase_order(
    po_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
):
    """Soft delete a purchase order (compliance invariant #3 -- no hard delete). Refuses if any
    line has received material so voided receipts/inventory aren't stranded."""
    po = db.query(PurchaseOrder).filter(PurchaseOrder.id == po_id, PurchaseOrder.company_id == company_id).first()
    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")

    # Re-delete guard (symmetric with restore_purchase_order's "not deleted" check): a
    # double DELETE must not reset deleted_at/deleted_by or write a duplicate audit row.
    if po.is_deleted:
        raise HTTPException(status_code=400, detail=f"Purchase order {po.po_number} is already deleted")

    # Guardrail: a PO with received material must have its receipt(s) voided first
    # (via receiving) so inventory/receipt rows aren't orphaned behind a deleted PO.
    received_line_count = (
        db.query(PurchaseOrderLine)
        .filter(
            PurchaseOrderLine.purchase_order_id == po.id,
            PurchaseOrderLine.company_id == company_id,
            PurchaseOrderLine.quantity_received > 0,
        )
        .count()
    )
    if received_line_count > 0:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot delete purchase order {po.po_number}: it has received material. "
                "Void the receipt(s) first, then delete."
            ),
        )

    audit = AuditService(db, current_user, request)

    po.soft_delete(current_user.id)
    # Log BEFORE the terminal commit so the audit row commits atomically with the
    # delete (AuditService.log only flushes; get_db never commits).
    audit.log_delete("purchase_order", po.id, po.po_number, soft_delete=True)
    db.commit()
    return {"message": f"Purchase order {po.po_number} deleted", "can_restore": True}


@router.post("/purchase-orders/{po_id}/restore", summary="Restore a soft-deleted purchase order")
def restore_purchase_order(
    po_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
):
    """Restore a soft-deleted purchase order. Raw lookup so it can see the soft-deleted row."""
    po = db.query(PurchaseOrder).filter(PurchaseOrder.id == po_id, PurchaseOrder.company_id == company_id).first()
    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")

    if not po.is_deleted:
        raise HTTPException(status_code=400, detail="Purchase order is not deleted")

    # A live PO must point at a vendor that still exists. ``delete_vendor`` counts blocking
    # POs with ``is_deleted == False``, so a soft-deleted PO does NOT hold its vendor open:
    # delete the PO, then delete the vendor, and restoring would bring a live (possibly
    # SENT) order back against a vendor that is gone -- receivable through
    # ``GET /receiving/open-pos``, which checks status and is_deleted but not the vendor,
    # and a state ``POST /purchase-orders`` flatly refuses to create. Refuse instead, and
    # name the fix. Checked AFTER the is_deleted guard so a double-restore still reports
    # the more specific "not deleted".
    #
    # Deliberately NOT mirroring the create path's ``is_active == true`` half: a live PO
    # against a merely DEACTIVATED vendor is already a representable state (``PUT
    # /vendors/{id}`` can deactivate one with no PO guard at all), so refusing on it here
    # would be stricter than the invariant the rest of the router keeps.
    if po.vendor is not None and po.vendor.is_deleted:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot restore purchase order {po.po_number}: its vendor "
                f"{po.vendor.name} is deleted. Restore the vendor first."
            ),
        )

    audit = AuditService(db, current_user, request)

    po.restore()
    # Log BEFORE the terminal commit so the audit row commits atomically with the
    # restore (AuditService.log only flushes; get_db never commits).
    audit.log_update(
        "purchase_order",
        po.id,
        po.po_number,
        old_values={"is_deleted": True},
        new_values={"is_deleted": False},
        action="restore",
    )
    db.commit()

    return {"message": f"Purchase order {po.po_number} restored"}


# ============ RECEIVING ============
# The receiving / inspection endpoints live in app/api/endpoints/receiving.py
# (mounted at /api/v1/receiving). The duplicate copies that previously lived here
# were removed; purchasing.py now owns only vendor and purchase-order endpoints.
