"""WebSocket API endpoints for real-time updates."""

import logging
from typing import Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.core.websocket import manager

logger = logging.getLogger(__name__)

router = APIRouter()


def _user_id_from_token(token: str) -> Optional[str]:
    from app.core.security import verify_token

    payload = verify_token(token)
    if not payload or not payload.get("user_id"):
        return None
    return str(payload["user_id"])


@router.websocket("/ws/updates")
async def websocket_updates(websocket: WebSocket, token: Optional[str] = Query(None)):
    """
    WebSocket endpoint for real-time dashboard and system updates.

    Connects without authentication for general updates.
    For user-specific notifications, include a valid JWT token.
    """
    user_id = None

    # Verify token if provided
    if token:
        try:
            user_id = _user_id_from_token(token)
        except Exception as e:
            logger.warning(f"Invalid token in WebSocket connection: {e}")

    await manager.connect(websocket, user_id)

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
    # Verify authentication
    user_id = _user_id_from_token(token)
    if not user_id:
        await websocket.close(code=1008)
        return

    await manager.connect(websocket, user_id)

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
    user_id = _user_id_from_token(token)
    if not user_id:
        await websocket.close(code=1008)
        return

    await manager.connect(websocket, user_id)

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
