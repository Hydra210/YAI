"""Yuki's skillset.

The bot's Python code does the actual work (lookups, searching memory, converting
images). Yuki (the model) only decides *which* skill to call and then talks about
the result. That keeps token usage low: skills are only shown to the model when the
message looks like it needs them (see select_skills), and every result is trimmed.

Adding a skill = write an async function and decorate it:

    @skill("util", "what it does", {"arg": P("string", "what it is")}, required=["arg"])
    async def my_skill(ctx, arg):
        return "plain text result"

Privacy rules baked in:
  - Anything that reads channels / old messages only looks at channels the *person
    asking* can see themselves.
  - Skills never touch env vars, tokens or the database file directly.
  - run_python is owner-only and off unless ENABLE_RUN_PYTHON=1 (see its docstring).
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import io
import json
import logging
import math
import operator
import os
import re
import signal
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import discord

try:
    from PIL import Image, ImageSequence

    Image.MAX_IMAGE_PIXELS = 80_000_000  # Pillow's hard bomb guard; we apply a lower cap ourselves
    HAVE_PIL = True
except ImportError:  # pragma: no cover
    HAVE_PIL = False

log = logging.getLogger("yuki.skills")

MAX_RESULT_CHARS = 2500          # per tool result sent back to the model
SKILL_TIMEOUT = 45               # seconds per skill call
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_FRAMES = 150
FRAME_PIXEL_BUDGET = 30_000_000  # width * height * frames, keeps RAM sane on a 512MB box
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


def _env_flag(name: str) -> bool:
    return os.getenv(name, "0").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- context


@dataclass
class Ctx:
    """Everything a skill is allowed to know about who asked and where."""

    client: discord.Client
    mem: Any
    scope: int
    guild: discord.Guild | None
    channel: Any
    channel_id: int
    user: Any
    member: discord.Member | None
    mentions: dict = field(default_factory=dict)      # user id -> display name
    attachments: list = field(default_factory=list)   # on the triggering message / /yuki option
    reply_to_id: int | None = None
    is_owner: bool = False
    file_limit: int = 8 * 1024 * 1024
    files: list = field(default_factory=list)         # (filename, bytes) attached to the reply
    _image: Any = None
    _image_searched: bool = False

    @property
    def owner_ok(self) -> bool:
        return self.is_owner and _env_flag("ENABLE_RUN_PYTHON")

    # --- permissions: only ever show people what they could already see themselves

    def can_view(self, channel) -> bool:
        if channel is None:
            return False
        if self.guild is None:
            return getattr(channel, "id", None) == self.channel_id
        if self.member is None:
            return False
        try:
            p = channel.permissions_for(self.member)
        except Exception:
            return False
        return bool(p.view_channel and p.read_message_history)

    def bot_can_read(self, channel) -> bool:
        if self.guild is None:
            return True
        me = self.guild.me
        if me is None:
            return False
        try:
            p = channel.permissions_for(me)
        except Exception:
            return False
        return bool(p.view_channel and p.read_message_history)

    def visible_channel_ids(self):
        """Channel ids the asker can read. None = no restriction needed (their own DM scope)."""
        if self.guild is None:
            return None if self.scope < 0 else set()
        if self.member is None:
            return set()
        ids = {c.id for c in self.guild.channels if self.can_view(c)}
        ids |= {t.id for t in self.guild.threads if self.can_view(t)}
        return ids

    def channel_label(self, channel_id: int) -> str:
        if self.guild is not None:
            ch = self.guild.get_channel_or_thread(channel_id)
            if ch is not None:
                return f"#{ch.name}"
        return "#dm" if self.scope < 0 else "#?"

    # --- images

    async def find_image(self, scan_history: bool = True):
        """Image attachment: this message -> the message being replied to -> last few messages."""
        if self._image is not None:
            return self._image
        for a in self.attachments:
            if is_image(a):
                self._image = a
                return a
        if self._image_searched:
            return None
        self._image_searched = True
        if self.reply_to_id and self.channel is not None:
            try:
                ref = await self.channel.fetch_message(self.reply_to_id)
                for a in ref.attachments:
                    if is_image(a):
                        self._image = a
                        return a
            except (discord.HTTPException, AttributeError):
                pass
        if scan_history and self.channel is not None and hasattr(self.channel, "history"):
            try:
                async for m in self.channel.history(limit=12):
                    for a in m.attachments:
                        if is_image(a):
                            self._image = a
                            return a
            except (discord.HTTPException, AttributeError):
                pass
        return None


def is_image(att) -> bool:
    ctype = (getattr(att, "content_type", None) or "").lower()
    name = (getattr(att, "filename", "") or "").lower()
    if "svg" in ctype or name.endswith(".svg"):
        return False
    return ctype.startswith("image/") or name.endswith(IMAGE_EXTS)


def where(ctx: Ctx) -> str:
    if ctx.guild is not None:
        return f'server "{ctx.guild.name}", channel {ctx.channel_label(ctx.channel_id)}'
    return "a DM" if ctx.scope < 0 else "a server i'm not in"


# ---------------------------------------------------------------- registry


@dataclass
class Skill:
    name: str
    group: str
    description: str
    params: dict
    required: list
    fn: Callable
    owner_only: bool = False


SKILLS: dict[str, Skill] = {}


def P(type_: str, description: str, **extra) -> dict:
    return {"type": type_, "description": description, **extra}


def skill(group: str, description: str, params: dict | None = None, required=(), owner_only: bool = False):
    def deco(fn):
        SKILLS[fn.__name__] = Skill(fn.__name__, group, description, params or {}, list(required), fn, owner_only)
        return fn

    return deco


def schema(s: Skill) -> dict:
    return {
        "type": "function",
        "function": {
            "name": s.name,
            "description": s.description,
            "parameters": {"type": "object", "properties": s.params, "required": s.required},
        },
    }


# Cheap keyword routing so the model only sees tool schemas when they might matter.
# No match = plain chat, same token cost as before skills existed.
TRIGGERS = {
    "discord": re.compile(
        r"\b(who(?:'s| is| are)|whois|profile|account|joined|join date|server|guild|channels?|members?|"
        r"roles?|owner|boosts?|boosted|topic|permissions?|created|how old|avatar|pfp|banner)\b|<#\d+>",
        re.I,
    ),
    "memory": re.compile(
        r"\b(remember|recall|remind\w*|said|says?|told|mentioned|talk(?:ed|ing)? about|earlier|yesterday|"
        r"last (?:week|month|year|night)|ago|back when|history|search|find|look(?:ing)? up|who (?:said|was)|"
        r"what did|did (?:anyone|anybody|someone)|ever)\b",
        re.I,
    ),
    "media": re.compile(
        r"\b(gif|png|jpe?g|webp|convert|resize|compress|shrink|enlarge|image|img|pic|picture|photo|"
        r"file ?size|dimensions?|resolution)\b",
        re.I,
    ),
    "util": re.compile(
        r"\b(time|date|today|tonight|timezone|utc|gmt|est|pst|calculate|calc|math|percent(?:age)?|sqrt|"
        r"square root|plus|minus|times|divided)\b|\d\s*[-+*/x×÷^%]\s*\d",
        re.I,
    ),
    "owner": re.compile(
        r"\b(run|python|script|code|execute|terminal|command|shell|program|generate)\b", re.I
    ),
}


def select_skills(text: str, ctx: Ctx) -> list[Skill]:
    """SKILLS_MODE=auto (default) | always | off"""
    mode = os.getenv("SKILLS_MODE", "auto").strip().lower()
    if mode == "off":
        return []
    if mode == "always":
        groups = set(TRIGGERS)
    else:
        groups = {g for g, rx in TRIGGERS.items() if rx.search(text or "")}
        if any(is_image(a) for a in ctx.attachments):
            groups.add("media")
    out = []
    for s in SKILLS.values():
        if s.group not in groups:
            continue
        if s.owner_only and not ctx.owner_ok:
            continue
        out.append(s)
    return out


def _int(value, default: int, lo: int, hi: int) -> int:
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


async def call_skill(ctx: Ctx, name: str, raw_args) -> str:
    """Run one skill call from the model. Always returns a short string, never raises."""
    sk = SKILLS.get(name)
    if sk is None or (sk.owner_only and not ctx.owner_ok):
        return f"no skill called {name}"
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args.strip() else (raw_args or {})
    except json.JSONDecodeError:
        return "bad arguments (not valid json)"
    if not isinstance(args, dict):
        args = {}
    args = {k: v for k, v in args.items() if k in sk.params and v is not None}
    try:
        inspect.signature(sk.fn).bind(ctx, **args)
    except TypeError as e:
        return f"bad arguments: {e}"
    try:
        result = await asyncio.wait_for(sk.fn(ctx, **args), SKILL_TIMEOUT)
    except asyncio.TimeoutError:
        return "that took too long, gave up"
    except Exception as e:  # a broken skill must never take the reply down with it
        log.exception("skill %s crashed", name)
        return f"skill failed ({type(e).__name__})"
    return str(result)[:MAX_RESULT_CHARS]


# ---------------------------------------------------------------- small helpers


def _ago(dt: datetime | None) -> str:
    if dt is None:
        return "unknown"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - dt).days
    if days >= 365:
        age = f"{days // 365}y {(days % 365) // 30}mo ago"
    elif days >= 30:
        age = f"{days // 30}mo ago"
    elif days >= 1:
        age = f"{days}d ago"
    else:
        age = "today"
    return f"{dt:%Y-%m-%d} ({age})"


def _fmt_ts(ts: int) -> str:
    return time.strftime("%b %d", time.gmtime(ts))


def _row(ctx: Ctx, r: dict) -> str:
    return f"[{_fmt_ts(r['ts'])} {ctx.channel_label(r['channel_id'])}] {r['author']}: {r['content'][:220]} (id {r['id']})"


async def _to_thread(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


async def resolve_user(ctx: Ctx, ref):
    """Turn 'bob', '@bob', '<@123>' or an id into (user_id, Member|User|None). None ref = the asker."""
    ref = str(ref).strip() if ref is not None else ""
    if not ref or ref.lower() in ("me", "myself", "i", "my"):
        return ctx.user.id, (ctx.member or ctx.user)
    uid = None
    m = re.search(r"\d{15,21}", ref)
    if m:
        uid = int(m.group())
    else:
        name = ref.lstrip("@").strip().lower()
        for i, n in ctx.mentions.items():
            if n.lower() == name:
                uid = i
                break
        if uid is None and ctx.guild is not None:
            members = list(ctx.guild.members)
            exact = lambda x: name in {x.name.lower(), x.display_name.lower(), (x.global_name or "").lower()}
            loose = lambda x: name in x.display_name.lower() or name in x.name.lower()
            for pred in (exact, loose):
                hit = next((x for x in members if pred(x)), None)
                if hit:
                    uid = hit.id
                    break
        if uid is None:
            hits = await _to_thread(ctx.mem.find_user, ctx.scope, name)
            if hits:
                uid = hits[0]["user_id"]
    if uid is None:
        return None, None
    obj = None
    if ctx.guild is not None:
        obj = ctx.guild.get_member(uid)
        if obj is None:
            try:
                obj = await ctx.guild.fetch_member(uid)
            except discord.HTTPException:
                obj = None
    if obj is None and uid == ctx.user.id:
        obj = ctx.user
    return uid, obj


def resolve_channel(ctx: Ctx, ref):
    """Find a channel the asker can see. No ref = current channel. Hidden channels look like they don't exist."""
    ref = str(ref).strip() if ref is not None else ""
    if not ref:
        return ctx.channel
    if ctx.guild is None:
        return None
    m = re.search(r"\d{15,21}", ref)
    if m:
        ch = ctx.guild.get_channel_or_thread(int(m.group()))
        return ch if ch is not None and ctx.can_view(ch) else None
    name = ref.lstrip("#").strip().lower().replace(" ", "-")
    pool = [
        c
        for c in [*ctx.guild.channels, *ctx.guild.threads]
        if not isinstance(c, discord.CategoryChannel) and ctx.can_view(c)
    ]
    for c in pool:
        if c.name.lower() == name:
            return c
    for c in pool:
        if name in c.name.lower():
            return c
    return None


