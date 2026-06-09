"""Unit tests for ``ShippingService`` (the carrier-integration business logic).

These drive the service directly against the shared per-worker SQLite ``db_session``
with a FAKE ``CarrierProvider`` patched in at ``registry.get_provider`` -- NO real
network. They pin the compliance-critical service behaviors that the thin routers
delegate to:

* **Egress kill switch.** With ``allow_carrier_egress`` OFF (the default), every
  customer-data-bearing method raises ``EgressDisabledError`` and the provider is
  NEVER invoked (asserted on the mock).
* **rate_shop** persists ``ShipmentRateQuote`` rows, captures the aggregator
  shipment id, and replaces a prior quote set.
* **buy_label IDEMPOTENCY** -- a second call for an already-purchased shipment is a
  no-op: no second provider buy, no second ``Document``, no second audit row.
* **buy_label audit** -- writes ONE tamper-evident ``AuditService`` row carrying the
  cost (Decimal) and carrier, never a secret.
* **record_tracking_events** de-dups by ``provider_event_id`` and, on a DELIVERED
  event, sets ``actual_delivery`` WITHOUT closing the work order.
* **Tenant isolation** -- the service refuses to load another company's carrier
  account or shipment.

Per the xdist/SQLite convention, rows use globally-unique natural keys (a
module-level counter) and assertions key off those, never row counts across the DB.
"""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.carrier_account import CarrierAccount, CompanyShippingProfile
from app.models.company import Company
from app.models.document import Document
from app.models.part import Part, PartType
from app.models.shipping import (
    Shipment,
    ShipmentRateQuote,
    ShipmentTrackingEvent,
)
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.services.audit_service import AuditService
from app.services.carriers.crypto import encrypt_secret
from app.services.carriers.exceptions import EgressDisabledError
from app.services.carriers.types import (
    Label,
    RateQuote,
    TrackingEvent,
    TrackingStatus,
)
from app.services.shipping_service import ShippingService

pytestmark = [pytest.mark.unit, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> None:
    if not db.query(Company).filter(Company.id == company_id).first():
        db.add(Company(id=company_id, name=f"Co {company_id}", slug=f"svc-co-{company_id}", is_active=True))
        db.commit()


def make_user(db: Session, *, company_id: int = COMPANY_A, role: UserRole = UserRole.MANAGER) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=f"svc-{n}@co{company_id}.test",
        employee_id=f"SVC-{n:05d}",
        first_name="Svc",
        last_name=f"C{company_id}",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",
        role=role,
        is_active=True,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def make_shipment(db: Session, *, company_id: int = COMPANY_A, aggregator_shipment_id=None) -> Shipment:
    _ensure_company(db, company_id)
    n = _next()
    part = Part(part_number=f"PN-{n:05d}", name=f"Part {n}", part_type=PartType.MANUFACTURED, company_id=company_id)
    db.add(part)
    db.flush()
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
    db: Session, *, company_id: int = COMPANY_A, api_key: str = "EZTK_secret_1234"
) -> CarrierAccount:
    _ensure_company(db, company_id)
    n = _next()
    account = CarrierAccount(
        name=f"Acct {n}",
        provider="easypost",
        environment="test",
        encrypted_api_key=encrypt_secret(api_key),
        is_active=True,
        is_default=True,
    )
    account.company_id = company_id
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def set_egress(db: Session, company_id: int, allow: bool) -> CompanyShippingProfile:
    _ensure_company(db, company_id)
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


def _rate(provider_rate_id="shp_svc_1:rate_a", amount="7.50", carrier="USPS"):
    return RateQuote(
        provider_rate_id=provider_rate_id,
        carrier=carrier,
        service_code="Priority",
        service_name="Priority",
        mode="parcel",
        amount=Decimal(amount),
        currency="USD",
        est_delivery_days=2,
    )


def _label(provider_shipment_id="shp_svc_1", cost="7.50", label_bytes=b"%PDF-1.4 svc-label"):
    # The real adapter now fetches the hosted label PDF and returns its bytes (so the
    # service can persist a DOWNLOADABLE Document with a file_path). Mirror that here.
    return Label(
        provider_shipment_id=provider_shipment_id,
        provider_label_id="pl_1",
        tracking_number="9400100000000000000000",
        label_format="PDF",
        label_url="https://easypost-files/label.pdf",
        label_bytes=label_bytes,
        carrier="USPS",
        service_code="Priority",
        cost=Decimal(cost),
    )


