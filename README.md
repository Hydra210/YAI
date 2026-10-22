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

## Skills

Yuki doesn't do the work himself. The bot's Python code (`skills.py`) does the lookups and the file
work, and Yuki just decides which skill to call and then talks about the result. Skills only get shown to
the model when the message looks like it needs them (`select_skills`), so normal chat costs exactly what
it did before.

| Group | Skills | Triggered by words like |
|---|---|---|
| discord | `get_user_info`, `get_server_info`, `list_channels`, `get_channel_info`, `read_channel` | who is, profile, joined, server, channel, `#channel` |
| memory | `search_memory` (filter by person / channel / days back), `recall_user`, `get_context` | said, remember, earlier, last week, what did |
| media | `image_info`, `convert_image` (gif/png/jpg/webp, animated stays animated), `resize_image` | gif, convert, resize, or just attaching an image |
| util | `current_time`, `calculate` | time, date, math |
| owner | `run_python` | run, python, script (only for `OWNER_ID`, only if `ENABLE_RUN_PYTHON=1`) |

Images come from the message itself, the message you reply to, or the last few messages in the channel.
`/yuki` also has an optional `image` field.

**Privacy:** every skill that reads channels or old messages only looks at channels the *person asking*
can see. Hidden channels act like they don't exist. This also applies to Yuki's automatic recall, which
used to search every channel in the server.

**Adding a skill:** write an `async def my_skill(ctx, ...)` in `skills.py`, decorate it with `@skill(...)`,
and add trigger words to `TRIGGERS` if it needs a new group. That's it.

**`run_python` is not a real sandbox.** It runs with an empty environment (no tokens), a temp dir, a 20s
timeout and CPU/memory limits, but it can still read files on the host. That's why it's locked to
`OWNER_ID` and off by default. Don't enable it on a box that holds secrets you wouldn't hand to the owner.

Extra env vars: `SKILLS_MODE` (`auto` / `always` / `off`), `OWNER_ID`, `ENABLE_RUN_PYTHON`,
`REPLY_TIMEOUT_SECONDS` (default 90), `MAX_CONCURRENT_REPLIES` (default 4), `GROQ_TIMEOUT_SECONDS` (default 40).

## Why Yuki used to stop answering

Old versions pushed every message in every channel through a single worker. If that one worker ever hung or
died, normal chat stopped until a redeploy, while `/yuki` kept working because it skipped the queue. Now
each reply is its own task with a hard timeout, one channel can't block another, and a crash only affects
that one message. `/status` shows ready state, latency, replies, errors and timeouts for debugging.

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
