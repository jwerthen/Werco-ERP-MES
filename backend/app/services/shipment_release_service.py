"""Accepting open quality items at ship and at certificate issue (shipping/2).

``quality_gate_service.shipment_release_exceptions`` finds the open items (NCRs that
hold the shipment, the latest first article not accepted, open quality-hold
blockers). This module owns the ONE rule for what a request must carry to go ahead
over them, shared by ``POST /shipping/{id}/ship`` and ``POST /shipping/{id}/coc``:

* nothing open -> nothing is required;
* something open and no acknowledgment -> 409 listing the items (a structured detail
  the Ship and Issue dialogs render);
* an acknowledgment from a standing API token -> 403 (accepting a quality risk is a
  person's judgment);
* an acknowledgment from a role other than Admin / Manager / Quality -> 403 (a
  superuser or platform admin passes, as everywhere ``require_role`` is used);
* an acknowledgment with no reason -> 422.

Owner decision D1 was NOT answered; this is the recommended default: an open item
does not hard-stop a shipment, it needs a named person's acknowledged reason.
"""

from __future__ import annotations

from typing import Any, List, Optional

from fastapi import HTTPException

from app.core.credentials import is_api_token_credential
from app.models.user import UserRole
from app.schemas.shipping import ReleaseExceptionAcknowledgment

RELEASE_ACK_ROLES = (UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY)
RELEASE_EXCEPTIONS_OPEN_CODE = "release_exceptions_open"
RELEASE_ACK_REASON_MIN_LENGTH = 3

SHIP_OVER_EXCEPTIONS_MESSAGE = (
    "This job has open quality items. Admin, Manager or Quality must accept them with a reason to ship."
)
COC_OVER_EXCEPTIONS_MESSAGE = (
    "This job has open quality items. Accept them with a reason to issue the Certificate of Conformance."
)
API_TOKEN_RELEASE_ACK = (
    "A standing API token cannot accept open quality items. A person signed in to the app must accept them."
)
API_TOKEN_COC_ISSUE = (
    "A standing API token cannot issue a Certificate of Conformance. A person signed in to the app must issue it."
)
RELEASE_ACK_ROLE_DETAIL = "Only Admin, Manager or Quality can accept open quality items on a job."
RELEASE_ACK_REASON_DETAIL = "A reason is required to accept open quality items."
QUALITY_SHIP_ONLY_OVER_EXCEPTIONS = (
    "Quality can mark a shipment shipped only to accept open quality items on it. "
    "Shipping, Supervisor, Manager or Admin ships a job with nothing open."
)


def can_acknowledge_release_exceptions(user: Any) -> bool:
    """Admin / Manager / Quality (and the platform principals ``require_role`` waves through)."""
    if getattr(user, "is_superuser", False) or getattr(user, "role", None) == UserRole.PLATFORM_ADMIN:
        return True
    return getattr(user, "role", None) in RELEASE_ACK_ROLES


def release_exceptions_conflict(message: str, exceptions: List[dict]) -> HTTPException:
    """The 409 the Ship / Issue dialogs read: a fixed message plus the open items."""
    return HTTPException(
        status_code=409,
        detail={"code": RELEASE_EXCEPTIONS_OPEN_CODE, "message": message, "release_exceptions": exceptions},
    )


def require_release_acknowledgment(
    user: Any,
    acknowledgment: Optional[ReleaseExceptionAcknowledgment],
    exceptions: List[dict],
    *,
    message: str,
) -> Optional[str]:
    """Apply the acknowledgment rule; return the trimmed reason, or None when nothing is open.

    Raises the 409 / 403 / 422 described in the module docstring. Writes nothing.
    """
    if not exceptions:
        return None
    if acknowledgment is None or not acknowledgment.acknowledge_exceptions:
        raise release_exceptions_conflict(message, exceptions)
    if is_api_token_credential(user):
        raise HTTPException(status_code=403, detail=API_TOKEN_RELEASE_ACK)
    if not can_acknowledge_release_exceptions(user):
        raise HTTPException(status_code=403, detail=RELEASE_ACK_ROLE_DETAIL)
    reason = (acknowledgment.reason or "").strip()
    if len(reason) < RELEASE_ACK_REASON_MIN_LENGTH:
        raise HTTPException(status_code=422, detail=RELEASE_ACK_REASON_DETAIL)
    return reason


SHIPPED_OVER_EXCEPTIONS_AUDIT_ACTION = "SHIPPED_OVER_QUALITY_EXCEPTIONS"


def is_quality_only_shipper(user: Any) -> bool:
    """Quality is on the ship route only to accept open items, never to ship a clean job."""
    if getattr(user, "is_superuser", False) or getattr(user, "role", None) == UserRole.PLATFORM_ADMIN:
        return False
    return getattr(user, "role", None) == UserRole.QUALITY


def record_ship_over_exceptions(
    audit: Any,
    *,
    shipment: Any,
    work_order: Any,
    exceptions: List[dict],
    reason: str,
    company_id: int,
) -> None:
    """REQUIRED audit evidence that a person shipped over open quality items, with their reason.

    The caller wraps it in ``app.api.audit_guard.required_audit`` so a failure rolls the
    whole ship back (503). Never commits.
    """
    audit.log_required(
        SHIPPED_OVER_EXCEPTIONS_AUDIT_ACTION,
        "shipment",
        resource_id=shipment.id,
        resource_identifier=shipment.shipment_number,
        description=(
            f"Shipment {shipment.shipment_number} (WO {work_order.work_order_number}) shipped over "
            f"{len(exceptions)} open quality item(s): {reason}"
        ),
        new_values={"accepted_release_exceptions": [exc.get("code") for exc in exceptions]},
        extra_data={
            "work_order_id": work_order.id,
            "work_order_number": work_order.work_order_number,
            "release_exceptions": exceptions,
            "reason": reason,
        },
        company_id=company_id,
    )
