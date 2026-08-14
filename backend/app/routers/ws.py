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
    """Live event feed for one session, named by `session_id`.

    This socket streams the agent's output as it is written, so it obeys exactly
    the rule the transcript does — otherwise a session would be private only
    until somebody watched it live.

    `session_id` used to be optional, and omitting it subscribed an administrator
    to every session on the instance at once. That was the same admin bypass the
    transcript just lost, arriving by a different door and carrying more: the
    live feed includes tool calls and their output as they happen. There is no
    filtered version of it here because nothing wants one — the Sessions page
    refetches its list over HTTP on a timer, and the only socket the UI opens is
    for the conversation being read. Filtering delivery instead would mean
    re-deciding visibility per message on the runner's hot path, to arrive at a
    feed no client asks for.
    """
    token = websocket.cookies.get(settings.cookie_name)
    async with SessionLocal() as db:
        user = await user_from_token(token, db)
        if user is None:
            await websocket.close(code=4401, reason="Not authenticated")
            return
        if session_id is None:
            await websocket.close(code=4400, reason="session_id is required")
            return
        sess = await db.get(Session, session_id)
        if sess is None or not await can_see_session(db, sess, user):
            await websocket.close(code=4404, reason="Session not found")
            return

    await websocket.accept()
    topic = session_id
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