# ---------------------------------------------------------------- discord skills


@skill(
    "discord",
    "Look up a server member's Discord profile: names, id, account age, join date, roles, avatar.",
    {"user": P("string", "name, @mention or user id. Omit for the person who asked.")},
)
async def get_user_info(ctx: Ctx, user: str = None):
    uid, obj = await resolve_user(ctx, user)
    if uid is None:
        return f"don't know anyone called {user!r} here"
    if obj is None:
        return f"user {uid} isn't in this server (or left)"
    lines = [f"name: {obj.display_name} (@{obj.name}) | id: {obj.id} | bot: {'yes' if obj.bot else 'no'}"]
    lines.append(f"account created: {_ago(obj.created_at)}")
    if isinstance(obj, discord.Member):
        if obj.joined_at:
            lines.append(f"joined this server: {_ago(obj.joined_at)}")
        roles = [r.name for r in reversed(obj.roles) if not r.is_default()]
        if roles:
            lines.append("roles (top first): " + ", ".join(roles[:12]))
        if obj.guild_permissions.administrator:
            lines.append("has admin")
        if obj.premium_since:
            lines.append(f"boosting since {_ago(obj.premium_since)}")
    lines.append(f"avatar: {obj.display_avatar.url}")
    stats = await _to_thread(ctx.mem.user_stats, ctx.scope, uid)
    if stats["opted_out"]:
        lines.append("they opted out of yuki remembering them")
    else:
        lines.append(f"messages yuki has saved from them here: {stats['messages']}")
    return "\n".join(lines)


