"""Multi-carrier shipping service.

The single place the carrier-integration business logic lives. Routers stay thin
and call into ``ShippingService``; the service talks to the provider abstraction
(``app.services.carriers``), persists rate quotes / packages / tracking events,
stores label & BOL PDFs as ``Document`` rows, and audits money-moving actions.

COMPLIANCE / SECURITY invariants enforced here:

* **Tenant isolation.** Every query is scoped by ``company_id`` (the ACTIVE
  company, passed by the caller from ``get_current_company_id``). Provider
  selection loads the company's own ``CarrierAccount`` only.
* **Customer-data egress kill switch.** ``_require_egress`` is called at the top
  of every method that transmits customer data to the aggregator (address
  validation, rate-shop, buy-label, buy-bol, schedule-pickup, void/refund). If
  ``CompanyShippingProfile.allow_carrier_egress`` is not ``True`` it raises
  ``EgressDisabledError`` and NO external call is made. A pure credential
  ``test_connection`` (sends no customer data) is exempt.
* **Audit.** Label/BOL purchase, void, and refund are financial transactions and
  are recorded through the tamper-evident ``AuditService`` (never the audit table
  directly).
* **Idempotency.** ``buy_label`` / ``buy_freight_bol`` compute a deterministic
  idempotency key, pre-check for an already-purchased label/BOL (no-op return),
  and pass the key to the provider (defense in depth with the DB partial-unique
  index ``uq_shipment_idempotency``).
* **Secrets.** Plaintext API keys / webhook secrets are NEVER logged, returned in
  API responses (last4 only), or placed in audit / operational-event payloads.
* **Money is Decimal** end to end (the columns are ``Numeric(12, 2)``).

Tracking events are informational: a ``DELIVERED`` event sets
``actual_delivery`` but DOES NOT auto-close the work order (product decision --
the manual ``mark_shipped`` path remains the only WO-closing action).
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.carrier_account import CarrierAccount, CompanyShippingProfile
from app.models.document import Document, DocumentType
from app.models.shipping import (
    Shipment,
    ShipmentPackage,
    ShipmentRateQuote,
    ShipmentTrackingEvent,
)
from app.services.audit_service import AuditService
from app.services.carriers import registry
from app.services.carriers.crypto import decrypt_secret
from app.services.carriers.exceptions import CarrierError, EgressDisabledError
from app.services.carriers.types import (
    AddressValidationResult,
    BillOfLading,
    CarrierAddress,
    Label,
    PalletDimensions,
    ParcelDimensions,
    Pickup,
    RateQuote,
    TrackingEvent,
    TrackingStatus,
)
from app.services.operational_event_service import OperationalEventService

logger = logging.getLogger(__name__)


def _resolve_upload_dir() -> str:
    """Resolve the label/BOL storage directory.

    Mirrors ``app/api/endpoints/documents.py`` so carrier artifacts land on the
    SAME local-disk storage as every other ``Document`` (S3 is out of scope).
    """
    preferred_dir = os.getenv("UPLOAD_DIR", "/app/uploads")
    try:
        os.makedirs(preferred_dir, exist_ok=True)
        return preferred_dir
    except OSError:
        fallback_dir = os.path.abspath(os.getenv("UPLOAD_DIR_FALLBACK", "./uploads"))
        os.makedirs(fallback_dir, exist_ok=True)
        return fallback_dir


class ShippingService:
    """Carrier-integration business logic for one request / unit of work."""

    def __init__(self, db: Session, audit: Optional[AuditService] = None) -> None:
        self.db = db
        self.audit = audit
        self.events = OperationalEventService(db)

    # ------------------------------------------------------------------
    # Egress kill switch + provider selection.
    # ------------------------------------------------------------------

    def _get_profile(self, company_id: int) -> Optional[CompanyShippingProfile]:
        return self.db.query(CompanyShippingProfile).filter(CompanyShippingProfile.company_id == company_id).first()

    def _require_egress(self, company_id: int) -> CompanyShippingProfile:
        """Gate customer-data-bearing carrier calls behind ``allow_carrier_egress``.

        Raises ``EgressDisabledError`` (and makes NO external call) when the
        per-company flag is missing or not explicitly ``True``. Call this at the
        TOP of every method that transmits customer data.
        """
        profile = self._get_profile(company_id)
        if profile is None or profile.allow_carrier_egress is not True:
            raise EgressDisabledError(
                "Carrier egress is disabled for this company. Enable "
                "'allow_carrier_egress' on the company shipping profile after "
                "CUI / data-egress sign-off before transmitting customer data to "
                "a carrier."
            )
        return profile

    def _load_carrier_account(self, company_id: int, carrier_account_id: Optional[int] = None) -> CarrierAccount:
        """Load the tenant's carrier account (explicit id, else the default).

        Tenant-scoped and soft-delete aware. Raises ``CarrierError`` when no
        usable account is configured.
        """
        query = self.db.query(CarrierAccount).filter(
            CarrierAccount.company_id == company_id,
            CarrierAccount.is_deleted == False,  # noqa: E712
        )
        if carrier_account_id is not None:
            account = query.filter(CarrierAccount.id == carrier_account_id).first()
            if account is None:
                raise CarrierError("Carrier account not found for this company")
            return account

        # No explicit account: prefer the default, then any active account.
        account = query.filter(
            CarrierAccount.is_active == True, CarrierAccount.is_default == True
        ).first()  # noqa: E712
        if account is None:
            account = query.filter(CarrierAccount.is_active == True).first()  # noqa: E712
        if account is None:
            raise CarrierError("No active carrier account configured for this company")
        return account

    def _provider_for(self, company_id: int, carrier_account_id: Optional[int] = None):
        """Return the concrete ``CarrierProvider`` for the tenant's account."""
        account = self._load_carrier_account(company_id, carrier_account_id)
        return registry.get_provider(account), account

    # ------------------------------------------------------------------
    # Tenant-scoped shipment lookup.
    # ------------------------------------------------------------------

    def _get_shipment(self, company_id: int, shipment_id: int) -> Shipment:
        shipment = (
            self.db.query(Shipment)
            .filter(
                Shipment.id == shipment_id,
                Shipment.company_id == company_id,
                Shipment.is_deleted == False,  # noqa: E712
            )
            .first()
        )
        if shipment is None:
            raise CarrierError("Shipment not found")
        return shipment

    def _resolve_shipment_by_aggregator_id(
        self,
        *,
        provider_shipment_id: Optional[str],
        tracking_number: Optional[str],
        company_id: int,
        carrier_account_id: Optional[int] = None,
    ) -> Optional[Shipment]:
        """Resolve the owning shipment from STORED data, SCOPED to the verifying tenant.

        Used by the inbound-webhook path. The webhook handler establishes trust by
        HMAC-verifying the request against a SPECIFIC ``CarrierAccount``'s secret;
        ``company_id`` (and, when known, ``carrier_account_id``) is that verifying
        account's owner and MUST be passed here so the lookup cannot cross the trust
        boundary. Without this scoping a tenant A holding a valid webhook secret could
        sign a body carrying tenant B's stored ``aggregator_shipment_id`` and write
        forged tracking events onto tenant B's shipment (cross-tenant write).

        Resolution is by stored ``aggregator_shipment_id`` (or ``tracking_number``),
        NEVER by anything the caller is free to choose outside its own tenant. The
        ``company_id`` scope is the security-critical boundary. When
        ``carrier_account_id`` is supplied it adds defense-in-depth: the match is
        constrained to that account OR to a shipment with no account assigned yet
        (a NULL ``carrier_account_id`` -- e.g. a shipment rated/labelled before the
        account stamp landed), so a tenant with multiple accounts cannot forge
        events across its own accounts while a legitimately-owned but unstamped
        shipment is still resolvable. Returns ``None`` when nothing matches (the
        caller drops the event).
        """

        def _scoped():
            q = self.db.query(Shipment).filter(
                Shipment.company_id == company_id,
                Shipment.is_deleted == False,  # noqa: E712
            )
            if carrier_account_id is not None:
                q = q.filter(
                    (Shipment.carrier_account_id == carrier_account_id) | (Shipment.carrier_account_id.is_(None))
                )
            return q

        if provider_shipment_id:
            shipment = _scoped().filter(Shipment.aggregator_shipment_id == provider_shipment_id).first()
            if shipment is not None:
                return shipment
        if tracking_number:
            return _scoped().filter(Shipment.tracking_number == tracking_number).first()
        return None

    # ------------------------------------------------------------------
    # Address / dimension mapping helpers.
    # ------------------------------------------------------------------

    @staticmethod
    def _to_carrier_address(data) -> CarrierAddress:
        """Coerce a schema/dict/CarrierAddress into a normalized ``CarrierAddress``."""
        if isinstance(data, CarrierAddress):
            return data
        if hasattr(data, "model_dump"):
            data = data.model_dump()
        return CarrierAddress(**data)

    def _ship_from_from_profile(self, profile: CompanyShippingProfile) -> CarrierAddress:
        return CarrierAddress(
            name=profile.ship_from_name,
            company=profile.ship_from_company,
            phone=profile.ship_from_phone,
            email=profile.ship_from_email,
            street1=profile.ship_from_street1 or "",
            street2=profile.ship_from_street2,
            city=profile.ship_from_city or "",
            state=profile.ship_from_state or "",
            zip=profile.ship_from_zip or "",
            country=profile.ship_from_country or "US",
        )

    @staticmethod
    def _ship_to_from_shipment(shipment: Shipment) -> CarrierAddress:
        return CarrierAddress(
            name=shipment.ship_to_name,
            street1=shipment.ship_to_address or "",
            city=shipment.ship_to_city or "",
            state=shipment.ship_to_state or "",
            zip=shipment.ship_to_zip or "",
            country="US",
        )

    @staticmethod
    def _to_parcels(items) -> List[ParcelDimensions]:
        out: List[ParcelDimensions] = []
        for item in items or []:
            if isinstance(item, ParcelDimensions):
                out.append(item)
            elif hasattr(item, "model_dump"):
                out.append(ParcelDimensions(**item.model_dump()))
            else:
                out.append(ParcelDimensions(**item))
        return out

    @staticmethod
    def _to_pallets(items) -> List[PalletDimensions]:
        out: List[PalletDimensions] = []
        for item in items or []:
            if isinstance(item, PalletDimensions):
                out.append(item)
            elif hasattr(item, "model_dump"):
                out.append(PalletDimensions(**item.model_dump()))
            else:
                out.append(PalletDimensions(**item))
        return out

    @staticmethod
    def _compute_idempotency_key(shipment: Shipment, rate_id: str) -> str:
        """Deterministic idempotency key for a buy-label/buy-bol against a rate.

        Stable across retries of the SAME purchase (same shipment + rate), so a
        re-submitted buy collides on the partial-unique index and the provider
        idempotency header makes the carrier side a no-op too.
        """
        raw = f"{shipment.company_id}:{shipment.id}:{rate_id}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:64]

    # ------------------------------------------------------------------
    # Credential-only connection test (EXEMPT from egress -- no customer data).
    # ------------------------------------------------------------------

    async def test_connection(self, company_id: int, carrier_account_id: int) -> Tuple[bool, Optional[str]]:
        """Validate stored credentials WITHOUT transmitting any customer data.

        Exempt from the egress kill switch by design (sends no ship-to / customer
        identity). Performs a benign provider round-trip that exercises auth only.
        Never returns or logs the plaintext key.
        """
        account = self._load_carrier_account(company_id, carrier_account_id)
        try:
            # Validate the account maps to a known provider (also surfaces a clean
            # NotSupportedError for an unimplemented provider), then exercise auth
            # with a no-customer-data call. For EasyPost a benign GET against the
            # base API with the configured key validates the credential.
            registry.get_provider(account)
            import httpx

            api_key = decrypt_secret(account.encrypted_api_key)
            async with httpx.AsyncClient(
                base_url="https://api.easypost.com/v2",
                auth=(api_key, ""),
                timeout=15.0,
            ) as client:
                resp = await client.get("/users")
            if resp.status_code < 400:
                return True, "Credentials accepted"
            return False, f"Provider rejected credentials (HTTP {resp.status_code})"
        except CarrierError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001 - never leak internals/secrets
            logger.warning("Carrier connection test failed for account %s: %s", account.id, type(exc).__name__)
            return False, "Connection test failed"

    # ------------------------------------------------------------------
    # Address validation (transmits customer data -> egress-gated).
    # ------------------------------------------------------------------

    async def validate_address(
        self, company_id: int, address, carrier_account_id: Optional[int] = None
    ) -> AddressValidationResult:
        self._require_egress(company_id)
        provider, _ = self._provider_for(company_id, carrier_account_id)
        carrier_address = self._to_carrier_address(address)
        return await provider.validate_address(carrier_address)

    # ------------------------------------------------------------------
    # Rate shopping (transmits customer data -> egress-gated). Persists quotes.
    # ------------------------------------------------------------------

    async def rate_shop(
        self,
        company_id: int,
        shipment_id: int,
        *,
        parcels=None,
        pallets=None,
        ship_from=None,
        ship_to=None,
        carrier_account_id: Optional[int] = None,
        user_id: Optional[int] = None,
    ) -> List[ShipmentRateQuote]:
        """Rate-shop a shipment and persist the returned quotes.

        Replaces any prior quote set for the shipment, stores the provider's
        shipment id on the shipment (the webhook tenant-resolution key), and emits
        a ``shipment_rates_fetched`` operational event. Returns the persisted
        ``ShipmentRateQuote`` rows. Commits.
        """
        self._require_egress(company_id)
        shipment = self._get_shipment(company_id, shipment_id)
        provider, account = self._provider_for(company_id, carrier_account_id)

        profile = self._get_profile(company_id)
        ship_from_addr = (
            self._to_carrier_address(ship_from)
            if ship_from is not None
            else (self._ship_from_from_profile(profile) if profile else None)
        )
        if ship_from_addr is None:
            raise CarrierError("No ship-from address: configure the company shipping profile or pass ship_from")
        ship_to_addr = (
            self._to_carrier_address(ship_to) if ship_to is not None else self._ship_to_from_shipment(shipment)
        )

        parcel_dims = self._to_parcels(parcels)
        pallet_dims = self._to_pallets(pallets)

        quotes: List[RateQuote] = await provider.get_rates(
            ship_from=ship_from_addr,
            ship_to=ship_to_addr,
            parcels=parcel_dims,
            pallets=pallet_dims,
        )

        # Persist: replace the prior quote set (compliance keeps the LATEST shop).
        self.db.query(ShipmentRateQuote).filter(
            ShipmentRateQuote.shipment_id == shipment.id,
            ShipmentRateQuote.company_id == company_id,
        ).delete(synchronize_session=False)

        persisted: List[ShipmentRateQuote] = []
        aggregator_shipment_id: Optional[str] = None
        fetched_at = datetime.utcnow()
        for quote in quotes:
            # provider_rate_id is the composite "<shipment_id>:<rate_id>"; capture
            # the provider shipment id once for webhook tenant resolution.
            if aggregator_shipment_id is None and ":" in (quote.provider_rate_id or ""):
                aggregator_shipment_id = quote.provider_rate_id.split(":", 1)[0] or None
            row = ShipmentRateQuote(
                shipment_id=shipment.id,
                provider_rate_id=quote.provider_rate_id,
                carrier=quote.carrier,
                service_code=quote.service_code,
                service_name=quote.service_name,
                mode=quote.mode,
                amount=quote.amount,
                currency=quote.currency,
                est_delivery_days=quote.est_delivery_days,
                est_delivery_date=quote.est_delivery_date,
                is_selected=False,
                fetched_at=fetched_at,
            )
            row.company_id = company_id
            self.db.add(row)
            persisted.append(row)

        if aggregator_shipment_id:
            shipment.aggregator_shipment_id = aggregator_shipment_id
        if account is not None:
            shipment.carrier_account_id = account.id
        if pallet_dims and not parcel_dims:
            shipment.ship_mode = "freight"
        elif parcel_dims:
            shipment.ship_mode = "parcel"

        self.db.flush()

        # Operational event -- NO secrets, only carrier/cost summary.
        cheapest = min((q.amount for q in quotes), default=None)
        self.events.emit(
            company_id=company_id,
            event_type="shipment_rates_fetched",
            source_module="shipping",
            entity_type="shipment",
            entity_id=shipment.id,
            work_order_id=shipment.work_order_id,
            user_id=user_id,
            severity="info",
            event_payload={
                "shipment_number": shipment.shipment_number,
                "rate_count": len(quotes),
                "carriers": sorted({q.carrier for q in quotes if q.carrier}),
                "cheapest_amount": str(cheapest) if cheapest is not None else None,
            },
        )
        self.db.commit()
        for row in persisted:
            self.db.refresh(row)
        return persisted

    # ------------------------------------------------------------------
    # Buy parcel label (transmits customer data -> egress-gated). IDEMPOTENT.
    # ------------------------------------------------------------------

    async def buy_label(
        self,
        company_id: int,
        shipment_id: int,
        rate_id: str,
        user_id: int,
        *,
        carrier_account_id: Optional[int] = None,
    ) -> Tuple[Shipment, bool]:
        """Purchase a parcel label, store the PDF as a ``Document``, audit it.

        IDEMPOTENT: if a label was already purchased for this shipment
        (``label_document_id`` or ``label_purchased_at`` set), returns the
        existing shipment with ``already_purchased=True`` and makes NO provider
        call. Otherwise computes a deterministic idempotency key (passed to the
        provider), buys, stores the label, sets the financial fields, COMMITS,
        then emits an operational event AND writes a tamper-evident audit entry.

        Returns ``(shipment, already_purchased)``.
        """
        self._require_egress(company_id)
        shipment = self._get_shipment(company_id, shipment_id)

        # Pre-check no-op (idempotency).
        if shipment.label_document_id is not None or shipment.label_purchased_at is not None:
            return shipment, True

        provider, account = self._provider_for(company_id, carrier_account_id)
        provider_shipment_id = shipment.aggregator_shipment_id or ""
        idempotency_key = self._compute_idempotency_key(shipment, rate_id)

        label: Label = await provider.buy_label(
            provider_shipment_id,
            rate_id,
            idempotency_key=idempotency_key,
        )

        # Persist the label PDF as a Document (same local-disk storage as uploads).
        label_document = self._store_label_document(
            company_id=company_id,
            shipment=shipment,
            label=label,
            user_id=user_id,
        )

        # Reflect the purchase on the shipment (money is Decimal).
        shipment.idempotency_key = idempotency_key
        shipment.carrier_account_id = account.id if account else shipment.carrier_account_id
        shipment.selected_rate_id = rate_id
        shipment.service_code = label.service_code or shipment.service_code
        shipment.carrier = label.carrier or shipment.carrier
        shipment.tracking_number = label.tracking_number or shipment.tracking_number
        shipment.actual_cost = label.cost
        shipment.cost_currency = shipment.cost_currency or "USD"
        shipment.label_purchased_at = datetime.utcnow()
        shipment.ship_mode = "parcel"
        if label_document is not None:
            shipment.label_document_id = label_document.id
        if label.provider_shipment_id:
            shipment.aggregator_shipment_id = label.provider_shipment_id

        # Mark the selected quote (best-effort; for the compliance trail).
        self._mark_selected_quote(company_id, shipment.id, rate_id)

        self.db.flush()

        # Operational event -- carrier/cost only, NO secrets.
        self.events.emit(
            company_id=company_id,
            event_type="shipment_label_purchased",
            source_module="shipping",
            entity_type="shipment",
            entity_id=shipment.id,
            work_order_id=shipment.work_order_id,
            user_id=user_id,
            severity="info",
            event_payload={
                "shipment_number": shipment.shipment_number,
                "carrier": shipment.carrier,
                "service_code": shipment.service_code,
                "tracking_number": shipment.tracking_number,
                "amount": str(shipment.actual_cost) if shipment.actual_cost is not None else None,
                "currency": shipment.cost_currency,
            },
        )

        # Financial-transaction audit (tamper-evident). NO secrets in extra_data.
        if self.audit is not None:
            self.audit.log(
                action=AuditService.ACTIONS["CREATE"],
                resource_type="shipment",
                resource_id=shipment.id,
                resource_identifier=shipment.shipment_number,
                description=(
                    f"Label purchased: {self._money_str(shipment.actual_cost, shipment.cost_currency)} "
                    f"via {shipment.carrier or 'carrier'} {shipment.service_code or ''}".strip()
                ),
                extra_data={
                    "cost": str(shipment.actual_cost) if shipment.actual_cost is not None else None,
                    "currency": shipment.cost_currency,
                    "carrier": shipment.carrier,
                    "service_code": shipment.service_code,
                    "rate_id": rate_id,
                    "tracking_number": shipment.tracking_number,
                    "label_document_id": shipment.label_document_id,
                },
            )

        try:
            self.db.commit()
        except IntegrityError:
            # Concurrent double-buy: a sibling request committed the SAME
            # (company_id, idempotency_key) first and won the partial-unique index
            # uq_shipment_idempotency. The carrier's Idempotency-Key already made the
            # provider side a no-op (no double charge); roll back our losing txn (its
            # Document / audit / event rows unwind) and return the already-purchased
            # no-op, mirroring the pre-check path -- NOT a raw 500.
            self.db.rollback()
            shipment = self._get_shipment(company_id, shipment_id)
            return shipment, True
        self.db.refresh(shipment)
        return shipment, False

    # ------------------------------------------------------------------
    # Buy freight BOL (transmits customer data -> egress-gated). IDEMPOTENT.
    # ------------------------------------------------------------------

    async def buy_freight_bol(
        self,
        company_id: int,
        shipment_id: int,
        rate_id: str,
        user_id: int,
        *,
        carrier_account_id: Optional[int] = None,
    ) -> Tuple[Shipment, bool]:
        """Purchase an LTL Bill of Lading, store the PDF, audit it.

        Mirrors ``buy_label`` for freight: idempotent pre-check on
        ``bol_document_id`` / ``label_purchased_at``, stores the BOL Document,
        sets ``bol_number`` / ``pro_number`` / ``actual_cost``, commits, emits an
        operational event, and writes a money-moving audit entry. The EasyPost
        adapter raises ``NotSupportedError`` (freight is Zenkraft's job); the
        service layer is provider-agnostic and unchanged when that lands.

        Returns ``(shipment, already_purchased)``.
        """
        self._require_egress(company_id)
        shipment = self._get_shipment(company_id, shipment_id)

        if shipment.bol_document_id is not None or (
            shipment.label_purchased_at is not None and shipment.ship_mode == "freight"
        ):
            return shipment, True

        provider, account = self._provider_for(company_id, carrier_account_id)
        provider_shipment_id = shipment.aggregator_shipment_id or ""
        idempotency_key = self._compute_idempotency_key(shipment, rate_id)

        bol: BillOfLading = await provider.buy_bol(
            provider_shipment_id,
            rate_id,
            idempotency_key=idempotency_key,
        )

        bol_document = self._store_bol_document(
            company_id=company_id,
            shipment=shipment,
            bol=bol,
            user_id=user_id,
        )

        shipment.idempotency_key = idempotency_key
        shipment.carrier_account_id = account.id if account else shipment.carrier_account_id
        shipment.selected_rate_id = rate_id
        shipment.carrier = bol.carrier or shipment.carrier
        shipment.bol_number = bol.bol_number or shipment.bol_number
        shipment.pro_number = bol.pro_number or shipment.pro_number
        if bol.cost is not None:
            shipment.actual_cost = bol.cost
        shipment.cost_currency = shipment.cost_currency or "USD"
        shipment.label_purchased_at = datetime.utcnow()
        shipment.ship_mode = "freight"
        if bol_document is not None:
            shipment.bol_document_id = bol_document.id
        if bol.provider_shipment_id:
            shipment.aggregator_shipment_id = bol.provider_shipment_id

        self._mark_selected_quote(company_id, shipment.id, rate_id)
        self.db.flush()

        self.events.emit(
            company_id=company_id,
            event_type="shipment_bol_purchased",
            source_module="shipping",
            entity_type="shipment",
            entity_id=shipment.id,
            work_order_id=shipment.work_order_id,
            user_id=user_id,
            severity="info",
            event_payload={
                "shipment_number": shipment.shipment_number,
                "carrier": shipment.carrier,
                "bol_number": shipment.bol_number,
                "pro_number": shipment.pro_number,
                "amount": str(shipment.actual_cost) if shipment.actual_cost is not None else None,
                "currency": shipment.cost_currency,
            },
        )

        if self.audit is not None:
            self.audit.log(
                action=AuditService.ACTIONS["CREATE"],
                resource_type="shipment",
                resource_id=shipment.id,
                resource_identifier=shipment.shipment_number,
                description=(
                    f"BOL purchased: {self._money_str(shipment.actual_cost, shipment.cost_currency)} "
                    f"via {shipment.carrier or 'carrier'} BOL {shipment.bol_number or ''}".strip()
                ),
                extra_data={
                    "cost": str(shipment.actual_cost) if shipment.actual_cost is not None else None,
                    "currency": shipment.cost_currency,
                    "carrier": shipment.carrier,
                    "bol_number": shipment.bol_number,
                    "pro_number": shipment.pro_number,
                    "rate_id": rate_id,
                    "bol_document_id": shipment.bol_document_id,
                },
            )

        try:
            self.db.commit()
        except IntegrityError:
            # Concurrent double-buy on the same (company_id, idempotency_key) -- see
            # the buy_label note. Roll back the losing txn and return the
            # already-purchased no-op instead of surfacing a raw 500.
            self.db.rollback()
            shipment = self._get_shipment(company_id, shipment_id)
            return shipment, True
        self.db.refresh(shipment)
        return shipment, False

    # ------------------------------------------------------------------
    # Schedule pickup (transmits customer data -> egress-gated).
    # ------------------------------------------------------------------

    async def schedule_pickup(
        self,
        company_id: int,
        shipment_id: int,
        *,
        pickup_date: str,
        window_start: str,
        window_end: str,
        carrier_account_id: Optional[int] = None,
        user_id: Optional[int] = None,
    ) -> Pickup:
        """Schedule a carrier pickup for an already-purchased shipment."""
        self._require_egress(company_id)
        shipment = self._get_shipment(company_id, shipment_id)
        provider, _ = self._provider_for(company_id, carrier_account_id)
        if not shipment.aggregator_shipment_id:
            raise CarrierError("Shipment has no provider shipment id; buy a label before scheduling a pickup")

        pickup = await provider.schedule_pickup(
            shipment.aggregator_shipment_id,
            pickup_date=pickup_date,
            window_start=window_start,
            window_end=window_end,
        )

        self.events.emit(
            company_id=company_id,
            event_type="shipment_pickup_scheduled",
            source_module="shipping",
            entity_type="shipment",
            entity_id=shipment.id,
            work_order_id=shipment.work_order_id,
            user_id=user_id,
            severity="info",
            event_payload={
                "shipment_number": shipment.shipment_number,
                "confirmation_number": pickup.confirmation_number,
                "scheduled_date": pickup.scheduled_date.isoformat() if pickup.scheduled_date else None,
                "status": pickup.status,
            },
        )
        self.db.commit()
        return pickup

    # ------------------------------------------------------------------
    # Void / refund (money-moving CANCEL -> egress-gated + audited).
    # ------------------------------------------------------------------

    async def void_label(
        self,
        company_id: int,
        shipment_id: int,
        user_id: int,
        *,
        carrier_account_id: Optional[int] = None,
    ) -> Shipment:
        """Void/refund a purchased label. Audited as a money-moving CANCEL."""
        self._require_egress(company_id)
        shipment = self._get_shipment(company_id, shipment_id)
        if shipment.label_purchased_at is None and shipment.label_document_id is None:
            raise CarrierError("No purchased label to void for this shipment")
        if shipment.voided_at is not None:
            return shipment  # idempotent: already voided

        provider, _ = self._provider_for(company_id, carrier_account_id)
        refund_status = await self._provider_refund(provider, shipment)

        shipment.voided_at = datetime.utcnow()
        shipment.refund_status = refund_status or "submitted"
        self.db.flush()

        self.events.emit(
            company_id=company_id,
            event_type="shipment_label_voided",
            source_module="shipping",
            entity_type="shipment",
            entity_id=shipment.id,
            work_order_id=shipment.work_order_id,
            user_id=user_id,
            severity="medium",
            event_payload={
                "shipment_number": shipment.shipment_number,
                "carrier": shipment.carrier,
                "refund_status": shipment.refund_status,
                "amount": str(shipment.actual_cost) if shipment.actual_cost is not None else None,
                "currency": shipment.cost_currency,
            },
        )

        if self.audit is not None:
            self.audit.log(
                action=AuditService.ACTIONS["CANCEL"],
                resource_type="shipment",
                resource_id=shipment.id,
                resource_identifier=shipment.shipment_number,
                description=(
                    f"Label voided / refund requested: "
                    f"{self._money_str(shipment.actual_cost, shipment.cost_currency)} "
                    f"via {shipment.carrier or 'carrier'}".strip()
                ),
                extra_data={
                    "cost": str(shipment.actual_cost) if shipment.actual_cost is not None else None,
                    "currency": shipment.cost_currency,
                    "carrier": shipment.carrier,
                    "refund_status": shipment.refund_status,
                    "tracking_number": shipment.tracking_number,
                },
            )

        self.db.commit()
        self.db.refresh(shipment)
        return shipment

    # refund_label is an alias for void_label (EasyPost void == refund request).
    async def refund_label(
        self,
        company_id: int,
        shipment_id: int,
        user_id: int,
        *,
        carrier_account_id: Optional[int] = None,
    ) -> Shipment:
        return await self.void_label(company_id, shipment_id, user_id, carrier_account_id=carrier_account_id)

    async def _provider_refund(self, provider, shipment: Shipment) -> Optional[str]:
        """Best-effort provider-side refund. EasyPost refunds via POST /shipments/{id}/refund.

        Implemented inline (the ABC has no dedicated refund method); falls back to
        recording the local void when the provider call is not available so the
        compliance record (voided_at / audit) is still written.
        """
        if not shipment.aggregator_shipment_id:
            return "local_void"
        try:
            import httpx

            # Re-derive the api key path via the adapter's client construction --
            # but we only need a refund POST; reuse the account decryption.
            account = (
                self.db.query(CarrierAccount).filter(CarrierAccount.id == shipment.carrier_account_id).first()
                if shipment.carrier_account_id
                else None
            )
            if account is None or not account.encrypted_api_key:
                return "local_void"
            api_key = decrypt_secret(account.encrypted_api_key)
            async with httpx.AsyncClient(
                base_url="https://api.easypost.com/v2",
                auth=(api_key, ""),
                timeout=30.0,
            ) as client:
                resp = await client.post(f"/shipments/{shipment.aggregator_shipment_id}/refund")
            if resp.status_code < 400:
                try:
                    body = resp.json()
                except Exception:  # noqa: BLE001
                    body = {}
                return (body.get("refund_status") or "submitted") if isinstance(body, dict) else "submitted"
            return "refund_failed"
        except Exception as exc:  # noqa: BLE001 - never leak internals/secrets
            logger.warning("Refund request failed for shipment %s: %s", shipment.id, type(exc).__name__)
            return "refund_failed"

    # ------------------------------------------------------------------
    # Tracking events (de-dup, status flow-back). NOT egress-gated -- this
    # applies INBOUND data (webhook / poll) under an already-resolved tenant.
    # ------------------------------------------------------------------

    def record_tracking_events(
        self,
        company_id: int,
        shipment_id: int,
        events: List[TrackingEvent],
        *,
        source: str = "webhook",
    ) -> List[ShipmentTrackingEvent]:
        """Append normalized tracking events to a shipment (de-duped), commit.

        De-dups by ``provider_event_id`` (within the shipment). Updates the
        shipment's ``tracking_status`` / ``tracking_status_detail`` /
        ``last_tracking_sync_at`` from the latest event, and on a ``DELIVERED``
        event sets ``actual_delivery``. Informational ONLY: never closes the work
        order (product decision -- the manual ship path owns WO closure).

        Tenant-scoped: the caller MUST pass the company resolved from STORED data
        (``aggregator_shipment_id``), never caller input.
        """
        shipment = self._get_shipment(company_id, shipment_id)

        existing_ids = {
            row.provider_event_id
            for row in self.db.query(ShipmentTrackingEvent.provider_event_id)
            .filter(
                ShipmentTrackingEvent.shipment_id == shipment.id,
                ShipmentTrackingEvent.company_id == company_id,
                ShipmentTrackingEvent.provider_event_id.isnot(None),
            )
            .all()
        }

        inserted: List[ShipmentTrackingEvent] = []
        latest_event: Optional[TrackingEvent] = None
        delivered_at: Optional[date] = None

        for event in events:
            # De-dup by provider_event_id when present (drop exact repeats).
            if event.provider_event_id and event.provider_event_id in existing_ids:
                continue
            status_value = event.status.value if isinstance(event.status, TrackingStatus) else str(event.status)
            row = ShipmentTrackingEvent(
                shipment_id=shipment.id,
                status=status_value,
                status_detail=event.status_detail,
                occurred_at=event.occurred_at,
                location=event.location,
                message=event.message,
                source=source,
                provider_event_id=event.provider_event_id,
            )
            row.company_id = company_id
            self.db.add(row)
            inserted.append(row)
            if event.provider_event_id:
                existing_ids.add(event.provider_event_id)

            # Track the most recent event for status flow-back.
            if latest_event is None or self._event_is_newer(event, latest_event):
                latest_event = event
            if event.status == TrackingStatus.DELIVERED and event.occurred_at is not None:
                delivered_at = event.occurred_at.date()

        if latest_event is not None:
            shipment.tracking_status = (
                latest_event.status.value
                if isinstance(latest_event.status, TrackingStatus)
                else str(latest_event.status)
            )
            shipment.tracking_status_detail = latest_event.status_detail
        shipment.last_tracking_sync_at = datetime.utcnow()
        if delivered_at is not None and shipment.actual_delivery is None:
            shipment.actual_delivery = delivered_at

        self.db.commit()
        for row in inserted:
            self.db.refresh(row)
        return inserted

    @staticmethod
    def _event_is_newer(candidate: TrackingEvent, current: TrackingEvent) -> bool:
        if candidate.occurred_at is None:
            return False
        if current.occurred_at is None:
            return True
        return candidate.occurred_at >= current.occurred_at

    # ------------------------------------------------------------------
    # Document storage (label / BOL PDFs) + quote selection.
    # ------------------------------------------------------------------

    def _store_label_document(
        self, *, company_id: int, shipment: Shipment, label: Label, user_id: int
    ) -> Optional[Document]:
        return self._store_artifact_document(
            company_id=company_id,
            shipment=shipment,
            user_id=user_id,
            doc_type=DocumentType.SHIPPING_LABEL,
            title=f"Shipping label {shipment.shipment_number}",
            data=label.label_bytes,
            url=label.label_url,
            label_format=label.label_format,
        )

    def _store_bol_document(
        self, *, company_id: int, shipment: Shipment, bol: BillOfLading, user_id: int
    ) -> Optional[Document]:
        return self._store_artifact_document(
            company_id=company_id,
            shipment=shipment,
            user_id=user_id,
            doc_type=DocumentType.BILL_OF_LADING,
            title=f"Bill of Lading {shipment.shipment_number}",
            data=bol.document_bytes,
            url=bol.document_url,
            label_format="PDF",
        )

    def _store_artifact_document(
        self,
        *,
        company_id: int,
        shipment: Shipment,
        user_id: int,
        doc_type: DocumentType,
        title: str,
        data: Optional[bytes],
        url: Optional[str],
        label_format: Optional[str],
    ) -> Optional[Document]:
        """Persist a label/BOL artifact as a ``Document`` (same local-disk path).

        Writes ``data`` to disk when the provider returned bytes; otherwise stores
        the provider-hosted ``url`` (label_url) on the record so the artifact stays
        retrievable. Returns the flushed ``Document`` (id populated) or ``None``
        when there is nothing to store.
        """
        if not data and not url:
            return None

        ext = self._format_to_ext(label_format)
        file_path: Optional[str] = None
        file_size: Optional[int] = None
        if data:
            upload_dir = _resolve_upload_dir()
            unique_name = f"{uuid.uuid4()}{ext}"
            file_path = os.path.join(upload_dir, unique_name)
            with open(file_path, "wb") as fh:
                fh.write(data)
            file_size = len(data)

        document = Document(
            document_number=self._generate_document_number(doc_type.value),
            revision="A",
            title=title,
            document_type=doc_type,
            description=(url if (url and not data) else None),
            work_order_id=shipment.work_order_id,
            file_name=f"{shipment.shipment_number}{ext}",
            file_path=file_path,
            file_size=file_size,
            mime_type=self._format_to_mime(label_format),
            status="released",
            created_by=user_id,
        )
        document.company_id = company_id
        self.db.add(document)
        self.db.flush()
        return document

    def _generate_document_number(self, doc_type: str) -> str:
        prefix = doc_type[:3].upper()
        today = datetime.now().strftime("%Y%m")
        last_doc = (
            self.db.query(Document)
            .filter(Document.document_number.like(f"{prefix}-{today}-%"))
            .order_by(Document.document_number.desc())
            .first()
        )
        if last_doc:
            try:
                last_num = int(last_doc.document_number.split("-")[-1])
            except (ValueError, IndexError):
                last_num = 0
            new_num = last_num + 1
        else:
            new_num = 1
        return f"{prefix}-{today}-{new_num:04d}"

    @staticmethod
    def _format_to_ext(label_format: Optional[str]) -> str:
        fmt = (label_format or "PDF").upper()
        return {"PDF": ".pdf", "PNG": ".png", "ZPL": ".zpl", "EPL2": ".epl"}.get(fmt, ".pdf")

    @staticmethod
    def _format_to_mime(label_format: Optional[str]) -> str:
        fmt = (label_format or "PDF").upper()
        return {
            "PDF": "application/pdf",
            "PNG": "image/png",
            "ZPL": "application/zpl",
            "EPL2": "application/epl",
        }.get(fmt, "application/pdf")

    def _mark_selected_quote(self, company_id: int, shipment_id: int, rate_id: str) -> None:
        """Flag the chosen rate quote (best-effort compliance trail)."""
        self.db.query(ShipmentRateQuote).filter(
            ShipmentRateQuote.shipment_id == shipment_id,
            ShipmentRateQuote.company_id == company_id,
        ).update({ShipmentRateQuote.is_selected: False}, synchronize_session=False)
        self.db.query(ShipmentRateQuote).filter(
            ShipmentRateQuote.shipment_id == shipment_id,
            ShipmentRateQuote.company_id == company_id,
            ShipmentRateQuote.provider_rate_id == rate_id,
        ).update({ShipmentRateQuote.is_selected: True}, synchronize_session=False)

    # ------------------------------------------------------------------
    # Package persistence (used by the rate-shop / schedule flow).
    # ------------------------------------------------------------------

    def replace_packages(self, company_id: int, shipment_id: int, packages) -> List[ShipmentPackage]:
        """Replace the persisted packages for a shipment (soft-deletes old rows).

        Tenant-scoped. Old rows are soft-deleted (never hard-deleted) so package
        records tied to a purchased label/BOL are preserved. Commits.
        """
        shipment = self._get_shipment(company_id, shipment_id)
        existing = (
            self.db.query(ShipmentPackage)
            .filter(
                ShipmentPackage.shipment_id == shipment.id,
                ShipmentPackage.company_id == company_id,
                ShipmentPackage.is_deleted == False,  # noqa: E712
            )
            .all()
        )
        for row in existing:
            row.soft_delete()

        created: List[ShipmentPackage] = []
        for idx, pkg in enumerate(packages or [], start=1):
            data = pkg.model_dump() if hasattr(pkg, "model_dump") else dict(pkg)
            row = ShipmentPackage(
                shipment_id=shipment.id,
                sequence=idx,
                package_type=data.get("package_type"),
                length_in=data.get("length_in"),
                width_in=data.get("width_in"),
                height_in=data.get("height_in"),
                weight_lbs=data.get("weight_lbs"),
                freight_class=data.get("freight_class"),
                nmfc_code=data.get("nmfc_code"),
                quantity=data.get("quantity", 1),
            )
            row.company_id = company_id
            self.db.add(row)
            created.append(row)
        self.db.commit()
        for row in created:
            self.db.refresh(row)
        return created

    # ------------------------------------------------------------------
    # Misc helpers.
    # ------------------------------------------------------------------

    @staticmethod
    def _money_str(amount: Optional[Decimal], currency: Optional[str]) -> str:
        if amount is None:
            return "$0.00"
        cur = currency or "USD"
        symbol = "$" if cur == "USD" else f"{cur} "
        return f"{symbol}{amount}"

    @staticmethod
    def last4(secret: Optional[str]) -> Optional[str]:
        """Return the last 4 chars of a plaintext secret (for read responses).

        SECURITY: this is the ONLY representation of a secret ever returned. The
        plaintext is decrypted in-memory only at the call site and never logged.
        """
        if not secret:
            return None
        return secret[-4:]
