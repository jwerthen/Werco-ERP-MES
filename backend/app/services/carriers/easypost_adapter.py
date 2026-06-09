"""EasyPost concrete ``CarrierProvider`` adapter.

Talks to EasyPost's documented v2 REST API with a raw ``httpx.AsyncClient``
(matching ``app/jobs/webhook_jobs.py`` -- we deliberately do NOT take the EasyPost
SDK as a hard dependency). Auth is HTTP Basic with the API key as the username
and an empty password.

Scope:
- PARCEL is implemented end-to-end: address verify, create-shipment + rates, buy
  label, trackers, and pickups, with EasyPost JSON mapped onto the normalized
  ``types`` and ALL money parsed to ``Decimal``.
- FREIGHT/LTL (``create_freight_shipment`` / ``buy_bol``) raises
  ``NotSupportedError``. EasyPost LTL is an Enterprise feature whose wire format
  cannot be exercised or verified here, so rather than fabricate endpoints we
  expose the same normalized interface and fail loudly. See the per-method TODOs
  pointing at the future Zenkraft adapter as the freight alternative.

EGRESS: this adapter performs raw provider I/O only. The per-company
``allow_carrier_egress`` kill switch is enforced one layer up in the service.
"""

import hashlib
import hmac
import logging
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional

import httpx

from app.services.carriers.base import CarrierProvider
from app.services.carriers.exceptions import (
    AddressInvalidError,
    CarrierError,
    LabelPurchaseError,
    NotSupportedError,
    RateUnavailableError,
)
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
    TrackingStatus,
)

logger = logging.getLogger(__name__)

EASYPOST_BASE_URL = "https://api.easypost.com/v2"
DEFAULT_TIMEOUT = 30.0

# EasyPost signs outbound webhooks with HMAC-SHA256 over the raw body, hex-encoded,
# carried as "hmac-sha256-hex=<hex>" in the X-Hmac-Signature header. The secret is
# Unicode NFKD-normalized before use (matches the official EasyPost clients).
WEBHOOK_SIGNATURE_HEADER = "X-Hmac-Signature"
WEBHOOK_SIGNATURE_PREFIX = "hmac-sha256-hex="

# Map EasyPost tracker statuses onto our normalized TrackingStatus.
_EASYPOST_STATUS_MAP: Dict[str, TrackingStatus] = {
    "pre_transit": TrackingStatus.PRE_TRANSIT,
    "in_transit": TrackingStatus.IN_TRANSIT,
    "out_for_delivery": TrackingStatus.OUT_FOR_DELIVERY,
    "delivered": TrackingStatus.DELIVERED,
    "available_for_pickup": TrackingStatus.OUT_FOR_DELIVERY,
    "return_to_sender": TrackingStatus.RETURNED,
    "failure": TrackingStatus.FAILURE,
    "cancelled": TrackingStatus.FAILURE,
    "error": TrackingStatus.FAILURE,
    "unknown": TrackingStatus.UNKNOWN,
}


