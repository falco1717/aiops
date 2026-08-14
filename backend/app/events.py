from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

MAX_QUEUE = 1000


class EventHub:
    """In-process fan-out of live run events to websocket subscribers.

    A topic is a session id, and nothing else. There used to be a "*" topic that
    every message was copied to as well, which an administrator's socket could
    subscribe to for a feed of every session at once; that went with the rest of
    the admin bypass on session visibility. It is gone from here rather than only
    refused at the router so that there is no reachable way to ask for it, and so
    the next thing to add a subscriber cannot re-open it by accident.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def subscribe(self, topic: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
        self._subscribers[topic].add(queue)
        return queue

    def unsubscribe(self, topic: str, queue: asyncio.Queue) -> None:
        subs = self._subscribers.get(topic)
        if not subs:
            return
        subs.discard(queue)
        if not subs:
            self._subscribers.pop(topic, None)

    def publish(self, topic: str, message: dict[str, Any]) -> None:
        for queue in list(self._subscribers.get(topic, ())):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # A slow consumer must not stall the agent process. Drop the
                # oldest message and keep the stream moving; the client can
                # refetch persisted events over HTTP to fill any gap.
                try:
                    queue.get_nowait()
                    queue.put_nowait(message)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass


hub = EventHub()
