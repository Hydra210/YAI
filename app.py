import logging
import os
import threading

logging.basicConfig(level=logging.INFO)


async def app(scope, receive, send):
    if scope["type"] != "http":
        return

    path = scope.get("path", "/")
    if path in ("/", "/health"):
        body = b"ok"
    else:
        body = b"not found"

    await send(
        {
            "type": "http.response.start",
            "status": 200 if path in ("/", "/health") else 404,
            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _start_discord_bot():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        logging.warning("DISCORD_TOKEN is not set; skipping bot startup")
        return

    try:
        import bot  # noqa: F401
        from bot import client

        logging.info("Starting Discord bot in background thread")
        client.run(token)
    except Exception:
        logging.exception("Failed to start Discord bot from app entrypoint")


threading.Thread(target=_start_discord_bot, daemon=True).start()