def _to_decimal(value: Any) -> Optional[Decimal]:
    """Parse a money value to ``Decimal`` (EasyPost sends rates as strings)."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_date(value: Any) -> Optional[date]:
    dt = _parse_dt(value)
    return dt.date() if dt else None


class EasyPostProvider(CarrierProvider):
    """EasyPost adapter. Parcel is fully implemented; freight is unsupported."""

    supports_freight = False
    supports_pickup = True

    def __init__(self, api_key: str, carrier_refs: Optional[Dict[str, str]] = None) -> None:
        """Construct from a DECRYPTED api key and the bring-your-own carrier refs.

        SECURITY: ``api_key`` is held only in-memory for the lifetime of this
        adapter and is NEVER logged or serialized.
        """
        self._api_key = api_key
        self.carrier_refs = carrier_refs or {}

    def _client(self) -> httpx.AsyncClient:
        # EasyPost uses HTTP Basic auth: api key as username, empty password.
        return httpx.AsyncClient(
            base_url=EASYPOST_BASE_URL,
            auth=(self._api_key, ""),
            timeout=DEFAULT_TIMEOUT,
        )

    async def _post(self, client: httpx.AsyncClient, path: str, json_body: Dict[str, Any]) -> Dict[str, Any]:
        resp = await client.post(path, json=json_body)
        return self._handle_response(resp)

    @staticmethod
    def _handle_response(resp: httpx.Response) -> Dict[str, Any]:
        if resp.status_code >= 400:
            # Do NOT echo the api key; only surface EasyPost's own error message.
            detail = ""
            try:
                body = resp.json()
                detail = (body.get("error") or {}).get("message") or str(body.get("error") or body)
            except Exception:  # noqa: BLE001 - response may not be JSON
                detail = resp.text[:500]
            raise CarrierError(f"EasyPost API error {resp.status_code}: {detail}")
        try:
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            raise CarrierError(f"EasyPost returned non-JSON response: {exc}") from exc

    # --- Address mapping helpers -----------------------------------------

    @staticmethod
    def _address_payload(address: CarrierAddress) -> Dict[str, Any]:
        return {
            "name": address.name,
            "company": address.company,
            "phone": address.phone,
            "email": address.email,
            "street1": address.street1,
            "street2": address.street2,
            "city": address.city,
            "state": address.state,
            "zip": address.zip,
            "country": address.country,
        }

    @staticmethod
    def _address_from_easypost(data: Dict[str, Any]) -> CarrierAddress:
        return CarrierAddress(
            name=data.get("name"),
            company=data.get("company"),
            phone=data.get("phone"),
            email=data.get("email"),
            street1=data.get("street1") or "",
            street2=data.get("street2"),
            city=data.get("city") or "",
            state=data.get("state") or "",
            zip=data.get("zip") or "",
            country=data.get("country") or "US",
            residential=data.get("residential"),
        )

    # --- Capability: address validation ----------------------------------

    async def validate_address(self, address: CarrierAddress) -> AddressValidationResult:
        # POST /addresses with verify=["delivery"] returns normalized fields plus a
        # verifications block of success/errors.
        payload = {"address": self._address_payload(address), "verify": ["delivery"]}
        async with self._client() as client:
            data = await self._post(client, "/addresses", payload)

        verifications = data.get("verifications") or {}
        delivery = verifications.get("delivery") or {}
        is_valid = bool(delivery.get("success", False))
        messages = [
            (err.get("message") or err.get("code") or "")
            for err in (delivery.get("errors") or [])
            if err.get("message") or err.get("code")
        ]
        deliverability = None
        details = delivery.get("details") or {}
        if isinstance(details, dict):
            deliverability = details.get("dpv_match_code") or details.get("dpv_active")

        if not is_valid and not data.get("street1"):
            raise AddressInvalidError("; ".join(messages) or "Address could not be verified")

        return AddressValidationResult(
            is_valid=is_valid,
            normalized=self._address_from_easypost(data),
            messages=messages,
            deliverability=str(deliverability) if deliverability is not None else None,
        )

    # --- Capability: rate shopping ---------------------------------------

    async def get_rates(
        self,
        *,
        ship_from: CarrierAddress,
        ship_to: CarrierAddress,
        parcels: List[ParcelDimensions],
        pallets: List[PalletDimensions],
    ) -> List[RateQuote]:
        if pallets:
            # FREIGHT/LTL rate-shop is not available on EasyPost's public REST API
            # (Enterprise feature). TODO(freight): rate-shop pallets via the
            # Zenkraft adapter. We only quote parcels here.
            logger.info("EasyPost adapter ignoring %d pallet(s): freight rating unsupported", len(pallets))
        if not parcels:
            raise RateUnavailableError("No parcels provided to rate-shop (EasyPost freight rating is unsupported)")

        # EasyPost rates one parcel per Shipment. Create a shipment per parcel and
        # aggregate the returned rates, tagging each quote as parcel mode.
        quotes: List[RateQuote] = []
        async with self._client() as client:
            for parcel in parcels:
                payload = {
                    "shipment": {
                        "to_address": self._address_payload(ship_to),
                        "from_address": self._address_payload(ship_from),
                        "parcel": {
                            "length": float(parcel.length_in),
                            "width": float(parcel.width_in),
                            "height": float(parcel.height_in),
                            "weight": float(parcel.weight_lbs) * 16.0,  # EasyPost weight is in ounces
                        },
                    }
                }
                data = await self._post(client, "/shipments", payload)
                quotes.extend(self._rates_from_shipment(data))

        if not quotes:
            raise RateUnavailableError("EasyPost returned no rates for the requested shipment")
        return quotes

    def _rates_from_shipment(self, shipment: Dict[str, Any]) -> List[RateQuote]:
        shipment_id = shipment.get("id") or ""
        out: List[RateQuote] = []
        for rate in shipment.get("rates") or []:
            amount = _to_decimal(rate.get("rate"))
            if amount is None:
                continue
            days = rate.get("delivery_days") or rate.get("est_delivery_days")
            out.append(
                RateQuote(
                    # provider_rate_id encodes both ids so buy_label can recover the
                    # shipment that owns the rate: "<shipment_id>:<rate_id>".
                    provider_rate_id=f"{shipment_id}:{rate.get('id')}",
                    carrier=rate.get("carrier") or "",
                    service_code=rate.get("service"),
                    service_name=rate.get("service"),
                    mode="parcel",
                    amount=amount,
                    currency=rate.get("currency") or "USD",
                    est_delivery_days=(
                        int(days) if isinstance(days, (int, float, str)) and str(days).isdigit() else None
                    ),
                    est_delivery_date=_parse_date(rate.get("delivery_date")),
                    carrier_account_ref=rate.get("carrier_account_id"),
                )
            )
        return out

    # --- Capability: buy parcel label ------------------------------------

    async def buy_label(self, provider_shipment_id: str, rate_id: str, *, idempotency_key: str) -> Label:
        # rate_id may arrive as the composite "<shipment_id>:<rate_id>" we emitted
        # in get_rates; split it and prefer the embedded shipment id.
        shipment_id = provider_shipment_id
        actual_rate_id = rate_id
        if ":" in rate_id:
            embedded_shipment, _, embedded_rate = rate_id.partition(":")
            shipment_id = embedded_shipment or provider_shipment_id
            actual_rate_id = embedded_rate
        if not shipment_id:
            raise LabelPurchaseError("buy_label requires a provider shipment id")

        payload = {"rate": {"id": actual_rate_id}}
        # The idempotency_key is sent as an EasyPost idempotency header so a retried
        # purchase is a provider-side no-op (defense in depth alongside our own
        # partial-unique index).
        headers = {"Idempotency-Key": idempotency_key}
        async with self._client() as client:
            resp = await client.post(f"/shipments/{shipment_id}/buy", json=payload, headers=headers)
            data = self._handle_response(resp)

        postage = data.get("postage_label") or {}
        selected_rate = data.get("selected_rate") or {}
        cost = _to_decimal(selected_rate.get("rate"))
        if cost is None:
            raise LabelPurchaseError("EasyPost buy succeeded but returned no rate amount")

        # EasyPost returns the label as a hosted URL (no inline bytes). Fetch the
        # bytes now so the service can persist a downloadable Document on local disk
        # -- otherwise the Document has only a URL, no file_path, and the document
        # download endpoint 404s (the print page would never get the PDF). Fetching
        # is best-effort: on failure we keep the URL so the label is not lost and the
        # purchase still succeeds (the money already moved).
        label_url = postage.get("label_url")
        label_bytes = await self._fetch_label_bytes(label_url) if label_url else None

        return Label(
            provider_shipment_id=data.get("id") or shipment_id,
            provider_label_id=str(postage.get("id")) if postage.get("id") else None,
            tracking_number=data.get("tracking_code"),
            label_format=postage.get("label_file_type"),
            label_url=label_url,
            label_bytes=label_bytes,
            carrier=selected_rate.get("carrier"),
            service_code=selected_rate.get("service"),
            cost=cost,
        )

    async def _fetch_label_bytes(self, label_url: str) -> Optional[bytes]:
        """Download the rendered label PDF/PNG/ZPL bytes from EasyPost's hosted URL.

        Best-effort: returns ``None`` on any failure (the caller keeps the URL and
        the purchase still succeeds). The label URL is a provider-hosted CDN link;
        the request goes through the backend so the bytes can be stored locally and
        served via the auth-gated document download (never the raw URL to the client).
        """
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as client:
                resp = await client.get(label_url)
            if resp.status_code < 400 and resp.content:
                return resp.content
            logger.warning("EasyPost label fetch returned HTTP %s; storing URL only", resp.status_code)
        except Exception as exc:  # noqa: BLE001 - never fail a completed purchase on a label fetch
            logger.warning("EasyPost label fetch failed (%s); storing URL only", type(exc).__name__)
        return None

    # --- Capability: freight / LTL (UNSUPPORTED on EasyPost public API) ---

    async def create_freight_shipment(
        self,
        *,
        ship_from: CarrierAddress,
        ship_to: CarrierAddress,
        pallets: List[PalletDimensions],
    ) -> str:
        # TODO(freight): EasyPost LTL is an Enterprise-gated feature with no
        # publicly documented REST wire format we can implement/verify here.
        # Implement LTL freight (create shipment + pallet/freight-class payload)
        # in the future Zenkraft adapter, which has native FedEx Freight support,
        # and register it as "zenkraft" in registry.get_provider.
        raise NotSupportedError(
            "EasyPost adapter does not support LTL freight shipment creation "
            "(Enterprise feature). Use the Zenkraft adapter for freight."
        )

    async def buy_bol(self, provider_shipment_id: str, rate_id: str, *, idempotency_key: str) -> BillOfLading:
        # TODO(freight): see create_freight_shipment -- BOL purchase belongs in the
        # Zenkraft adapter. Returning the normalized BillOfLading shape from there
        # keeps the service layer unchanged.
        raise NotSupportedError(
            "EasyPost adapter does not support LTL Bill of Lading purchase "
            "(Enterprise feature). Use the Zenkraft adapter for freight."
        )

    # --- Capability: pickups ---------------------------------------------

    async def schedule_pickup(
        self,
        provider_shipment_id: str,
        *,
        pickup_date: str,
        window_start: str,
        window_end: str,
    ) -> Pickup:
        payload = {
            "pickup": {
                "shipment": {"id": provider_shipment_id},
                "min_datetime": window_start,
                "max_datetime": window_end,
            }
        }
        async with self._client() as client:
            data = await self._post(client, "/pickups", payload)

        confirmations = data.get("confirmations") or []
        confirmation_number = None
        if confirmations and isinstance(confirmations[0], dict):
            confirmation_number = confirmations[0].get("confirmation_number")

        return Pickup(
            provider_pickup_id=data.get("id") or "",
            confirmation_number=confirmation_number,
            scheduled_date=_parse_date(data.get("min_datetime")),
            window_start=_parse_dt(data.get("min_datetime")),
            window_end=_parse_dt(data.get("max_datetime")),
            status=data.get("status"),
        )

    # --- Capability: tracking --------------------------------------------

    async def get_tracking(self, tracking_number: str, *, carrier: str) -> List[TrackingEvent]:
        payload = {"tracker": {"tracking_code": tracking_number, "carrier": carrier}}
        async with self._client() as client:
            data = await self._post(client, "/trackers", payload)
        return self._events_from_tracker(data)

    def _events_from_tracker(self, tracker: Dict[str, Any]) -> List[TrackingEvent]:
        events: List[TrackingEvent] = []
        for detail in tracker.get("tracking_details") or []:
            raw_status = (detail.get("status") or "").lower()
            location_parts = []
            loc = detail.get("tracking_location") or {}
            if isinstance(loc, dict):
                location_parts = [loc.get("city"), loc.get("state"), loc.get("country")]
            location = ", ".join(p for p in location_parts if p) or None
            events.append(
                TrackingEvent(
                    status=_EASYPOST_STATUS_MAP.get(raw_status, TrackingStatus.UNKNOWN),
                    status_detail=detail.get("status_detail") or detail.get("status"),
                    occurred_at=_parse_dt(detail.get("datetime")),
                    location=location,
                    message=detail.get("message"),
                    provider_event_id=detail.get("id"),
                )
            )
        return events

    # --- Capability: inbound webhook -------------------------------------

    async def parse_tracking_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> ParsedTrackingWebhook:
        import json

        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except (ValueError, UnicodeDecodeError) as exc:
            raise CarrierError(f"Unparseable webhook body: {exc}") from exc

        result = payload.get("result") or {}
        # EasyPost tracker webhooks carry the tracker object under "result"; the
        # owning shipment id is on the tracker (resolved by the service to a tenant
        # via stored aggregator_shipment_id -- NEVER from caller input).
        provider_shipment_id = result.get("shipment_id")
        tracking_number = result.get("tracking_code")
        events = self._events_from_tracker(result) if result else []
        return ParsedTrackingWebhook(
            provider_shipment_id=provider_shipment_id,
            tracking_number=tracking_number,
            events=events,
            verified=False,  # signature is verified separately via verify_webhook_signature
        )

    def verify_webhook_signature(self, headers: Mapping[str, str], raw_body: bytes, secret: str) -> bool:
        # Case-insensitive header lookup.
        provided = None
        for key, value in headers.items():
            if key.lower() == WEBHOOK_SIGNATURE_HEADER.lower():
                provided = value
                break
        if not provided:
            return False

        # EasyPost NFKD-normalizes the secret before using it as the HMAC key.
        normalized_secret = unicodedata.normalize("NFKD", secret).encode("utf-8")
        digest = hmac.new(normalized_secret, raw_body, hashlib.sha256).hexdigest()
        expected = f"{WEBHOOK_SIGNATURE_PREFIX}{digest}"
        # Constant-time compare (EasyPost's comparison is case-insensitive).
        return hmac.compare_digest(expected.lower(), provided.strip().lower())