def _patch_provider(monkeypatch, provider) -> None:
    """Patch registry.get_provider AS IMPORTED BY the service module."""
    monkeypatch.setattr("app.services.carriers.registry.get_provider", lambda account: provider)


# ---------------------------------------------------------------------------
# 1. Egress kill switch: no provider call when egress is OFF.
# ---------------------------------------------------------------------------


async def test_rate_shop_egress_off_raises_and_never_calls_provider(db_session: Session, monkeypatch):
    make_carrier_account(db_session)
    set_egress(db_session, COMPANY_A, allow=False)
    shipment = make_shipment(db_session)

    provider = AsyncMock()
    _patch_provider(monkeypatch, provider)

    service = ShippingService(db_session)
    with pytest.raises(EgressDisabledError):
        await service.rate_shop(
            COMPANY_A,
            shipment.id,
            parcels=[{"length_in": "10", "width_in": "8", "height_in": "4", "weight_lbs": "2"}],
        )
    provider.get_rates.assert_not_called()  # short-circuited BEFORE any provider I/O
    # And no quotes were persisted.
    assert db_session.query(ShipmentRateQuote).filter(ShipmentRateQuote.shipment_id == shipment.id).count() == 0


async def test_buy_label_egress_off_raises_and_never_calls_provider(db_session: Session, monkeypatch):
    make_carrier_account(db_session)
    set_egress(db_session, COMPANY_A, allow=False)
    user = make_user(db_session)
    shipment = make_shipment(db_session, aggregator_shipment_id="shp_svc_1")

    provider = AsyncMock()
    _patch_provider(monkeypatch, provider)

    service = ShippingService(db_session)
    with pytest.raises(EgressDisabledError):
        await service.buy_label(COMPANY_A, shipment.id, "shp_svc_1:rate_a", user.id)
    provider.buy_label.assert_not_called()
    db_session.refresh(shipment)
    assert shipment.label_purchased_at is None
    assert shipment.label_document_id is None


# ---------------------------------------------------------------------------
# 2. rate_shop persists quotes + captures aggregator id + replaces prior set.
# ---------------------------------------------------------------------------


async def test_rate_shop_persists_quotes_and_captures_aggregator_id(db_session: Session, monkeypatch):
    make_carrier_account(db_session)
    set_egress(db_session, COMPANY_A, allow=True)
    shipment = make_shipment(db_session)

    provider = AsyncMock()
    provider.get_rates.return_value = [
        _rate("shp_rs_1:rate_a", "7.50", "USPS"),
        _rate("shp_rs_1:rate_b", "9.13", "UPS"),
    ]
    _patch_provider(monkeypatch, provider)

    service = ShippingService(db_session)
    persisted = await service.rate_shop(
        COMPANY_A,
        shipment.id,
        parcels=[{"length_in": "10", "width_in": "8", "height_in": "4", "weight_lbs": "2"}],
    )
    assert len(persisted) == 2
    rows = (
        db_session.query(ShipmentRateQuote)
        .filter(ShipmentRateQuote.shipment_id == shipment.id, ShipmentRateQuote.company_id == COMPANY_A)
        .all()
    )
    assert {r.provider_rate_id for r in rows} == {"shp_rs_1:rate_a", "shp_rs_1:rate_b"}
    # Money is Numeric/Decimal on the row.
    amounts = {r.provider_rate_id: r.amount for r in rows}
    assert amounts["shp_rs_1:rate_a"] == Decimal("7.50")
    assert isinstance(amounts["shp_rs_1:rate_a"], Decimal)
    # Aggregator shipment id captured from the composite rate id (webhook key).
    db_session.refresh(shipment)
    assert shipment.aggregator_shipment_id == "shp_rs_1"
    assert shipment.ship_mode == "parcel"


