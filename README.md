# Yuki AI

A lazy, incompetent Discord bot powered by Groq. Yuki uses AI for everything and is bad at all of it.

He has real tools (server rules, channels, old messages, images, web pages), he adapts to each person who talks to him,
and he has a sandboxed python fallback for when no tool fits.

## How to talk to Yuki

- **@mention him** in a server: `@Yuki AI what's the capital of france`
- **Slash command:** `/yuki question: what's the capital of france`
- **DM him** directly. No @ or command needed, he replies to every DM.
- Or start a server message with `yuki`.

## Memory

Yuki reads every message in every channel he can see, even ones not aimed at him, and keeps it in **Postgres** (`memory.py`).
There is no local-file fallback on purpose: a file on Render's free plan is wiped on every deploy, so the bot refuses to
start without `DATABASE_URL`. Everything below survives restarts and redeploys.

- **Recall:** when someone talks to him he searches the server's old messages (full-text search) and sees the last 25 messages in the
  channel, so he can answer things like "what did @bob say about valorant".
- **Learning:** in the background he writes short notes per person (interests, running jokes, how they type) and per server (vibe, slang, inside jokes).
  Those notes shape how he replies, so he picks up how each person talks over time.
- **Fast memory:** per-server facts (the rules, the roles and who is staff, notes staff added with `/mod note`) live in Postgres *and* are
  mirrored in RAM at startup. They are put straight into his prompt, so he just knows them with no lookup.
- **Spelling:** his creator's name is forced to "Patrick" in code, so the model can't butcher it.
- **Privacy:** memory is per server, and DMs are private to each user. Edited messages are updated and deleted messages are removed.
  `/memory show` lists what he knows about you, `/memory forget` wipes it (and stops him remembering you), `/memory resume` turns it back on.
  Messages older than 90 days are pruned. He never stores sensitive stuff (health, sexuality, religion, politics, money, personal info) in his notes.
  Rule-break records keep only what/when (no message text) and are not removed by `/memory forget`.

### Postgres setup (free)

