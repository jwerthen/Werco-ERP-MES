"""Thermal receiving-label print orchestration (ProxyBox / WHTP203e).

The single place the label-printing business logic lives. Routers and the
auto-print ARQ job stay thin and call into ``PrintService``; the service:

* loads the receipt + its part / PO / vendor / location (tenant-scoped),
* renders the 4x6 PDF via ``label_service``,
* persists the PDF as a ``Document`` (RECEIVING_LABEL) for record retention,
* decrypts the ProxyBox key in-memory and submits the print via ``ProxyBoxClient``,
* records the print on the tamper-evident ``AuditService`` and emits an
  ``OperationalEvent``.

COMPLIANCE / SECURITY invariants enforced here (mirrors ``ShippingService``):

* **Tenant isolation.** Every query is scoped by ``company_id`` (the ACTIVE
  company for a request, or the explicit company for a background job). A receipt
  from another tenant is not found -> 404.
* **Egress kill switch.** ``_require_egress`` is called before any ProxyBox call;
  if the profile is missing/inactive or ``allow_print_egress`` is not ``True`` it
  raises ``PrintEgressDisabledError`` and NO outbound call is made.
* **Record retention.** The rendered label is stored as a ``Document`` even when
  the print POST fails, so the UI can reprint; the print failure is surfaced to
  the caller separately.
* **Audit.** Every successful print attempt is recorded via
  ``AuditService.log_create("label_print", ...)`` (never the audit table directly).
* **Secrets.** The ProxyBox API key is decrypted in-memory only and is never
  logged or returned.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Optional, Tuple

from sqlalchemy.orm import Session, joinedload

from app.models.document import Document, DocumentType
from app.models.print_profile import CompanyPrintProfile
from app.models.purchasing import POReceipt, PurchaseOrder, PurchaseOrderLine
from app.services.audit_service import AuditService
from app.services.document_numbering import generate_document_number
from app.services.label_service import build_receiving_label_pdf
from app.services.operational_event_service import OperationalEventService
from app.services.proxybox_client import ProxyBoxClient
from app.services.storage_service import get_storage, resolve_upload_dir

logger = logging.getLogger(__name__)


class PrintEgressDisabledError(Exception):
    """A print to the ProxyBox bridge was attempted while egress is OFF.

    SAFETY: every outbound call to the printer tunnel is gated behind the
    per-company ``allow_print_egress`` flag (defaults FALSE). When it is off (or
    no active profile exists) the service raises this and makes NO external call.
    """


class PrintService:
    """Receiving-label print business logic for one request / unit of work."""

    def __init__(self, db: Session, audit: Optional[AuditService] = None) -> None:
        self.db = db
        self.audit = audit
        self.events = OperationalEventService(db)

    # ------------------------------------------------------------------
    # Profile + egress kill switch.
    # ------------------------------------------------------------------

    def get_profile(self, company_id: int) -> Optional[CompanyPrintProfile]:
        """Tenant-scoped load of the company's print profile (or ``None``)."""
        return self.db.query(CompanyPrintProfile).filter(CompanyPrintProfile.company_id == company_id).first()

    def _require_egress(self, company_id: int) -> CompanyPrintProfile:
        """Gate ProxyBox calls behind ``allow_print_egress``.

        Raises ``PrintEgressDisabledError`` (and makes NO external call) when the
        profile is missing, inactive, or the per-company flag is not explicitly
        ``True``. Call this at the TOP of any method that transmits to the bridge.
        """
        profile = self.get_profile(company_id)
        if profile is None or not profile.is_active or profile.allow_print_egress is not True:
            raise PrintEgressDisabledError(
                "Label-print egress is disabled for this company. Configure the print profile "
                "and enable 'allow_print_egress' before sending labels to the ProxyBox printer."
            )
        if not profile.proxybox_base_url or not profile.proxybox_target or not profile.encrypted_api_key:
            raise PrintEgressDisabledError(
                "Print profile is incomplete: a ProxyBox base URL, target printer, and API key "
                "are all required before a label can be printed."
            )
        return profile

    # ------------------------------------------------------------------
    # Receipt load + label rendering.
    # ------------------------------------------------------------------

    def _get_receipt(self, company_id: int, receipt_id: int) -> POReceipt:
        """Tenant-scoped receipt load with the part / PO / vendor / location joined."""
        receipt = (
            self.db.query(POReceipt)
            .options(
                joinedload(POReceipt.po_line).joinedload(PurchaseOrderLine.part),
                joinedload(POReceipt.po_line)
                .joinedload(PurchaseOrderLine.purchase_order)
                .joinedload(PurchaseOrder.vendor),
                joinedload(POReceipt.location),
            )
            .filter(POReceipt.id == receipt_id, POReceipt.company_id == company_id)
            .first()
        )
        if receipt is None:
            # "not found" is mapped to 404 by the router (mirrors the carrier path).
            raise LookupError("Receipt not found")
        return receipt

    def build_label_for_receipt(self, company_id: int, receipt_id: int) -> bytes:
        """Render the 4x6 receiving-label PDF for a receipt (tenant-scoped). Returns bytes."""
        receipt = self._get_receipt(company_id, receipt_id)
        return self._render_label(receipt)

    @staticmethod
    def _render_label(receipt: POReceipt) -> bytes:
        po_line = receipt.po_line
        part = po_line.part if po_line else None
        po = po_line.purchase_order if po_line else None
        vendor = po.vendor if po else None
        location = receipt.location

        received = receipt.received_at.strftime("%Y-%m-%d") if receipt.received_at else None

        return build_receiving_label_pdf(
            part_number=part.part_number if part else None,
            revision=part.revision if part else None,
            part_description=(part.description or part.name) if part else None,
            quantity=receipt.quantity_received,
            unit_of_measure=(
                part.unit_of_measure.value
                if part and hasattr(part.unit_of_measure, "value")
                else (part.unit_of_measure if part else None)
            ),
            lot_number=receipt.lot_number,
            serial_numbers=receipt.serial_numbers,
            heat_number=receipt.heat_number,
            po_number=po.po_number if po else None,
            vendor_name=vendor.name if vendor else None,
            receipt_number=receipt.receipt_number,
            received_date=received,
            location_code=location.code if location else None,
            is_critical=bool(part.is_critical) if part else False,
        )

    # ------------------------------------------------------------------
    # Print orchestration.
    # ------------------------------------------------------------------

    async def print_receipt_label(
        self,
        company_id: int,
        receipt_id: int,
        user_id: int,
        *,
        copies: Optional[int] = None,
    ) -> Tuple[Document, bool]:
        """Render + store the label, send it to ProxyBox, audit it.

        Order of operations (record retention first):
          1. Require egress (raises ``PrintEgressDisabledError`` when OFF -> 409).
          2. Render the PDF and persist it as a ``Document`` (RECEIVING_LABEL),
             link it onto ``POReceipt.label_document_id``, audit + emit, COMMIT --
             so the label is retained even if the print POST then fails.
          3. Decrypt the key, construct ``ProxyBoxClient``, ``print_and_wait``. A
             provider/printer failure raises ``ProxyBoxError`` AFTER the Document is
             safely committed, so the caller can surface the failure while the UI
             can still reprint.

        Returns ``(document, printed_ok)``. ``printed_ok`` is ``True`` when the
        bridge accepted the job (or reported success); a hard print failure raises
        ``ProxyBoxError`` instead of returning.
        """
        profile = self._require_egress(company_id)
        receipt = self._get_receipt(company_id, receipt_id)

        pdf_bytes = self._render_label(receipt)
        document = self._store_label_document(company_id=company_id, receipt=receipt, data=pdf_bytes, user_id=user_id)
        receipt.label_document_id = document.id

        # Audit the label generation/print on the tamper-evident chain (NO secrets).
        if self.audit is not None:
            self.audit.log_create(
                resource_type="label_print",
                resource_id=receipt.id,
                resource_identifier=receipt.receipt_number,
                new_values={
                    "receipt_number": receipt.receipt_number,
                    "lot_number": receipt.lot_number,
                    "label_document_id": document.id,
                    "paper_size": profile.default_paper_size,
                    "target": profile.proxybox_target,
                },
                description=(
                    f"Receiving label printed for receipt {receipt.receipt_number} " f"(lot {receipt.lot_number})"
                ),
            )

        self.events.emit(
            company_id=company_id,
            event_type="receiving_label_printed",
            source_module="receiving",
            entity_type="po_receipt",
            entity_id=receipt.id,
            user_id=user_id,
            severity="info",
            event_payload={
                "receipt_number": receipt.receipt_number,
                "lot_number": receipt.lot_number,
                "label_document_id": document.id,
                "target": profile.proxybox_target,
            },
        )

        # Commit the Document + link + audit + event BEFORE the network call, so the
        # label is retained for reprint even if the printer is unreachable.
        self.db.commit()
        self.db.refresh(document)

        # Decrypt the key in-memory only (never logged / returned).
        api_key = profile.get_api_key()
        client = ProxyBoxClient(
            base_url=profile.proxybox_base_url,
            api_key=api_key,
            target=profile.proxybox_target,
            timeout=_proxybox_timeout(),
        )
        effective_copies = copies if copies is not None else (profile.default_copies or 1)
        # A ProxyBoxError here propagates to the caller (router -> 502); the Document
        # is already committed so a reprint is possible.
        await client.print_and_wait(
            pdf_bytes,
            copies=effective_copies,
            paper_size=profile.default_paper_size or "4x6",
            poll_interval=_proxybox_poll_interval(),
            max_wait=_proxybox_max_wait(),
        )
        return document, True

    # ------------------------------------------------------------------
    # Document storage (mirrors ShippingService._store_artifact_document).
    # ------------------------------------------------------------------

    def _store_label_document(self, *, company_id: int, receipt: POReceipt, data: bytes, user_id: int) -> Document:
        """Persist the rendered label PDF as a ``Document`` (same storage path as labels/BOLs)."""
        storage = get_storage()
        unique_name = f"{uuid.uuid4()}.pdf"
        if storage.is_remote:
            key = f"{company_id}/receiving/{unique_name}"
        else:
            key = os.path.join(resolve_upload_dir(), unique_name)
        file_path = storage.save(data, key=key)

        document = Document(
            document_number=self._generate_document_number(DocumentType.RECEIVING_LABEL.value),
            revision="A",
            title=f"Receiving label {receipt.receipt_number}",
            document_type=DocumentType.RECEIVING_LABEL,
            file_name=f"{receipt.receipt_number}.pdf",
            file_path=file_path,
            file_size=len(data),
            mime_type="application/pdf",
            status="released",
            created_by=user_id,
        )
        document.company_id = company_id
        self.db.add(document)
        self.db.flush()
        return document

    def _generate_document_number(self, doc_type: str) -> str:
        """Delegates to the shared global-lock generator (PR 4 dedupe of a 4x copy)."""
        return generate_document_number(self.db, doc_type)


# ---------------------------------------------------------------------------
# Tunable timeouts (env-backed via settings; conservative defaults).
# ---------------------------------------------------------------------------


def _proxybox_timeout() -> float:
    from app.core.config import settings

    return float(getattr(settings, "PROXYBOX_TIMEOUT_SECONDS", 30.0) or 30.0)


def _proxybox_poll_interval() -> float:
    from app.core.config import settings

    return float(getattr(settings, "PROXYBOX_POLL_INTERVAL_SECONDS", 1.0) or 1.0)


def _proxybox_max_wait() -> float:
    from app.core.config import settings

    return float(getattr(settings, "PROXYBOX_MAX_WAIT_SECONDS", 30.0) or 30.0)
