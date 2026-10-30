"""Persistent memory for Yuki, stored in Postgres (set DATABASE_URL).

Everything Yuki remembers lives in Postgres, nothing in a local file, so it survives Render restarts and deploys:
  - messages  : a log of what people said (full-text searchable for recall)
  - profiles  : learned notes + "how this person talks" + server role tier, per user per server
  - scopes    : learned server vibe / slang, per server
  - facts     : "fast memory": per-server facts (rules, roles, notes...) that are also cached in RAM at startup,
                so Yuki knows them instantly without any lookup
  - mod_log   : when somebody broke a rule (no message text is kept, just what/when)
  - optouts   : users who asked Yuki to forget them

"scope" is the server id. DMs use -user_id so one person's DMs never leak to anyone else.
All methods are synchronous and thread-safe; call them with asyncio.to_thread from the bot.
The fast_* readers only touch RAM and are safe to call straight from async code.
"""

import json
import logging
import re
import threading
import time
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

log = logging.getLogger("yuki.memory")

MAX_CHARS = 500  # per stored message

STOPWORDS = {
    "the", "and", "for", "that", "this", "with", "you", "your", "was", "were", "are", "but", "not",
    "have", "has", "had", "what", "when", "where", "who", "why", "how", "did", "does", "say", "said",
    "says", "about", "yuki", "from", "they", "them", "their", "its", "can", "could", "would", "should",
    "will", "just", "like", "know", "remember", "tell", "told", "ago", "earlier", "before", "last",
    "time", "any", "some", "there", "here", "then", "than", "too", "very", "got", "get", "his", "her",
    "him", "she", "out", "all", "one", "yes", "yeah", "lol", "lmao", "bro", "dude", "ever",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         BIGINT PRIMARY KEY,
    scope      BIGINT   NOT NULL,
    channel_id BIGINT   NOT NULL,
    user_id    BIGINT   NOT NULL,
    author     TEXT     NOT NULL,
    content    TEXT     NOT NULL,
    ts         BIGINT   NOT NULL,
    is_bot     SMALLINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_user    ON messages(scope, user_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_ts      ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_messages_fts     ON messages USING GIN (to_tsvector('simple', content));

CREATE TABLE IF NOT EXISTS profiles (
    scope    BIGINT NOT NULL,
    user_id  BIGINT NOT NULL,
    name     TEXT   NOT NULL DEFAULT '',
    username TEXT   NOT NULL DEFAULT '',
    notes    TEXT   NOT NULL DEFAULT '[]',
    style    TEXT   NOT NULL DEFAULT '',
    tier     TEXT   NOT NULL DEFAULT '',
    top_role TEXT   NOT NULL DEFAULT '',
    pending  INTEGER NOT NULL DEFAULT 0,
    updated  BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, user_id)
);

CREATE TABLE IF NOT EXISTS scopes (
    scope   BIGINT PRIMARY KEY,
    vibe    TEXT   NOT NULL DEFAULT '{}',
    topics  TEXT   NOT NULL DEFAULT '[]',
    pending INTEGER NOT NULL DEFAULT 0,
    updated BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS mood_states (
    scope   BIGINT PRIMARY KEY,
    state   TEXT NOT NULL DEFAULT '{}',
    updated BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS facts (
    scope   BIGINT NOT NULL,
    key     TEXT   NOT NULL,
    value   TEXT   NOT NULL,
    updated BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, key)
);

CREATE TABLE IF NOT EXISTS mod_log (
    id         BIGSERIAL PRIMARY KEY,
    scope      BIGINT NOT NULL,
    user_id    BIGINT NOT NULL,
    category   TEXT   NOT NULL,
    rule_n     INTEGER,
    channel_id BIGINT,
    message_id BIGINT,
    ts         BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mod_log_user ON mod_log(scope, user_id, ts);

CREATE TABLE IF NOT EXISTS optouts (
    user_id BIGINT PRIMARY KEY
);

-- Explicitly saved personal notes shared by the Discord bot and Yuki Cloud.
CREATE TABLE IF NOT EXISTS yuki_personal_memories (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'discord',
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
    UNIQUE(user_id, content)
);
CREATE INDEX IF NOT EXISTS yuki_personal_memories_user ON yuki_personal_memories(user_id, updated_at DESC);
"""


def _json(raw, default):
    try:
        return json.loads(raw) if raw else default
    except (json.JSONDecodeError, TypeError):
        return default


class Memory:
    def __init__(self, database_url: str, retention_days: int = 90, pool_max: int = 5):
        if not (database_url or "").strip():
            raise RuntimeError(
                "DATABASE_URL is not set. Yuki's memory lives in Postgres only (no local fallback), "
                "so set DATABASE_URL to a Postgres connection string."
            )
        self.retention_days = retention_days
        self.slots = threading.BoundedSemaphore(pool_max)  # the pool raises when full instead of waiting
        self.pool = self._open_pool(database_url.strip(), pool_max)
        def initialize(cur):
            cur.execute(SCHEMA)
            # Safe migration for databases created before topic-learning was added.
            cur.execute("ALTER TABLE scopes ADD COLUMN IF NOT EXISTS topics TEXT NOT NULL DEFAULT '[]'")
        self._do(initialize)
        self.optouts = {r["user_id"] for r in self._do(self._fetch("SELECT user_id FROM optouts"))}
        self.fast: dict[int, dict[str, Any]] = {}
        for r in self._do(self._fetch("SELECT scope, key, value FROM facts")):
            self.fast.setdefault(r["scope"], {})[r["key"]] = _json(r["value"], None)
        log.info(
            "memory ready on postgres (%d opt-outs, fast memory for %d servers)",
            len(self.optouts), len(self.fast),
        )

    # ---------- connection handling ----------

    @staticmethod
    def _open_pool(url: str, pool_max: int):
        last = None
        for attempt in range(1, 5):
            try:
                return ThreadedConnectionPool(
                    1, pool_max, url,
                    connect_timeout=10,
                    keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
                )
            except psycopg2.OperationalError as e:
                last = e
                log.warning("postgres not reachable yet (attempt %d/4): %s", attempt, str(e).strip()[:120])
                time.sleep(2 * attempt)
        raise RuntimeError(f"couldn't connect to Postgres: {last}")

    def _do(self, work, retries: int = 1):
        """Run work(cursor) in one transaction. Retries once on a dropped connection (Neon/Supabase idle drops)."""
        for attempt in range(retries + 1):
            self.slots.acquire()
            conn = None
            bad = False
            try:
                conn = self.pool.getconn()
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    result = work(cur)
                conn.commit()
                return result
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                bad = True
                if attempt == retries:
                    raise
                log.warning("postgres connection dropped (%s), retrying", type(e).__name__)
            except Exception:
                try:
                    if conn is not None:
                        conn.rollback()
                except Exception:
                    bad = True
                raise
            finally:
                try:
                    if conn is not None:
                        self.pool.putconn(conn, close=bad)
                finally:
                    self.slots.release()

    @staticmethod
    def _fetch(sql, args=()):
        def work(cur):
            cur.execute(sql, args)
            return cur.fetchall()

        return work

    def _all(self, sql, args=()):
        return [dict(r) for r in self._do(self._fetch(sql, args))]

    def _one(self, sql, args=()):
        rows = self._do(self._fetch(sql, args))
        return dict(rows[0]) if rows else None

    def _exec(self, sql, args=()) -> int:
        def work(cur):
            cur.execute(sql, args)
            return cur.rowcount

        return self._do(work)

    # ---------- message log ----------

    def add_message(
        self, msg_id, scope, channel_id, user_id, author, content, is_bot=False,
        tier="", top_role="", username="",
    ) -> bool:
        content = (content or "").strip()[:MAX_CHARS].replace("\x00", "")
        if not content:
            return False
        if not is_bot and user_id in self.optouts:
            return False
        now = int(time.time())

        def work(cur):
            cur.execute(
                "INSERT INTO messages(id, scope, channel_id, user_id, author, content, ts, is_bot) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (msg_id, scope, channel_id, user_id, author, content, now, int(is_bot)),
            )
            if cur.rowcount and not is_bot:
                cur.execute(
                    "INSERT INTO profiles(scope, user_id, name, username, tier, top_role, pending) "
                    "VALUES (%s, %s, %s, %s, %s, %s, 1) "
                    "ON CONFLICT (scope, user_id) DO UPDATE SET "
                    " name = EXCLUDED.name, pending = profiles.pending + 1, "
                    " username = CASE WHEN EXCLUDED.username <> '' THEN EXCLUDED.username ELSE profiles.username END, "
                    " tier = CASE WHEN EXCLUDED.tier <> '' THEN EXCLUDED.tier ELSE profiles.tier END, "
                    " top_role = CASE WHEN EXCLUDED.tier <> '' THEN EXCLUDED.top_role ELSE profiles.top_role END",
                    (scope, user_id, author, username or "", tier or "", top_role or ""),
                )
                if scope > 0:
                    cur.execute(
                        "INSERT INTO scopes(scope, pending) VALUES (%s, 1) "
                        "ON CONFLICT (scope) DO UPDATE SET pending = scopes.pending + 1",
                        (scope,),
                    )

        self._do(work)
        return True

    def edit_message(self, msg_id, content):
        content = (content or "").strip()[:MAX_CHARS].replace("\x00", "")
        if content:
            self._exec("UPDATE messages SET content = %s WHERE id = %s", (content, msg_id))

    def delete_message(self, msg_id):
        self._exec("DELETE FROM messages WHERE id = %s", (msg_id,))

    def recent(self, channel_id, limit=15) -> list:
        """Last `limit` messages in a channel, oldest first."""
        rows = self._all(
            "SELECT id, user_id, author, content, ts, is_bot FROM messages "
            "WHERE channel_id = %s ORDER BY id DESC LIMIT %s",
            (channel_id, limit),
        )
        return list(reversed(rows))

    def user_messages(self, scope, user_id, limit=10) -> list:
        """A user's latest messages in a scope, newest first."""
        return self._all(
            "SELECT id, channel_id, author, content, ts FROM messages "
            "WHERE scope = %s AND user_id = %s AND is_bot = 0 ORDER BY id DESC LIMIT %s",
            (scope, user_id, limit),
        )

    def scope_sample(self, scope, limit=60) -> list:
        """Latest messages from anyone in a scope, oldest first."""
        rows = self._all(
            "SELECT author, content FROM messages WHERE scope = %s AND is_bot = 0 ORDER BY id DESC LIMIT %s",
            (scope, limit),
        )
        return list(reversed(rows))

    # ---------- recall ----------

    @staticmethod
    def _stem(w: str) -> str:
        for suf in ("ing", "ed", "es", "s"):
            if w.endswith(suf) and len(w) - len(suf) >= 3:
                return w[: -len(suf)]
        return w

    @staticmethod
    def _terms(query: str) -> list:
        out = []
        for w in re.findall(r"[^\W_]{3,}", (query or "").lower()):
            if w not in STOPWORDS and w not in out:
                out.append(w)
        return out[:8]

    def search(
        self, scope, query, limit=8, *, user_id=None, channel_id=None, since=None, until=None, allow=None
    ) -> list:
        """Keyword search over saved messages in a scope.

        user_id / channel_id / since / until (unix seconds) narrow it down. With no keywords but at least
        one filter it just lists the newest matches. `allow` is a set of channel ids the caller may see
        (None = no restriction); anything outside it is dropped so private channels never leak.
        """
        if allow is not None and not allow:
            return []
        terms = self._terms(query)
        filt, args = ["m.scope = %s", "m.is_bot = 0"], [scope]
        if user_id is not None:
            filt.append("m.user_id = %s")
            args.append(user_id)
        if channel_id is not None:
            filt.append("m.channel_id = %s")
            args.append(channel_id)
        if since is not None:
            filt.append("m.ts >= %s")
            args.append(since)
        if until is not None:
            filt.append("m.ts <= %s")
            args.append(until)
        if allow is not None:
            filt.append("m.channel_id = ANY(%s::bigint[])")
            args.append(list(allow))
        where = " AND ".join(filt)
        cols = "m.id, m.channel_id, m.user_id, m.author, m.content, m.ts"
        try:
            if not terms:
                if len(filt) == 2:  # nothing to search for and nothing to filter by
                    return []
                return self._all(
                    f"SELECT {cols} FROM messages m WHERE {where} ORDER BY m.id DESC LIMIT %s", (*args, limit)
                )
            tsq = " | ".join(f"{self._stem(t)}:*" for t in terms)
            return self._all(
                f"SELECT {cols} FROM messages m "
                f"WHERE to_tsvector('simple', m.content) @@ to_tsquery('simple', %s) AND {where} "
                f"ORDER BY ts_rank(to_tsvector('simple', m.content), to_tsquery('simple', %s)) DESC, m.id DESC "
                f"LIMIT %s",
                (tsq, *args, tsq, limit),
            )
        except psycopg2.Error:
            log.exception("memory search failed")
            return []

    def find_user(self, scope, name, limit=5, exact=False) -> list:
        """People Yuki has seen in this scope whose last-known display name or username matches `name`.
        exact=True skips the partial-match fallback (used before pinging anyone)."""
        n = (name or "").strip().lstrip("@").lower()
        if not n:
            return []
        rows = self._all(
            "SELECT user_id, name FROM profiles WHERE scope = %s AND (lower(name) = %s OR lower(username) = %s) "
            "LIMIT %s",
            (scope, n, n, limit),
        )
        if rows or exact:
            return rows
        like = n.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return self._all(
            "SELECT user_id, name FROM profiles WHERE scope = %s AND (lower(name) LIKE %s OR lower(username) LIKE %s) "
            "ORDER BY updated DESC, pending DESC LIMIT %s",
            (scope, f"%{like}%", f"%{like}%", limit),
        )

    def around(self, msg_id, before=3, after=3):
        """The saved messages around one message, oldest first (includes Yuki's own replies)."""
        base = self._one(
            "SELECT id, scope, channel_id, author, content, ts FROM messages WHERE id = %s", (msg_id,)
        )
        if not base:
            return None
        prev = self._all(
            "SELECT id, author, content, ts FROM messages WHERE channel_id = %s AND id < %s "
            "ORDER BY id DESC LIMIT %s",
            (base["channel_id"], msg_id, before),
        )
        nxt = self._all(
            "SELECT id, author, content, ts FROM messages WHERE channel_id = %s AND id > %s "
            "ORDER BY id ASC LIMIT %s",
            (base["channel_id"], msg_id, after),
        )
        rows = list(reversed(prev)) + [base] + nxt
        return {"scope": base["scope"], "channel_id": base["channel_id"], "rows": rows}

    # ---------- profiles (learned) ----------

    def get_profile(self, scope, user_id):
        row = self._one(
            "SELECT name, notes, style, tier, top_role FROM profiles WHERE scope = %s AND user_id = %s",
            (scope, user_id),
        )
        if not row:
            return None
        row["notes"] = _json(row["notes"], [])
        return row

    def next_profile_job(self, min_pending, priority_ids=None, priority_min=None):
        """The next person to (re)learn. People in priority_ids are picked up sooner (priority_min messages)."""
        pids = list(priority_ids or [])
        pmin = priority_min if priority_min is not None else min_pending
        row = self._one(
            "SELECT scope, user_id, name, notes, style, pending FROM profiles "
            "WHERE pending >= %s OR (user_id = ANY(%s::bigint[]) AND pending >= %s) "
            "ORDER BY (user_id = ANY(%s::bigint[])) DESC, pending DESC LIMIT 1",
            (min_pending, pids, pmin, pids),
        )
        if not row:
            return None
        row["notes"] = _json(row["notes"], [])
        return row

    def save_profile(self, scope, user_id, notes=None, style=None):
        """Save learned notes/style and clear the pending counter. Pass None to only reset pending."""
        if notes is not None and style is not None:
            self._exec(
                "UPDATE profiles SET notes = %s, style = %s, pending = 0, updated = %s "
                "WHERE scope = %s AND user_id = %s",
                (json.dumps(notes), style, int(time.time()), scope, user_id),
            )
        else:
            self._exec("UPDATE profiles SET pending = 0 WHERE scope = %s AND user_id = %s", (scope, user_id))

    # ---------- server vibe (learned) ----------

    def get_vibe(self, scope) -> dict:
        row = self._one("SELECT vibe, topics FROM scopes WHERE scope = %s", (scope,))
        if not row:
            return {}
        vibe = _json(row["vibe"], {})
        vibe["topics"] = _json(row.get("topics"), [])
        return vibe

    def next_scope_job(self, min_pending):
        row = self._one(
            "SELECT scope, vibe, topics, pending FROM scopes WHERE pending >= %s ORDER BY pending DESC LIMIT 1",
            (min_pending,),
        )
        if not row:
            return None
        row["vibe"] = _json(row["vibe"], {})
        row["topics"] = _json(row.get("topics"), [])
        row["vibe"]["topics"] = row["topics"]
        return row

    def save_vibe(self, scope, vibe=None):
        if vibe is not None:
            topics = vibe.get("topics", []) if isinstance(vibe, dict) else []
            core = {k: v for k, v in vibe.items() if k != "topics"} if isinstance(vibe, dict) else {}
            self._exec(
                "UPDATE scopes SET vibe = %s, topics = %s, pending = 0, updated = %s WHERE scope = %s",
                (json.dumps(core), json.dumps(topics), int(time.time()), scope),
            )
        else:
            self._exec("UPDATE scopes SET pending = 0 WHERE scope = %s", (scope,))

    # ---------- persistent simulated emotional state ----------

    _MOOD_DEFAULTS = {
        "happiness": 0, "annoyance": 0, "excitement": 0,
        "embarrassment": 0, "curiosity": 15, "energy": 70,
    }

    def get_mood(self, scope) -> dict:
        """Return persistent mood with gentle time-based settling; never stores real personal traits."""
        now = int(time.time())
        row = self._one("SELECT state, updated FROM mood_states WHERE scope = %s", (scope,))
        state = dict(self._MOOD_DEFAULTS)
        if row:
            state.update(_json(row.get("state"), {}))
            elapsed_hours = max(0, now - int(row.get("updated") or now)) / 3600
            settle = min(30, elapsed_hours * 2.0)
            for key in ("happiness", "annoyance", "excitement", "embarrassment"):
                value = float(state.get(key, 0))
                state[key] = round(value - min(abs(value), settle) * (1 if value > 0 else -1))
            state["energy"] = round(max(20, min(100, float(state.get("energy", 70)) + min(20, elapsed_hours * 1.5))))
            self.save_mood(scope, state, now=now)
        return state

    def save_mood(self, scope, state: dict, now=None):
        clean = {}
        for key, default in self._MOOD_DEFAULTS.items():
            try:
                value = int(state.get(key, default))
                clean[key] = max(0, min(100, value)) if key in ("curiosity", "energy") else max(-100, min(100, value))
            except (TypeError, ValueError):
                clean[key] = default
        self._exec(
            "INSERT INTO mood_states(scope, state, updated) VALUES (%s, %s, %s) "
            "ON CONFLICT (scope) DO UPDATE SET state = EXCLUDED.state, updated = EXCLUDED.updated",
            (scope, json.dumps(clean), int(now or time.time())),
        )

    def adjust_mood(self, scope, changes: dict):
        state = self.get_mood(scope)
        for key, delta in changes.items():
            if key in state:
                try:
                    state[key] = max(-100, min(100, int(state[key]) + int(delta)))
                except (TypeError, ValueError):
                    continue
        self.save_mood(scope, state)
        return state

    # ---------- fast memory (per-server facts, cached in RAM) ----------

    def fast_get(self, scope, key, default=None):
        """RAM only, never touches the database. Safe to call from async code."""
        return self.fast.get(scope, {}).get(key, default)

    def fast_all(self, scope) -> dict:
        return dict(self.fast.get(scope, {}))

    def fast_set(self, scope, key, value):
        """Write to Postgres first, then to the RAM cache."""
        self._exec(
            "INSERT INTO facts(scope, key, value, updated) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (scope, key) DO UPDATE SET value = EXCLUDED.value, updated = EXCLUDED.updated",
            (scope, key, json.dumps(value), int(time.time())),
        )
        self.fast.setdefault(scope, {})[key] = value

    def fast_delete(self, scope, key):
        self._exec("DELETE FROM facts WHERE scope = %s AND key = %s", (scope, key))
        self.fast.get(scope, {}).pop(key, None)

    # ---------- moderation log ----------

    def log_violation(self, scope, user_id, category, rule_n, channel_id, message_id):
        self._exec(
            "INSERT INTO mod_log(scope, user_id, category, rule_n, channel_id, message_id, ts) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (scope, user_id, category, rule_n, channel_id, message_id, int(time.time())),
        )

    def violation_count(self, scope, user_id, within_s=86400) -> int:
        row = self._one(
            "SELECT COUNT(*) AS c FROM mod_log WHERE scope = %s AND user_id = %s AND ts >= %s",
            (scope, user_id, int(time.time()) - within_s),
        )
        return int(row["c"]) if row else 0

    # ---------- privacy ----------

    def forget_user(self, user_id) -> int:
        """Delete everything about a user and stop remembering them. Returns messages deleted."""

        def work(cur):
            cur.execute("DELETE FROM messages WHERE user_id = %s AND is_bot = 0", (user_id,))
            n = cur.rowcount
            # their DM scope is private to them, so clear Yuki's side of it too
            cur.execute("DELETE FROM messages WHERE scope = %s", (-user_id,))
            cur.execute("DELETE FROM profiles WHERE user_id = %s", (user_id,))
            # Explicit personal notes are also erased when the user invokes the existing forget command.
            cur.execute("DELETE FROM yuki_personal_memories WHERE user_id = %s", (user_id,))
            cur.execute("INSERT INTO optouts(user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,))
            return n

        n = self._do(work)
        self.optouts.add(user_id)
        return n

    def resume_user(self, user_id):
        self._exec("DELETE FROM optouts WHERE user_id = %s", (user_id,))
        self.optouts.discard(user_id)

    def user_stats(self, scope, user_id) -> dict:
        row = self._one(
            "SELECT COUNT(*) AS c FROM messages WHERE scope = %s AND user_id = %s AND is_bot = 0",
            (scope, user_id),
        )
        return {"messages": int(row["c"]) if row else 0, "opted_out": user_id in self.optouts}

    # ---------- cross-device personal memories ----------

    def personal_memories(self, user_id, limit=50) -> list:
        """Return only the requesting Discord user's explicitly saved personal notes."""
        rows = self._do(self._fetch(
            "SELECT id, content, source, created_at, updated_at FROM yuki_personal_memories "
            "WHERE user_id = %s ORDER BY updated_at DESC LIMIT %s", (int(user_id), max(1, min(int(limit), 100)))
        ))
        return rows

    def add_personal_memory(self, user_id, content, source="discord") -> dict:
        content = str(content or "").strip()
        if not content or len(content) > 1000:
            raise ValueError("personal memory must contain 1-1000 characters")
        timestamp = int(time.time())
        row = self._one(
            "INSERT INTO yuki_personal_memories(user_id, content, source, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT(user_id, content) "
            "DO UPDATE SET updated_at = EXCLUDED.updated_at RETURNING id, content, source, created_at, updated_at",
            (int(user_id), content, str(source or "discord")[:40], timestamp, timestamp),
        )
        return row or {}

    def delete_personal_memory(self, user_id, content) -> int:
        """Delete a matching note only for its owner; exact case-insensitive match."""
        return self._exec(
            "DELETE FROM yuki_personal_memories WHERE user_id = %s AND lower(content) = lower(%s)",
            (int(user_id), str(content or "").strip()),
        )

    # ---------- housekeeping ----------

    def prune(self) -> int:
        cutoff = int(time.time()) - self.retention_days * 86400
        n = self._exec("DELETE FROM messages WHERE ts < %s", (cutoff,))
        self._exec("DELETE FROM mod_log WHERE ts < %s", (cutoff,))
        if n:
            log.info("pruned %d old messages", n)
        return n