@skill("discord", "Info about the current server: owner, member count, age, channel/role counts, boosts.")
async def get_server_info(ctx: Ctx):
    g = ctx.guild
    if g is None:
        return "not in a server right now (dm, or i'm not in this one)"
    owner = g.get_member(g.owner_id)
    lines = [
        f"server: {g.name} | id {g.id}",
        f"owner: {owner.display_name if owner else 'id ' + str(g.owner_id)}",
        f"members: {g.member_count}",
        f"created: {_ago(g.created_at)}",
        f"channels: {len(g.text_channels)} text, {len(g.voice_channels)} voice, {len(g.forums)} forum, "
        f"{len(g.categories)} categories",
        f"roles: {max(len(g.roles) - 1, 0)} | emojis: {len(g.emojis)} | stickers: {len(g.stickers)}",
        f"boosts: level {g.premium_tier} ({g.premium_subscription_count} boosts)",
        f"verification level: {g.verification_level.name}",
    ]
    if g.description:
        lines.append(f"description: {g.description[:200]}")
    return "\n".join(lines)


@skill("discord", "List the channels in this server that the asker can see, grouped by category.")
async def list_channels(ctx: Ctx):
    g = ctx.guild
    if g is None:
        return "no server to list channels for"
    rows = []
    for cat, chans in g.by_category():
        names = []
        for c in chans:
            if not ctx.can_view(c):
                continue
            suffix = ""
            if isinstance(c, discord.VoiceChannel):
                suffix = " (voice)"
            elif isinstance(c, discord.StageChannel):
                suffix = " (stage)"
            elif isinstance(c, discord.ForumChannel):
                suffix = " (forum)"
            names.append(f"#{c.name}{suffix}")
        if names:
            rows.append(f"{cat.name if cat else 'no category'}: " + ", ".join(names))
    return "\n".join(rows) or "no channels you can see"


