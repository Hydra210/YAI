# Yuki AI

A lazy, incompetent Discord bot powered by Groq. Yuki uses AI for everything and is bad at all of it.

## How to talk to Yuki

- **@mention him** in a server: `@Yuki AI what's the capital of france`
- **Slash command:** `/yuki question: what's the capital of france`
- **DM him** directly. No @ or command needed, he replies to every DM.
- Or start a server message with `yuki`.

## Memory

Yuki reads every message in every channel he can see, even ones not aimed at him, and keeps it in a SQLite database (`memory.py`).

- **Recall:** when someone talks to him, he searches the server's old messages for relevant ones and sees the last 15 messages in the channel, so he can answer things like "what did @bob say about valorant".
- **Learning:** in the background he writes short notes per person (interests, running jokes, how they type) and per server (vibe, slang, inside jokes). Those notes shape how he replies, so he picks up each person's slang over time. It runs at most one Groq call every 45 seconds.
- **Spelling:** his creator's name is forced to "Patrick" in code, so the model can't butcher it.
- **Privacy:** memory is per server, and DMs are private to each user. Edited messages are updated and deleted messages are removed. `/memory show` lists what he knows about you, `/memory forget` wipes it and stops him remembering you, `/memory resume` turns it back on. Messages older than 90 days are pruned.
- He never stores sensitive stuff (health, sexuality, religion, politics, money, personal info) in his notes.

### Making memory survive restarts on Render

Render's free plan wipes the filesystem on every deploy and restart, so Yuki would forget everything. To keep memory you need a persistent disk (paid plan): add a disk mounted at `/data` and set `DATABASE_PATH=/data/yuki.db`. There's a commented-out example in `render.yaml`. Locally it just works, the file is `yuki.db` in the project folder.

If you're running a bot in servers besides your own, Discord's developer policy expects you to tell people what you store and honor deletion requests. `/memory forget` covers the deletion part, you'll still want a short privacy blurb.

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
| `DATABASE_PATH` | no | Where the memory database lives (default `yuki.db`) |
| `MEMORY_RETENTION_DAYS` | no | How long raw messages are kept (default `90`) |
| `LEARN_INTERVAL_SECONDS` | no | Min seconds between background learning calls (default `45`) |
| `LEARN_EVERY_MESSAGES` | no | New messages from a person before Yuki re-learns them (default `20`) |
| `VIBE_EVERY_MESSAGES` | no | New server messages before Yuki re-learns the server vibe (default `60`) |

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
