"""The provider-agnostic carrier interface.

``CarrierProvider`` is the ONLY surface the service layer programs against. Each
concrete aggregator (EasyPost, later Zenkraft) implements it and maps its own
wire format onto the normalized ``types``. Swapping providers is a registry
change, nothing else.

EGRESS NOTE: these methods perform the raw provider I/O only. The per-company
``allow_carrier_egress`` kill switch is enforced one layer up (in the service),
so an adapter method assumes it has already been cleared to transmit.
"""

from abc import ABC, abstractmethod
from typing import List, Mapping

from app.services.carriers.types import (
    AddressValidationResult,
    BillOfLading,
    CarrierAddress,
    Label,
    PalletDimensions,
    ParcelDimensions,
    ParsedTrackingWebhook,
    Pickup,
    RateQuote,
    TrackingEvent,
)


class CarrierProvider(ABC):
    """Abstract multi-carrier aggregator provider."""

    #: Whether this provider/adapter can quote and buy LTL freight + BOLs.
    supports_freight: bool = False
    #: Whether this provider/adapter can schedule carrier pickups.
    supports_pickup: bool = False

    @abstractmethod
    async def validate_address(self, address: CarrierAddress) -> AddressValidationResult:
        """Verify/normalize a postal address (transmits customer data)."""
        ...

    @abstractmethod
    async def get_rates(
        self,
        *,
        ship_from: CarrierAddress,
        ship_to: CarrierAddress,
        parcels: List[ParcelDimensions],
        pallets: List[PalletDimensions],
    ) -> List[RateQuote]:
        """Rate-shop the shipment, returning both parcel and freight quotes."""
        ...

    @abstractmethod
    async def buy_label(self, provider_shipment_id: str, rate_id: str, *, idempotency_key: str) -> Label:
        """Purchase a parcel label for a previously created shipment/rate."""
        ...

    @abstractmethod
    async def create_freight_shipment(
        self,
        *,
        ship_from: CarrierAddress,
        ship_to: CarrierAddress,
        pallets: List[PalletDimensions],
    ) -> str:
        """Create an LTL freight shipment, returning the provider shipment id."""
        ...

    @abstractmethod
    async def buy_bol(self, provider_shipment_id: str, rate_id: str, *, idempotency_key: str) -> BillOfLading:
        """Purchase an LTL Bill of Lading for a freight shipment/rate."""
        ...

    @abstractmethod
    async def schedule_pickup(
        self,
        provider_shipment_id: str,
        *,
        pickup_date: str,
        window_start: str,
        window_end: str,
    ) -> Pickup:
        """Schedule a carrier pickup for an already-purchased shipment."""
        ...

    @abstractmethod
    async def get_tracking(self, tracking_number: str, *, carrier: str) -> List[TrackingEvent]:
        """Fetch the current tracking event history for a shipment."""
        ...

    @abstractmethod
    async def parse_tracking_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> ParsedTrackingWebhook:
        """Parse an inbound tracking webhook into normalized events.

        Returns the provider shipment id / tracking number (used by the service
        to resolve the owning tenant from stored data, NEVER from caller input)
        and the parsed events.
        """
        ...

    @abstractmethod
    def verify_webhook_signature(self, headers: Mapping[str, str], raw_body: bytes, secret: str) -> bool:
        """Constant-time verify an inbound webhook's signature (synchronous)."""
        ...
