from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..config import settings
from ..db import SessionLocal
from ..events import hub
from ..security import user_from_token

log = logging.getLogger("aiops.ws")
router = APIRouter()

HEARTBEAT_SECONDS = 25


@router.websocket("/api/ws")
async def websocket_endpoint(websocket: WebSocket, session_id: str | None = None):
    """Live event feed.

    Without `session_id` the socket receives every event (dashboard view);
    with one it receives only that session's.
    """
    token = websocket.cookies.get(settings.cookie_name)
    async with SessionLocal() as db:
        user = await user_from_token(token, db)
    if user is None:
        await websocket.close(code=4401, reason="Not authenticated")
        return

    await websocket.accept()
    topic = session_id or "*"
    queue = hub.subscribe(topic)
    await websocket.send_json({"type": "connected", "topic": topic})

    reader = asyncio.create_task(_discard_incoming(websocket))
    try:
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping"})
                continue
            await websocket.send_json(message)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        log.debug("websocket closed unexpectedly", exc_info=True)
    finally:
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader
        hub.unsubscribe(topic, queue)


async def _discard_incoming(websocket: WebSocket) -> None:
    """Consume client frames so a disconnect surfaces promptly."""
    with contextlib.suppress(WebSocketDisconnect, RuntimeError):
        while True:
            await websocket.receive_text()
