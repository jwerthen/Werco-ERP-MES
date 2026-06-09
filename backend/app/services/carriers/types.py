"""Normalized internal shapes for the carrier abstraction.

These are the ONLY shapes the rest of the application sees -- every concrete
provider adapter maps its wire format onto these. Implemented as Pydantic v2
models (matching the repo's schema convention) so they validate at the boundary
and serialize predictably.

MONEY is always ``Decimal`` (never float). Physical dimensions/weights are also
``Decimal`` to stay consistent with the ``Numeric`` columns on the shipment child
tables.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class TrackingStatus(str, Enum):
    """Normalized tracking lifecycle, provider-agnostic."""

    PRE_TRANSIT = "pre_transit"
    IN_TRANSIT = "in_transit"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    RETURNED = "returned"
    FAILURE = "failure"
    UNKNOWN = "unknown"


class CarrierAddress(BaseModel):
    """A label-grade postal address (ship-from or ship-to)."""

    name: Optional[str] = None
    company: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    street1: str
    street2: Optional[str] = None
    city: str
    state: str
    zip: str
    country: str = "US"
    residential: Optional[bool] = None


class ParcelDimensions(BaseModel):
    """A single parcel's physical dimensions/weight (inches / pounds)."""

    length_in: Decimal
    width_in: Decimal
    height_in: Decimal
    weight_lbs: Decimal


class PalletDimensions(BaseModel):
    """A single LTL pallet's dimensions/weight plus freight classification."""

    length_in: Decimal
    width_in: Decimal
    height_in: Decimal
    weight_lbs: Decimal
    freight_class: Optional[str] = None
    nmfc: Optional[str] = None
    stackable: bool = False


class RateQuote(BaseModel):
    """A single rate-shop result (parcel or freight)."""

    provider_rate_id: str
    carrier: str
    service_code: Optional[str] = None
    service_name: Optional[str] = None
    mode: Literal["parcel", "freight"]
    amount: Decimal
    currency: str = "USD"
    est_delivery_days: Optional[int] = None
    est_delivery_date: Optional[date] = None
    carrier_account_ref: Optional[str] = None


class Label(BaseModel):
    """A purchased parcel shipping label."""

    provider_shipment_id: str
    provider_label_id: Optional[str] = None
    tracking_number: Optional[str] = None
    label_format: Optional[str] = None  # e.g. "PDF" | "ZPL" | "PNG"
    label_url: Optional[str] = None
    label_bytes: Optional[bytes] = None
    carrier: Optional[str] = None
    service_code: Optional[str] = None
    cost: Decimal


class BillOfLading(BaseModel):
    """A purchased LTL freight Bill of Lading."""

    provider_shipment_id: str
    bol_number: Optional[str] = None
    pro_number: Optional[str] = None
    document_url: Optional[str] = None
    document_bytes: Optional[bytes] = None
    carrier: Optional[str] = None
    cost: Optional[Decimal] = None


class Pickup(BaseModel):
    """A scheduled carrier pickup."""

    provider_pickup_id: str
    confirmation_number: Optional[str] = None
    scheduled_date: Optional[date] = None
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    status: Optional[str] = None


class TrackingEvent(BaseModel):
    """A single normalized tracking event."""

    status: TrackingStatus = TrackingStatus.UNKNOWN
    status_detail: Optional[str] = None
    occurred_at: Optional[datetime] = None
    location: Optional[str] = None
    message: Optional[str] = None
    provider_event_id: Optional[str] = None


class AddressValidationResult(BaseModel):
    """The outcome of an address-validation call."""

    is_valid: bool
    normalized: CarrierAddress
    messages: List[str] = Field(default_factory=list)
    deliverability: Optional[str] = None


class ParsedTrackingWebhook(BaseModel):
    """The normalized result of parsing an inbound carrier tracking webhook.

    ``provider_shipment_id`` / ``tracking_number`` are the keys the service uses
    to resolve the owning tenant from stored shipment data -- NEVER from caller
    input. ``verified`` reflects whether the HMAC signature checked out.
    """

    provider_shipment_id: Optional[str] = None
    tracking_number: Optional[str] = None
    events: List[TrackingEvent] = Field(default_factory=list)
    verified: bool = False