@skill(
    "discord",
    "Details about one channel: topic, category, slowmode, nsfw, age, last activity.",
    {"channel": P("string", "channel name, #name, <#id> or id. Omit for the current channel.")},
)
async def get_channel_info(ctx: Ctx, channel: str = None):
    ch = resolve_channel(ctx, channel)
    if ch is None:
        return "can't find a channel like that that you can see"
    lines = [f"#{getattr(ch, 'name', 'dm')} | id {ch.id} | type {getattr(ch.type, 'name', 'dm')}"]
    cat = getattr(ch, "category", None)
    if cat is not None:
        lines.append(f"category: {cat.name}")
    topic = getattr(ch, "topic", None)
    if topic:
        lines.append(f"topic: {topic[:300]}")
    created = getattr(ch, "created_at", None)
    if created:
        lines.append(f"created: {_ago(created)}")
    if getattr(ch, "slowmode_delay", 0):
        lines.append(f"slowmode: {ch.slowmode_delay}s")
    if getattr(ch, "nsfw", False):
        lines.append("nsfw: yes")
    last = getattr(ch, "last_message_id", None)
    if last:
        lines.append(f"last message: {_ago(discord.utils.snowflake_time(last))}")
    return "\n".join(lines)


@skill(
    "discord",
    "Read the latest messages live from a channel (use for other channels or when recent chat isn't enough).",
    {
        "channel": P("string", "channel name, #name, <#id> or id. Omit for the current channel."),
        "limit": P("integer", "how many recent messages, 1-40 (default 20)"),
    },
)
async def read_channel(ctx: Ctx, channel: str = None, limit: int = 20):
    ch = resolve_channel(ctx, channel)
    if ch is None:
        return "can't find a channel like that that you can see"
    if not hasattr(ch, "history"):
        return "that isn't a text channel"
    if not ctx.can_view(ch):
        return "you can't see that channel"
    if not ctx.bot_can_read(ch):
        return "i don't have permission to read that channel"
    limit = _int(limit, 20, 1, 40)
    try:
        msgs = [m async for m in ch.history(limit=limit)]
    except discord.HTTPException as e:
        return f"discord wouldn't let me read it ({e.status})"
    out, used = [], 0
    for m in msgs:  # newest first so the budget cuts the oldest, not the latest
        body = (m.clean_content or "").replace("\n", " ")[:220]
        if m.attachments:
            body += f" [+{len(m.attachments)} attachment(s)]"
        line = f"[{m.created_at:%b %d %H:%M}] {m.author.display_name}: {body}"
        if used + len(line) > MAX_RESULT_CHARS - 100:
            break
        out.append(line)
        used += len(line) + 1
    if not out:
        return "channel is empty (or nothing readable)"
    return f"latest in #{getattr(ch, 'name', 'dm')} (oldest first):\n" + "\n".join(reversed(out))


