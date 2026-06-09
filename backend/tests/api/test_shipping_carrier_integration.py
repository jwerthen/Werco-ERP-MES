"""Behavior locks for the multi-carrier shipping ENDPOINTS stage.

Covers the REST routes (rate-shop / buy-label / buy-bol / void / refund /
schedule-pickup / validate-address / tracking), the admin credential CRUD +
connection test + shipping profile, and the inbound carrier tracking webhook.

The compliance invariants this file pins down:

1. **Egress kill switch.** With ``allow_carrier_egress`` OFF (the default), every
   customer-data-bearing route returns HTTP 409 and the provider is NEVER called
   (asserted via a provider double whose call would raise). ``test-connection``
   is exempt (works with egress OFF -- it sends no customer data).

2. **RBAC.** The carrier write routes are gated to ADMIN/MANAGER/SUPERVISOR/
   SHIPPING; an OPERATOR gets 403 and nothing happens. Admin credential CRUD is
   ADMIN-only (OPERATOR -> 403).

3. **Secrets never leak.** Creating / listing a carrier account returns only
   ``api_key_last4`` (+ ``has_webhook_secret``), never the plaintext key, and no
   audit row carries the secret. DELETE soft-deletes (no physical delete).

4. **Webhook tenant resolution from STORED data.** A valid-signature webhook
   resolves the owning company from the stored ``aggregator_shipment_id`` and
   enqueues the job with THAT company_id -- never anything the caller sent. A
   bad signature, or a verified event for a shipment we don't own, is dropped
   (204) with no enqueue and no existence oracle.

5. **Error mapping.** EasyPost buy-bol (freight unsupported) -> 501.
"""

