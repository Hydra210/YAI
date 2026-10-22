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
        status = 200
    else:
        body = b"not found"
        status = 404

    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        }
    )
    await send({"type": "http.response.body", "body": body})


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
