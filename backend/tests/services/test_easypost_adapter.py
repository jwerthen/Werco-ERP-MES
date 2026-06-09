"""Unit tests for the EasyPost ``CarrierProvider`` adapter.

These exercise the adapter in isolation -- NO network. EasyPost HTTP I/O is mocked
with ``httpx.MockTransport``: each test wires the provider's ``AsyncClient`` to a
handler that asserts on the outbound request and returns canned EasyPost JSON, so
we test the REAL request-building + response-mapping code, not a stub.

What is pinned down:

* EasyPost JSON maps onto the normalized ``types`` (rates, label, tracking,
  address, pickup) -- the only shapes the rest of the app sees.
* Money parses to ``Decimal`` (EasyPost sends rates as strings); weight is
  converted lbs -> ounces on the wire; the composite ``"<shipment_id>:<rate_id>"``
  provider_rate_id is emitted and recovered by ``buy_label``.
* Webhook signature verification ACCEPTS a correctly-HMAC'd body and REJECTS a
  tampered body, a wrong secret, and a missing header (constant-time, NFKD-secret).
* Freight (``create_freight_shipment`` / ``buy_bol``) raises ``NotSupportedError``.

These are pure unit tests (no DB), so they don't need unique natural keys.
"""

import hashlib
import hmac
import unicodedata
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.carriers.easypost_adapter import (
    WEBHOOK_SIGNATURE_HEADER,
    WEBHOOK_SIGNATURE_PREFIX,
    EasyPostProvider,
)
from app.services.carriers.exceptions import (
    AddressInvalidError,
    CarrierError,
    LabelPurchaseError,
    NotSupportedError,
    RateUnavailableError,
)
from app.services.carriers.types import (
    CarrierAddress,
    ParcelDimensions,
    TrackingStatus,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers: wire the provider to an httpx.MockTransport.
# ---------------------------------------------------------------------------


def _provider_with(handler, api_key="EZTK_test_key", carrier_refs=None):
    """Return an EasyPostProvider whose AsyncClient routes through ``handler``.

    ``handler`` is a ``Callable[[httpx.Request], httpx.Response]`` -- the real
    EasyPost transport replaced, so we never touch the network.
    """
    provider = EasyPostProvider(api_key=api_key, carrier_refs=carrier_refs)
    transport = httpx.MockTransport(handler)

    def _client():
        return httpx.AsyncClient(
            base_url="https://api.easypost.com/v2",
            auth=(api_key, ""),
            transport=transport,
            timeout=5.0,
        )

    provider._client = _client  # type: ignore[method-assign]
    return provider


def _addr():
    return CarrierAddress(
        name="Acme Receiving",
        street1="1 Main St",
        city="Springfield",
        state="IL",
        zip="62704",
        country="US",
    )


def _parcel():
    return ParcelDimensions(
        length_in=Decimal("10"),
        width_in=Decimal("8"),
        height_in=Decimal("4"),
        weight_lbs=Decimal("2.5"),
    )


# ---------------------------------------------------------------------------
# Rate-shop mapping + Decimal money + weight conversion.
# ---------------------------------------------------------------------------


async def test_get_rates_maps_easypost_json_to_decimal_quotes():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        import json as _json

        captured["body"] = _json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "id": "shp_12345",
                "rates": [
                    {
                        "id": "rate_aaa",
                        "carrier": "USPS",
                        "service": "Priority",
                        "rate": "7.50",
                        "currency": "USD",
                        "delivery_days": 2,
                        "delivery_date": "2026-06-12T00:00:00Z",
                        "carrier_account_id": "ca_999",
                    },
                    {
                        "id": "rate_bbb",
                        "carrier": "UPS",
                        "service": "Ground",
                        "rate": "9.13",
                        "currency": "USD",
                        "delivery_days": None,
                    },
                ],
            },
        )

    provider = _provider_with(handler)
    quotes = await provider.get_rates(ship_from=_addr(), ship_to=_addr(), parcels=[_parcel()], pallets=[])

    assert captured["url"].endswith("/shipments")
    # Weight is converted lbs -> ounces on the wire (2.5 lb -> 40 oz).
    assert captured["body"]["shipment"]["parcel"]["weight"] == 40.0

    assert len(quotes) == 2
    q0 = quotes[0]
    assert q0.carrier == "USPS"
    assert q0.mode == "parcel"
    # Money is Decimal, not float.
    assert q0.amount == Decimal("7.50")
    assert isinstance(q0.amount, Decimal)
    assert q0.est_delivery_days == 2
    assert q0.est_delivery_date is not None and q0.est_delivery_date.isoformat() == "2026-06-12"
    assert q0.carrier_account_ref == "ca_999"
    # provider_rate_id encodes both ids so buy_label can recover the shipment.
    assert q0.provider_rate_id == "shp_12345:rate_aaa"
    assert quotes[1].amount == Decimal("9.13")
    assert quotes[1].est_delivery_days is None


