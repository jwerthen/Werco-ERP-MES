"""Carrier tracking ARQ jobs: inbound-webhook apply + cron poll fallback.

Two background entry points keep a shipment's tracking state current without ever
blocking a request handler:

* ``process_tracking_webhook_task`` -- applies the normalized events an inbound
  carrier webhook produced. The owning tenant was ALREADY resolved (by the
  verified webhook handler) from STORED shipment data, never caller input, and is
  passed in as ``company_id``; this task simply hands the events to
  ``ShippingService.record_tracking_events`` under that tenant.

* ``poll_tracking_task`` -- a cron fallback for shipments that have a tracking
  number but a stale/non-terminal status (e.g. a webhook was missed). It fans out
  over every active company (mirroring ``run_mrp_task`` / ``run_scheduling_task``),
  and -- because ``provider.get_tracking`` is an OUTBOUND carrier call -- only
  polls tenants whose ``allow_carrier_egress`` kill switch is ON. It is strictly
  best-effort: every per-shipment / per-company failure is swallowed and logged so
  the cron never raises out of the worker.

COMPLIANCE:
* Tenant isolation -- both paths scope every DB write by ``company_id`` and the
  poll loop runs one isolated pass per tenant.
* Egress kill switch -- the poll loop honors ``allow_carrier_egress`` (defaults
  OFF) before making any provider call; the webhook apply path makes NO outbound
  call (it only persists inbound data) so it is not egress-gated.
* Secrets are never logged; provider construction decrypts in-memory only.
"""

import logging
from typing import List, Optional

from app.db.session import SessionLocal
from app.models.carrier_account import CompanyShippingProfile
from app.models.company import Company
from app.models.shipping import Shipment
from app.services.carriers.exceptions import CarrierError
from app.services.carriers.types import TrackingEvent, TrackingStatus
from app.services.shipping_service import ShippingService

logger = logging.getLogger(__name__)

# Tracking statuses that mean the shipment lifecycle is over -- the poll fallback
# skips these (nothing left to learn). Anything else (incl. NULL / unknown) is
# still in flight and worth re-polling.
_TERMINAL_TRACKING_STATUSES = {
    TrackingStatus.DELIVERED.value,
    TrackingStatus.RETURNED.value,
    TrackingStatus.FAILURE.value,
}


def _event_from_payload(payload: dict) -> TrackingEvent:
    """Rehydrate a JSON-safe event dict (from the webhook handler) into a TrackingEvent.

    The inbound-webhook endpoint serialized each ``TrackingEvent`` to primitives
    via ``_event_to_payload`` before enqueuing; this is the inverse. Pydantic v2
    coerces the ISO ``occurred_at`` string and the ``status`` enum value.
    """
    return TrackingEvent.model_validate(payload)


async def process_tracking_webhook_task(
    *,
    company_id: int,
    shipment_id: int,
    provider: Optional[str] = None,
    events: Optional[List[dict]] = None,
) -> dict:
    """Apply inbound carrier-webhook tracking events to a shipment.

    ``company_id`` / ``shipment_id`` were resolved by the verified webhook handler
    from STORED shipment data (the ``aggregator_shipment_id``), so this task trusts
    them as the tenant scope. Opens its own session, rehydrates the event dicts,
    and applies them via ``ShippingService.record_tracking_events`` (de-dup +
    status flow-back), then closes the session.
    """
    event_objs: List[TrackingEvent] = [_event_from_payload(e) for e in (events or [])]
    if not event_objs:
        return {"shipment_id": shipment_id, "company_id": company_id, "applied": 0, "reason": "no_events"}

    db = SessionLocal()
    try:
        service = ShippingService(db)
        inserted = service.record_tracking_events(
            company_id=company_id,
            shipment_id=shipment_id,
            events=event_objs,
            source="webhook",
        )
        return {
            "shipment_id": shipment_id,
            "company_id": company_id,
            "provider": provider,
            "applied": len(inserted),
        }
    finally:
        db.close()


async def poll_tracking_task() -> dict:
    """Cron fallback: refresh tracking for in-flight shipments, per tenant.

    Fans out over every active company (one isolated pass each). For tenants with
    ``allow_carrier_egress`` enabled, finds shipments with a tracking number and a
    non-terminal tracking status, calls ``provider.get_tracking``, and applies the
    returned events via ``record_tracking_events`` (source="poll"). Best-effort:
    every failure is logged and swallowed so the cron never raises.
    """
    db = SessionLocal()
    polled = 0
    updated = 0
    companies_polled = 0
    try:
        company_ids: List[int] = [
            row_id for (row_id,) in db.query(Company.id).filter(Company.is_active == True).all()  # noqa: E712
        ]
        for cid in company_ids:
            try:
                # Egress kill switch (defaults OFF): no outbound carrier traffic
                # for a tenant that has not signed off on data egress.
                profile = db.query(CompanyShippingProfile).filter(CompanyShippingProfile.company_id == cid).first()
                if profile is None or profile.allow_carrier_egress is not True:
                    continue
                companies_polled += 1
                p, u = await _poll_tracking_for_company(db, cid)
                polled += p
                updated += u
            except Exception:  # noqa: BLE001 - one tenant must not abort the others
                logger.exception("poll_tracking: company %s failed", cid)
        return {"companies_polled": companies_polled, "shipments_polled": polled, "shipments_updated": updated}
    finally:
        db.close()


async def _poll_tracking_for_company(db, company_id: int) -> tuple[int, int]:
    """Poll all in-flight shipments for one tenant. Returns (polled, updated)."""
    shipments = (
        db.query(Shipment)
        .filter(
            Shipment.company_id == company_id,
            Shipment.is_deleted == False,  # noqa: E712
            Shipment.tracking_number.isnot(None),
            Shipment.voided_at.is_(None),
        )
        .all()
    )

    service = ShippingService(db)
    polled = 0
    updated = 0
    for shipment in shipments:
        status_value = (shipment.tracking_status or "").strip().lower()
        if status_value in _TERMINAL_TRACKING_STATUSES:
            continue
        if not shipment.tracking_number:
            continue
        try:
            events = await _fetch_tracking(service, company_id, shipment)
        except Exception:  # noqa: BLE001 - never leak internals/secrets; keep polling
            logger.warning("poll_tracking: get_tracking failed for shipment %s (company %s)", shipment.id, company_id)
            continue
        polled += 1
        if not events:
            continue
        try:
            inserted = service.record_tracking_events(
                company_id=company_id,
                shipment_id=shipment.id,
                events=events,
                source="poll",
            )
            if inserted:
                updated += 1
        except Exception:  # noqa: BLE001 - a bad apply must not abort the rest
            logger.exception("poll_tracking: applying events failed for shipment %s", shipment.id)
    return polled, updated


async def _fetch_tracking(service: ShippingService, company_id: int, shipment: Shipment) -> List[TrackingEvent]:
    """Resolve the tenant's provider and fetch the current tracking history.

    Tenant-scoped provider selection (the shipment's own carrier account when set,
    else the company default). Raises ``CarrierError`` when no provider is usable;
    the caller treats that as a skip.
    """
    provider, _ = service._provider_for(company_id, shipment.carrier_account_id)
    try:
        return await provider.get_tracking(shipment.tracking_number, carrier=shipment.carrier or "")
    except CarrierError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize to a skip without leaking detail
        raise CarrierError(f"tracking fetch failed: {type(exc).__name__}") from exc