async def test_rate_shop_replaces_prior_quote_set(db_session: Session, monkeypatch):
    make_carrier_account(db_session)
    set_egress(db_session, COMPANY_A, allow=True)
    shipment = make_shipment(db_session)

    provider = AsyncMock()
    _patch_provider(monkeypatch, provider)
    service = ShippingService(db_session)

    provider.get_rates.return_value = [_rate("shp_old:rate_old", "5.00")]
    await service.rate_shop(
        COMPANY_A, shipment.id, parcels=[{"length_in": "1", "width_in": "1", "height_in": "1", "weight_lbs": "1"}]
    )

    provider.get_rates.return_value = [_rate("shp_new:rate_new", "6.00")]
    await service.rate_shop(
        COMPANY_A, shipment.id, parcels=[{"length_in": "1", "width_in": "1", "height_in": "1", "weight_lbs": "1"}]
    )

    rows = db_session.query(ShipmentRateQuote).filter(ShipmentRateQuote.shipment_id == shipment.id).all()
    # The prior set was replaced, not appended.
    assert [r.provider_rate_id for r in rows] == ["shp_new:rate_new"]


# ---------------------------------------------------------------------------
# 3. buy_label: idempotency + audit (cost, no secret) + Document storage.
# ---------------------------------------------------------------------------


async def test_buy_label_purchases_stores_document_and_audits_cost(db_session: Session, monkeypatch):
    make_carrier_account(db_session)
    set_egress(db_session, COMPANY_A, allow=True)
    user = make_user(db_session)
    shipment = make_shipment(db_session, aggregator_shipment_id="shp_buy_1")

    provider = AsyncMock()
    provider.buy_label.return_value = _label("shp_buy_1", "7.50")
    _patch_provider(monkeypatch, provider)

    audit = AuditService(db_session, user)
    service = ShippingService(db_session, audit)
    result_shipment, already = await service.buy_label(COMPANY_A, shipment.id, "shp_buy_1:rate_a", user.id)

    assert already is False
    provider.buy_label.assert_awaited_once()
    db_session.refresh(shipment)
    # Money is Decimal end-to-end.
    assert shipment.actual_cost == Decimal("7.50")
    assert isinstance(shipment.actual_cost, Decimal)
    assert shipment.tracking_number == "9400100000000000000000"
    assert shipment.carrier == "USPS"
    assert shipment.label_purchased_at is not None
    assert shipment.label_document_id is not None

    # A label Document was created for this tenant AND is DOWNLOADABLE: the fetched
    # PDF bytes were written to disk (file_path set + the file exists on disk), so
    # the auth-gated document download serves real bytes instead of 404ing (finding 3).
    import os as _os

    doc = db_session.query(Document).filter(Document.id == shipment.label_document_id).first()
    assert doc is not None
    assert doc.company_id == COMPANY_A
    assert doc.file_path is not None
    assert _os.path.exists(doc.file_path)
    assert doc.file_size and doc.file_size > 0
    with open(doc.file_path, "rb") as fh:
        assert fh.read() == b"%PDF-1.4 svc-label"

    # Exactly ONE shipment audit row, carrying the cost, with NO secret anywhere.
    audits = (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type == "shipment", AuditLog.resource_id == shipment.id)
        .all()
    )
    assert len(audits) == 1
    assert audits[0].action == "CREATE"
    assert audits[0].extra_data["cost"] == "7.50"
    assert audits[0].extra_data["carrier"] == "USPS"
    import json as _json

    blob = _json.dumps({"e": audits[0].extra_data, "d": audits[0].description})
    assert "EZTK_secret_1234" not in blob