async def test_get_rates_skips_rate_with_unparseable_amount():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "shp_x",
                "rates": [
                    {"id": "rate_good", "carrier": "USPS", "service": "Priority", "rate": "5.00"},
                    {"id": "rate_bad", "carrier": "USPS", "service": "Express", "rate": "not-a-number"},
                ],
            },
        )

    provider = _provider_with(handler)
    quotes = await provider.get_rates(ship_from=_addr(), ship_to=_addr(), parcels=[_parcel()], pallets=[])
    assert [q.provider_rate_id for q in quotes] == ["shp_x:rate_good"]


async def test_get_rates_no_parcels_raises_rate_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not be hit
        raise AssertionError("no HTTP call should be made when there are no parcels")

    provider = _provider_with(handler)
    with pytest.raises(RateUnavailableError):
        await provider.get_rates(ship_from=_addr(), ship_to=_addr(), parcels=[], pallets=[])


async def test_get_rates_empty_rates_raises_rate_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "shp_x", "rates": []})

    provider = _provider_with(handler)
    with pytest.raises(RateUnavailableError):
        await provider.get_rates(ship_from=_addr(), ship_to=_addr(), parcels=[_parcel()], pallets=[])


# ---------------------------------------------------------------------------
# Buy label mapping + composite rate id recovery + idempotency header.
# ---------------------------------------------------------------------------


async def test_buy_label_maps_label_and_recovers_shipment_from_composite_rate_id():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["idempotency"] = request.headers.get("Idempotency-Key")
        import json as _json

        captured["body"] = _json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "id": "shp_buy_1",
                "tracking_code": "9400100000000000000000",
                "postage_label": {
                    "id": "pl_1",
                    "label_url": "https://easypost-files/label.pdf",
                    "label_file_type": "PDF",
                },
                "selected_rate": {
                    "rate": "7.50",
                    "carrier": "USPS",
                    "service": "Priority",
                },
            },
        )

    provider = _provider_with(handler)
    # The label-bytes fetch hits a separate hosted URL, not the EasyPost API client;
    # stub it so this test stays focused on the buy mapping (the fetch has its own test).
    provider._fetch_label_bytes = AsyncMock(return_value=b"%PDF-1.4 label")  # type: ignore[method-assign]
    # provider_shipment_id intentionally blank: the composite rate id carries it.
    label = await provider.buy_label("", "shp_buy_1:rate_aaa", idempotency_key="idem_xyz")

    # The shipment id is recovered from the composite, and only the rate id half is sent.
    assert "/shipments/shp_buy_1/buy" in captured["url"]
    assert captured["body"] == {"rate": {"id": "rate_aaa"}}
    assert captured["idempotency"] == "idem_xyz"

    assert label.provider_shipment_id == "shp_buy_1"
    assert label.tracking_number == "9400100000000000000000"
    assert label.label_url == "https://easypost-files/label.pdf"
    assert label.label_format == "PDF"
    assert label.carrier == "USPS"
    assert label.service_code == "Priority"
    assert label.cost == Decimal("7.50")
    assert isinstance(label.cost, Decimal)
    # The label PDF bytes were fetched so the service can store a downloadable Document.
    provider._fetch_label_bytes.assert_awaited_once_with("https://easypost-files/label.pdf")
    assert label.label_bytes == b"%PDF-1.4 label"


