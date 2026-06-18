"""Auto-print-on-receipt ARQ job for the 4x6 thermal receiving label.

Enqueued (best-effort) by ``receive_material`` AFTER the receipt commits, with the
tenant passed explicitly as ``company_id``. The job is the SOLE decider of whether
to print: it is a no-op unless the company's ``CompanyPrintProfile`` exists, is
active, has ``auto_print_on_receipt`` ON, and has ``allow_print_egress`` ON.

COMPLIANCE / SECURITY:
* Tenant isolation -- every DB access is scoped by the explicit ``company_id`` arg.
* Egress kill switch -- the gate below mirrors ``PrintService._require_egress``;
  the actual ProxyBox call is additionally gated inside the service, so no outbound
  traffic can occur when egress is OFF.
* Best-effort -- any printer/tunnel/render failure is caught, logged (never leaking
  the API key), and recorded as a failed-print operational event; the job NEVER
  raises out of the worker, and the session is always closed.
"""

import logging

from app.db.session import SessionLocal
from app.models.print_profile import CompanyPrintProfile
from app.services.operational_event_service import OperationalEventService
from app.services.print_service import PrintEgressDisabledError, PrintService
from app.services.proxybox_client import ProxyBoxError

logger = logging.getLogger(__name__)


async def print_receiving_label_task(*, company_id: int, receipt_id: int, user_id: int) -> dict:
    """Print the receiving label for a receipt IF the company opted in.

    Returns a small status dict for observability. Never raises: a printer outage,
    a disabled toggle, or a render error must not affect the worker or the receipt.
    """
    db = SessionLocal()
    try:
        profile = db.query(CompanyPrintProfile).filter(CompanyPrintProfile.company_id == company_id).first()
        # No-op gates: the auto-print toggle AND the egress kill switch must both be
        # ON, the profile active, and a usable target configured.
        if profile is None or not profile.is_active:
            return {"receipt_id": receipt_id, "company_id": company_id, "printed": False, "reason": "no_profile"}
        if not profile.auto_print_on_receipt:
            return {"receipt_id": receipt_id, "company_id": company_id, "printed": False, "reason": "auto_print_off"}
        if profile.allow_print_egress is not True:
            return {"receipt_id": receipt_id, "company_id": company_id, "printed": False, "reason": "egress_off"}

        service = PrintService(db)
        try:
            document, printed = await service.print_receipt_label(company_id, receipt_id, user_id)
            return {
                "receipt_id": receipt_id,
                "company_id": company_id,
                "printed": printed,
                "label_document_id": document.id,
            }
        except PrintEgressDisabledError:
            # Profile became incomplete/disabled between the gate and the call -- no-op.
            return {"receipt_id": receipt_id, "company_id": company_id, "printed": False, "reason": "egress_off"}
        except LookupError:
            return {"receipt_id": receipt_id, "company_id": company_id, "printed": False, "reason": "receipt_not_found"}
        except ProxyBoxError as exc:
            # The label Document is committed by the service before the print call, so
            # a reprint is possible; surface the failure as an operational event only.
            logger.warning(
                "auto-print: ProxyBox print failed for receipt %s (company %s): %s",
                receipt_id,
                company_id,
                str(exc),
            )
            _emit_failed_event(db, company_id, receipt_id, user_id, reason=str(exc))
            return {"receipt_id": receipt_id, "company_id": company_id, "printed": False, "reason": "print_failed"}
    except Exception as exc:  # noqa: BLE001 - the job must never raise out of the worker
        logger.exception(
            "auto-print: unexpected failure for receipt %s (company %s): %s",
            receipt_id,
            company_id,
            type(exc).__name__,
        )
        try:
            _emit_failed_event(db, company_id, receipt_id, user_id, reason=type(exc).__name__)
        except Exception:  # noqa: BLE001 - the failure signal itself must not raise
            pass
        return {"receipt_id": receipt_id, "company_id": company_id, "printed": False, "reason": "error"}
    finally:
        db.close()


def _emit_failed_event(db, company_id: int, receipt_id: int, user_id: int, *, reason: str) -> None:
    """Record a failed auto-print as an operational event (best-effort, no secrets)."""
    try:
        # A prior failed/partial transaction could leave the session unusable; reset it.
        db.rollback()
    except Exception:  # noqa: BLE001
        pass
    OperationalEventService(db).emit(
        company_id=company_id,
        event_type="receiving_label_print_failed",
        source_module="receiving",
        entity_type="po_receipt",
        entity_id=receipt_id,
        user_id=user_id,
        severity="warning",
        event_payload={"receipt_id": receipt_id, "reason": reason},
    )
    db.commit()
