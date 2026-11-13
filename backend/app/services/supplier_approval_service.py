"""Approved Supplier List (ASL) on the vendor record -- AS9100D 8.4, Werco form QL 7.4.

One module owns the whole vocabulary and every rule that reads or writes it:

* the three controlled vocabularies (status, approved scope of supply, approval basis) as
  ``str`` enums with display labels and the spreadsheet's own spellings as aliases
  (en-dash labels, trailing spaces, the sheet's "Metalics" / "Labroatory" misspellings,
  the " Cert" suffix on the basis dropdown);
* :func:`apply_vendor_approval` -- the ONLY writer of the ASL columns, and the one place
  ``is_approved`` is derived from a status;
* :func:`approval_warnings` -- the advisory purchase-order checks (warn, never block);
* :func:`assert_vendor_not_disqualified` -- the one hard refusal: a purchase order cannot
  be raised to a vendor that is Disqualified on the ASL. Every PO-create door calls it.

Callers: ``PUT /purchasing/vendors/{id}/approval``, the update-only ASL import
(``vendor_asl_import_service``), ``PUT /purchasing/vendors/{id}`` (the is_approved
contradiction 409), the overdue re-evaluation list, ``POST /purchasing/supplier-approval-check``,
the PO responses and every PO-create door (``erp_draft_commands.create_purchase_order_command``
for the UI / Hank / AI applier, PO upload, the open-PO loader, the MRP drafts).
"""

from __future__ import annotations

import enum
import re
from datetime import date, datetime
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

from fastapi import HTTPException
from sqlalchemy import or_

from app.core.time_utils import CENTRAL_TIME_ZONE
from app.models.purchasing import VENDOR_APPROVAL_STATUS_VALUES, Vendor

# ---------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------


class ApprovalStatus(str, enum.Enum):
    APPROVED = "approved"
    CONDITIONAL = "conditional"
    DISQUALIFIED = "disqualified"
    APPROVED_CUSTOMER_DIRECTED = "approved_customer_directed"


class AslScope(str, enum.Enum):
    """The 19 "APPROVED SCOPE OF SUPPLY" values of the QL 7.4 dropdown list."""

    ADHESIVES_BONDING = "adhesives_bonding"
    CALIBRATION_SERVICES = "calibration_services"
    CUSTOMER_DIRECTED_SUPPLIER = "customer_directed_supplier"
    ELECTRONIC_COMPONENTS = "electronic_components"
    ENGINEERING_DESIGN_SERVICES = "engineering_design_services"
    HARDWARE = "hardware"
    HONEYCOMB_CORE_PANELS = "honeycomb_core_panels"
    MACHINING = "machining"
    NDT_INSPECTION_LABORATORY = "ndt_inspection_laboratory"
    PAINT_POWDERS = "paint_powders"
    RAW_MATERIAL_COMPOSITE = "raw_material_composite"
    RAW_MATERIAL_METALLICS = "raw_material_metallics"
    RAW_MATERIAL_OTHER = "raw_material_other"
    SCREEN_PRINTING = "screen_printing"
    SPECIAL_PROCESS_PASSIVATION = "special_process_passivation"
    SPECIAL_PROCESS_WELDING = "special_process_welding"
    SPECIAL_PROCESS_HEAT_TREATMENT = "special_process_heat_treatment"
    SPECIAL_PROCESS_PLATING_COATING = "special_process_plating_coating"
    WELDING_MATERIALS = "welding_materials"


class ApprovalBasis(str, enum.Enum):
    """The 7 "APPROVAL BASIS" values of the QL 7.4 dropdown list."""

    AS9100 = "as9100"
    AS9120 = "as9120"
    ISO9001 = "iso9001"
    ISO17025 = "iso17025"
    NADCAP = "nadcap"
    QUESTIONNAIRE = "questionnaire"
    CUSTOMER_REQUIREMENT = "customer_requirement"


STATUS_LABELS: Dict[str, str] = {
    ApprovalStatus.APPROVED.value: "Approved",
    ApprovalStatus.CONDITIONAL.value: "Conditional",
    ApprovalStatus.DISQUALIFIED.value: "Disqualified",
    ApprovalStatus.APPROVED_CUSTOMER_DIRECTED.value: "Approved - Customer Directed",
}

