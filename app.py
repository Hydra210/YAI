import asyncio
import json
import logging
import os
import sys
import threading
from urllib.parse import parse_qs

logging.basicConfig(level=logging.INFO)

_HERE = os.path.dirname(os.path.abspath(__file__))


async def _send(send, status, body: bytes, content_type: str):
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", content_type.encode()), (b"cache-control", b"no-store")],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _key_report(fresh: bool):
    """Run the key check on the bot's own event loop (its Groq clients belong to that loop). None = the bot isn't up yet."""
    b = sys.modules.get("bot")
    loop = getattr(getattr(b, "client", None), "loop", None)
    if not isinstance(loop, asyncio.AbstractEventLoop) or not loop.is_running():
        return None
    fut = asyncio.run_coroutine_threadsafe(b.key_report(fresh=fresh), loop)
    return await asyncio.wait_for(asyncio.wrap_future(fut), timeout=40)


async def app(scope, receive, send):
    if scope["type"] != "http":
        return

    path = scope.get("path", "/")
    query = parse_qs((scope.get("query_string") or b"").decode("latin-1"))

    if path in ("/keys", "/keys.json"):
        # same key status page as on Render. KEYS_PAGE_TOKEN (optional) works the same way: /keys?token=...
        token = (os.getenv("KEYS_PAGE_TOKEN") or "").strip()
        if token and query.get("token", [""])[0] != token:
            return await _send(send, 401, b"wrong or missing ?token=", "text/plain; charset=utf-8")
        if path == "/keys":
            b = sys.modules.get("bot")
            if b is None or not hasattr(b, "KEYS_HTML"):
                return await _send(send, 503, b"the bot is still starting, refresh in a few seconds", "text/plain; charset=utf-8")
            return await _send(send, 200, b.KEYS_HTML.encode(), "text/html; charset=utf-8")
        try:
            report = await _key_report(query.get("fresh", [""])[0] == "1")
        except Exception:
            logging.exception("key check failed")
            report = None
        if report is None:
            return await _send(send, 503, b'{"error": "the bot is still starting, try again in a few seconds"}', "application/json")
        return await _send(send, 200, json.dumps(report).encode(), "application/json")

    if path in ("/", "/health"):
        body = b"ok"
        status = 200
    else:
        body = b"not found"
        status = 404
    await _send(send, status, body, "text/plain; charset=utf-8")


def _start_bot():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        logging.warning("DISCORD_TOKEN is not set; skipping bot startup")
        return

    try:
        import bot

        logging.info("Starting Discord bot in background thread")
        bot.client.run(token)
    except Exception:
        logging.exception("Failed to start Discord bot from Vercel entrypoint")


threading.Thread(target=_start_bot, daemon=True).start()