# ---------------------------------------------------------------- memory skills


@skill(
    "memory",
    "Search old messages Yuki has saved in this server (months back). Filter by person, channel or time. "
    "Leave query empty to just list someone's/a channel's recent messages.",
    {
        "query": P("string", "keywords to look for, e.g. 'valorant tournament'"),
        "user": P("string", "only messages from this person (name, @mention or id)"),
        "channel": P("string", "only this channel (name, #name or id)"),
        "days_back": P("integer", "only messages from the last N days"),
        "limit": P("integer", "max results, 1-15 (default 8)"),
    },
)
async def search_memory(
    ctx: Ctx, query: str = "", user: str = None, channel: str = None, days_back: int = None, limit: int = 8
):
    uid = chan_id = since = None
    if user:
        uid, _ = await resolve_user(ctx, user)
        if uid is None:
            return f"don't know anyone called {user!r} here"
        if uid in ctx.mem.optouts:
            return "that person opted out, so nothing is saved from them"
    if channel:
        ch = resolve_channel(ctx, channel)
        if ch is None:
            return "can't find a channel like that that you can see"
        chan_id = ch.id
    if days_back:
        since = int(time.time()) - _int(days_back, 30, 1, 3650) * 86400
    limit = _int(limit, 8, 1, 15)
    rows = await _to_thread(
        ctx.mem.search, ctx.scope, query or "", limit,
        user_id=uid, channel_id=chan_id, since=since, allow=ctx.visible_channel_ids(),
    )
    if not rows:
        return "found nothing"
    return "\n".join(_row(ctx, r) for r in rows)


@skill(
    "memory",
    "What Yuki knows about a person: his learned notes on them plus their latest saved messages here.",
    {
        "user": P("string", "name, @mention or id. Omit for the person who asked."),
        "limit": P("integer", "how many recent messages, 1-12 (default 6)"),
    },
)
async def recall_user(ctx: Ctx, user: str = None, limit: int = 6):
    uid, _ = await resolve_user(ctx, user)
    if uid is None:
        return f"don't know anyone called {user!r} here"
    if uid in ctx.mem.optouts:
        return "that person opted out, nothing saved"
    limit = _int(limit, 6, 1, 12)
    profile = await _to_thread(ctx.mem.get_profile, ctx.scope, uid)
    msgs = await _to_thread(ctx.mem.user_messages, ctx.scope, uid, limit * 4)
    allow = ctx.visible_channel_ids()
    msgs = [m for m in msgs if allow is None or m["channel_id"] in allow][:limit]
    lines = []
    if profile and profile["notes"]:
        lines.append("notes: " + "; ".join(profile["notes"]))
    if profile and profile["style"]:
        lines.append("talks like: " + profile["style"])
    for m in reversed(msgs):
        lines.append(
            f"[{_fmt_ts(m['ts'])} {ctx.channel_label(m['channel_id'])}] {m['content'][:200]} (id {m['id']})"
        )
    return "\n".join(lines) or "haven't saved anything about them yet"