SCOPE_LABELS: Dict[str, str] = {
    AslScope.ADHESIVES_BONDING.value: "Adhesives / Bonding Materials",
    AslScope.CALIBRATION_SERVICES.value: "Calibration Services",
    AslScope.CUSTOMER_DIRECTED_SUPPLIER.value: "Customer-Directed Supplier",
    AslScope.ELECTRONIC_COMPONENTS.value: "Electronic Components",
    AslScope.ENGINEERING_DESIGN_SERVICES.value: "Engineering / Design Services",
    AslScope.HARDWARE.value: "Hardware",
    AslScope.HONEYCOMB_CORE_PANELS.value: "Honeycomb Core / Panels",
    AslScope.MACHINING.value: "Machining",
    AslScope.NDT_INSPECTION_LABORATORY.value: "NDT / Inspection / Laboratory Services",
    AslScope.PAINT_POWDERS.value: "Paint / Powders",
    AslScope.RAW_MATERIAL_COMPOSITE.value: "Raw Material - Composite",
    AslScope.RAW_MATERIAL_METALLICS.value: "Raw Material - Metallics",
    AslScope.RAW_MATERIAL_OTHER.value: "Raw Material - Other",
    AslScope.SCREEN_PRINTING.value: "Screen Printing",
    AslScope.SPECIAL_PROCESS_PASSIVATION.value: "Special Process - Passivation",
    AslScope.SPECIAL_PROCESS_WELDING.value: "Special Process - Welding",
    AslScope.SPECIAL_PROCESS_HEAT_TREATMENT.value: "Special Process - Heat Treatment",
    AslScope.SPECIAL_PROCESS_PLATING_COATING.value: "Special Process - Plating / Coating",
    AslScope.WELDING_MATERIALS.value: "Welding Materials",
}

BASIS_LABELS: Dict[str, str] = {
    ApprovalBasis.AS9100.value: "AS9100 Cert",
    ApprovalBasis.AS9120.value: "AS9120 Cert",
    ApprovalBasis.ISO9001.value: "ISO 9001 Cert",
    ApprovalBasis.ISO17025.value: "ISO 17025 Cert",
    ApprovalBasis.NADCAP.value: "NADCAP Cert",
    ApprovalBasis.QUESTIONNAIRE.value: "Questionnaire",
    ApprovalBasis.CUSTOMER_REQUIREMENT.value: "Customer Requirement",
}

# The spreadsheet's own spellings that the canonical label does not already cover after
# normalization. The dropdown misspells two scopes; keep accepting them forever, because
# the controlled sheet (Rev A, 2026-01-30) carries them.
SCOPE_SHEET_ALIASES: Dict[str, str] = {
    "Raw Material – Metalics": AslScope.RAW_MATERIAL_METALLICS.value,
    "NDT / Inspection / Labroatory Services": AslScope.NDT_INSPECTION_LABORATORY.value,
}

# Statuses whose vendor may be bought from (``is_approved`` derives True).
APPROVED_STATUSES: FrozenSet[str] = frozenset(
    {ApprovalStatus.APPROVED.value, ApprovalStatus.APPROVED_CUSTOMER_DIRECTED.value}
)
# Statuses that carry a re-evaluation obligation (the overdue list). Disqualified does not.
REEVALUATION_STATUSES: FrozenSet[str] = frozenset(
    {
        ApprovalStatus.APPROVED.value,
        ApprovalStatus.CONDITIONAL.value,
        ApprovalStatus.APPROVED_CUSTOMER_DIRECTED.value,
    }
)

# A part with no ``supply_scope`` gets a HINT from its part_type: if the vendor holds none of
# these scopes, the PO warns "unverified". purchased / manufactured / assembly give no hint.
PART_TYPE_SCOPE_HINTS: Dict[str, FrozenSet[str]] = {
    "hardware": frozenset({AslScope.HARDWARE.value}),
    "raw_material": frozenset(
        {
            AslScope.RAW_MATERIAL_METALLICS.value,
            AslScope.RAW_MATERIAL_COMPOSITE.value,
            AslScope.RAW_MATERIAL_OTHER.value,
        }
    ),
    "consumable": frozenset(
        {AslScope.ADHESIVES_BONDING.value, AslScope.PAINT_POWDERS.value, AslScope.WELDING_MATERIALS.value}
    ),
}

