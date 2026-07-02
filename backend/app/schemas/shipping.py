"""Pydantic v2 request/response contracts for the shipping domain.

Covers the legacy manual-shipment shapes (moved verbatim out of
``app/api/endpoints/shipping.py``) PLUS the multi-carrier integration shapes:
carrier-account / shipping-profile admin CRUD, address validation, rate-shop,
buy-label / buy-bol, schedule-pickup, packages, and tracking.

MONEY is always ``Decimal`` (the ORM columns are ``Numeric(12, 2)``); physical
dimensions/weights are ``Decimal`` too. SECRETS are never accepted back nor
returned: a carrier account's API key is write-only on create/update and exposed
only as ``api_key_last4`` on read.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.base import UTCModel

# ---------------------------------------------------------------------------
# Legacy manual-shipment shapes (moved verbatim from the endpoint module).
# ---------------------------------------------------------------------------


class ShipmentCreate(BaseModel):
    work_order_id: int
    ship_to_name: Optional[str] = None
    ship_to_address: Optional[str] = None
    ship_to_city: Optional[str] = None
    ship_to_state: Optional[str] = None
    ship_to_zip: Optional[str] = None
    carrier: Optional[str] = None
    service_type: Optional[str] = None
    quantity_shipped: float
    weight_lbs: Optional[float] = None
    num_packages: int = 1
    packing_notes: Optional[str] = None
    cert_of_conformance: bool = False


class ShipmentUpdate(BaseModel):
    carrier: Optional[str] = None
    service_type: Optional[str] = None
    tracking_number: Optional[str] = None
    ship_date: Optional[date] = None
    estimated_delivery: Optional[date] = None
    status: Optional[str] = None


class ShipmentResponse(UTCModel):
    id: int
    shipment_number: str
    work_order_id: int
    work_order_number: Optional[str] = None
    customer_name: Optional[str] = None
    part_number: Optional[str] = None
    status: str
    ship_to_name: Optional[str] = None
    carrier: Optional[str] = None
    tracking_number: Optional[str] = None
    quantity_shipped: float
    ship_date: Optional[date] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True, use_enum_values=True)


# ---------------------------------------------------------------------------
# Carrier account admin CRUD.
# ---------------------------------------------------------------------------


class CarrierAccountCreate(BaseModel):
    """Create a per-company carrier-aggregator credential.

    ``api_key`` / ``webhook_secret`` are write-only secrets -- they are Fernet
    encrypted before storage and NEVER returned (the response exposes only
    ``api_key_last4``).
    """

    name: str = Field(..., max_length=120)
    provider: str = Field(..., max_length=50, description='"easypost" | "zenkraft"')
    environment: str = Field("production", max_length=20, description='"production" | "test"')
    api_key: str = Field(..., min_length=1, description="Write-only; encrypted at rest, never returned.")
    webhook_secret: Optional[str] = Field(None, description="Write-only; encrypted at rest, never returned.")
    carrier_refs: Dict[str, str] = Field(
        default_factory=dict,
        description='Opaque bring-your-own-carrier account refs, e.g. {"fedex": "...", "ups": "..."}.',
    )
    is_active: bool = True
    is_default: bool = False


class CarrierAccountUpdate(BaseModel):
    """Patch a carrier account. Omitted fields are left unchanged.

    Sending ``api_key`` / ``webhook_secret`` rotates the stored secret; omitting
    them keeps the existing one.
    """

    name: Optional[str] = Field(None, max_length=120)
    environment: Optional[str] = Field(None, max_length=20)
    api_key: Optional[str] = Field(None, min_length=1, description="Write-only; rotates the stored key when present.")
    webhook_secret: Optional[str] = Field(None, description="Write-only; rotates the stored secret when present.")
    carrier_refs: Optional[Dict[str, str]] = None
    is_active: Optional[bool] = None
    is_default: Optional[bool] = None


class CarrierAccountResponse(UTCModel):
    """Carrier account read shape. NEVER exposes the plaintext key/secret."""

    id: int
    name: str
    provider: str
    environment: Optional[str] = None
    is_active: Optional[bool] = None
    is_default: Optional[bool] = None
    carrier_refs: List[str] = Field(default_factory=list, description="Carrier-ref KEYS only (no values).")
    api_key_last4: Optional[str] = None
    has_webhook_secret: bool = False
    created_at: Optional[datetime] = None


class CarrierConnectionTestResponse(BaseModel):
    """Outcome of a credential-only ``test-connection`` (transmits NO customer data)."""

    ok: bool
    provider: str
    message: Optional[str] = None


# ---------------------------------------------------------------------------
# Company shipping profile (ship-from origin + egress kill switch).
# ---------------------------------------------------------------------------


class CompanyShippingProfileBase(UTCModel):
    ship_from_name: Optional[str] = None
    ship_from_company: Optional[str] = None
    ship_from_phone: Optional[str] = None
    ship_from_email: Optional[str] = None
    ship_from_street1: Optional[str] = None
    ship_from_street2: Optional[str] = None
    ship_from_city: Optional[str] = None
    ship_from_state: Optional[str] = None
    ship_from_zip: Optional[str] = None
    ship_from_country: Optional[str] = "US"
    default_package_weight_lbs: Optional[Decimal] = None
    default_package_length_in: Optional[Decimal] = None
    default_package_width_in: Optional[Decimal] = None
    default_package_height_in: Optional[Decimal] = None


class CompanyShippingProfileCreate(CompanyShippingProfileBase):
    # SAFETY: the customer-data egress kill switch DEFAULTS OFF. A human must opt
    # in explicitly (CUI / DoD sign-off) before any outbound carrier call that
    # transmits customer data is permitted.
    allow_carrier_egress: bool = False


class CompanyShippingProfileUpdate(CompanyShippingProfileBase):
    allow_carrier_egress: Optional[bool] = None


class CompanyShippingProfileResponse(CompanyShippingProfileBase):
    id: int
    allow_carrier_egress: bool
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Address validation.
# ---------------------------------------------------------------------------


class AddressSchema(BaseModel):
    """A label-grade postal address at the API boundary (maps to CarrierAddress)."""

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


class AddressValidationRequest(BaseModel):
    address: AddressSchema


class AddressValidationResponse(BaseModel):
    is_valid: bool
    normalized: AddressSchema
    messages: List[str] = Field(default_factory=list)
    deliverability: Optional[str] = None


# ---------------------------------------------------------------------------
# Packages (parcels + pallets) and rate-shop.
# ---------------------------------------------------------------------------


class ParcelSchema(BaseModel):
    length_in: Decimal
    width_in: Decimal
    height_in: Decimal
    weight_lbs: Decimal


class PalletSchema(BaseModel):
    length_in: Decimal
    width_in: Decimal
    height_in: Decimal
    weight_lbs: Decimal
    freight_class: Optional[str] = None
    nmfc: Optional[str] = None
    stackable: bool = False


class RateShopRequest(BaseModel):
    """Rate-shop a shipment. ``parcels`` drives parcel rating; ``pallets`` is the
    LTL/freight path (only honored by a freight-capable provider).

    ``carrier_account_id`` selects which configured account to use; omitting it
    uses the company default. ``ship_to`` / ``ship_from`` override the shipment's
    stored / profile addresses when supplied.
    """

    carrier_account_id: Optional[int] = None
    ship_from: Optional[AddressSchema] = None
    ship_to: Optional[AddressSchema] = None
    parcels: List[ParcelSchema] = Field(default_factory=list)
    pallets: List[PalletSchema] = Field(default_factory=list)


class RateQuoteResponse(UTCModel):
    id: Optional[int] = None  # persisted ShipmentRateQuote id (None for transient)
    provider_rate_id: str
    carrier: str
    service_code: Optional[str] = None
    service_name: Optional[str] = None
    mode: Literal["parcel", "freight"]
    amount: Decimal
    currency: str = "USD"
    est_delivery_days: Optional[int] = None
    est_delivery_date: Optional[date] = None
    is_selected: bool = False

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Label / BOL purchase, pickups.
# ---------------------------------------------------------------------------


class BuyLabelRequest(BaseModel):
    """Buy a parcel label for a previously rate-shopped shipment.

    ``rate_id`` is the ``provider_rate_id`` of the chosen quote (the composite
    ``"<shipment_id>:<rate_id>"`` the adapter emits is accepted as-is).
    """

    rate_id: str
    carrier_account_id: Optional[int] = None


class BuyBolRequest(BaseModel):
    """Buy an LTL Bill of Lading for a freight-rated shipment."""

    rate_id: str
    carrier_account_id: Optional[int] = None


class SchedulePickupRequest(BaseModel):
    """Schedule a carrier pickup for an already-purchased shipment."""

    pickup_date: str = Field(..., description="ISO date, e.g. 2026-06-10.")
    window_start: str = Field(..., description="ISO datetime for the earliest pickup window.")
    window_end: str = Field(..., description="ISO datetime for the latest pickup window.")
    carrier_account_id: Optional[int] = None


class SchedulePickupResponse(UTCModel):
    provider_pickup_id: str
    confirmation_number: Optional[str] = None
    scheduled_date: Optional[date] = None
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    status: Optional[str] = None


class VoidRefundResponse(UTCModel):
    """Result of a void / refund request (a money-moving CANCEL)."""

    shipment_id: int
    voided_at: Optional[datetime] = None
    refund_status: Optional[str] = None
    message: Optional[str] = None


# ---------------------------------------------------------------------------
# Shipment packages (persisted child rows).
# ---------------------------------------------------------------------------


class ShipmentPackageCreate(BaseModel):
    package_type: Optional[str] = Field(None, description='"box" | "pallet"')
    length_in: Optional[Decimal] = None
    width_in: Optional[Decimal] = None
    height_in: Optional[Decimal] = None
    weight_lbs: Optional[Decimal] = None
    freight_class: Optional[str] = None
    nmfc_code: Optional[str] = None
    quantity: int = 1


class ShipmentPackageResponse(UTCModel):
    id: int
    shipment_id: int
    sequence: Optional[int] = None
    package_type: Optional[str] = None
    length_in: Optional[Decimal] = None
    width_in: Optional[Decimal] = None
    height_in: Optional[Decimal] = None
    weight_lbs: Optional[Decimal] = None
    tracking_number: Optional[str] = None
    freight_class: Optional[str] = None
    nmfc_code: Optional[str] = None
    quantity: Optional[int] = None
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Tracking.
# ---------------------------------------------------------------------------


class TrackingEventResponse(UTCModel):
    id: Optional[int] = None
    status: Optional[str] = None
    status_detail: Optional[str] = None
    occurred_at: Optional[datetime] = None
    location: Optional[str] = None
    message: Optional[str] = None
    source: Optional[str] = None
    provider_event_id: Optional[str] = None
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class ShipmentTrackingResponse(UTCModel):
    shipment_id: int
    shipment_number: str
    tracking_number: Optional[str] = None
    tracking_status: Optional[str] = None
    tracking_status_detail: Optional[str] = None
    last_tracking_sync_at: Optional[datetime] = None
    actual_delivery: Optional[date] = None
    events: List[TrackingEventResponse] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Label/BOL purchase results (returned by the buy endpoints).
# ---------------------------------------------------------------------------


class BuyLabelResponse(UTCModel):
    shipment_id: int
    shipment_number: str
    carrier: Optional[str] = None
    service_code: Optional[str] = None
    tracking_number: Optional[str] = None
    actual_cost: Optional[Decimal] = None
    cost_currency: Optional[str] = None
    label_document_id: Optional[int] = None
    label_purchased_at: Optional[datetime] = None
    already_purchased: bool = False


class BuyBolResponse(UTCModel):
    shipment_id: int
    shipment_number: str
    carrier: Optional[str] = None
    bol_number: Optional[str] = None
    pro_number: Optional[str] = None
    actual_cost: Optional[Decimal] = None
    cost_currency: Optional[str] = None
    bol_document_id: Optional[int] = None
    label_purchased_at: Optional[datetime] = None
    already_purchased: bool = False


# Extra normalized tracking-event input the service accepts from jobs/webhooks.
TrackingEventInput = Dict[str, Any]
