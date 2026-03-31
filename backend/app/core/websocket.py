"""
WebSocket connection manager for real-time updates.
Handles broadcasting messages to connected clients.
"""
from typing import List, Dict, Any
from datetime import datetime, timezone
from fastapi import WebSocket, WebSocketDisconnect
import logging
import json

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages WebSocket connections and broadcasts messages."""

    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.user_connections: Dict[str, List[WebSocket]] = {}
        self.user_connected_at: Dict[str, datetime] = {}

    async def connect(self, websocket: WebSocket, user_id: str = None):
        """Accept and store a WebSocket connection."""
        await websocket.accept()
        self.active_connections.append(websocket)

        if user_id:
            user_id = str(user_id)
            if user_id not in self.user_connections:
                self.user_connections[user_id] = []
                self.user_connected_at[user_id] = datetime.now(timezone.utc)
            self.user_connections[user_id].append(websocket)

        logger.info(f"WebSocket connected. Total connections: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket, user_id: str = None):
        """Remove a WebSocket connection."""
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

        candidate_user_ids = [str(user_id)] if user_id else [
            uid for uid, connections in self.user_connections.items() if websocket in connections
        ]

        for candidate_user_id in candidate_user_ids:
            if candidate_user_id not in self.user_connections:
                continue
            if websocket in self.user_connections[candidate_user_id]:
                self.user_connections[candidate_user_id].remove(websocket)
            if not self.user_connections[candidate_user_id]:
                del self.user_connections[candidate_user_id]
                self.user_connected_at.pop(candidate_user_id, None)

        logger.info(f"WebSocket disconnected. Total connections: {len(self.active_connections)}")

    async def broadcast(self, message: Dict[str, Any], message_type: str = "update"):
        """Send a message to all connected clients."""
        if not self.active_connections:
            return

        data = {
            "type": message_type,
            "data": message,
            "timestamp": None  # Will be set by frontend
        }

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

    async def send_to_user(self, user_id: str, message: Dict[str, Any], message_type: str = "notification"):
        """Send a message to a specific user's connections."""
        if user_id not in self.user_connections:
            return

        data = {
            "type": message_type,
            "data": message,
            "timestamp": None
        }

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


async def broadcast_dashboard_update(update_data: Dict[str, Any]):
    """Broadcast dashboard updates to all clients."""
    await manager.broadcast(update_data, message_type="dashboard_update")


async def broadcast_work_order_update(work_order_id: int, update_data: Dict[str, Any]):
    """Broadcast work order status updates."""
    message = {
        "work_order_id": work_order_id,
        **update_data
    }
    await manager.broadcast(message, message_type="work_order_update")


async def broadcast_shop_floor_update(work_center_id: int, update_data: Dict[str, Any]):
    """Broadcast shop floor updates for a work center."""
    message = {
        "work_center_id": work_center_id,
        **update_data
    }
    await manager.broadcast(message, message_type="shop_floor_update")