DISQUALIFIED_REFUSAL_TEMPLATE = (
    "{name} is Disqualified on the Approved Supplier List; a purchase order cannot be raised to it."
)

_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_MULTI_VALUE_SPLIT = re.compile(r"[;\n,]+")


def label_key(value: Any) -> str:
    """The matching key for a vocabulary label: casefold, every dash / space / punctuation run
    removed. "Special Process- Heat Treatment " == "special process – heat treatment" ==
    "special_process_heat_treatment"."""
    text = str(value or "").replace("–", "-").replace("—", "-").casefold()
    return _NON_ALNUM.sub("", text)


def _vocabulary(
    labels: Dict[str, str], aliases: Optional[Dict[str, str]] = None, strip_suffix: Optional[str] = None
) -> Dict[str, str]:
    table: Dict[str, str] = {}
    for slug, label in labels.items():
        table[label_key(slug)] = slug
        table[label_key(label)] = slug
        if strip_suffix and label.endswith(strip_suffix):
            table[label_key(label[: -len(strip_suffix)])] = slug
    for alias, slug in (aliases or {}).items():
        table[label_key(alias)] = slug
    return table


_STATUS_KEYS = _vocabulary(STATUS_LABELS)
_SCOPE_KEYS = _vocabulary(SCOPE_LABELS, SCOPE_SHEET_ALIASES)
_BASIS_KEYS = _vocabulary(BASIS_LABELS, strip_suffix=" Cert")
_SCOPE_ORDER = {scope.value: index for index, scope in enumerate(AslScope)}
_BASIS_ORDER = {basis.value: index for index, basis in enumerate(ApprovalBasis)}


class AslValueError(ValueError):
    """An ASL label that is not in the controlled vocabulary. Maps to 422 / a row error."""


def _allowed(labels: Dict[str, str]) -> str:
    return ", ".join(labels.values())


def parse_status(value: Any) -> Optional[str]:
    """A status slug or sheet label -> slug. Blank -> None (= not on the ASL)."""
    if value is None or not str(value).strip():
        return None
    slug = _STATUS_KEYS.get(label_key(value))
    if slug is None:
        raise AslValueError(f"Unknown supplier status '{str(value).strip()}'. Allowed: {_allowed(STATUS_LABELS)}")
    return slug


def parse_scope(value: Any) -> Optional[str]:
    """One scope slug or sheet label -> slug. Blank -> None."""
    if value is None or not str(value).strip():
        return None
    slug = _SCOPE_KEYS.get(label_key(value))
    if slug is None:
        raise AslValueError(f"Unknown scope of supply '{str(value).strip()}'. Allowed: {_allowed(SCOPE_LABELS)}")
    return slug


def parse_basis(value: Any) -> Optional[str]:
    """One basis slug or sheet label -> slug. Blank -> None."""
    if value is None or not str(value).strip():
        return None
    slug = _BASIS_KEYS.get(label_key(value))
    if slug is None:
        raise AslValueError(f"Unknown approval basis '{str(value).strip()}'. Allowed: {_allowed(BASIS_LABELS)}")
    return slug


def _split(values: Any) -> List[str]:
    if values is None:
        return []
    if isinstance(values, str):
        return [part for part in _MULTI_VALUE_SPLIT.split(values) if part.strip()]
    return [str(part) for part in values if part is not None and str(part).strip()]


def parse_scopes(values: Any) -> List[str]:
    """A list (or a ';' / ',' / newline separated cell) of scopes -> de-duplicated slugs in
    the dropdown's canonical order, so a re-run compares equal regardless of cell order."""
    slugs = {slug for slug in (parse_scope(value) for value in _split(values)) if slug}
    return sorted(slugs, key=_SCOPE_ORDER.__getitem__)


