# app/ws/manager.py
from fastapi import WebSocket
from typing import Dict, List
import json
import logging

logger = logging.getLogger(__name__)

# Fixed set of channels this app actually serves. app/main.py's
# websocket_endpoint validates {channel} against this before ever
# calling connect() (audit b05) -- previously an arbitrary
# client-supplied channel string was accepted and silently created a
# new, permanent entry in active_connections that nothing ever cleaned
# up, even after every connection on it disconnected (see connect()'s
# "if channel not in self.active_connections" auto-vivification
# below, unchanged here since main.py's upstream check now makes that
# branch unreachable in practice -- kept only as a defensive no-op for
# any future direct caller).
KNOWN_CHANNELS = ("overview", "alerts", "metrics")


class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, List[WebSocket]] = {
            "overview": [],
            "alerts": [],
            "metrics": [],
        }
        # websocket -> set of aws_accounts.id the user may see, or None for
        # unrestricted. Captured at connect time (a grant change takes effect
        # on the next reconnect). Payloads carrying an `account_id` are only
        # delivered to sockets whose scope includes it -- before this every
        # logged-in user received every account's alert/metric stream.
        self._scopes: Dict[WebSocket, object] = {}

    async def connect(self, websocket: WebSocket, channel: str = "overview", accessible=None):
        await websocket.accept()
        self._scopes[websocket] = None if accessible is None else set(accessible)
        if channel not in self.active_connections:
            self.active_connections[channel] = []
        self.active_connections[channel].append(websocket)
        logger.info(f"WS connected: channel={channel}")

    def disconnect(self, websocket: WebSocket, channel: str = "overview"):
        self._scopes.pop(websocket, None)
        if channel in self.active_connections:
            try:
                self.active_connections[channel].remove(websocket)
            except ValueError:
                pass
        logger.info(f"WS disconnected: channel={channel}")

    async def broadcast(self, channel: str, data: dict):
        if channel not in self.active_connections:
            return
        dead = []
        message = json.dumps(data)
        account_id = data.get("account_id") if isinstance(data, dict) else None
        for ws in self.active_connections[channel]:
            if account_id is not None:
                scope = self._scopes.get(ws)
                if scope is not None and account_id not in scope:
                    continue
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            try:
                self.active_connections[channel].remove(ws)
            except ValueError:
                pass

    async def broadcast_all(self, data: dict):
        for channel in self.active_connections:
            await self.broadcast(channel, data)

    def connection_count(self) -> dict:
        return {ch: len(conns) for ch, conns in self.active_connections.items()}


ws_manager = ConnectionManager()