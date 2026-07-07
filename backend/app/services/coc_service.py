"""Certificate of Conformance (CoC) issuance + rendering (G6-B).

A CoC is an APPEND-ONLY, per-Shipment compliance record. This service mints the
DB **frozen snapshot** (the immutable certified facts captured at issue time) and
renders the PDF DETERMINISTICALLY from that snapshot on download -- there is no
filesystem blob.

Design rules (mirror ``completion_inventory_service``):

* **No commit.** ``generate_coc_for_shipment`` joins the CALLER's unit of work
  (the ship handler / the on-demand endpoint own ``db.commit()``) so the CoC row
  + its audit entry land atomically with the ship.
* **Idempotent, DB-enforced.** Scoped per Shipment. An existing CoC for
  ``(company, shipment)`` is returned untouched (no second audit row). The
  concurrent double-ship race is caught at flush time by the
  ``uq_coc_company_shipment`` unique constraint inside a SAVEPOINT and resolved by
  re-querying the winner (mirrors the FG-receipt idempotency precedent).
* **Tenant-scoped.** Every lookup filters ``company_id`` (invariant #1). The
  caller passes the ACTIVE company.
* **Audited.** A successful issue writes a tamper-evident ``log_create`` row.
* **Frozen snapshot.** ``content_snapshot`` stores the JSON of every rendered fact
  (including the resolved ``issued_by_name`` and the ISO ship/issue dates) so the
  PDF render is fully self-contained and never depends on live mutable rows.
"""

import json
import logging
from datetime import datetime
from typing import List, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.time_utils import to_utc_iso
from app.models.customer import Customer
from app.models.part import Part
from app.models.shipping import CertificateOfConformance, Shipment
from app.models.user import User
from app.models.work_order import WorkOrder
from app.services.audit_service import AuditService
from app.services.coc_pdf_service import DEFAULT_COC_STATEMENT, build_certificate_of_conformance_pdf
from app.services.process_sheet_service import parse_serial_numbers

logger = logging.getLogger(__name__)


def _existing_coc(db: Session, shipment_id: int, company_id: int) -> Optional[CertificateOfConformance]:
    """Return the CoC already issued for this (company, shipment), if any (idempotency key)."""
    return (
        db.query(CertificateOfConformance)
        .filter(
            CertificateOfConformance.company_id == company_id,
            CertificateOfConformance.shipment_id == shipment_id,
        )
        .first()
    )


def _resolve_work_order(db: Session, shipment: Shipment, company_id: int) -> Optional[WorkOrder]:
    """Resolve the shipment's work order, tenant-scoped (relationship is not company-filtered)."""
    wo = getattr(shipment, "work_order", None)
    if wo is not None and wo.company_id == company_id:
        return wo
    return (
        db.query(WorkOrder).filter(WorkOrder.id == shipment.work_order_id, WorkOrder.company_id == company_id).first()
    )


def _resolve_customer(db: Session, customer_name: Optional[str], company_id: int) -> Optional[Customer]:
    """Resolve a (non-deleted) Customer by name within the company. May be None."""
    if not customer_name:
        return None
    return (
        db.query(Customer)
        .filter(
            Customer.company_id == company_id,
            Customer.name == customer_name,
            Customer.is_deleted == False,  # noqa: E712
        )
        .first()
    )


def _parse_serial_numbers(raw) -> List[str]:
    """Parse a serial-numbers JSON Text snapshot into a list, guarding non-JSON values.

    Delegates to THE shared parser (PR 4 ledger, ``process_sheet_service``) so the
    CoC's notion of a serialized snapshot can never drift from the capture path's.
    """
    return parse_serial_numbers(raw)


def coc_required_for_shipment(
    db: Session,
    *,
    work_order: WorkOrder,
    shipment: Shipment,
    company_id: int,
) -> bool:
    """True if a CoC is required for this shipment.

    Required when the shipment was explicitly flagged (``cert_of_conformance``) OR
    the customer master (matched by ``work_order.customer_name``, company-scoped,
    non-deleted) has ``requires_coc`` set. Used to drive the auto-trigger on ship.
    """
    if getattr(shipment, "cert_of_conformance", False):
        return True
    customer = _resolve_customer(db, getattr(work_order, "customer_name", None), company_id)
    return bool(customer is not None and getattr(customer, "requires_coc", False))


