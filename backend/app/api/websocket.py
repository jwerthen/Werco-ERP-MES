"""WebSocket API endpoints for real-time updates."""

import logging
from typing import Optional, Tuple

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.core.websocket import manager

logger = logging.getLogger(__name__)

router = APIRouter()

# WebSocket policy-violation close code (RFC 6455). Used when a connection
# cannot be authenticated, matching the existing authenticated /ws routes.
WS_POLICY_VIOLATION = 1008


def _identity_from_token(token: str) -> Optional[Tuple[str, int]]:
    """Resolve ``(user_id, company_id)`` for a WebSocket connection.

    Derives the active company the same way ``get_current_company_id`` does:
    the token's ``cid`` claim (which already reflects platform-admin company
    switching), falling back to the user's own ``company_id`` for legacy tokens
    that predate the ``cid`` claim. Returns ``None`` when the token is invalid,
    carries no user, or no company can be resolved.
    """
    from app.core.security import verify_token

    payload = verify_token(token)
    if not payload or not payload.get("user_id"):
        return None

    user_id = str(payload["user_id"])
    company_id = payload.get("company_id")

    if company_id is None:
        # Legacy token without a cid claim: fall back to the user's own company,
        # mirroring get_current_company_id's fallback to user.company_id.
        from app.db.database import SessionLocal
        from app.models.user import User

        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == int(user_id)).first()
            if user is None or user.company_id is None:
                return None
            company_id = user.company_id
        finally:
            db.close()

    return user_id, int(company_id)


@router.websocket("/ws/updates")
async def websocket_updates(websocket: WebSocket, token: Optional[str] = Query(None)):
    """
    WebSocket endpoint for real-time dashboard and system updates.

    Requires authentication: the connection is bound to the caller's active
    company so tenant-scoped broadcasts (work-order / dashboard / shop-floor
    completion events) reach only that company's clients. Unauthenticated
    connections are rejected with a policy-violation close (tenant isolation,
    invariant #1).
    """
    identity = None
    if token:
        try:
            identity = _identity_from_token(token)
        except Exception as e:
            logger.warning(f"Invalid token in WebSocket connection: {e}")

    if identity is None:
        await websocket.close(code=WS_POLICY_VIOLATION)
        return

    user_id, company_id = identity
    await manager.connect(websocket, user_id, company_id=company_id)

    try:
        # Welcome message
        await websocket.send_json(
            {"type": "connected", "data": {"message": "Connected to Werco ERP real-time updates", "user_id": user_id}}
        )

        # Keep connection alive
        while True:
            data = await websocket.receive_json()
            logger.debug(f"Received WebSocket message: {data}")

    except WebSocketDisconnect:
        manager.disconnect(websocket, user_id)
        logger.info("WebSocket client disconnected")

    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        manager.disconnect(websocket, user_id)


@router.websocket("/ws/shop-floor/{work_center_id}")
async def websocket_shop_floor(websocket: WebSocket, work_center_id: int, token: str = Query(...)):
    """
    WebSocket endpoint for real-time shop floor updates for a specific work center.
    Requires authentication.
    """
    # Verify authentication and resolve the active company for tenant scoping.
    identity = _identity_from_token(token)
    if identity is None:
        await websocket.close(code=WS_POLICY_VIOLATION)
        return

    user_id, company_id = identity
    await manager.connect(websocket, user_id, company_id=company_id)

    try:
        # Send initial data
        await websocket.send_json(
            {
                "type": "connected",
                "data": {
                    "work_center_id": work_center_id,
                    "user_id": user_id,
                    "message": f"Connected to work center {work_center_id} updates",
                },
            }
        )

        while True:
            # Receive heartbeat or commands
            data = await websocket.receive_json()
            logger.debug(f"Shop floor WebSocket message: {data}")

    except WebSocketDisconnect:
        manager.disconnect(websocket, user_id)
        logger.info(f"Shop floor WebSocket disconnected for work center {work_center_id}")

    except Exception as e:
        logger.error(f"Shop floor WebSocket error: {e}")
        manager.disconnect(websocket, user_id)


@router.websocket("/ws/work-order/{work_order_id}")
async def websocket_work_order(websocket: WebSocket, work_order_id: int, token: str = Query(...)):
    """
    WebSocket endpoint for real-time work order status updates.
    Requires authentication.
    """
    identity = _identity_from_token(token)
    if identity is None:
        await websocket.close(code=WS_POLICY_VIOLATION)
        return

    user_id, company_id = identity
    await manager.connect(websocket, user_id, company_id=company_id)

    try:
        await websocket.send_json(
            {
                "type": "connected",
                "data": {
                    "work_order_id": work_order_id,
                    "user_id": user_id,
                    "message": f"Connected to work order {work_order_id} updates",
                },
            }
        )

        while True:
            data = await websocket.receive_json()
            logger.debug(f"Work order WebSocket message: {data}")

    except WebSocketDisconnect:
        manager.disconnect(websocket, user_id)
        logger.info(f"Work order WebSocket disconnected for work order {work_order_id}")

    except Exception as e:
        logger.error(f"Work order WebSocket error: {e}")
        manager.disconnect(websocket, user_id)
