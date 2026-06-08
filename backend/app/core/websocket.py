"""
WebSocket connection manager for real-time updates.
Handles broadcasting messages to connected clients.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages WebSocket connections and broadcasts messages.

    Tenant isolation (invariant #1): each connection's active ``company_id`` is
    captured at connect time so broadcasts can be scoped to a single tenant.
    ``broadcast_to_company`` only sends to sockets belonging to that company.
    """

    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.user_connections: Dict[str, List[WebSocket]] = {}
        self.user_connected_at: Dict[str, datetime] = {}
        # Per-connection tenant identity, captured from the verified token at connect time.
        self.company_connections: Dict[int, List[WebSocket]] = {}
        self.connection_company: Dict[WebSocket, int] = {}

    async def connect(self, websocket: WebSocket, user_id: str = None, company_id: Optional[int] = None):
        """Accept and store a WebSocket connection.

        ``company_id`` is the *active* company for the connecting client (already
        resolved through the same path as ``get_current_company_id``). When
        provided, the connection is registered for company-scoped broadcasts.
        """
        await websocket.accept()
        self.active_connections.append(websocket)

        if user_id:
            user_id = str(user_id)
            if user_id not in self.user_connections:
                self.user_connections[user_id] = []
                self.user_connected_at[user_id] = datetime.now(timezone.utc)
            self.user_connections[user_id].append(websocket)

        if company_id is not None:
            self.company_connections.setdefault(company_id, []).append(websocket)
            self.connection_company[websocket] = company_id

        logger.info(f"WebSocket connected. Total connections: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket, user_id: str = None):
        """Remove a WebSocket connection."""
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

        candidate_user_ids = (
            [str(user_id)]
            if user_id
            else [uid for uid, connections in self.user_connections.items() if websocket in connections]
        )

        for candidate_user_id in candidate_user_ids:
            if candidate_user_id not in self.user_connections:
                continue
            if websocket in self.user_connections[candidate_user_id]:
                self.user_connections[candidate_user_id].remove(websocket)
            if not self.user_connections[candidate_user_id]:
                del self.user_connections[candidate_user_id]
                self.user_connected_at.pop(candidate_user_id, None)

        company_id = self.connection_company.pop(websocket, None)
        if company_id is not None:
            company_sockets = self.company_connections.get(company_id)
            if company_sockets and websocket in company_sockets:
                company_sockets.remove(websocket)
            if not company_sockets:
                self.company_connections.pop(company_id, None)

        logger.info(f"WebSocket disconnected. Total connections: {len(self.active_connections)}")

    async def broadcast(self, message: Dict[str, Any], message_type: str = "update"):
        """Send a message to all connected clients."""
        if not self.active_connections:
            return

        data = {"type": message_type, "data": message, "timestamp": None}  # Will be set by frontend

        message_json = json.dumps(data)
        disconnected = []

        for connection in self.active_connections:
            try:
                await connection.send_text(message_json)
            except Exception as e:
                logger.error(f"Error sending to WebSocket: {e}")
                disconnected.append(connection)

        # Remove disconnected clients
        for conn in disconnected:
            self.disconnect(conn)

    async def broadcast_to_company(self, company_id: int, message: Dict[str, Any], message_type: str = "update"):
        """Send a message only to connections belonging to ``company_id``.

        This is the tenant-scoped broadcast: clients of other companies never
        receive the payload. Connections that never identified a company (e.g.
        legacy/unauthenticated sockets, which are no longer accepted on the
        authenticated routes) are not included.
        """
        connections = self.company_connections.get(company_id)
        if not connections:
            return

        data = {"type": message_type, "data": message, "timestamp": None}
        message_json = json.dumps(data)
        disconnected = []

        # Iterate a copy: disconnect() mutates company_connections.
        for connection in list(connections):
            try:
                await connection.send_text(message_json)
            except Exception as e:
                logger.error(f"Error sending to company WebSocket: {e}")
                disconnected.append(connection)

        for conn in disconnected:
            self.disconnect(conn)

    async def send_to_user(self, user_id: str, message: Dict[str, Any], message_type: str = "notification"):
        """Send a message to a specific user's connections."""
        if user_id not in self.user_connections:
            return

        data = {"type": message_type, "data": message, "timestamp": None}

        message_json = json.dumps(data)
        disconnected = []

        for connection in self.user_connections[user_id]:
            try:
                await connection.send_text(message_json)
            except Exception as e:
                logger.error(f"Error sending to user WebSocket: {e}")
                disconnected.append(connection)

        # Remove disconnected clients
        for conn in disconnected:
            self.disconnect(conn, user_id)

    def get_connection_count(self) -> int:
        """Get total number of active connections."""
        return len(self.active_connections)

    def get_user_connection_count(self, user_id: str) -> int:
        """Get number of connections for a specific user."""
        return len(self.user_connections.get(str(user_id), []))

    def get_connected_user_ids(self) -> List[str]:
        """Get unique authenticated user IDs with an active WebSocket presence."""
        return list(self.user_connections.keys())

    def get_connected_since(self, user_id: str) -> str | None:
        """Return when the user first connected in the current presence session."""
        connected_at = self.user_connected_at.get(str(user_id))
        return connected_at.isoformat() if connected_at else None


# Global connection manager instance
manager = ConnectionManager()


async def broadcast_dashboard_update(update_data: Dict[str, Any], company_id: Optional[int] = None):
    """Broadcast dashboard updates.

    When ``company_id`` is provided the update is delivered only to that
    company's connections (tenant isolation, invariant #1). ``None`` preserves
    the legacy global broadcast for any non-tenant-scoped caller.
    """
    if company_id is not None:
        await manager.broadcast_to_company(company_id, update_data, message_type="dashboard_update")
        return
    await manager.broadcast(update_data, message_type="dashboard_update")


async def broadcast_work_order_update(
    work_order_id: int, update_data: Dict[str, Any], company_id: Optional[int] = None
):
    """Broadcast work order status updates (tenant-scoped when ``company_id`` is given)."""
    message = {"work_order_id": work_order_id, **update_data}
    if company_id is not None:
        await manager.broadcast_to_company(company_id, message, message_type="work_order_update")
        return
    await manager.broadcast(message, message_type="work_order_update")


async def broadcast_shop_floor_update(
    work_center_id: int, update_data: Dict[str, Any], company_id: Optional[int] = None
):
    """Broadcast shop floor updates for a work center (tenant-scoped when ``company_id`` is given)."""
    message = {"work_center_id": work_center_id, **update_data}
    if company_id is not None:
        await manager.broadcast_to_company(company_id, message, message_type="shop_floor_update")
        return
    await manager.broadcast(message, message_type="shop_floor_update")
