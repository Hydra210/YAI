"""A minimum gap between the messages Yuki sends in one server (default 2 seconds).

pace(key) reserves the next free send slot and waits for it, so it also works when several things want to send at once
(a reply, a moderation reminder, a slang question): they simply line up two seconds apart."""

from __future__ import annotations

import asyncio
import os
import time

GAP = float(os.getenv("SERVER_SEND_GAP_SECONDS", "2"))
_next_slot: dict = {}


async def pace(key) -> None:
    """Wait until it's okay to send another message in this server. key = guild id (None / 0 = DMs, no gap)."""
    if not key or GAP <= 0:
        return
    now = time.monotonic()
    if len(_next_slot) > 5000:
        for k in [k for k, t in _next_slot.items() if t < now]:
            del _next_slot[k]
    at = max(now, _next_slot.get(key, 0.0))
    _next_slot[key] = at + GAP
    if at > now:
        await asyncio.sleep(at - now)
