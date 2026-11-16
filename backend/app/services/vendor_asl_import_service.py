"""Update-only load of the Approved Supplier List (QL 7.4) onto EXISTING vendors.

Why update-only: production already holds every supplier on the ASL as a vendor
(VND-001..VND-100, loaded at go-live with the ASL data only in free-text notes). Creating
rows here would duplicate them, so this import NEVER creates a vendor: a sheet row binds to
exactly one existing live vendor or it is a row error. It is therefore not routed through
the create-only Import Center batch flow (``import_batch_service``); it is its own
preview / commit pair, like the routing import.

Matching (an AS9100-controlled list must never bind a row to the wrong supplier). SUPPLIER
NAME is required on every row:

1. a ``vendor_code`` (or ``code`` / ``supplier_code``) column, when filled, matches the
   vendor by code (case-insensitive) -- and the row's normalized name must then be that
   vendor's too, or it is a row error (a mistyped code never binds silently);
2. otherwise the EXACT normalized name: casefold, whitespace collapsed, en/em dashes read as
   '-', spaces beside '-' and '.' removed ("Cinder- Forge" == "Cinder-Forge",
   "R.T.Hollis" == "R.T. Hollis"). Nothing fuzzier: two names that differ in a word are two
   suppliers.

Zero matches, more than one live match, a soft-deleted match (restore it first) and two rows
binding the same vendor are ROW ERRORS, and any row error makes the commit refuse (409)
having written nothing -- all-or-nothing.

Values: an unknown status / scope / basis label is a row error naming the allowed labels.
A blank STATUS on a vendor not on the ASL leaves it off (with a warning); on a vendor that IS
on the ASL it is a row error -- an import never takes a supplier off the list (from
Disqualified that would lift the one hard PO refusal); Update approval does, with a reason.
Every other blank cell leaves the vendor's value unchanged -- a blank date never clears one. APPROVED DATE overwrites ``approval_date`` (audited). The
address and the name are compared only and never written.

Idempotent: a row whose values already match writes nothing, and a re-run of the same sheet
writes no vendor change and no audit row at all. A commit that changes anything writes one
REQUIRED audit row per changed vendor (changed fields only) plus one summary row, all in the
caller's single transaction (``app.api.audit_guard.required_audit`` rolls it all back).

``preview_asl_import`` takes no ``AuditService`` and writes nothing, structurally.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.purchasing import Vendor
from app.services import supplier_approval_service as asl
from app.services.audit_service import AuditService
from app.services.import_service import ParsedTable, coerce_cell, parse_date_field, parse_import_file

ASL_SHEET_NAME = "Approved Supplier List"
ASL_HEADER_DETECT = frozenset({"supplier_name", "status"})
ASL_REQUIRED_COLUMNS = {"supplier_name", "status"}
IMPORT_SOURCE = "asl_import"

# Accepted column spellings per field (normalized headers); the first is the template's.
CODE_COLUMNS = ("vendor_code", "code", "supplier_code")
SCOPE_COLUMNS = ("approved_scope_of_supply", "approved_scope", "scope_of_supply", "approved_scopes")
BASIS_COLUMNS = ("approval_basis", "approval_bases")
APPROVED_DATE_COLUMNS = ("approved_date", "approval_date")
REEVALUATION_COLUMNS = ("re_evaluation_due_date", "reevaluation_due_date", "reevaluation_due", "re_evaluation_due")
PERFORMANCE_COLUMNS = ("performance_evaluation_date", "last_performance_evaluation", "performance_evaluation")
NOTES_COLUMNS = ("notes", "approval_notes")
ADDRESS_COLUMNS = ("supplier_address", "address")

_ADDRESS_KEY = re.compile(r"[^0-9a-z]+")


def source_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# The exact-normalized supplier name used for matching (see the module docstring). It lives in
# the ASL service because the Disqualified-twin refusal on every vendor-create door compares
# names by the same rule.
vendor_name_key = asl.vendor_name_key


def _address_key(text: Any) -> str:
    return _ADDRESS_KEY.sub("", str(text or "").casefold())


def _vendor_address(vendor: Vendor) -> str:
    return " ".join(
        part
        for part in (vendor.address_line1, vendor.address_line2, vendor.city, vendor.state, vendor.postal_code)
        if part
    )


def parse_asl_file(filename: Optional[str], content: bytes) -> ParsedTable:
    """Parse the QL 7.4 workbook (or the template, or a CSV): title block skipped, the
    "Approved Supplier List" sheet preferred. Raises ``ImportFileError`` (-> 400)."""
    return parse_import_file(
        filename,
        content,
        required_columns=ASL_REQUIRED_COLUMNS,
        header_detect=ASL_HEADER_DETECT,
        sheet_name=ASL_SHEET_NAME,
    )


def document_identity(table: ParsedTable) -> Dict[str, Optional[str]]:
    """Form number / revision / rev date read off the title block, when it carries them."""
    labels = {"formnumber": "form_number", "form": "form_number", "revision": "revision", "revdate": "rev_date"}
    found: Dict[str, Optional[str]] = {"form_number": None, "revision": None, "rev_date": None}
    for cells in table.preamble:
        for index, cell in enumerate(cells):
            key = labels.get(_ADDRESS_KEY.sub("", cell.casefold()))
            if not key or found[key]:
                continue
            value = next((coerce_cell(c) for c in cells[index + 1 :] if coerce_cell(c)), None)
            found[key] = value[:40] if value else None
    return found


def _cell(row: Dict[str, str], columns: Sequence[str]) -> str:
    for column in columns:
        value = row.get(column)
        if value is not None and value.strip():
            return value.strip()
    return ""


@dataclass
class _RowPlan:
    row: int
    supplier_name: str
    vendor: Optional[Vendor] = None
    match_by: Optional[str] = None
    values: Dict[str, Any] = field(default_factory=dict)
    changes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def outcome(self) -> str:
        if self.errors:
            return "error"
        return "update" if self.changes else "unchanged"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "row": self.row,
            "supplier_name": self.supplier_name,
            "vendor_id": self.vendor.id if self.vendor is not None else None,
            "vendor_code": self.vendor.code if self.vendor is not None else None,
            "vendor_name": self.vendor.name if self.vendor is not None else None,
            "match_by": self.match_by,
            "outcome": self.outcome,
            "changes": self.changes,
            "warnings": self.warnings,
            "error": "; ".join(self.errors) if self.errors else None,
        }


class _Index:
    def __init__(self, vendors: Sequence[Vendor]):
        self.by_code: Dict[str, List[Vendor]] = {}
        self.by_name: Dict[str, List[Vendor]] = {}
        for vendor in vendors:
            self.by_code.setdefault((vendor.code or "").strip().upper(), []).append(vendor)
            self.by_name.setdefault(vendor_name_key(vendor.name), []).append(vendor)


def _restore_hint(vendor: Vendor) -> str:
    return (
        f"vendor {vendor.code} ('{vendor.name}') is deleted -- restore it "
        f"(POST /api/v1/purchasing/vendors/{vendor.id}/restore) before loading its ASL entry"
    )


def _match(plan: _RowPlan, row: Dict[str, str], index: _Index) -> None:
    code = _cell(row, CODE_COLUMNS).upper()
    if not plan.supplier_name:
        # Required even beside a vendor_code: the code alone cannot be checked against
        # anything, and a mistyped code would bind the row to the wrong supplier silently.
        plan.errors.append("SUPPLIER NAME is required")
        return
    if code:
        plan.match_by = "code"
        hits = index.by_code.get(code, [])
        live = [vendor for vendor in hits if not vendor.is_deleted]
        if len(live) == 1:
            vendor = live[0]
            if vendor_name_key(plan.supplier_name) != vendor_name_key(vendor.name):
                # A code and a name that disagree are a typo in one of them (VND-012 for VND-021)
                # or codes swapped since; either way binding would land this supplier's approval
                # on another supplier's controlled record. Refused, never a warning.
                plan.errors.append(
                    f"vendor_code '{code}' is {vendor.code} '{vendor.name}', but SUPPLIER NAME is "
                    f"'{plan.supplier_name}'; a row binds only when its code and name name the same vendor. "
                    "Correct the code or the name (rename the vendor first if its name has changed)"
                )
                return
            plan.vendor = vendor
        elif len(live) > 1:
            codes = ", ".join(sorted(vendor.code for vendor in live))
            plan.errors.append(
                f"vendor_code '{code}' matches {len(live)} vendors whose codes differ only in case ({codes}); "
                "rename one of them first"
            )
        elif hits:
            plan.errors.append(_restore_hint(hits[0]))
        else:
            plan.errors.append(f"no vendor with code '{code}'")
        return
    plan.match_by = "name"
    hits = index.by_name.get(vendor_name_key(plan.supplier_name), [])
    live = [vendor for vendor in hits if not vendor.is_deleted]
    if len(live) == 1:
        plan.vendor = live[0]
    elif len(live) > 1:
        codes = ", ".join(sorted(vendor.code for vendor in live))
        plan.errors.append(
            f"'{plan.supplier_name}' matches {len(live)} vendors ({codes}); add a vendor_code column to choose one"
        )
    elif hits:
        plan.errors.append(_restore_hint(hits[0]))
    else:
        plan.errors.append(
            f"no vendor named '{plan.supplier_name}'; add a vendor_code column or create the vendor first "
            "(this import never creates vendors)"
        )


def _parse_values(plan: _RowPlan, row: Dict[str, str]) -> None:
    values: Dict[str, Any] = {}
    try:
        values["status"] = asl.parse_status(row.get("status"))
    except asl.AslValueError as exc:
        plan.errors.append(str(exc))
    scope_cell = _cell(row, SCOPE_COLUMNS)
    if scope_cell:
        try:
            values["scopes"] = asl.parse_scopes(scope_cell)
        except asl.AslValueError as exc:
            plan.errors.append(str(exc))
    basis_cell = _cell(row, BASIS_COLUMNS)
    if basis_cell:
        try:
            values["bases"] = asl.parse_bases(basis_cell)
        except asl.AslValueError as exc:
            plan.errors.append(str(exc))
    for key, columns, label in (
        ("approval_date", APPROVED_DATE_COLUMNS, "APPROVED DATE"),
        ("reevaluation_due", REEVALUATION_COLUMNS, "RE-EVALUATION DUE DATE"),
        ("last_performance_evaluation", PERFORMANCE_COLUMNS, "PERFORMANCE EVALUATION DATE"),
    ):
        text = _cell(row, columns)
        if not text:
            continue  # a blank date never clears one
        try:
            values[key] = parse_date_field(text, label)
        except ValueError as exc:
            plan.errors.append(str(exc))
    notes = _cell(row, NOTES_COLUMNS)
    if notes:
        values["approval_notes"] = notes[:4000]
    plan.values = values


def _diff(plan: _RowPlan) -> None:
    vendor = plan.vendor
    assert vendor is not None
    before = asl.snapshot(vendor)
    shadow = SimpleNamespace(name=vendor.name, **before)
    asl.apply_vendor_approval(shadow, **plan.values)
    after = asl.snapshot(shadow)
    old, new = asl.changed_fields(before, after)
    plan.changes = {name: {"old": old[name], "new": new[name]} for name in old}

    status = plan.values.get("status")
    if status is None:
        if before.get("approval_status"):
            # Taking a supplier OFF the list is a decision -- and from Disqualified it lifts the
            # one hard purchase-order refusal -- so a blank cell never makes it. It goes through
            # Update approval, which records who decided and why.
            plan.errors.append(
                f"STATUS is blank, but {vendor.code} is {asl.status_label(before['approval_status'])} on the "
                "Approved Supplier List; an import never takes a supplier off the list. Fill its STATUS, or remove "
                "it with Update approval (PUT /api/v1/purchasing/vendors/"
                f"{vendor.id}/approval), which records the reason"
            )
            plan.changes = {}
            return
        plan.warnings.append("STATUS is blank: the vendor is recorded as not on the Approved Supplier List yet.")
    elif status == asl.ApprovalStatus.DISQUALIFIED.value:
        plan.warnings.append("Disqualified: purchase orders to this vendor will be refused.")
    if not vendor.is_active:
        plan.warnings.append(f"Vendor {vendor.code} is inactive; its ASL entry is updated but it stays inactive.")


def _address_warning(plan: _RowPlan, row: Dict[str, str]) -> None:
    vendor = plan.vendor
    sheet_address = _cell(row, ADDRESS_COLUMNS)
    if vendor is None or not sheet_address:
        return
    if _address_key(sheet_address) != _address_key(_vendor_address(vendor)):
        plan.warnings.append("SUPPLIER ADDRESS differs from the vendor record; this import never changes an address.")


def build_plan(vendors: Sequence[Vendor], table: ParsedTable) -> List[_RowPlan]:
    """Match and diff every sheet row against ``vendors`` (ALL of the company's vendors,
    deleted ones included, so a tombstone match gets the restore hint). Pure: writes nothing."""
    index = _Index(vendors)
    plans: List[_RowPlan] = []
    for row_number, row in table.iter_rows():
        plan = _RowPlan(row=row_number, supplier_name=(row.get("supplier_name") or "").strip())
        _match(plan, row, index)
        _parse_values(plan, row)
        _address_warning(plan, row)
        if plan.vendor is not None and not plan.errors:
            _diff(plan)
        plans.append(plan)

    rows_by_vendor: Dict[int, List[_RowPlan]] = {}
    for plan in plans:
        if plan.vendor is not None:
            rows_by_vendor.setdefault(plan.vendor.id, []).append(plan)
    for duplicates in rows_by_vendor.values():
        if len(duplicates) < 2:
            continue
        rows = ", ".join(str(plan.row) for plan in duplicates)
        code = duplicates[0].vendor.code if duplicates[0].vendor is not None else ""
        for plan in duplicates:
            plan.errors.append(f"rows {rows} all match vendor {code}; a vendor may appear only once")
            plan.changes = {}
    return plans


def _summary(plans: Sequence[_RowPlan]) -> Dict[str, int]:
    outcomes = [plan.outcome for plan in plans]
    return {
        "total_rows": len(plans),
        "matched_rows": sum(1 for plan in plans if plan.vendor is not None and not plan.errors),
        "update_count": outcomes.count("update"),
        "unchanged_count": outcomes.count("unchanged"),
        "error_count": outcomes.count("error"),
        "warning_count": sum(len(plan.warnings) for plan in plans),
        "not_on_asl_count": sum(
            1 for plan in plans if not plan.errors and plan.vendor is not None and plan.values.get("status") is None
        ),
    }


def _company_vendors(db: Session, company_id: int, *, lock: bool) -> List[Vendor]:
    # Deliberately includes soft-deleted vendors: a row that names one gets a restore hint
    # rather than a misleading "no vendor named X" (it would be re-created otherwise, which
    # the tombstone-spanning code constraint refuses anyway).
    query = db.query(Vendor).filter(Vendor.company_id == company_id).order_by(Vendor.id)
    if lock:
        query = query.with_for_update().populate_existing()
    return query.all()


def plan_hash(plans: Sequence[_RowPlan]) -> str:
    """A digest of what the plan WOULD WRITE, read against the vendors as they are now.

    The preview returns it; the commit recomputes it under its row locks and refuses (409) when
    it differs. ``source_hash`` proves the file is the one previewed; this proves the vendors
    are -- so a decision recorded after the preview (Quality disqualifying a vendor the preview
    showed as unchanged) is never silently written over by a commit nobody re-read."""
    canonical = [
        {
            "row": plan.row,
            "vendor_id": plan.vendor.id if plan.vendor is not None else None,
            "outcome": plan.outcome,
            "changes": plan.changes,
            "errors": plan.errors,
        }
        for plan in plans
    ]
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _response(
    filename: Optional[str], content_hash: str, table: ParsedTable, plans: Sequence[_RowPlan], *, committed: bool
) -> Dict[str, Any]:
    return {
        "filename": filename,
        "source_hash": content_hash,
        "plan_hash": plan_hash(plans),
        "document": document_identity(table),
        "committed": committed,
        "summary": _summary(plans),
        "rows": [plan.as_dict() for plan in plans],
    }


def preview_asl_import(db: Session, *, company_id: int, filename: Optional[str], content: bytes) -> Dict[str, Any]:
    """Dry run: parse, match and diff. Writes nothing (takes no AuditService)."""
    table = parse_asl_file(filename, content)
    plans = build_plan(_company_vendors(db, company_id, lock=False), table)
    return _response(filename, source_hash(content), table, plans, committed=False)


def commit_asl_import(
    db: Session,
    *,
    company_id: int,
    filename: Optional[str],
    content: bytes,
    expected_source_hash: str,
    expected_plan_hash: str,
    audit: AuditService,
) -> Dict[str, Any]:
    """Apply the sheet. All-or-nothing; the CALLER commits (inside ``required_audit``).

    The file must be the one the preview read (``expected_source_hash``, 409 otherwise), and the
    plan -- every row's match, outcome and changes, re-run under row locks on the company's
    vendors -- must be the one the preview showed (``expected_plan_hash``, 409 otherwise): a
    vendor changed since the preview means the committer has not seen what would be written.
    """
    content_hash = source_hash(content)
    if (expected_source_hash or "").strip().lower() != content_hash:
        raise HTTPException(
            status_code=409,
            detail="This file is not the one that was previewed. Preview it again, then commit.",
        )
    table = parse_asl_file(filename, content)
    plans = build_plan(_company_vendors(db, company_id, lock=True), table)
    errors = [plan for plan in plans if plan.errors]
    if errors:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (f"{len(errors)} row(s) have errors; nothing was imported. Fix them and preview again."),
                "errors": [
                    {"row": plan.row, "supplier_name": plan.supplier_name, "error": "; ".join(plan.errors)}
                    for plan in errors
                ],
            },
        )
    # After the row errors (they say what to fix): a clean plan that is not the previewed one.
    if (expected_plan_hash or "").strip().lower() != plan_hash(plans):
        raise HTTPException(
            status_code=409,
            detail=(
                "The vendors changed after this file was previewed, so the import would no longer write what the "
                "preview showed. Preview it again, then commit."
            ),
        )

    document = document_identity(table)
    updates: List[Tuple[_RowPlan, Vendor]] = [
        (plan, plan.vendor) for plan in plans if plan.outcome == "update" and plan.vendor is not None
    ]
    for plan, vendor in updates:
        before = asl.snapshot(vendor)
        asl.apply_vendor_approval(vendor, **plan.values)
        old, new = asl.changed_fields(before, asl.snapshot(vendor))
        db.flush()
        audit.log_required(
            AuditService.ACTIONS["UPDATE"],
            "vendor",
            resource_id=vendor.id,
            resource_identifier=vendor.code,
            description=f"ASL import updated the supplier approval of {vendor.name}",
            old_values=old,
            new_values=new,
            extra_data={
                "source": IMPORT_SOURCE,
                "form": document.get("form_number"),
                "revision": document.get("revision"),
                "rev_date": document.get("rev_date"),
                "filename": filename,
                "source_hash": content_hash,
                "row": plan.row,
                "supplier_name": plan.supplier_name,
                "match_by": plan.match_by,
            },
            company_id=company_id,
        )
    response = _response(filename, content_hash, table, plans, committed=True)
    if updates:
        audit.log_required(
            AuditService.ACTIONS["IMPORT"],
            "vendor_asl_import",
            resource_identifier=(filename or "approved-supplier-list")[:255],
            description=f"Approved Supplier List import: {len(updates)} vendor(s) updated",
            new_values=response["summary"],
            extra_data={
                "source": IMPORT_SOURCE,
                "filename": filename,
                "source_hash": content_hash,
                "document": document,
                "updated_vendor_ids": [vendor.id for _, vendor in updates],
            },
            company_id=company_id,
        )
    return response