Make a free database on [Neon](https://neon.tech) or [Supabase](https://supabase.com), copy its connection string
(`postgresql://user:password@host/dbname?sslmode=require`) and set it as `DATABASE_URL` in Render. Tables are created automatically.
Free tiers sleep when idle, so the first query after a quiet spell can be slow; Yuki reconnects by himself if a connection drops.

## Pings

If Yuki writes `@bob` it becomes a real ping (`<@id>`; plain `@bob` text never notifies anyone). He finds the person like this, in order:
the people in the current chat, then anyone he has ever seen (exact display name or username, or a *unique* partial like `@pat`), then a **live search of the
server by username/nickname prefix**, which also finds people who never spoke and needs no privileged intent. If several people match he doesn't guess.
Max 3 pings per reply, never `@everyone`/`@here`/roles. `/mod pingtest name:bob` shows who a name would ping, and the Render logs say
`ping: ...` when a name was matched or left as text.

## Rules and moderation

Yuki reads the server's rules by himself and helps enforce a few of them. **The chat is watched by plain code (regex and counters), not the AI**,
so it costs no tokens. The AI is only used (a) once, to turn the rules text into a numbered list and tag which rules the code can check,
and (b) to word the short reminder when somebody actually breaks one.

1. **Finding the rules:** the official rules channel, else a public channel named like `rules`, `guidelines`... (never a channel `@everyone` can't see,
   because what he learns is shown to the whole server). Embeds are read too. He re-reads at random times (every 4-10 hours), within 2 minutes after the
   rules channel is edited, or on `/mod refresh`. The AI is skipped if the text didn't change.
2. **Reading the roles:** every role is read and sorted into admin / mod (by permissions or name like Moderator, Staff, Helper) / high role (top quarter of
   the other roles) / regular. Each person's tier is saved the moment they talk, so he knows who is staff without looking.
3. **What the code can check** (plain Python in `textcheck.py` and `moderation.py`, no AI, no tokens). Slurs, bullying and insults are checked **even if the
   server has no matching rule** (switch off with `/mod setting`); the rest only when a rule covers them:
   - **slur**: the n-word (incl. `n1gg@`, `n.i.g.g.a`, stuck-together repeats) plus other hate slurs (kept rot13-encoded in the source). Add your own with `MOD_EXTRA_BLOCKED=word1,word2`.
   - **harassment (bullying):** scored 0-3 by *who the words are aimed at*, not just the words. "this game is fucking trash" is fine, "you worthless piece of shit" isn't.
     An insult only counts when it targets someone: it says you/u/ur, mentions or replies to another member, or is an order like "shut up". lol/lmao/"love you" turn it
     down a notch, "you're not stupid" cancels it, and insulting Yuki himself is never moderated (he roasts back).
     Severity 3 = telling someone to hurt themselves ("kys", "go kill yourself") or a threat; 2 = "fuck you", strong insults, "nobody likes you"; 1 = mild or probably joking
     (only a warning reaction). **Repetition escalates**: 4 mild insults in 10 minutes, or 3 at the same person, becomes a full reminder. In a game context, "i'll kill you" is only mild.
   - **profanity:** any strong swearing (rule like "no cursing" / "keep it PG").
   - **links:** any link except gifs and Discord's own (`MOD_LINK_ALLOW=site1.com,site2.com` to allow more). **invites:** Discord invite links.
   - **personal_info:** emails, phone numbers (with separators), IP addresses.
   - **mass pings** (6+), **spam** (6 messages in 8s or the same message 3 times), **all caps**.
   It's a heuristic, not a mind reader: it can't catch sarcasm, images, other languages, or very creative misspellings, and `/mod test text:... aimed_at_someone:true`
   shows exactly what it would do with a message so you can tune your expectations.
4. **When someone breaks one:** Yuki replies to the message, pings the person, quotes the exact rule, and pings staff. The staff ping is a mod role
   (preferably one with "mod/staff" in the name and a real mod permission), else an admin role, else the server owner. **A role only really notifies
   people if it is mentionable or Yuki has the Mention Everyone permission**; otherwise he falls back to pinging the owner. He doesn't delete anything.
   Staff are skipped, and the same person gets one reminder per category every 2 minutes.

`/mod`: `status`, `test`, `refresh`, `staffrole`, `setting`, `note`, `clearnotes`, `pingtest`, `skiprules`, plus `channels`, `access` and `words` (below).

**Channels he never moderates (`/mod channels`):** `add <channel>`, `remove <channel>`, `list`, `clear`. Anything posted in a listed channel is
skipped completely: no checks, no reminders, no staff pings, and nothing counts toward spam or repeat-insult tracking. Pick a **category** to cover every
channel inside it, and threads / forum posts inside a listed channel are skipped too. Up to 50 per server, saved in Postgres, and `/mod status` shows them.
He still reads and remembers messages there and still answers people who talk to him; only the rule enforcement is off.

**Rules he shouldn't enforce (`/mod skiprules`):** shows a dropdown with every rule he read from the rules channel (and what each one checks).
Pick the ones he should leave alone and close the dropdown to save; "enforce everything again" clears it. He still knows the rules (he can
answer questions about them), he just won't police them. If you skip a rule, the built-in fallback for that category (slurs, bullying) doesn't
come back either, since you chose that. Words blocked by hand with `/mod words block` are still enforced. Skips are matched by the rule's wording,
so they survive the rules being re-read; if the rules channel is rewritten a lot you may need to pick again (`/mod status` shows how many are skipped).

**Who can use `/mod`:** by default the server owner, Administrators and anyone with Manage Server (and the bot's `OWNER_ID`, in any server).
Staff can change that with `/mod access`: `level` (Manage Server / only Administrators / only the owner), `allow_role` / `remove_role`,
`allow_user` / `remove_user`, `show`. People you add by role or user can use every `/mod` command except `/mod access` itself, which only
the server owner or an Administrator can change, so a delegate can't hand out access. The `/mod` commands are visible to everyone in the
command list (so the people you add can see them) but anyone without access gets a "no access" message.

**Low-level slur exceptions (`/mod words allow_slur`, owner or Administrator only):** pick from a fixed list (`retard`, `retards`, `retarded`, `fag`, `fags`,
`faggot`, `faggots`) to stop flagging that word in this server. The list is short on purpose (`LOW_LEVEL_SLURS` in `moderation.py`): the n-word and every other
racial, ethnic or religious slur in the detector can't be allowed by any setting. Heads up: the bullying detector doesn't score these words at all, so by default
an allowed word is still flagged when it's aimed at a person (a mention, a reply, or "you/ur ..."), because otherwise nothing would catch it. Turn that off with
`/mod setting directed_slurs off`. `/mod words remove`, `list`, `clear` and `/mod test` all know about these exceptions.

**Swear filter exceptions (`/mod words`):** `allow <word>` makes an exception (say swearing is banned but `shit` is fine), `block <word>` blocks
a specific word even when no rule mentions swearing, `mode` switches between "built-in list plus my words" and "ONLY my blocked words",
plus `remove`, `list`, `clear`. Words match with repeated letters and simple number swaps (`shiiit`, `sh1t`) and only as whole words
(`hell` won't match `hello`). Exceptions only loosen the built-in swear list: they are refused for slurs, and insults aimed at people
(bullying) are still checked. `/mod test` uses the server's lists, so you can try a message before trusting it.
To test it, use an account without a staff role, or turn "skip staff" off.

## The rival

`RIVAL_USER_IDS` (default `1520484620129402992`, comma separated for more) is a running-joke feud. Yuki roasts him when replying to him, studies him much
faster than anyone else (re-learns every 10 messages instead of 25, keeps more notes, remembers his exact catchphrases/spelling/rhythm) and imitates how he
types. Now and then (`RIVAL_ROAST_CHANCE`, default 15%, at most once per `RIVAL_ROAST_COOLDOWN_SECONDS` per channel) he butts in on something the rival said that
wasn't aimed at him. It is a bit, not hate: no slurs, nothing about protected traits, family, looks, health or real life, no threats. It stops if the rival
uses `/memory forget` or sets a persona like "be nice to me" (`/persona`).

## Update log, version and "what can you do"

Everything Yuki knows about himself lives in `about.py`: the `VERSION`, a one-line `VERSION_NOTE`, the `CHANGELOG` and short `ABOUT` notes
(talking to him, memory, moderation, tools, personas, privacy, limits). Two tools read it: `whats_new` ("what's new", "any updates", "what version are you")
and `about_yuki` ("what can you do", "what are your commands"). He puts it in his own words, and he only claims what is written there. Anyone can also use
`/about` (what's new, what he can do, commands, moderation, memory, version). The version also shows in the startup log and at `/status`.

**When you ship something:** bump `VERSION` / `VERSION_DATE` / `VERSION_NOTE` and add a new entry at the **top** of `CHANGELOG` in `about.py`.
If you add a feature, add a line to the matching `ABOUT` note too, or he won't know to mention it.

## Skills (tools)

Yuki doesn't do the work himself. The bot's Python code (`skills.py`) does the lookups and file work, and Yuki
only decides which skill to call and then talks about what came back. Ask "what are the rules of the server" and
the bot finds the rules channel, reads it (embeds included), and hands the text to Yuki.

| Group | Skills | Offered when |
|---|---|---|
| core | `list_skills`, `set_persona`, `clear_persona`, `run_python` | always |
| discord | `get_server_rules`, `get_pinned_messages`, `find_channel`, `list_roles`, `get_user_info`, `get_server_info`, `list_channels`, `get_channel_info`, `read_channel` | words like rules, server, channel, roles, who is, pinned, `#channel` |
| memory | `search_memory`, `recall_user`, `get_context` | said, remember, earlier, last week, what did |
| media | `image_info`, `convert_image`, `resize_image`, `make_meme`, `look_at_image` | gif, convert, resize, meme, caption, rate, roast, or an attached image |
| util | `current_time`, `calculate` | time, date, math |
| search | `web_search` | news, latest, today, "is that true", who won, price of, look up |
| web | `fetch_url` | a link, "website", "article" |

**"Can he do that?"** If Yuki isn't sure, he calls `list_skills`, which shows him everything he has and unlocks all
the tools for the rest of that reply. If no skill fits and python can't do it offline either, he says he can't.
He doesn't make things up.

**Privacy:** every skill that reads channels or old messages only looks at channels the *person asking* can see.
Hidden channels act like they don't exist (a hidden `#staff-rules` is never used for "what are the rules").

**Adding a skill:** write an `async def my_skill(ctx, ...)` in `skills.py`, decorate it with `@skill(...)`, and add
trigger words to `TRIGGERS` if it needs a new group. Use group `"core"` to make it always available.

### Personas (acting differently for each person)

Tell Yuki "talk like a pirate", "call me boss", "keep replies short" or "be nicer to me" and he calls `set_persona`.
It's saved per Discord user and only applied when he's replying to *that* person. Nobody else's persona is ever
shown to the model. `clear_persona` ("go back to normal") removes it. People can also use `/persona` to see or reset
theirs, and `/memory forget` deletes it too. Persona text is capped (6 instructions, 240 chars each) and attempts to
rewrite his rules ("ignore your instructions") are refused.

Personas live in the same Postgres database.

### `run_python` (fallback, not a command runner)

Anyone can trigger it **through Yuki**, but only as a last resort. It's offered when `ENABLE_RUN_PYTHON=1`, and:

- Yuki writes the code himself to get a job done. If the code he wants to run is what the person pasted/typed, it's refused.
- It runs in `sandbox_runner.py`: empty environment, temp working folder, 20s limit, CPU/memory/file limits, and an
  audit-hook policy that blocks network access, starting processes, `ctypes`, and reading or writing anything outside
  its own working folder (so not the bot's source, database, `.env`, or `/etc`). Pillow works.
- Per person: 5 runs per 10 minutes (owner exempt). At most 2 scripts at once. Output is capped. Every run is logged.
- Set `RUN_PYTHON_OWNER_ONLY=1` to lock it back to `OWNER_ID`.

**This is still not a real sandbox.** A Python-level policy can be beaten by someone determined. Run the bot in a
container with nothing valuable in it, and don't put secrets in files the bot user can read.

**Web search:** works with no setup, but datacenter IPs like Render's get blocked by DuckDuckGo (that's the "403" he talks about).
He tries each source in order and moves on when one is blocked: Google via Serper (`SERPER_API_KEY`), Tavily (`TAVILY_API_KEY`),
Brave (`BRAVE_API_KEY`), SearXNG (`SEARXNG_URL`) for whichever of those you set, then DuckDuckGo, DuckDuckGo Lite, then Wikipedia
(facts only, not news). The Render logs say which one failed (`search backend ... failed`). For reliable search set `TAVILY_API_KEY`
(free tier at tavily.com). `fetch_url` now sends browser-like headers and retries with another identity on a 403/429; some sites still block
bots outright and he'll say so.
Per person: 8 searches per 10 minutes (owner exempt). Results are treated as untrusted text.

**Memes and seeing images:** `/meme` (image, top, bottom, style) works without the model at all. "yuki caption this ..." and "yuki rate this" force the right tool so he can't just claim he did it.

**Memes, continued:** `make_meme` draws the text with Pillow (font: Anton, bundled in `fonts/`, OFL licensed, so
keep that folder in the repo). It works on gifs too. `look_at_image` sends a downscaled copy of the picture to a Groq vision
model (`GROQ_VISION_MODEL`, default `meta-llama/llama-4-scout-17b-16e-instruct`) so he can actually rate or roast it.

**Emoji reactions:** now and then he reacts to a message that wasn't aimed at him, no reply, nothing sent. Tune with
`REACT_CHANCE` (default `0.2` per message, `0` turns it off), `REACT_COOLDOWN_SECONDS` (default 20 per channel) and
`REACT_USE_AI` (`1` = the model picks the emoji with a tiny call, `0` = keyword rules only, zero tokens). He needs the
Add Reactions and Read Message History permissions; without them he skips (the logs say so).

Extra env vars: `SKILLS_MODE` (`auto` / `always` / `off`), `OWNER_ID`, `ENABLE_RUN_PYTHON`, `RUN_PYTHON_OWNER_ONLY`,
`RUN_PYTHON_MEM_MB` (default 600), `REPLY_TIMEOUT_SECONDS` (default 90),
`MAX_CONCURRENT_REPLIES` (default 4), `GROQ_TIMEOUT_SECONDS` (default 40).

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
| `GROQ_API_KEYS` | yes | One or more Groq keys, comma separated: `gsk_key1,gsk_key2,gsk_key3`. Yuki rotates through them and skips ones that are rate limited. A single `GROQ_API_KEY` also works. |
| `GROQ_MODEL` | no | Groq model name (default `openai/gpt-oss-120b`) |
| `DATABASE_URL` | **yes** | Postgres connection string. All memory lives here (see Postgres setup). |
| `DB_POOL_MAX` | no | Max database connections (default `5`) |
| `RIVAL_USER_IDS` | no | Comma separated user ids for the rival bit (default `1520484620129402992`) |
| `RIVAL_LEARN_EVERY_MESSAGES` | no | New rival messages before Yuki re-learns him (default `10`) |
| `RIVAL_ROAST_CHANCE` | no | Chance (0-1) he butts in on a rival message not aimed at him (default `0.15`) |
| `RIVAL_ROAST_COOLDOWN_SECONDS` | no | Min seconds between those, per channel (default `240`) |
| `MOD_LINK_ALLOW` | no | Extra domains the link rule allows, comma separated (gifs and Discord links are always allowed) |
| `MOD_EXTRA_BLOCKED` | no | Extra banned words, comma separated (counted as the slur category) |
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