def parse_bases(values: Any) -> List[str]:
    """As :func:`parse_scopes`, for approval bases."""
    slugs = {slug for slug in (parse_basis(value) for value in _split(values)) if slug}
    return sorted(slugs, key=_BASIS_ORDER.__getitem__)


def status_label(status: Optional[str]) -> str:
    return STATUS_LABELS.get(status or "", "Not on the ASL")


def scope_label(scope: Optional[str]) -> str:
    return SCOPE_LABELS.get(scope or "", scope or "")


def derived_is_approved(status: Optional[str]) -> Optional[bool]:
    """What ``is_approved`` must be for ``status``; None = no derivation (not on the ASL)."""
    if status is None:
        return None
    return status in APPROVED_STATUSES


def central_today() -> date:
    return datetime.now(CENTRAL_TIME_ZONE).date()


# ---------------------------------------------------------------------------
# The one writer
# ---------------------------------------------------------------------------

# The ASL columns plus the derived flag, in the order an audit diff lists them.
ASL_FIELDS: Tuple[str, ...] = (
    "approval_status",
    "approved_scopes",
    "approval_bases",
    "approval_date",
    "reevaluation_due",
    "last_performance_evaluation",
    "approval_notes",
)
AUDITED_FIELDS: Tuple[str, ...] = ASL_FIELDS + ("is_approved",)

_UNSET: Any = object()


def snapshot(vendor: Any) -> Dict[str, Any]:
    """The audited ASL fields of ``vendor`` as plain values (lists copied)."""
    values = {field: getattr(vendor, field) for field in AUDITED_FIELDS}
    for field in ("approved_scopes", "approval_bases"):
        values[field] = list(values[field]) if values[field] else None
    return values


def apply_vendor_approval(
    vendor: Any,
    *,
    status: Any = _UNSET,
    scopes: Any = _UNSET,
    bases: Any = _UNSET,
    approval_date: Any = _UNSET,
    reevaluation_due: Any = _UNSET,
    last_performance_evaluation: Any = _UNSET,
    approval_notes: Any = _UNSET,
) -> None:
    """Write the given ASL fields onto ``vendor`` and derive ``is_approved``.

    Only the keyword arguments actually passed are written (an omitted one leaves the column
    as it is). ``status`` is a slug or None. When a status is set, ``is_approved`` is derived:
    approved / approved_customer_directed -> True, conditional / disqualified -> False. A
    NULL status (not on the ASL) leaves ``is_approved`` exactly as it was. ``is_active`` and
    ``is_deleted`` are never touched -- removal and approval are different decisions.
    """
    if status is not _UNSET:
        if status is not None and status not in VENDOR_APPROVAL_STATUS_VALUES:
            raise AslValueError(f"Unknown supplier status '{status}'")
        vendor.approval_status = status
    if scopes is not _UNSET:
        vendor.approved_scopes = list(scopes) if scopes else None
    if bases is not _UNSET:
        vendor.approval_bases = list(bases) if bases else None
    if approval_date is not _UNSET:
        vendor.approval_date = approval_date
    if reevaluation_due is not _UNSET:
        vendor.reevaluation_due = reevaluation_due
    if last_performance_evaluation is not _UNSET:
        vendor.last_performance_evaluation = last_performance_evaluation
    if approval_notes is not _UNSET:
        vendor.approval_notes = approval_notes
    derived = derived_is_approved(vendor.approval_status)
    if derived is not None:
        vendor.is_approved = derived