async def test_buy_label_concurrent_idempotency_conflict_is_graceful_noop(db_session: Session, monkeypatch):
    """Finding 4: a concurrent double-buy that loses the uq_shipment_idempotency race
    surfaces IntegrityError on commit. The service must treat it as the documented
    already_purchased no-op (rollback + reload + return True), NOT a raw 500.
    """
    from sqlalchemy.exc import IntegrityError

    make_carrier_account(db_session)
    set_egress(db_session, COMPANY_A, allow=True)
    user = make_user(db_session)
    shipment = make_shipment(db_session, aggregator_shipment_id="shp_race_1")

    provider = AsyncMock()
    provider.buy_label.return_value = _label("shp_race_1", "7.50")
    _patch_provider(monkeypatch, provider)

    audit = AuditService(db_session, user)
    service = ShippingService(db_session, audit)

    # Simulate the losing transaction: the commit that would persist this buy hits the
    # partial-unique index (a sibling already committed the same idempotency key).
    real_commit = db_session.commit
    state = {"raised": False}

    def _commit_raises_once():
        if not state["raised"]:
            state["raised"] = True
            raise IntegrityError("INSERT", {}, Exception("uq_shipment_idempotency"))
        return real_commit()

    monkeypatch.setattr(db_session, "commit", _commit_raises_once)

    # Must NOT raise -- returns the graceful already_purchased no-op.
    result_shipment, already = await service.buy_label(COMPANY_A, shipment.id, "shp_race_1:rate_a", user.id)
    assert already is True
    assert state["raised"] is True  # the conflict path was actually exercised

    # The losing txn rolled back: no purchase landed on the shipment, no audit row.
    monkeypatch.setattr(db_session, "commit", real_commit)
    db_session.rollback()
    db_session.refresh(shipment)
    assert shipment.label_purchased_at is None
    audits = (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type == "shipment", AuditLog.resource_id == shipment.id)
        .all()
    )
    assert len(audits) == 0


async def test_buy_label_is_idempotent_second_call_noop(db_session: Session, monkeypatch):
    make_carrier_account(db_session)
    set_egress(db_session, COMPANY_A, allow=True)
    user = make_user(db_session)
    shipment = make_shipment(db_session, aggregator_shipment_id="shp_idem_1")

    provider = AsyncMock()
    provider.buy_label.return_value = _label("shp_idem_1", "7.50")
    _patch_provider(monkeypatch, provider)

    audit = AuditService(db_session, user)
    service = ShippingService(db_session, audit)

    _, first_already = await service.buy_label(COMPANY_A, shipment.id, "shp_idem_1:rate_a", user.id)
    assert first_already is False

    # Second call for the SAME shipment is a no-op.
    _, second_already = await service.buy_label(COMPANY_A, shipment.id, "shp_idem_1:rate_a", user.id)
    assert second_already is True

    # No second provider buy.
    provider.buy_label.assert_awaited_once()
    # No second Document.
    docs = db_session.query(Document).filter(Document.work_order_id == shipment.work_order_id).all()
    assert len(docs) == 1
    # No second audit row.
    audits = (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type == "shipment", AuditLog.resource_id == shipment.id)
        .all()
    )
    assert len(audits) == 1


# ---------------------------------------------------------------------------
# 4. record_tracking_events: de-dup + delivered -> actual_delivery, no WO close.
# ---------------------------------------------------------------------------


def test_record_tracking_events_dedups_by_provider_event_id(db_session: Session):
    shipment = make_shipment(db_session, aggregator_shipment_id="shp_trk_1")
    service = ShippingService(db_session)

    events = [
        TrackingEvent(status=TrackingStatus.IN_TRANSIT, provider_event_id="evt_1", message="A"),
        TrackingEvent(status=TrackingStatus.IN_TRANSIT, provider_event_id="evt_1", message="dup"),  # duplicate id
        TrackingEvent(status=TrackingStatus.OUT_FOR_DELIVERY, provider_event_id="evt_2", message="B"),
    ]
    inserted = service.record_tracking_events(COMPANY_A, shipment.id, events, source="webhook")
    assert {e.provider_event_id for e in inserted} == {"evt_1", "evt_2"}

    rows = db_session.query(ShipmentTrackingEvent).filter(ShipmentTrackingEvent.shipment_id == shipment.id).all()
    assert sorted(r.provider_event_id for r in rows) == ["evt_1", "evt_2"]

    # Re-applying the same batch inserts nothing new (cross-call de-dup).
    again = service.record_tracking_events(COMPANY_A, shipment.id, events, source="webhook")
    assert again == []
    rows2 = db_session.query(ShipmentTrackingEvent).filter(ShipmentTrackingEvent.shipment_id == shipment.id).all()
    assert len(rows2) == 2