async def test_fetch_label_bytes_returns_pdf_bytes(monkeypatch):
    """The hosted label URL is fetched so the service stores a downloadable Document."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://easypost-files/label.pdf"
        return httpx.Response(200, content=b"%PDF-1.4 the-label")

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def _client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("app.services.carriers.easypost_adapter.httpx.AsyncClient", _client_factory)
    provider = EasyPostProvider(api_key="k")
    data = await provider._fetch_label_bytes("https://easypost-files/label.pdf")
    assert data == b"%PDF-1.4 the-label"


async def test_fetch_label_bytes_swallows_failure_and_returns_none(monkeypatch):
    """A failed label fetch must NOT fail the (already-completed) purchase -- it
    degrades to None so the caller keeps the hosted URL."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"oops")

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def _client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("app.services.carriers.easypost_adapter.httpx.AsyncClient", _client_factory)
    provider = EasyPostProvider(api_key="k")
    assert await provider._fetch_label_bytes("https://easypost-files/label.pdf") is None


async def test_buy_label_no_rate_amount_raises_label_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "shp_buy_1", "postage_label": {}, "selected_rate": {}})

    provider = _provider_with(handler)
    with pytest.raises(LabelPurchaseError):
        await provider.buy_label("shp_buy_1", "rate_aaa", idempotency_key="k")


async def test_buy_label_requires_a_shipment_id():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not be hit
        raise AssertionError("no HTTP call when there is no shipment id")

    provider = _provider_with(handler)
    with pytest.raises(LabelPurchaseError):
        await provider.buy_label("", "rate_with_no_shipment", idempotency_key="k")


# ---------------------------------------------------------------------------
# Address validation mapping.
# ---------------------------------------------------------------------------


async def test_validate_address_maps_verified_result():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "adr_1",
                "street1": "1 MAIN ST",
                "city": "SPRINGFIELD",
                "state": "IL",
                "zip": "62704-0001",
                "country": "US",
                "verifications": {"delivery": {"success": True, "errors": [], "details": {"dpv_match_code": "Y"}}},
            },
        )

    provider = _provider_with(handler)
    result = await provider.validate_address(_addr())
    assert result.is_valid is True
    assert result.normalized.zip == "62704-0001"
    assert result.normalized.street1 == "1 MAIN ST"
    assert result.deliverability == "Y"
    assert result.messages == []


async def test_validate_address_unverifiable_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                # No normalized street1 -> truly unverifiable.
                "verifications": {
                    "delivery": {"success": False, "errors": [{"code": "E.ADDRESS.NOT_FOUND", "message": "Not found"}]}
                },
            },
        )

    provider = _provider_with(handler)
    with pytest.raises(AddressInvalidError):
        await provider.validate_address(_addr())


# ---------------------------------------------------------------------------
# Tracking event mapping (status normalization, location join).
# ---------------------------------------------------------------------------


async def test_get_tracking_maps_events_and_normalizes_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "trk_1",
                "tracking_details": [
                    {
                        "id": "td_1",
                        "status": "in_transit",
                        "status_detail": "departed_facility",
                        "datetime": "2026-06-09T12:00:00Z",
                        "message": "Departed",
                        "tracking_location": {"city": "Memphis", "state": "TN", "country": "US"},
                    },
                    {
                        "id": "td_2",
                        "status": "delivered",
                        "datetime": "2026-06-10T09:00:00Z",
                        "tracking_location": {"city": "Springfield", "state": "IL"},
                    },
                ],
            },
        )

    provider = _provider_with(handler)
    events = await provider.get_tracking("1Z999", carrier="UPS")
    assert len(events) == 2
    assert events[0].status == TrackingStatus.IN_TRANSIT
    assert events[0].location == "Memphis, TN, US"
    assert events[0].provider_event_id == "td_1"
    assert events[1].status == TrackingStatus.DELIVERED
    assert events[1].location == "Springfield, IL"


async def test_parse_tracking_webhook_extracts_shipment_id_and_events():
    provider = _provider_with(lambda r: httpx.Response(200, json={}))
    raw = (
        b'{"result": {"shipment_id": "shp_hook_1", "tracking_code": "1Z999", '
        b'"tracking_details": [{"id": "td_a", "status": "out_for_delivery", "datetime": "2026-06-09T14:00:00Z"}]}}'
    )
    parsed = await provider.parse_tracking_webhook({}, raw)
    assert parsed.provider_shipment_id == "shp_hook_1"
    assert parsed.tracking_number == "1Z999"
    assert len(parsed.events) == 1
    assert parsed.events[0].status == TrackingStatus.OUT_FOR_DELIVERY
    # The handler verifies the signature separately -- parse never claims verified.
    assert parsed.verified is False


async def test_parse_tracking_webhook_unparseable_body_raises():
    provider = _provider_with(lambda r: httpx.Response(200, json={}))
    with pytest.raises(CarrierError):
        await provider.parse_tracking_webhook({}, b"{not json")


