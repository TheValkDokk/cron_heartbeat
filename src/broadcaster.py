"""
SSE Broadcaster
===============
In-memory publish/subscribe system for Server-Sent Events.

Each user gets their own asyncio.Queue.
- Cron jobs call `publish(owner_id, event)` to push results.
- The SSE endpoint reads from `subscribe(owner_id)` and streams to the browser.
"""

import asyncio
import json
from typing import Any

# user_id -> list of active subscriber queues
_subscribers: dict[int, list[asyncio.Queue]] = {}


def subscribe(owner_id: int) -> asyncio.Queue:
    """Register a new SSE listener for a user and return their queue."""
    q: asyncio.Queue = asyncio.Queue()
    _subscribers.setdefault(owner_id, []).append(q)
    return q


def unsubscribe(owner_id: int, q: asyncio.Queue) -> None:
    """Remove a disconnected listener."""
    listeners = _subscribers.get(owner_id, [])
    if q in listeners:
        listeners.remove(q)


async def publish(owner_id: int, event: dict[str, Any]) -> None:
    """Push an event (dict) to all active SSE listeners for this user."""
    listeners = _subscribers.get(owner_id, [])
    data = json.dumps(event)
    for q in list(listeners):
        await q.put(data)
