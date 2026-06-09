"""Inbound carrier tracking webhooks (EasyPost / future Zenkraft).

This is the ONLY router in the app with NO auth dependency -- a carrier cannot
present a JWT. Trust is established by HMAC signature verification against a
stored per-tenant webhook secret, and the owning tenant is resolved exclusively
from STORED shipment data (never from anything the caller sends).

SECURITY / COMPLIANCE flow (defense in depth):

1. Read the RAW body BEFORE any parsing (HMAC is computed over the exact bytes).
2. Find candidate ``CarrierAccount`` rows for ``{provider}`` that have a webhook
   secret configured -- across ALL tenants (the caller didn't tell us which).
3. Verify the signature against each candidate's DECRYPTED webhook secret with
   the provider's constant-time ``verify_webhook_signature``. A request that
   matches NO secret is dropped with 204 -- we do NOT reveal whether a shipment
   exists or whether verification failed vs. matched-no-shipment (no oracle).
4. Parse the payload to recover the provider shipment id / tracking number, then
   resolve the owning ``Shipment`` from that STORED id via
   ``ShippingService._resolve_shipment_by_aggregator_id`` -- SCOPED to the
   VERIFYING account's ``company_id`` (and ``carrier_account_id``). The HMAC trust
   boundary and the resolved tenant are thus the SAME account: a tenant holding a
   valid webhook secret can only ever touch ITS OWN shipments, never another
   tenant's, even if it embeds another tenant's stored ``aggregator_shipment_id``
   in the body. The caller-supplied path/body can never select the tenant.
5. Enqueue the ARQ ``process_tracking_webhook_job`` with the RESOLVED
   ``company_id`` + ``shipment_id`` + normalized events, and return 200 fast.

The actual DB write (de-dup + status flow-back via
``ShippingService.record_tracking_events``) happens in the ARQ job (next stage),
so the request handler stays fast and never blocks on the write.
"""

import logging
from typing import List, Optional, Tuple

from fastapi import APIRouter, Request, Response, status

from app.core.queue import enqueue_job
from app.db.database import SessionLocal
from app.models.carrier_account import CarrierAccount
from app.services.carriers import registry
from app.services.carriers.crypto import decrypt_secret
from app.services.carriers.types import ParsedTrackingWebhook, TrackingEvent, TrackingStatus
from app.services.shipping_service import ShippingService

logger = logging.getLogger(__name__)

router = APIRouter()

# ARQ job name (registered by the next stage's worker wiring). The handler
# enqueues by string name so it does not import the job module directly.
TRACKING_WEBHOOK_JOB = "process_tracking_webhook_job"


def _event_to_payload(event: TrackingEvent) -> dict:
    """Serialize a normalized TrackingEvent into JSON-safe primitives for the job."""
    status_value = event.status.value if isinstance(event.status, TrackingStatus) else str(event.status)
    return {
        "status": status_value,
        "status_detail": event.status_detail,
        "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
        "location": event.location,
        "message": event.message,
        "provider_event_id": event.provider_event_id,
    }


async def _verify_and_parse(
    db, provider_name: str, headers, raw_body: bytes
) -> Optional[Tuple[CarrierAccount, ParsedTrackingWebhook]]:
    """Verify the signature against any tenant's secret and parse the payload.

    Iterates candidate carrier accounts for ``provider_name`` that have a webhook
    secret. On the FIRST account whose secret verifies the signature, returns
    ``(verifying_account, parsed_webhook)``; ``None`` if no candidate verifies
    (drop the request).

    SECURITY: this does NOT scope by company (the caller hasn't authenticated to
    one) -- trust comes from the HMAC match. The VERIFYING account is returned so
    the handler can constrain shipment resolution to THAT account's tenant: the
    HMAC trust boundary and the resolved tenant must be the same account, or a
    tenant with a valid secret could forge events onto another tenant's shipment.
    """
    candidates = (
        db.query(CarrierAccount)
        .filter(
            CarrierAccount.provider == provider_name,
            CarrierAccount.is_deleted == False,  # noqa: E712
            CarrierAccount.webhook_secret_encrypted.isnot(None),
        )
        .all()
    )
    for account in candidates:
        try:
            provider = registry.get_provider(account)
            secret = decrypt_secret(account.webhook_secret_encrypted)
        except Exception:  # noqa: BLE001 - bad key/secret / unsupported provider => not a match
            continue
        try:
            if not provider.verify_webhook_signature(headers, raw_body, secret):
                continue
            parsed = await provider.parse_tracking_webhook(headers, raw_body)
        except Exception:  # noqa: BLE001 - a malformed body from one match must not 500
            logger.warning("Carrier webhook (%s): signature matched but payload parse failed", provider_name)
            return None
        return account, parsed
    return None


@router.post("/carriers/{provider}")
async def receive_carrier_webhook(provider: str, request: Request):
    """Inbound carrier tracking webhook. NO auth -- trust is HMAC + stored data.

    Always returns 200/204 quickly. A drop (no signature match or no owning
    shipment) returns 204 with no body so the caller learns nothing about what
    exists in the system. On a verified, resolvable event the normalized events
    are enqueued to the ARQ job with the RESOLVED company_id + shipment_id.
    """
    provider_name = (provider or "").strip().lower()
    # 1. Read the RAW body BEFORE parsing -- the HMAC is over the exact bytes.
    raw_body = await request.body()
    headers = request.headers

    # Use a dedicated short-lived session (this path has no get_db dependency, to
    # avoid pulling in the auth/read-only-context machinery); always close it.
    db = SessionLocal()
    try:
        verified = await _verify_and_parse(db, provider_name, headers, raw_body)
        if verified is None:
            # No tenant secret verified the signature -> drop silently (no oracle).
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        account, parsed = verified
        if parsed is None or not parsed.events:
            # Nothing actionable -> drop silently.
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        # 4. Resolve the owning shipment from STORED data, SCOPED to the VERIFYING
        # account's tenant + account. The HMAC trust boundary and the resolved
        # tenant are the SAME account: a tenant cannot use its own valid secret to
        # write tracking onto another tenant's (or another account's) shipment by
        # embedding a foreign aggregator_shipment_id in the body.
        service = ShippingService(db)
        shipment = service._resolve_shipment_by_aggregator_id(
            provider_shipment_id=parsed.provider_shipment_id,
            tracking_number=parsed.tracking_number,
            company_id=account.company_id,
            carrier_account_id=account.id,
        )
        if shipment is None:
            # Signature valid but no shipment owned by the verifying account
            # matches -> drop silently (no existence oracle).
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        company_id = shipment.company_id
        shipment_id = shipment.id
    finally:
        db.close()

    # 5. Enqueue the write off the request thread with the RESOLVED tenant.
    event_payloads: List[dict] = [_event_to_payload(e) for e in parsed.events]
    try:
        await enqueue_job(
            TRACKING_WEBHOOK_JOB,
            company_id=company_id,
            shipment_id=shipment_id,
            provider=provider_name,
            events=event_payloads,
        )
    except Exception:  # noqa: BLE001 - a Redis hiccup must not 500 the carrier
        logger.exception("Failed to enqueue %s for shipment %s", TRACKING_WEBHOOK_JOB, shipment_id)
        # Still acknowledge so the carrier does not hammer us with retries; the
        # poll fallback / next webhook re-delivers the tracking state.
        return Response(status_code=status.HTTP_202_ACCEPTED)

    return Response(status_code=status.HTTP_200_OK)