import json
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.carrier_account import CarrierAccount, CompanyShippingProfile
from app.models.company import Company
from app.models.part import Part
from app.models.shipping import Shipment
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.services.carriers.crypto import decrypt_secret
from app.services.carriers.types import (
    AddressValidationResult,
    CarrierAddress,
    ParsedTrackingWebhook,
    RateQuote,
    TrackingEvent,
    TrackingStatus,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"
_seq = {"n": 0}

XRW = {"X-Requested-With": "XMLHttpRequest"}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(id=company_id, name=f"Company {company_id}", slug=f"company-{company_id}", is_active=True)
        db.add(company)
        db.commit()
    return company


def make_user(db: Session, *, role: UserRole, company_id: int = COMPANY_A) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"u{n}@werco.com",
        employee_id=f"EMP-{n:05d}",
        first_name="T",
        last_name="U",
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=True,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def headers_for(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", **XRW}


def make_part(db: Session, company_id: int = COMPANY_A) -> Part:
    from app.models.part import PartType

    n = _next()
    part = Part(
        part_number=f"PN-{n:05d}",
        name=f"Part {n}",
        part_type=PartType.MANUFACTURED,
        company_id=company_id,
    )
    db.add(part)
    db.flush()
    return part


def make_shipment(db: Session, *, company_id: int = COMPANY_A, aggregator_shipment_id=None) -> Shipment:
    part = make_part(db, company_id)
    n = _next()
    wo = WorkOrder(
        work_order_number=f"WO-{n:05d}",
        customer_name="Acme",
        part_id=part.id,
        quantity_ordered=10,
        quantity_complete=10,
        status=WorkOrderStatus.COMPLETE,
        priority=5,
        company_id=company_id,
    )
    db.add(wo)
    db.flush()
    shipment = Shipment(
        shipment_number=f"SHP-{n:05d}",
        work_order_id=wo.id,
        ship_to_name="Acme Receiving",
        ship_to_address="1 Main St",
        ship_to_city="Springfield",
        ship_to_state="IL",
        ship_to_zip="62704",
        quantity_shipped=10,
        aggregator_shipment_id=aggregator_shipment_id,
    )
    shipment.company_id = company_id
    db.add(shipment)
    db.commit()
    db.refresh(shipment)
    return shipment


def make_carrier_account(
    db: Session,
    *,
    company_id: int = COMPANY_A,
    provider: str = "easypost",
    api_key: str = "EZTKSECRETKEY1234",
    webhook_secret=None,
    is_default: bool = True,
) -> CarrierAccount:
    from app.services.carriers.crypto import encrypt_secret

    n = _next()
    account = CarrierAccount(
        name=f"Acct {n}",
        provider=provider,
        environment="test",
        encrypted_api_key=encrypt_secret(api_key),
        webhook_secret_encrypted=encrypt_secret(webhook_secret) if webhook_secret else None,
        is_active=True,
        is_default=is_default,
    )
    account.company_id = company_id
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def set_egress(db: Session, company_id: int, allow: bool) -> CompanyShippingProfile:
    profile = db.query(CompanyShippingProfile).filter(CompanyShippingProfile.company_id == company_id).first()
    if not profile:
        profile = CompanyShippingProfile(
            allow_carrier_egress=allow,
            ship_from_name="Werco",
            ship_from_street1="100 Plant Rd",
            ship_from_city="Toledo",
            ship_from_state="OH",
            ship_from_zip="43601",
        )
        profile.company_id = company_id
        db.add(profile)
    else:
        profile.allow_carrier_egress = allow
    db.commit()
    return profile


class _ExplodingProvider:
    """A provider double whose customer-data calls MUST NOT be reached.

    If any of these is invoked while egress is off, the test fails loudly --
    proving the kill switch short-circuits BEFORE any provider I/O.
    """

    supports_freight = False
    supports_pickup = True

    async def validate_address(self, address):  # pragma: no cover - must not be called
        raise AssertionError("provider.validate_address called while egress OFF")

    async def get_rates(self, **kwargs):  # pragma: no cover
        raise AssertionError("provider.get_rates called while egress OFF")

    async def buy_label(self, *a, **k):  # pragma: no cover
        raise AssertionError("provider.buy_label called while egress OFF")


# ---------------------------------------------------------------------------
# 1. Egress kill switch.
# ---------------------------------------------------------------------------


def test_rate_shop_blocked_when_egress_off(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN)
    make_carrier_account(db_session)
    set_egress(db_session, COMPANY_A, allow=False)
    shipment = make_shipment(db_session)

    with patch("app.services.carriers.registry.get_provider", return_value=_ExplodingProvider()):
        resp = client.post(
            f"/api/v1/shipping/{shipment.id}/rate-shop",
            headers=headers_for(admin),
            json={"parcels": [{"length_in": "10", "width_in": "8", "height_in": "4", "weight_lbs": "2"}]},
        )
    assert resp.status_code == status.HTTP_409_CONFLICT


def test_buy_label_blocked_when_egress_off(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN)
    make_carrier_account(db_session)
    set_egress(db_session, COMPANY_A, allow=False)
    shipment = make_shipment(db_session, aggregator_shipment_id="shp_abc")

    with patch("app.services.carriers.registry.get_provider", return_value=_ExplodingProvider()):
        resp = client.post(
            f"/api/v1/shipping/{shipment.id}/buy-label",
            headers=headers_for(admin),
            json={"rate_id": "shp_abc:rate_1"},
        )
    assert resp.status_code == status.HTTP_409_CONFLICT
    db_session.refresh(shipment)
    assert shipment.label_purchased_at is None  # nothing was purchased


# ---------------------------------------------------------------------------
# 2. RBAC.
# ---------------------------------------------------------------------------


def test_operator_cannot_rate_shop(client: TestClient, db_session: Session):
    operator = make_user(db_session, role=UserRole.OPERATOR)
    set_egress(db_session, COMPANY_A, allow=True)
    shipment = make_shipment(db_session)
    resp = client.post(
        f"/api/v1/shipping/{shipment.id}/rate-shop",
        headers=headers_for(operator),
        json={"parcels": []},
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN


def test_operator_cannot_list_carrier_accounts(client: TestClient, db_session: Session):
    operator = make_user(db_session, role=UserRole.OPERATOR)
    resp = client.get("/api/v1/admin/settings/carrier-accounts", headers=headers_for(operator))
    assert resp.status_code == status.HTTP_403_FORBIDDEN


def test_shipping_role_can_rate_shop(client: TestClient, db_session: Session):
    shipper = make_user(db_session, role=UserRole.SHIPPING)
    make_carrier_account(db_session)
    set_egress(db_session, COMPANY_A, allow=True)
    shipment = make_shipment(db_session)

    provider = AsyncMock()
    provider.get_rates.return_value = [
        RateQuote(
            provider_rate_id="shp_x:rate_1",
            carrier="USPS",
            service_code="Priority",
            service_name="Priority",
            mode="parcel",
            amount=Decimal("7.50"),
            currency="USD",
        )
    ]
    with patch("app.services.carriers.registry.get_provider", return_value=provider):
        resp = client.post(
            f"/api/v1/shipping/{shipment.id}/rate-shop",
            headers=headers_for(shipper),
            json={"parcels": [{"length_in": "10", "width_in": "8", "height_in": "4", "weight_lbs": "2"}]},
        )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["carrier"] == "USPS"
    assert body[0]["amount"] == "7.50"
    db_session.refresh(shipment)
    assert shipment.aggregator_shipment_id == "shp_x"  # captured for webhook resolution


# ---------------------------------------------------------------------------
# 3. Admin credential CRUD: secrets masked, soft delete, audited.
# ---------------------------------------------------------------------------


def test_create_carrier_account_masks_secret_and_audits(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN)
    resp = client.post(
        "/api/v1/admin/settings/carrier-accounts",
        headers=headers_for(admin),
        json={
            "name": "EasyPost Prod",
            "provider": "easypost",
            "environment": "production",
            "api_key": "EZTK_SUPER_SECRET_KEY_9999",
            "webhook_secret": "whsec_topsecret",
            "carrier_refs": {"fedex": "acct_1"},
            "is_default": True,
        },
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    body = resp.json()
    # Plaintext secrets never returned -- only the last4 mask + a boolean flag.
    serialized = json.dumps(body)
    assert "EZTK_SUPER_SECRET_KEY_9999" not in serialized
    assert "whsec_topsecret" not in serialized
    assert "encrypted_api_key" not in body and "webhook_secret" not in body
    assert body["api_key_last4"] == "9999"
    assert body["has_webhook_secret"] is True
    assert body["carrier_refs"] == ["fedex"]  # KEYS only, no values

    # Stored encrypted (not plaintext) and decrypts back.
    account = db_session.query(CarrierAccount).filter(CarrierAccount.id == body["id"]).first()
    assert account.encrypted_api_key != "EZTK_SUPER_SECRET_KEY_9999"
    assert decrypt_secret(account.encrypted_api_key) == "EZTK_SUPER_SECRET_KEY_9999"

    # Audit row written with NO secret anywhere in it.
    audits = db_session.query(AuditLog).filter(AuditLog.resource_type == "carrier_account").all()
    assert len(audits) == 1
    blob = json.dumps(
        {
            "old": audits[0].old_values,
            "new": audits[0].new_values,
            "extra": audits[0].extra_data,
            "desc": audits[0].description,
        }
    )
    assert "SUPER_SECRET" not in blob
    assert "whsec_topsecret" not in blob


def test_delete_carrier_account_soft_deletes(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN)
    account = make_carrier_account(db_session)
    resp = client.delete(f"/api/v1/admin/settings/carrier-accounts/{account.id}", headers=headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK
    db_session.refresh(account)
    assert account.is_deleted is True  # soft, not physical
    # Still present in the table (not hard-deleted) but hidden from the list.
    assert db_session.query(CarrierAccount).filter(CarrierAccount.id == account.id).first() is not None
    list_resp = client.get("/api/v1/admin/settings/carrier-accounts", headers=headers_for(admin))
    assert account.id not in [a["id"] for a in list_resp.json()]


def test_tenant_isolation_carrier_accounts(client: TestClient, db_session: Session):
    admin_a = make_user(db_session, role=UserRole.ADMIN, company_id=COMPANY_A)
    make_carrier_account(db_session, company_id=COMPANY_B)
    resp = client.get("/api/v1/admin/settings/carrier-accounts", headers=headers_for(admin_a))
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json() == []  # company A sees none of company B's accounts


# ---------------------------------------------------------------------------
# 4. test-connection is exempt from egress.
# ---------------------------------------------------------------------------


def test_connection_works_with_egress_off(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN)
    account = make_carrier_account(db_session)
    set_egress(db_session, COMPANY_A, allow=False)  # egress OFF -- still allowed

    async def _fake_test(self, company_id, carrier_account_id):
        return True, "Credentials accepted"

    with patch("app.services.shipping_service.ShippingService.test_connection", _fake_test):
        resp = client.post(
            f"/api/v1/admin/settings/carrier-accounts/{account.id}/test-connection",
            headers=headers_for(admin),
        )
    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    assert body["ok"] is True
    assert body["provider"] == "easypost"


# ---------------------------------------------------------------------------
# 5. Shipping profile + egress toggle audit.
# ---------------------------------------------------------------------------


def test_shipping_profile_egress_toggle_audited(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN)
    # Create profile with egress OFF (default).
    resp = client.put(
        "/api/v1/admin/settings/shipping-profile",
        headers=headers_for(admin),
        json={"ship_from_name": "Werco", "ship_from_city": "Toledo"},
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["allow_carrier_egress"] is False

    # Flip egress ON -- recorded as a STATUS_CHANGE.
    resp = client.put(
        "/api/v1/admin/settings/shipping-profile",
        headers=headers_for(admin),
        json={"allow_carrier_egress": True},
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["allow_carrier_egress"] is True

    status_changes = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.resource_type == "company_shipping_profile",
            AuditLog.action == "STATUS_CHANGE",
        )
        .all()
    )
    assert len(status_changes) == 1


# ---------------------------------------------------------------------------
# 6. Inbound webhook tenant resolution from STORED data.
# ---------------------------------------------------------------------------


def _webhook_provider(*, verify: bool, parsed=None):
    """Provider double for the webhook path.

    ``verify_webhook_signature`` is SYNCHRONOUS in the real ABC (a plain bool),
    so it must be a ``MagicMock`` -- an ``AsyncMock`` would return an unawaited
    coroutine that the handler treats as truthy. ``parse_tracking_webhook`` is
    async, so it stays an ``AsyncMock``.
    """
    provider = MagicMock()
    provider.verify_webhook_signature = MagicMock(return_value=verify)
    provider.parse_tracking_webhook = AsyncMock(return_value=parsed)
    return provider


def _parsed(provider_shipment_id="shp_webhook_1"):
    return ParsedTrackingWebhook(
        provider_shipment_id=provider_shipment_id,
        tracking_number="1Z999",
        events=[
            TrackingEvent(
                status=TrackingStatus.IN_TRANSIT,
                status_detail="In transit",
                occurred_at=datetime(2026, 6, 9, 12, 0, 0),
                provider_event_id="evt_1",
            )
        ],
        verified=False,
    )


def test_webhook_bad_signature_dropped(client: TestClient, db_session: Session):
    make_carrier_account(db_session, webhook_secret="whsec_a")
    make_shipment(db_session, aggregator_shipment_id="shp_webhook_1")

    provider = _webhook_provider(verify=False)  # bad sig
    with (
        patch("app.services.carriers.registry.get_provider", return_value=provider),
        patch("app.api.endpoints.carrier_webhooks.enqueue_job", new=AsyncMock()) as enq,
    ):
        resp = client.post("/api/v1/webhooks/carriers/easypost", content=b'{"x":1}')
    assert resp.status_code == status.HTTP_204_NO_CONTENT
    enq.assert_not_called()  # nothing enqueued


def test_webhook_valid_signature_resolves_tenant_and_enqueues(client: TestClient, db_session: Session):
    # Company B owns the shipment with this aggregator id.
    make_carrier_account(db_session, company_id=COMPANY_B, webhook_secret="whsec_b")
    shipment = make_shipment(db_session, company_id=COMPANY_B, aggregator_shipment_id="shp_webhook_1")

    provider = _webhook_provider(verify=True, parsed=_parsed("shp_webhook_1"))

    with (
        patch("app.services.carriers.registry.get_provider", return_value=provider),
        patch("app.api.endpoints.carrier_webhooks.enqueue_job", new=AsyncMock()) as enq,
    ):
        # The caller sends NOTHING that identifies a company; resolution is purely
        # from the stored aggregator_shipment_id.
        resp = client.post("/api/v1/webhooks/carriers/easypost", content=b'{"result":{}}')

    assert resp.status_code == status.HTTP_200_OK
    enq.assert_awaited_once()
    _, kwargs = enq.call_args
    assert kwargs["company_id"] == COMPANY_B  # resolved from stored data, not caller input
    assert kwargs["shipment_id"] == shipment.id
    assert kwargs["events"][0]["provider_event_id"] == "evt_1"
    assert kwargs["events"][0]["status"] == "in_transit"


def test_webhook_cross_tenant_forgery_dropped(client: TestClient, db_session: Session):
    """Finding 1 regression: a webhook signed with tenant A's VALID secret that
    embeds tenant B's stored aggregator_shipment_id must NOT write onto tenant B.

    Only tenant A has a carrier account with a webhook secret, so A is the verifying
    account. The parsed body carries B's aggregator id. Because resolution is scoped
    to the verifying account's (A's) tenant, B's shipment is invisible -> 204, no
    enqueue. (Before the fix, the global lookup resolved B's shipment and B's
    company_id flowed back as the write tenant -- a cross-tenant write.)
    """
    make_carrier_account(db_session, company_id=COMPANY_A, webhook_secret="whsec_a")
    # Tenant B owns the shipment whose stored aggregator id the attacker embeds.
    b_shipment = make_shipment(db_session, company_id=COMPANY_B, aggregator_shipment_id="shp_tenant_b")

    provider = _webhook_provider(verify=True, parsed=_parsed("shp_tenant_b"))
    with (
        patch("app.services.carriers.registry.get_provider", return_value=provider),
        patch("app.api.endpoints.carrier_webhooks.enqueue_job", new=AsyncMock()) as enq,
    ):
        resp = client.post("/api/v1/webhooks/carriers/easypost", content=b'{"result":{}}')

    assert resp.status_code == status.HTTP_204_NO_CONTENT
    enq.assert_not_called()  # nothing enqueued -> nothing written onto tenant B

    # Belt-and-suspenders: no tracking events were written onto tenant B's shipment.
    from app.models.shipping import ShipmentTrackingEvent

    assert (
        db_session.query(ShipmentTrackingEvent).filter(ShipmentTrackingEvent.shipment_id == b_shipment.id).count() == 0
    )


def test_webhook_verified_but_unknown_shipment_dropped(client: TestClient, db_session: Session):
    make_carrier_account(db_session, webhook_secret="whsec_a")
    # No shipment with this aggregator id exists.
    provider = _webhook_provider(verify=True, parsed=_parsed("shp_does_not_exist"))

    with (
        patch("app.services.carriers.registry.get_provider", return_value=provider),
        patch("app.api.endpoints.carrier_webhooks.enqueue_job", new=AsyncMock()) as enq,
    ):
        resp = client.post("/api/v1/webhooks/carriers/easypost", content=b'{"result":{}}')
    assert resp.status_code == status.HTTP_204_NO_CONTENT
    enq.assert_not_called()


def test_webhook_requires_no_auth(client: TestClient, db_session: Session):
    # No Authorization header at all -> NOT 401 (carriers can't present a JWT).
    # With no carrier accounts configured, it drops with 204.
    resp = client.post("/api/v1/webhooks/carriers/easypost", content=b'{}')
    assert resp.status_code == status.HTTP_204_NO_CONTENT


# ---------------------------------------------------------------------------
# 7. Error mapping: freight unsupported -> 501.
# ---------------------------------------------------------------------------


def test_buy_bol_not_supported_maps_501(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN)
    make_carrier_account(db_session)
    set_egress(db_session, COMPANY_A, allow=True)
    shipment = make_shipment(db_session, aggregator_shipment_id="shp_freight")

    from app.services.carriers.exceptions import NotSupportedError

    provider = AsyncMock()
    provider.buy_bol.side_effect = NotSupportedError("EasyPost adapter does not support LTL")
    with patch("app.services.carriers.registry.get_provider", return_value=provider):
        resp = client.post(
            f"/api/v1/shipping/{shipment.id}/buy-bol",
            headers=headers_for(admin),
            json={"rate_id": "shp_freight:rate_1"},
        )
    assert resp.status_code == status.HTTP_501_NOT_IMPLEMENTED


def test_validate_address_egress_gated(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN)
    make_carrier_account(db_session)
    set_egress(db_session, COMPANY_A, allow=False)
    with patch("app.services.carriers.registry.get_provider", return_value=_ExplodingProvider()):
        resp = client.post(
            "/api/v1/shipping/validate-address",
            headers=headers_for(admin),
            json={
                "address": {
                    "street1": "1 Main St",
                    "city": "Springfield",
                    "state": "IL",
                    "zip": "62704",
                }
            },
        )
    assert resp.status_code == status.HTTP_409_CONFLICT


def test_validate_address_success(client: TestClient, db_session: Session):
    admin = make_user(db_session, role=UserRole.ADMIN)
    make_carrier_account(db_session)
    set_egress(db_session, COMPANY_A, allow=True)

    normalized = CarrierAddress(street1="1 MAIN ST", city="SPRINGFIELD", state="IL", zip="62704-0001")
    provider = AsyncMock()
    provider.validate_address.return_value = AddressValidationResult(
        is_valid=True, normalized=normalized, messages=[], deliverability="Y"
    )
    with patch("app.services.carriers.registry.get_provider", return_value=provider):
        resp = client.post(
            "/api/v1/shipping/validate-address",
            headers=headers_for(admin),
            json={"address": {"street1": "1 main st", "city": "springfield", "state": "il", "zip": "62704"}},
        )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["is_valid"] is True
    assert body["normalized"]["zip"] == "62704-0001"


def test_tracking_read_returns_stored_events(client: TestClient, db_session: Session):
    """The tracking GET is read-only, not egress-gated, and serves stored events."""
    from app.models.shipping import ShipmentTrackingEvent

    viewer = make_user(db_session, role=UserRole.OPERATOR)  # any authenticated user may read
    shipment = make_shipment(db_session)
    shipment.tracking_status = "in_transit"
    evt = ShipmentTrackingEvent(
        shipment_id=shipment.id,
        status="in_transit",
        occurred_at=datetime(2026, 6, 9, 10, 0, 0),
        provider_event_id="evt_a",
        source="webhook",
    )
    evt.company_id = COMPANY_A
    db_session.add(evt)
    db_session.commit()

    resp = client.get(f"/api/v1/shipping/{shipment.id}/tracking", headers=headers_for(viewer))
    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    assert body["tracking_status"] == "in_transit"
    assert len(body["events"]) == 1
    assert body["events"][0]["provider_event_id"] == "evt_a"


# ---------------------------------------------------------------------------
# 9. ARQ tracking jobs: webhook-apply task + cron poll fallback.
# ---------------------------------------------------------------------------


async def test_process_tracking_webhook_task_applies_events_tenant_scoped(db_session: Session, monkeypatch):
    """The webhook-apply task rehydrates the enqueued dicts and persists them under
    the RESOLVED tenant (company_id passed in by the verified webhook handler)."""
    from app.jobs import shipping_jobs
    from app.models.shipping import ShipmentTrackingEvent

    monkeypatch.setattr(shipping_jobs, "SessionLocal", lambda: db_session)

    shipment = make_shipment(db_session, company_id=COMPANY_B, aggregator_shipment_id="shp_apply_1")
    shipment_id, company_id = shipment.id, shipment.company_id

    # Shape matches carrier_webhooks._event_to_payload (JSON-safe primitives).
    events = [
        {
            "status": "in_transit",
            "status_detail": "Departed facility",
            "occurred_at": "2026-06-09T12:00:00",
            "location": "Memphis, TN",
            "message": "In transit",
            "provider_event_id": "evt_apply_1",
        }
    ]

    result = await shipping_jobs.process_tracking_webhook_task(
        company_id=company_id, shipment_id=shipment_id, provider="easypost", events=events
    )

    assert result["applied"] == 1
    assert result["company_id"] == COMPANY_B
    rows = db_session.query(ShipmentTrackingEvent).filter(ShipmentTrackingEvent.shipment_id == shipment_id).all()
    assert len(rows) == 1
    assert rows[0].provider_event_id == "evt_apply_1"
    assert rows[0].company_id == COMPANY_B  # tenant-scoped to the resolved company
    assert rows[0].source == "webhook"
    refreshed = db_session.query(Shipment).filter(Shipment.id == shipment_id).first()
    assert refreshed.tracking_status == "in_transit"


async def test_process_tracking_webhook_task_no_events_is_noop(db_session: Session, monkeypatch):
    from app.jobs import shipping_jobs

    monkeypatch.setattr(shipping_jobs, "SessionLocal", lambda: db_session)
    shipment = make_shipment(db_session, aggregator_shipment_id="shp_apply_2")

    result = await shipping_jobs.process_tracking_webhook_task(
        company_id=shipment.company_id, shipment_id=shipment.id, provider="easypost", events=[]
    )
    assert result["applied"] == 0
    assert result["reason"] == "no_events"


async def test_poll_tracking_skips_company_with_egress_off(db_session: Session, monkeypatch):
    """The poll fallback makes NO outbound carrier call for a tenant whose
    allow_carrier_egress kill switch is OFF (the default)."""
    from app.jobs import shipping_jobs

    monkeypatch.setattr(shipping_jobs, "SessionLocal", lambda: db_session)
    set_egress(db_session, COMPANY_A, allow=False)
    make_carrier_account(db_session)
    shipment = make_shipment(db_session, aggregator_shipment_id="shp_poll_off")
    shipment.tracking_number = "1Z_POLL_OFF"
    shipment.tracking_status = "in_transit"
    db_session.commit()

    async def _boom(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("get_tracking called while egress OFF")

    provider = MagicMock()
    provider.get_tracking = AsyncMock(side_effect=_boom)
    with patch("app.services.carriers.registry.get_provider", return_value=provider):
        result = await shipping_jobs.poll_tracking_task()

    assert result["companies_polled"] == 0
    assert result["shipments_polled"] == 0
    provider.get_tracking.assert_not_called()


async def test_poll_tracking_polls_in_flight_and_skips_terminal(db_session: Session, monkeypatch):
    """With egress ON, the poll fallback fetches tracking for the in-flight
    shipment and applies new events, while skipping a delivered (terminal) one."""
    from app.jobs import shipping_jobs
    from app.models.shipping import ShipmentTrackingEvent

    monkeypatch.setattr(shipping_jobs, "SessionLocal", lambda: db_session)
    set_egress(db_session, COMPANY_A, allow=True)
    make_carrier_account(db_session)

    in_flight = make_shipment(db_session, aggregator_shipment_id="shp_poll_inflight")
    in_flight.tracking_number = "1Z_INFLIGHT"
    in_flight.tracking_status = "in_transit"
    in_flight.carrier = "ups"
    in_flight_id = in_flight.id

    delivered = make_shipment(db_session, aggregator_shipment_id="shp_poll_delivered")
    delivered.tracking_number = "1Z_DELIVERED"
    delivered.tracking_status = "delivered"
    delivered.carrier = "ups"
    delivered_id = delivered.id
    db_session.commit()

    fetched_for: list[str] = []

    async def _get_tracking(tracking_number, *, carrier):
        fetched_for.append(tracking_number)
        return [
            TrackingEvent(
                status=TrackingStatus.OUT_FOR_DELIVERY,
                occurred_at=datetime(2026, 6, 9, 14, 0, 0),
                provider_event_id="evt_poll_1",
                message="Out for delivery",
            )
        ]

    provider = MagicMock()
    provider.get_tracking = AsyncMock(side_effect=_get_tracking)
    with patch("app.services.carriers.registry.get_provider", return_value=provider):
        result = await shipping_jobs.poll_tracking_task()

    # Only the in-flight shipment was polled; the delivered one was skipped.
    assert fetched_for == ["1Z_INFLIGHT"]
    assert result["companies_polled"] == 1
    assert result["shipments_polled"] == 1
    assert result["shipments_updated"] == 1

    in_flight_rows = (
        db_session.query(ShipmentTrackingEvent).filter(ShipmentTrackingEvent.shipment_id == in_flight_id).all()
    )
    assert [r.provider_event_id for r in in_flight_rows] == ["evt_poll_1"]
    assert in_flight_rows[0].source == "poll"
    assert (
        db_session.query(ShipmentTrackingEvent).filter(ShipmentTrackingEvent.shipment_id == delivered_id).count() == 0
    )


async def test_poll_tracking_swallows_provider_error(db_session: Session, monkeypatch):
    """A provider failure for one shipment is logged and swallowed -- the cron
    never raises out of the worker."""
    from app.jobs import shipping_jobs

    monkeypatch.setattr(shipping_jobs, "SessionLocal", lambda: db_session)
    set_egress(db_session, COMPANY_A, allow=True)
    make_carrier_account(db_session)
    shipment = make_shipment(db_session, aggregator_shipment_id="shp_poll_err")
    shipment.tracking_number = "1Z_ERR"
    shipment.tracking_status = "in_transit"
    db_session.commit()

    provider = MagicMock()
    provider.get_tracking = AsyncMock(side_effect=RuntimeError("carrier 500"))
    with patch("app.services.carriers.registry.get_provider", return_value=provider):
        result = await shipping_jobs.poll_tracking_task()  # must not raise

    assert result["companies_polled"] == 1
    assert result["shipments_updated"] == 0
