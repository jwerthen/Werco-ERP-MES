"""Authenticated real-time updates, bound to a live user and active tenant."""

import asyncio
import logging
from typing import Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from app.core.security import verify_token
from app.core.websocket import manager
from app.db.database import SessionLocal
from app.db.tenant_filter import tenant_query
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder
from app.services.access_identity import resolve_access_identity

logger = logging.getLogger(__name__)
router = APIRouter()
WS_POLICY_VIOLATION = 1008
WS_AUTH_RECHECK_SECONDS = 30


def _identity_from_token(
    token: str, *, work_center_id: Optional[int] = None, work_order_id: Optional[int] = None
) -> Optional[Tuple[str, int]]:
    """Check an interactive access token and any requested tenant-owned resource.

    HTTP shares the live account/company resolver. Socket channels accept only
    unscoped access JWTs; kiosk, API, display, station and refresh credentials do
    not grant this separate real-time channel. Each check owns a short session.
    """
    payload = verify_token(token)
    if payload is None or payload.get("scope") is not None:
        return None
    with SessionLocal() as db:
        try:
            identity = resolve_access_identity(db, payload)
        except HTTPException:
            return None
        company_id = identity.company_id
        if work_center_id is not None:
            center = tenant_query(db, WorkCenter, company_id).filter(WorkCenter.id == work_center_id).first()
            if center is None:
                return None
        if work_order_id is not None:
            order = (
                tenant_query(db, WorkOrder, company_id)
                .filter(WorkOrder.id == work_order_id, WorkOrder.is_deleted == False)
                .first()
            )
            if order is None:
                return None
        return str(identity.user.id), company_id


async def _serve_socket(websocket: WebSocket, token: Optional[str], **resource: int) -> None:
    async def resolve():
        if not token:
            return None
        try:
            return await run_in_threadpool(_identity_from_token, token, **resource)
        except Exception:
            # A database outage must not leave an unverified socket authorized.
            # Never put query-string credentials or received payloads in logs.
            logger.warning("WebSocket identity check unavailable")
            return None

    identity = await resolve()
    if identity is None:
        await websocket.close(code=WS_POLICY_VIOLATION)
        return
    user_id, company_id = identity

    async def still_authorized() -> bool:
        return await resolve() == identity

    await manager.connect(websocket, user_id, company_id=company_id, authorize=still_authorized)
    try:
        await websocket.send_json(
            {
                "type": "connected",
                "data": {"message": "Connected to Werco ERP real-time updates", "user_id": user_id, **resource},
            }
        )
        while True:
            try:
                await asyncio.wait_for(websocket.receive_json(), timeout=WS_AUTH_RECHECK_SECONDS)
            except asyncio.TimeoutError:
                pass
            if not await still_authorized():
                await websocket.close(code=WS_POLICY_VIOLATION)
                return
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.info("WebSocket connection ended")
    finally:
        manager.disconnect(websocket, user_id)


@router.websocket("/ws/updates")
async def websocket_updates(websocket: WebSocket, token: Optional[str] = Query(None)):
    """Tenant updates for an active interactive user; revalidated before delivery.

    Expired tokens and disabled/moved users or inactive companies close with
    1008. Idle sockets also revalidate every 30 seconds. Access JWTs retain the
    HTTP lifetime contract: logout does not revoke an unexpired access JWT.
    """
    await _serve_socket(websocket, token)


@router.websocket("/ws/shop-floor/{work_center_id}")
async def websocket_shop_floor(websocket: WebSocket, work_center_id: int, token: Optional[str] = Query(None)):
    """Live-user updates after resolving the work center inside the active tenant."""
    await _serve_socket(websocket, token, work_center_id=work_center_id)


@router.websocket("/ws/work-order/{work_order_id}")
async def websocket_work_order(websocket: WebSocket, work_order_id: int, token: Optional[str] = Query(None)):
    """Live-user updates after resolving an undeleted work order in the active tenant."""
    await _serve_socket(websocket, token, work_order_id=work_order_id)
