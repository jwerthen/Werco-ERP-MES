"""
WebSocket connection manager for real-time updates.
Handles broadcasting messages to connected clients.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)

# Upper bound on one ``send_text`` to one socket. Realtime delivery is serialized per
# process (``app.core.realtime``), so a socket whose client has stopped reading would
# otherwise hold up every other socket's events behind it. A send that times out is
# treated like any failed send: that socket is dropped from delivery.
WS_SEND_TIMEOUT_SECONDS = 5.0


async def _send_bounded(websocket: WebSocket, message_json: str) -> None:
    await asyncio.wait_for(websocket.send_text(message_json), timeout=WS_SEND_TIMEOUT_SECONDS)


class ConnectionManager:
    """Manages WebSocket connections and broadcasts messages.

    Tenant isolation (invariant #1): each connection's active ``company_id`` is
    captured at connect time so broadcasts can be scoped to a single tenant.
    ``broadcast_to_company`` only sends to sockets belonging to that company, and
    ``send_to_user`` only to that user's sockets admitted under the named company.

    There is deliberately NO all-tenants fan-out. Every event this app broadcasts
    is tenant data (work-order, operation, blocker and work-center ids, statuses),
    and the old ``broadcast()`` -- reached whenever a caller left ``company_id``
    out -- delivered those to every company's sockets. No caller needs a global
    path; add one only for a genuinely tenant-free message, under a name that says
    so, never as a fallback for a missing ``company_id``.
    """

    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.user_connections: Dict[str, List[WebSocket]] = {}
        self.user_connected_at: Dict[str, datetime] = {}
        # Per-connection tenant identity, captured from the verified token at connect time.
        self.company_connections: Dict[int, List[WebSocket]] = {}
        self.connection_company: Dict[WebSocket, int] = {}
        self.connection_authorizers: Dict[WebSocket, Callable[[], Awaitable[bool]]] = {}

    async def connect(
        self,
        websocket: WebSocket,
        user_id: str = None,
        company_id: Optional[int] = None,
        *,
        authorize: Optional[Callable[[], Awaitable[bool]]] = None,
    ):
        """Accept and store a WebSocket connection.

        ``company_id`` is the *active* company for the connecting client (already
        resolved through the same path as ``get_current_company_id``). When
        provided, the connection is registered for company-scoped broadcasts.
        """
        await websocket.accept()
        self.active_connections.append(websocket)
        if authorize is not None:
            self.connection_authorizers[websocket] = authorize

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
        self.connection_authorizers.pop(websocket, None)
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

    async def _may_deliver(self, websocket: WebSocket) -> bool:
        """Revalidate before every event so revocation cannot leak a final payload."""
        authorize = self.connection_authorizers.get(websocket)
        if authorize is None:
            return websocket in self.active_connections
        try:
            if await authorize():
                return websocket in self.active_connections
        except Exception:
            logger.warning("WebSocket delivery authorization unavailable")
        try:
            await websocket.close(code=1008)
        except Exception:
            pass
        self.disconnect(websocket)
        return False

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
                if not await self._may_deliver(connection):
                    continue
                await _send_bounded(connection, message_json)
            except Exception as e:
                logger.error("Error sending to company WebSocket: %s: %s", type(e).__name__, e)
                disconnected.append(connection)

        for conn in disconnected:
            self.disconnect(conn)

    async def send_to_user(
        self,
        user_id: str,
        message: Dict[str, Any],
        message_type: str = "notification",
        *,
        company_id: Optional[int],
    ):
        """Send a message to ``user_id``'s connections admitted under ``company_id`` only.

        A user is not a tenant boundary: a platform principal can hold one socket in
        each company it has switched into, so "every socket this user holds" spans
        tenants. ``company_id`` is therefore REQUIRED (keyword-only, no default) and an
        explicit ``None`` reaches nobody -- the same rule as the ``broadcast_*`` helpers.
        """
        if company_id is None:
            logger.warning(
                "Dropped %s message to a user with no company_id: tenant events are never sent across tenants",
                message_type,
            )
            return
        if user_id not in self.user_connections:
            return

        data = {"type": message_type, "data": message, "timestamp": None}

        message_json = json.dumps(data)
        disconnected = []

        for connection in list(self.user_connections[user_id]):
            if self.connection_company.get(connection) != company_id:
                continue
            try:
                if not await self._may_deliver(connection):
                    continue
                await _send_bounded(connection, message_json)
            except Exception as e:
                logger.error("Error sending to user WebSocket: %s: %s", type(e).__name__, e)
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


async def _broadcast_tenant_event(company_id: Optional[int], message: Dict[str, Any], message_type: str) -> None:
    """Deliver one tenant event to ``company_id``'s sockets, or to NOBODY.

    ``company_id is None`` used to mean "broadcast to every tenant". It now means the
    caller failed to say whose data this is, so the message is dropped and a warning
    is logged (the message type and event name only -- never the payload, which
    carries the tenant's ids). Dropping is the safe direction: a missed realtime
    nudge leaves one tenant's screen stale until its next fetch, while a delivered
    one hands that tenant's work-order, operation and blocker ids to every other
    company's sockets and triggers refetches on their screens.
    """
    if company_id is None:
        logger.warning(
            "Dropped %s broadcast (event=%s) with no company_id: tenant events are never sent to every tenant",
            message_type,
            message.get("event"),
        )
        return
    await manager.broadcast_to_company(company_id, message, message_type=message_type)


async def broadcast_dashboard_update(update_data: Dict[str, Any], *, company_id: Optional[int]) -> None:
    """Broadcast a dashboard update to ``company_id``'s connections only.

    ``company_id`` is REQUIRED (keyword-only, no default): pass the active company
    from ``get_current_company_id``. An explicit ``None`` is refused -- logged and
    dropped, never fanned out to every tenant (invariant #1).
    """
    await _broadcast_tenant_event(company_id, update_data, "dashboard_update")


async def broadcast_work_order_update(
    work_order_id: int, update_data: Dict[str, Any], *, company_id: Optional[int]
) -> None:
    """Broadcast a work-order update to ``company_id``'s connections only (``None`` is refused)."""
    message = {"work_order_id": work_order_id, **update_data}
    await _broadcast_tenant_event(company_id, message, "work_order_update")


async def broadcast_shop_floor_update(
    work_center_id: int, update_data: Dict[str, Any], *, company_id: Optional[int]
) -> None:
    """Broadcast a shop-floor update for a work center to ``company_id``'s connections only (``None`` is refused)."""
    message = {"work_center_id": work_center_id, **update_data}
    await _broadcast_tenant_event(company_id, message, "shop_floor_update")
