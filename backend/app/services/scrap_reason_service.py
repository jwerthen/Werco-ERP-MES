"""Shared validation for structured scrap reason codes (Lean Phase 1).

The three scrap write paths (shop-floor clock-out, shop-floor production
report, office ``/work-orders/{id}/complete``) all accept an optional
``scrap_reason_code_id``. This module owns the single resolve+validate rule so
the paths cannot drift: the code must exist, belong to the ACTIVE company
(tenant isolation -- a cross-tenant id is indistinguishable from a missing one,
so both are "not found"), and be active.

Raises domain errors (not HTTPException) so the service stays transport-free;
``resolve_scrap_reason_code_or_http`` is the endpoint-facing wrapper that maps
them to the agreed statuses (404 unknown/cross-tenant, 422 inactive).
"""

from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.db.tenant_filter import tenant_query
from app.models.scrap_reason import ScrapReasonCode


class ScrapReasonCodeNotFoundError(ValueError):
    """Unknown id, or a code belonging to another tenant (never disclosed)."""


class ScrapReasonCodeInactiveError(ValueError):
    """The code exists for this tenant but has been deactivated."""


def resolve_scrap_reason_code(db: Session, company_id: int, scrap_reason_code_id: int) -> ScrapReasonCode:
    """Resolve a scrap reason code id for the active company or raise.

    Tenant-scoped via ``tenant_query`` -- another company's code id raises
    ``ScrapReasonCodeNotFoundError`` exactly like a nonexistent one.
    """
    code = tenant_query(db, ScrapReasonCode, company_id).filter(ScrapReasonCode.id == scrap_reason_code_id).first()
    if code is None:
        raise ScrapReasonCodeNotFoundError(f"Scrap reason code {scrap_reason_code_id} not found")
    if not code.is_active:
        raise ScrapReasonCodeInactiveError(f"Scrap reason code '{code.code}' is inactive")
    return code


def resolve_scrap_reason_code_or_http(
    db: Session, company_id: int, scrap_reason_code_id: Optional[int]
) -> Optional[ScrapReasonCode]:
    """Endpoint-facing wrapper: ``None`` passes through; errors become 404/422."""
    if scrap_reason_code_id is None:
        return None
    try:
        return resolve_scrap_reason_code(db, company_id, scrap_reason_code_id)
    except ScrapReasonCodeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ScrapReasonCodeInactiveError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
