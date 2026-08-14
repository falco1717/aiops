from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..access import can_see_session
from ..config import settings
from ..db import SessionLocal
from ..events import hub
from ..models import Session
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
        # This socket streams the agent's output as it is written, so it has to
        # obey the same rule the transcript does — otherwise a session is private
        # only until somebody watches it live. The unscoped feed carries every
        # session at once, which nobody but an administrator can be entitled to.
        if session_id is None:
            allowed = user.is_admin
        else:
            sess = await db.get(Session, session_id)
            allowed = sess is not None and await can_see_session(db, sess, user)
        if not allowed:
            await websocket.close(code=4404, reason="Session not found")
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