def changed_fields(before: Dict[str, Any], after: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """(old, new) restricted to the fields that differ, JSON-ready (dates as ISO text)."""
    old: Dict[str, Any] = {}
    new: Dict[str, Any] = {}
    for field in AUDITED_FIELDS:
        if before.get(field) != after.get(field):
            old[field] = _jsonable(before.get(field))
            new[field] = _jsonable(after.get(field))
    return old, new


def _jsonable(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


# ---------------------------------------------------------------------------
# Purchase-order checks
# ---------------------------------------------------------------------------


def is_disqualified(vendor: Any) -> bool:
    return getattr(vendor, "approval_status", None) == ApprovalStatus.DISQUALIFIED.value


def disqualified_refusal(vendor: Any) -> str:
    return DISQUALIFIED_REFUSAL_TEMPLATE.format(name=vendor.name)


def not_disqualified_clause() -> Any:
    """SQL twin of ``not is_disqualified``: for a query that PICKS a vendor automatically
    (the MRP auto-PO and review defaults), so a Disqualified vendor is never chosen."""
    return or_(Vendor.approval_status.is_(None), Vendor.approval_status != ApprovalStatus.DISQUALIFIED.value)


def assert_vendor_not_disqualified(vendor: Any) -> None:
    """Refuse 409 a purchase order to a Disqualified vendor (owner decision 2026-11-13).

    The one hard ASL gate. Every PO-create door calls it before writing anything; the scope
    and status checks beside it are warnings only.
    """
    if is_disqualified(vendor):
        raise HTTPException(status_code=409, detail=disqualified_refusal(vendor))


def _warning(code: str, message: str, part: Any = None) -> Dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "part_id": getattr(part, "id", None),
        "part_number": getattr(part, "part_number", None),
    }


def _part_type_value(part: Any) -> str:
    value = getattr(part, "part_type", None)
    return str(getattr(value, "value", value) or "").strip().lower()


def approval_warnings(vendor: Any, parts: Iterable[Any] = (), *, today: Optional[date] = None) -> List[Dict[str, Any]]:
    """The advisory ASL warnings for buying ``parts`` from ``vendor``. Pure: no query, no write.

    Codes: ``vendor_not_on_asl``, ``vendor_conditional``, ``vendor_disqualified``,
    ``reevaluation_overdue``, ``scope_not_approved`` and ``scope_unverified``. Per-part scope
    checks run only for a vendor ON the ASL (not disqualified, not customer-directed -- a
    customer-directed source's scope is the customer's direction, not Werco's approval).
    """
    today = today or central_today()
    name = vendor.name
    status = getattr(vendor, "approval_status", None)
    warnings: List[Dict[str, Any]] = []
    if status is None:
        if not getattr(vendor, "is_approved", False):
            warnings.append(_warning("vendor_not_on_asl", f"{name} is not on the Approved Supplier List."))
        return warnings
    if status == ApprovalStatus.DISQUALIFIED.value:
        warnings.append(_warning("vendor_disqualified", disqualified_refusal(vendor)))
        return warnings
    if status == ApprovalStatus.CONDITIONAL.value:
        warnings.append(_warning("vendor_conditional", f"{name} is Conditional on the Approved Supplier List."))
    due = getattr(vendor, "reevaluation_due", None)
    if status in REEVALUATION_STATUSES and due is not None and due < today:
        warnings.append(_warning("reevaluation_overdue", f"{name}'s supplier re-evaluation was due {due.isoformat()}."))
    if status == ApprovalStatus.APPROVED_CUSTOMER_DIRECTED.value:
        return warnings

    vendor_scopes: Set[str] = set(getattr(vendor, "approved_scopes", None) or [])
    seen: Set[Any] = set()
    for part in parts:
        if part is None or getattr(part, "id", None) in seen:
            continue
        seen.add(getattr(part, "id", None))
        scope = getattr(part, "supply_scope", None)
        if scope:
            if scope not in vendor_scopes:
                warnings.append(
                    _warning(
                        "scope_not_approved",
                        f"{name} is not approved for {scope_label(scope)} (part {part.part_number}).",
                        part,
                    )
                )
            continue
        hint = PART_TYPE_SCOPE_HINTS.get(_part_type_value(part))
        if hint and not (hint & vendor_scopes):
            labels = " / ".join(scope_label(slug) for slug in sorted(hint, key=_SCOPE_ORDER.__getitem__))
            warnings.append(
                _warning(
                    "scope_unverified",
                    f"Part {part.part_number} has no supply scope set, and {name} is not approved for {labels}.",
                    part,
                )
            )
    return warnings


def warning_codes(warnings: Sequence[Dict[str, Any]]) -> List[str]:
    return [warning["code"] for warning in warnings]