@skill(
    "memory",
    "Get the messages around one saved message (use an id from search results) to see what the conversation was.",
    {
        "message_id": P("string", "the id from a search result"),
        "span": P("integer", "how many messages before and after, 1-8 (default 4)"),
    },
    required=["message_id"],
)
async def get_context(ctx: Ctx, message_id: str, span: int = 4):
    m = re.search(r"\d{10,21}", str(message_id))
    if not m:
        return "that isn't a message id"
    span = _int(span, 4, 1, 8)
    got = await _to_thread(ctx.mem.around, int(m.group()), span, span)
    if not got or got["scope"] != ctx.scope:
        return "don't have that message"
    allow = ctx.visible_channel_ids()
    if allow is not None and got["channel_id"] not in allow:
        return "that's in a channel you can't see"
    return f"around that message in {ctx.channel_label(got['channel_id'])}:\n" + "\n".join(
        f"[{_fmt_ts(r['ts'])}] {r['author']}: {r['content'][:200]}" for r in got["rows"]
    )


# ---------------------------------------------------------------- util skills


@skill(
    "util",
    "Get the current date and time (needed for anything about 'today', 'now', or a timezone).",
    {"timezone": P("string", "IANA name like America/New_York. Default UTC.")},
)
async def current_time(ctx: Ctx, timezone: str = "UTC"):  # noqa: A002 - name is part of the tool schema
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        tz = ZoneInfo(str(timezone or "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        return "unknown timezone, use something like America/New_York"
    now = datetime.now(tz)
    return f"{now:%A, %B %d %Y %H:%M} {now.tzname()} ({tz.key})"


_BIN = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UN = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {
    "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan, "log": math.log,
    "log10": math.log10, "log2": math.log2, "abs": abs, "round": round, "floor": math.floor,
    "ceil": math.ceil, "min": min, "max": max,
}
_CONST = {"pi": math.pi, "e": math.e}


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.Name) and node.id in _CONST:
        return _CONST[node.id]
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UN:
        return _UN[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow):
            if abs(right) > 10000:
                raise ValueError("exponent too big")
            if isinstance(left, int) and isinstance(right, int) and right > 0 and abs(left) > 1:
                if right * math.log10(abs(left)) > 400:
                    raise ValueError("result too big")
        return _BIN[type(node.op)](left, right)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
        return _FUNCS[node.func.id](*[_eval(a) for a in node.args])
    raise ValueError("unsupported expression")


@skill(
    "util",
    "Calculate a math expression exactly (+ - * / // % ** parentheses, sqrt, sin, cos, tan, log, abs, round, pi, e).",
    {"expression": P("string", "e.g. '(12.5 * 8) / 3'")},
    required=["expression"],
)
async def calculate(ctx: Ctx, expression: str):
    expr = str(expression).strip()[:200].replace("^", "**").replace("×", "*").replace("÷", "/")
    try:
        result = _eval(ast.parse(expr, mode="eval"))
    except ZeroDivisionError:
        return "can't divide by zero"
    except (ValueError, SyntaxError, TypeError, OverflowError, RecursionError) as e:
        return f"couldn't calculate that ({e})"
    if isinstance(result, float):
        result = int(result) if result.is_integer() and abs(result) < 1e15 else round(result, 10)
    return str(result)


# ---------------------------------------------------------------- media skills (Pillow does the work)


def _need_pil():
    if not HAVE_PIL:
        raise ValueError("image tools aren't installed on this box (Pillow missing)")


async def _load_image(ctx: Ctx):
    att = await ctx.find_image()
    if att is None:
        return "no image found. attach one, reply to one, or post it in the last few messages"
    if att.size and att.size > MAX_IMAGE_BYTES:
        return f"that image is too big ({att.size // (1024 * 1024)}MB, my limit is {MAX_IMAGE_BYTES // (1024 * 1024)}MB)"
    try:
        return await att.read(), att.filename
    except discord.HTTPException:
        return "couldn't download that image"


def _open(data: bytes):
    _need_pil()
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        raise ValueError("couldn't read that as an image")
    if im.width * im.height > MAX_IMAGE_PIXELS:
        raise ValueError("that image has way too many pixels for me")
    return im


def _frames(im):
    n = getattr(im, "n_frames", 1)
    if n > MAX_FRAMES:
        raise ValueError(f"too many frames ({n}, my limit is {MAX_FRAMES})")
    if im.width * im.height * n > FRAME_PIXEL_BUDGET:
        raise ValueError("that animation is too heavy for me, try a smaller one")
    base_dur = im.info.get("duration", 100)
    out = []
    for frame in ImageSequence.Iterator(im):
        out.append((frame.convert("RGBA"), frame.info.get("duration", base_dur)))
    return out


def _encode(frames, fmt: str):
    buf = io.BytesIO()
    imgs = [f for f, _ in frames]
    durs = [max(int(d or 100), 20) for _, d in frames]
    animated = len(imgs) > 1
    note = ""
    anim_kwargs = {"save_all": True, "append_images": imgs[1:], "duration": durs, "loop": 0} if animated else {}
    if fmt == "gif":
        imgs[0].save(buf, "GIF", disposal=2, **anim_kwargs)
    elif fmt == "png":
        imgs[0].save(buf, "PNG", optimize=True, **anim_kwargs)
    elif fmt == "webp":
        imgs[0].save(buf, "WEBP", quality=85, **anim_kwargs)
    elif fmt == "jpg":
        flat = Image.new("RGB", imgs[0].size, (255, 255, 255))
        flat.paste(imgs[0], mask=imgs[0].getchannel("A"))
        flat.save(buf, "JPEG", quality=90, optimize=True)
        if animated:
            note = "jpg can't animate so it only kept the first frame."
    else:
        raise ValueError(f"can't write {fmt}")
    return buf.getvalue(), note


def _canon(fmt: str) -> str:
    fmt = (fmt or "").lower().lstrip(".")
    return {"jpeg": "jpg"}.get(fmt, fmt)


def _process(data: bytes, filename: str, fmt: str | None, resize: tuple | None):
    """Runs in a worker thread. Returns (bytes, out_filename, note)."""
    im = _open(data)
    src_fmt = _canon(im.format or "png")
    target = fmt or (src_fmt if src_fmt in ("gif", "png", "jpg", "webp") else "png")
    frames = _frames(im)
    suffix = ""
    if resize:
        width, height, scale = resize
        w, h = im.size
        if scale:
            nw, nh = w * scale / 100, h * scale / 100
        elif width and height:
            ratio = min(width / w, height / h)
            nw, nh = w * ratio, h * ratio
        elif width:
            nw, nh = width, h * width / w
        else:
            nw, nh = w * height / h, height
        nw, nh = max(1, min(4096, round(nw))), max(1, min(4096, round(nh)))
        frames = [(f.resize((nw, nh), Image.LANCZOS), d) for f, d in frames]
        suffix = f"_{nw}x{nh}"
    out, note = _encode(frames, target)
    stem = re.sub(r"[^\w.-]+", "_", Path(filename or "image").stem)[:60] or "image"
    return out, f"{stem}{suffix}.{target}", note


def _attach(ctx: Ctx, data: bytes, name: str, note: str) -> str:
    if len(data) > ctx.file_limit:
        return f"result is {len(data) // 1024}KB which is over this server's upload limit, try a smaller size"
    ctx.files.append((name, data))
    return f"made {name} ({max(len(data) // 1024, 1)} KB), it gets attached to your reply. {note}".strip()


@skill(
    "media",
    "Describe the user's image (attached, replied-to, or most recent in chat): format, size, frames, file size.",
)
async def image_info(ctx: Ctx):
    got = await _load_image(ctx)
    if isinstance(got, str):
        return got
    data, name = got

    def work():
        im = _open(data)
        return f"{name}: {im.format}, {im.width}x{im.height}, mode {im.mode}, {getattr(im, 'n_frames', 1)} frame(s), {len(data) // 1024} KB"

    try:
        return await _to_thread(work)
    except ValueError as e:
        return str(e)


@skill(
    "media",
    "Convert the user's image (attached, replied-to, or most recent in chat) to another format. "
    "Use format=gif to turn an image into a gif. Animated gif/webp stay animated where the target allows.",
    {"format": P("string", "target format", enum=["gif", "png", "jpg", "webp"])},
    required=["format"],
)
async def convert_image(ctx: Ctx, format: str):  # noqa: A002 - name is part of the tool schema
    fmt = _canon(str(format))
    if fmt not in ("gif", "png", "jpg", "webp"):
        return "i can do gif, png, jpg or webp"
    got = await _load_image(ctx)
    if isinstance(got, str):
        return got
    data, name = got
    try:
        out, outname, note = await _to_thread(_process, data, name, fmt, None)
    except ValueError as e:
        return str(e)
    return _attach(ctx, out, outname, note)


@skill(
    "media",
    "Resize the user's image (attached, replied-to, or most recent in chat). Keeps aspect ratio. "
    "Give scale_percent, or width and/or height in pixels (both = fit inside that box).",
    {
        "width": P("integer", "target width in px"),
        "height": P("integer", "target height in px"),
        "scale_percent": P("integer", "scale by this percent, e.g. 50 for half size"),
    },
)
async def resize_image(ctx: Ctx, width: int = None, height: int = None, scale_percent: int = None):
    w = _int(width, 0, 0, 4096)
    h = _int(height, 0, 0, 4096)
    s = _int(scale_percent, 0, 0, 1000)
    if not (w or h or s):
        return "need a width, height or scale_percent"
    got = await _load_image(ctx)
    if isinstance(got, str):
        return got
    data, name = got
    try:
        out, outname, note = await _to_thread(_process, data, name, None, (w, h, s))
    except ValueError as e:
        return str(e)
    return _attach(ctx, out, outname, note)


# ---------------------------------------------------------------- owner-only python runner


@skill(
    "owner",
    "Run a short python script (3.x, Pillow installed). Printed output comes back; files it writes in the "
    "working dir get attached. If the user attached an image it is saved as input.<ext> there. Owner only.",
    {"code": P("string", "python source code")},
    required=["code"],
    owner_only=True,
)
async def run_python(ctx: Ctx, code: str):
    """NOT a real sandbox. It runs with an empty environment (no tokens), a temp working dir, a 20s timeout and
    CPU/memory/file-size limits, but it can still read files on the host. That's why it only works for OWNER_ID
    and only when ENABLE_RUN_PYTHON=1. Don't open it up to other users on a box that holds secrets."""
    if not ctx.owner_ok:
        return "not allowed"
    code = str(code)[:8000]
    mem_mb = _int(os.getenv("RUN_PYTHON_MEM_MB"), 600, 128, 2048)
    runner = (
        "import resource, runpy, sys\n"
        "resource.setrlimit(resource.RLIMIT_CPU, (15, 15))\n"
        f"resource.setrlimit(resource.RLIMIT_AS, ({mem_mb} * 1024 * 1024,) * 2)\n"
        "resource.setrlimit(resource.RLIMIT_FSIZE, (25 * 1024 * 1024,) * 2)\n"
        "sys.argv = ['main.py']\n"
        "runpy.run_path('main.py', run_name='__main__')\n"
    )
    img = await ctx.find_image(scan_history=False)
    with tempfile.TemporaryDirectory(prefix="yuki-") as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "main.py").write_text(code, encoding="utf-8")
        (tmp_path / "runner.py").write_text(runner, encoding="utf-8")
        skip = {"main.py", "runner.py"}
        if img is not None and (not img.size or img.size <= MAX_IMAGE_BYTES):
            ext = Path(img.filename).suffix.lower() or ".png"
            (tmp_path / f"input{ext}").write_bytes(await img.read())
            skip.add(f"input{ext}")
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": tmp,
            "TMPDIR": tmp,
            "LANG": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
        }
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "runner.py",
            cwd=tmp, env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            raw, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
            return "script timed out after 20s and got killed"
        out = raw.decode("utf-8", "replace").strip()
        if len(out) > 2000:
            out = out[:1000] + "\n...[trimmed]...\n" + out[-1000:]
        attached = []
        for p in sorted(tmp_path.iterdir()):
            if p.name in skip or not p.is_file() or len(attached) >= 3:
                continue
            if p.stat().st_size <= ctx.file_limit:
                ctx.files.append((p.name, p.read_bytes()))
                attached.append(p.name)
        result = f"exit code {proc.returncode}\n{out or '(no output)'}"
        if attached:
            result += "\nattached files: " + ", ".join(attached)
        return result