# ---------------------------------------------------------------------------
# Pickup mapping.
# ---------------------------------------------------------------------------


async def test_schedule_pickup_maps_confirmation():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "pickup_1",
                "status": "scheduled",
                "min_datetime": "2026-06-10T09:00:00Z",
                "max_datetime": "2026-06-10T17:00:00Z",
                "confirmations": [{"confirmation_number": "CONF123"}],
            },
        )

    provider = _provider_with(handler)
    pickup = await provider.schedule_pickup(
        "shp_1", pickup_date="2026-06-10", window_start="2026-06-10T09:00:00Z", window_end="2026-06-10T17:00:00Z"
    )
    assert pickup.provider_pickup_id == "pickup_1"
    assert pickup.confirmation_number == "CONF123"
    assert pickup.status == "scheduled"
    assert pickup.scheduled_date is not None and pickup.scheduled_date.isoformat() == "2026-06-10"


# ---------------------------------------------------------------------------
# Error handling: HTTP >= 400 surfaces the EasyPost message, never the key.
# ---------------------------------------------------------------------------


async def test_http_error_surfaces_message_not_key():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "Authentication required"}})

    provider = _provider_with(handler, api_key="EZTK_super_secret_key")
    with pytest.raises(CarrierError) as exc:
        await provider.validate_address(_addr())
    msg = str(exc.value)
    assert "Authentication required" in msg
    assert "EZTK_super_secret_key" not in msg  # the api key must never leak into the error


# ---------------------------------------------------------------------------
# Freight is unsupported on the EasyPost public API.
# ---------------------------------------------------------------------------


async def test_create_freight_shipment_not_supported():
    provider = _provider_with(lambda r: httpx.Response(200, json={}))
    with pytest.raises(NotSupportedError):
        await provider.create_freight_shipment(ship_from=_addr(), ship_to=_addr(), pallets=[])


async def test_buy_bol_not_supported():
    provider = _provider_with(lambda r: httpx.Response(200, json={}))
    with pytest.raises(NotSupportedError):
        await provider.buy_bol("shp_1", "rate_1", idempotency_key="k")


# ---------------------------------------------------------------------------
# Webhook HMAC signature verification: accept / tamper / wrong-secret / missing.
# ---------------------------------------------------------------------------

_SECRET = "whsec_topsecret"
_BODY = b'{"result": {"shipment_id": "shp_hook_1"}}'


def _sign(body: bytes, secret: str) -> str:
    normalized = unicodedata.normalize("NFKD", secret).encode("utf-8")
    digest = hmac.new(normalized, body, hashlib.sha256).hexdigest()
    return f"{WEBHOOK_SIGNATURE_PREFIX}{digest}"


def test_verify_webhook_signature_accepts_correct_hmac():
    provider = EasyPostProvider(api_key="k")
    headers = {WEBHOOK_SIGNATURE_HEADER: _sign(_BODY, _SECRET)}
    assert provider.verify_webhook_signature(headers, _BODY, _SECRET) is True


def test_verify_webhook_signature_accepts_case_insensitive_header():
    provider = EasyPostProvider(api_key="k")
    # Header name lookup is case-insensitive (real webhooks vary the casing).
    headers = {WEBHOOK_SIGNATURE_HEADER.lower(): _sign(_BODY, _SECRET)}
    assert provider.verify_webhook_signature(headers, _BODY, _SECRET) is True


def test_verify_webhook_signature_rejects_tampered_body():
    provider = EasyPostProvider(api_key="k")
    # Signature was computed over _BODY but a different body is presented.
    headers = {WEBHOOK_SIGNATURE_HEADER: _sign(_BODY, _SECRET)}
    tampered = _BODY + b" "
    assert provider.verify_webhook_signature(headers, tampered, _SECRET) is False


def test_verify_webhook_signature_rejects_wrong_secret():
    provider = EasyPostProvider(api_key="k")
    headers = {WEBHOOK_SIGNATURE_HEADER: _sign(_BODY, "whsec_WRONG")}
    assert provider.verify_webhook_signature(headers, _BODY, _SECRET) is False


def test_verify_webhook_signature_rejects_missing_header():
    provider = EasyPostProvider(api_key="k")
    assert provider.verify_webhook_signature({}, _BODY, _SECRET) is False
