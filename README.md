# Yuki AI

A lazy, incompetent Discord bot powered by Groq. Yuki uses AI for everything and is bad at all of it.

## How to talk to Yuki

- **@mention him** in a server: `@Yuki AI what's the capital of france`
- **Slash command:** `/yuki question: what's the capital of france`
- **DM him** directly. No @ or command needed, he replies to every DM.
- Or start a server message with `yuki`.

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill in your tokens
python bot.py
```

## Environment variables

| Name | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | yes | Bot token from the Discord developer portal |
| `GROQ_API_KEY` | yes | API key from console.groq.com |
| `GROQ_MODEL` | no | Groq model name (default `openai/gpt-oss-120b`) |

## Discord setup

1. Create an application at discord.com/developers/applications, then copy the bot token from the **Bot** tab.
2. On the **Bot** tab, enable **Message Content Intent**.
3. Under **Installation**, enable both **Guild Install** and **User Install**. Make sure `bot` and `applications.commands` are listed as scopes for Guild Install.
4. Invite the bot to your server using the install link from that Installation page (or OAuth2 → URL Generator with the `bot` and `applications.commands` scopes).
5. To use `/yuki` in DMs and servers the bot isn't in, open the same install link and choose **Add to My Apps**.

DMs only work with users who share a server with the bot or who have added it to their apps.

## Deploy on Render

1. Push this repo to GitHub.
2. In Render: New > Web Service > connect the repo (or use the `render.yaml` blueprint).
3. Build command: `pip install -r requirements.txt`. Start command: `python bot.py`.
4. Add the env vars above, plus `PYTHON_VERSION=3.11.9`.
5. Free web services sleep after ~15 minutes without HTTP traffic, so point an uptime pinger (e.g. UptimeRobot) at `https://<your-service>.onrender.com/health` every 5 minutes.

Only run one copy of the bot at a time, or every message gets duplicate replies.

## Models get retired

Groq retires models regularly (llama-3.3-70b-versatile was shut down on 2026-08-16). If Yuki says his model is gone, pick a current one from https://console.groq.com/docs/models and set `GROQ_MODEL` in your Render environment variables. Note that a `GROQ_MODEL` set in Render overrides the default in the code.