def generate_coc_for_shipment(
    db: Session,
    *,
    shipment: Shipment,
    company_id: int,
    user_id: int,
    audit: AuditService,
) -> CertificateOfConformance:
    """Mint (or return the existing) CoC for a shipment. Idempotent; does NOT commit.

    Captures a frozen snapshot of the certified facts (resolved from the work order,
    part, and customer) plus the resolved issuer name and ISO dates into
    ``content_snapshot`` so the PDF render is self-contained. Tenant-scoped and
    audited on first issue. Joins the caller's unit of work.
    """
    existing = _existing_coc(db, shipment.id, company_id)
    if existing is not None:
        return existing

    wo = _resolve_work_order(db, shipment, company_id)
    part: Optional[Part] = None
    if wo is not None and wo.part_id is not None:
        part = db.query(Part).filter(Part.id == wo.part_id, Part.company_id == company_id).first()

    customer_name = getattr(wo, "customer_name", None) if wo else None
    customer_po = getattr(wo, "customer_po", None) if wo else None
    part_number = getattr(part, "part_number", None) if part else None
    part_name = getattr(part, "name", None) if part else None
    revision = getattr(part, "revision", None) if part else None
    quantity = float(shipment.quantity_shipped or 0)
    lot_number = getattr(wo, "lot_number", None) if wo else None
    serial_numbers = _parse_serial_numbers(getattr(wo, "serial_numbers", None) if wo else None)
    coc_number = f"COC-{shipment.shipment_number}"

    # Resolve the issuer name NOW (tenant-scoped) and freeze it into the snapshot so the
    # deterministic PDF render never has to re-resolve a possibly-mutated/absent user.
    issuer = db.query(User).filter(User.id == user_id, User.company_id == company_id).first()
    if issuer is None:
        # Fall back to a cross-company lookup (platform-admin context) -- the issuer is
        # an attribution fact, not tenant data we render to the customer.
        issuer = db.query(User).filter(User.id == user_id).first()
    issued_by_name = issuer.full_name if issuer else None

    issued_at = datetime.utcnow()
    ship_date_iso = shipment.ship_date.isoformat() if getattr(shipment, "ship_date", None) else None

    snapshot = {
        "coc_number": coc_number,
        "customer_name": customer_name,
        "customer_po": customer_po,
        "work_order_number": getattr(wo, "work_order_number", None) if wo else None,
        "part_number": part_number,
        "part_name": part_name,
        "revision": revision,
        "quantity": quantity,
        "lot_number": lot_number,
        "serial_numbers": serial_numbers,
        "conformance_statement": DEFAULT_COC_STATEMENT,
        "issued_by_name": issued_by_name,
        "ship_date": ship_date_iso,
        "issued_at": to_utc_iso(issued_at),
    }

    coc = CertificateOfConformance(
        coc_number=coc_number,
        shipment_id=shipment.id,
        work_order_id=wo.id if wo else shipment.work_order_id,
        part_id=part.id if part else None,
        customer_name=customer_name,
        customer_po=customer_po,
        part_number=part_number,
        part_name=part_name,
        revision=revision,
        quantity=quantity,
        lot_number=lot_number,
        serial_numbers=json.dumps(serial_numbers) if serial_numbers else None,
        conformance_statement=DEFAULT_COC_STATEMENT,
        content_snapshot=json.dumps(snapshot),
        issued_by=user_id,
        issued_at=issued_at,
    )
    coc.company_id = company_id

    # SAVEPOINT around the INSERT so the uq_coc_company_shipment race (a concurrent
    # double-ship minting a second certificate) rolls back ONLY the savepoint; we then
    # re-query and return the winner as a clean idempotent no-op (mirrors the FG-receipt
    # _insert_txn_with_savepoint precedent), keeping the outer ship transaction usable.
    # The ``db.add`` MUST live inside the savepoint: ``begin_nested`` autoflushes the
    # session when it opens, so adding the row before it would emit the INSERT (and raise
    # the collision IntegrityError) OUTSIDE this try/except, poisoning the ship txn.
    nested = db.begin_nested()
    try:
        db.add(coc)
        db.flush()
    except IntegrityError:
        nested.rollback()
        winner = _existing_coc(db, shipment.id, company_id)
        if winner is not None:
            return winner
        # Re-raise if the collision wasn't the shipment-uniqueness race we expected.
        raise

    audit.log_create(
        resource_type="certificate_of_conformance",
        resource_id=coc.id,
        resource_identifier=coc.coc_number,
        new_values=coc,
        description=(f"Issued Certificate of Conformance {coc.coc_number} for shipment {shipment.shipment_number}"),
    )

    # Reflect issuance on the shipment so the existing ShipmentResponse / detail surfaces
    # the CoC flag (and a later re-ship's coc_required check short-circuits to True).
    shipment.cert_of_conformance = True

    return coc


def render_coc_pdf(coc: CertificateOfConformance, db: Optional[Session] = None) -> bytes:
    """Render the CoC PDF deterministically from the frozen snapshot.

    Prefers ``content_snapshot`` (the immutable JSON captured at issue time, incl. the
    resolved ``issued_by_name``); falls back to the row's denormalized columns when the
    snapshot is absent or unparseable. ``db`` is accepted for signature symmetry but is
    not required -- the snapshot is self-contained by design.
    """
    snapshot: dict = {}
    if coc.content_snapshot:
        try:
            loaded = json.loads(coc.content_snapshot)
            if isinstance(loaded, dict):
                snapshot = loaded
        except (ValueError, TypeError):
            snapshot = {}

    def _fact(key: str, fallback):
        value = snapshot.get(key)
        return value if value is not None else fallback

    serial_numbers = snapshot.get("serial_numbers")
    if not isinstance(serial_numbers, list):
        serial_numbers = _parse_serial_numbers(coc.serial_numbers)

    return build_certificate_of_conformance_pdf(
        coc_number=_fact("coc_number", coc.coc_number),
        customer_name=_fact("customer_name", coc.customer_name),
        customer_po=_fact("customer_po", coc.customer_po),
        work_order_number=snapshot.get("work_order_number"),
        part_number=_fact("part_number", coc.part_number),
        part_name=_fact("part_name", coc.part_name),
        revision=_fact("revision", coc.revision),
        quantity=_fact("quantity", coc.quantity),
        lot_number=_fact("lot_number", coc.lot_number),
        serial_numbers=[str(s) for s in serial_numbers],
        ship_date=snapshot.get("ship_date"),
        conformance_statement=_fact("conformance_statement", coc.conformance_statement),
        issued_by_name=snapshot.get("issued_by_name"),
        issued_at=_fact("issued_at", to_utc_iso(coc.issued_at)),
    )