def test_record_tracking_events_delivered_sets_actual_delivery_without_closing_wo(db_session: Session):
    from datetime import datetime

    shipment = make_shipment(db_session, aggregator_shipment_id="shp_trk_2")
    wo_id = shipment.work_order_id
    # Make the WO not-closed so we can prove the tracking apply does NOT close it.
    wo = db_session.query(WorkOrder).filter(WorkOrder.id == wo_id).first()
    wo.status = WorkOrderStatus.COMPLETE
    db_session.commit()

    service = ShippingService(db_session)
    delivered_at = datetime(2026, 6, 10, 9, 30, 0)
    events = [
        TrackingEvent(
            status=TrackingStatus.DELIVERED,
            status_detail="Delivered, front door",
            occurred_at=delivered_at,
            provider_event_id="evt_delivered",
        )
    ]
    service.record_tracking_events(COMPANY_A, shipment.id, events, source="webhook")

    db_session.refresh(shipment)
    assert shipment.tracking_status == "delivered"
    assert shipment.actual_delivery == delivered_at.date()
    assert shipment.last_tracking_sync_at is not None

    # Product decision: tracking is informational -- the WO is NOT auto-closed.
    db_session.refresh(wo)
    assert wo.status == WorkOrderStatus.COMPLETE
    assert wo.status != WorkOrderStatus.CLOSED


# ---------------------------------------------------------------------------
# 5. Tenant isolation: cannot load another company's account or shipment.
# ---------------------------------------------------------------------------


def test_service_refuses_foreign_company_shipment(db_session: Session):
    shipment_b = make_shipment(db_session, company_id=COMPANY_B, aggregator_shipment_id="shp_b")
    service = ShippingService(db_session)
    from app.services.carriers.exceptions import CarrierError

    # Company A asking for company B's shipment id -> not found (tenant-scoped).
    with pytest.raises(CarrierError):
        service._get_shipment(COMPANY_A, shipment_b.id)


def test_service_refuses_foreign_company_carrier_account(db_session: Session):
    account_b = make_carrier_account(db_session, company_id=COMPANY_B)
    service = ShippingService(db_session)
    from app.services.carriers.exceptions import CarrierError

    # Explicit foreign account id is not visible to company A.
    with pytest.raises(CarrierError):
        service._load_carrier_account(COMPANY_A, account_b.id)
    # And company A has no default account of its own.
    with pytest.raises(CarrierError):
        service._load_carrier_account(COMPANY_A)


def test_resolve_shipment_by_aggregator_id_returns_owning_tenant(db_session: Session):
    """Webhook tenant resolution: the company is derived from the STORED aggregator id,
    SCOPED to the verifying account's tenant (passed in by the webhook handler)."""
    shipment_b = make_shipment(db_session, company_id=COMPANY_B, aggregator_shipment_id="shp_resolve_b")
    service = ShippingService(db_session)
    resolved = service._resolve_shipment_by_aggregator_id(
        provider_shipment_id="shp_resolve_b", tracking_number=None, company_id=COMPANY_B
    )
    assert resolved is not None
    assert resolved.id == shipment_b.id
    assert resolved.company_id == COMPANY_B
    # An unknown aggregator id resolves to nothing (caller drops the event).
    assert (
        service._resolve_shipment_by_aggregator_id(
            provider_shipment_id="nope", tracking_number=None, company_id=COMPANY_B
        )
        is None
    )


def test_resolve_shipment_by_aggregator_id_is_tenant_scoped_against_cross_tenant_forgery(db_session: Session):
    """A foreign tenant's aggregator id is NOT resolvable when scoped to a different
    company -- the core defense against cross-tenant webhook write (finding 1).

    Tenant B owns the shipment with this stored aggregator id. Resolving with
    company_id=COMPANY_A (the verifying account's owner) must return None, so a
    webhook signed by tenant A's secret that embeds tenant B's aggregator id can
    never write onto tenant B's shipment.
    """
    make_shipment(db_session, company_id=COMPANY_B, aggregator_shipment_id="shp_cross_tenant")
    service = ShippingService(db_session)

    # Scoped to the WRONG company -> not found (the security boundary holds).
    assert (
        service._resolve_shipment_by_aggregator_id(
            provider_shipment_id="shp_cross_tenant", tracking_number=None, company_id=COMPANY_A
        )
        is None
    )
    # Scoped to the owning company -> resolves (legitimate path still works).
    assert (
        service._resolve_shipment_by_aggregator_id(
            provider_shipment_id="shp_cross_tenant", tracking_number=None, company_id=COMPANY_B
        )
        is not None
    )
