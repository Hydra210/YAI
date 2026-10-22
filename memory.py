"""Persistent memory for Yuki.

Stores everything in one SQLite file:
  - messages  : a log of what people said (searchable with FTS5 for recall)
  - profiles  : learned notes + "how this person talks", per user per server
  - scopes    : learned server vibe / slang, per server
  - optouts   : users who asked Yuki to forget them

"scope" is the server id. DMs use -user_id so one person's DMs never leak to anyone else.
All methods are synchronous and thread-safe; call them with asyncio.to_thread from the bot.
"""

import json
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path

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

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY,
    scope      INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    author     TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    ts         INTEGER NOT NULL,
    is_bot     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_user    ON messages(scope, user_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_ts      ON messages(ts);

CREATE TABLE IF NOT EXISTS profiles (
    scope   INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    name    TEXT    NOT NULL DEFAULT '',
    notes   TEXT    NOT NULL DEFAULT '[]',
    style   TEXT    NOT NULL DEFAULT '',
    pending INTEGER NOT NULL DEFAULT 0,
    updated INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, user_id)
);

CREATE TABLE IF NOT EXISTS scopes (
    scope   INTEGER PRIMARY KEY,
    vibe    TEXT    NOT NULL DEFAULT '{}',
    pending INTEGER NOT NULL DEFAULT 0,
    updated INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS optouts (
    user_id INTEGER PRIMARY KEY
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content, content='messages', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF content ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


class Memory:
    def __init__(self, path: str, retention_days: int = 90):
        parent = Path(path).resolve().parent
        parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.fts = True
        self._init_schema()
        self.optouts = self._load_optouts()
        log.info("memory ready at %s (fts=%s, %d opt-outs)", path, self.fts, len(self.optouts))

    # ---------- setup ----------

    def _init_schema(self):
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(BASE_SCHEMA)
            try:
                self.conn.executescript(FTS_SCHEMA)
            except sqlite3.OperationalError:
                self.fts = False
                log.warning("FTS5 not available, recall will use slower LIKE search")
            self.conn.commit()

    def _load_optouts(self) -> set:
        with self.lock:
            return {r["user_id"] for r in self.conn.execute("SELECT user_id FROM optouts")}

    # ---------- message log ----------

    def add_message(self, msg_id, scope, channel_id, user_id, author, content, is_bot=False) -> bool:
        content = (content or "").strip()[:MAX_CHARS]
        if not content:
            return False
        if not is_bot and user_id in self.optouts:
            return False
        now = int(time.time())
        with self.lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO messages(id, scope, channel_id, user_id, author, content, ts, is_bot) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (msg_id, scope, channel_id, user_id, author, content, now, int(is_bot)),
            )
            if cur.rowcount and not is_bot:
                self.conn.execute(
                    "INSERT INTO profiles(scope, user_id, name, pending) VALUES (?, ?, ?, 1) "
                    "ON CONFLICT(scope, user_id) DO UPDATE SET name = excluded.name, pending = pending + 1",
                    (scope, user_id, author),
                )
                if scope > 0:
                    self.conn.execute(
                        "INSERT INTO scopes(scope, pending) VALUES (?, 1) "
                        "ON CONFLICT(scope) DO UPDATE SET pending = pending + 1",
                        (scope,),
                    )
            self.conn.commit()
        return True

    def edit_message(self, msg_id, content):
        content = (content or "").strip()[:MAX_CHARS]
        if not content:
            return
        with self.lock:
            self.conn.execute("UPDATE messages SET content = ? WHERE id = ?", (content, msg_id))
            self.conn.commit()

    def delete_message(self, msg_id):
        with self.lock:
            self.conn.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
            self.conn.commit()

    def recent(self, channel_id, limit=15) -> list:
        """Last `limit` messages in a channel, oldest first."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, user_id, author, content, ts, is_bot FROM messages "
                "WHERE channel_id = ? ORDER BY id DESC LIMIT ?",
                (channel_id, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def user_messages(self, scope, user_id, limit=10) -> list:
        """A user's latest messages in a scope, newest first."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, author, content, ts FROM messages "
                "WHERE scope = ? AND user_id = ? AND is_bot = 0 ORDER BY id DESC LIMIT ?",
                (scope, user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def scope_sample(self, scope, limit=60) -> list:
        """Latest messages from anyone in a scope, oldest first."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT author, content FROM messages WHERE scope = ? AND is_bot = 0 "
                "ORDER BY id DESC LIMIT ?",
                (scope, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    # ---------- recall ----------

    @staticmethod
    def _terms(query: str) -> list:
        words = re.findall(r"[\w']{3,}", (query or "").lower())
        out = []
        for w in words:
            w = w.strip("'")
            if len(w) >= 3 and w not in STOPWORDS and w not in out:
                out.append(w)
        return out[:8]

    def search(self, scope, query, limit=8) -> list:
        terms = self._terms(query)
        if not terms:
            return []
        try:
            with self.lock:
                if self.fts:
                    match = " OR ".join(f'"{t}"' for t in terms)
                    rows = self.conn.execute(
                        "SELECT m.id, m.user_id, m.author, m.content, m.ts "
                        "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
                        "WHERE messages_fts MATCH ? AND m.scope = ? AND m.is_bot = 0 "
                        "ORDER BY bm25(messages_fts) LIMIT ?",
                        (match, scope, limit),
                    ).fetchall()
                else:
                    clause = " OR ".join("m.content LIKE ?" for _ in terms)
                    rows = self.conn.execute(
                        "SELECT m.id, m.user_id, m.author, m.content, m.ts FROM messages m "
                        f"WHERE m.scope = ? AND m.is_bot = 0 AND ({clause}) ORDER BY m.id DESC LIMIT ?",
                        (scope, *[f"%{t}%" for t in terms], limit),
                    ).fetchall()
        except sqlite3.OperationalError:
            log.exception("memory search failed")
            return []
        return [dict(r) for r in rows]

    # ---------- profiles (learned) ----------

    def get_profile(self, scope, user_id):
        with self.lock:
            row = self.conn.execute(
                "SELECT name, notes, style FROM profiles WHERE scope = ? AND user_id = ?",
                (scope, user_id),
            ).fetchone()
        if not row:
            return None
        try:
            notes = json.loads(row["notes"] or "[]")
        except json.JSONDecodeError:
            notes = []
        return {"name": row["name"], "notes": notes, "style": row["style"]}

    def next_profile_job(self, min_pending):
        with self.lock:
            row = self.conn.execute(
                "SELECT scope, user_id, name, notes, style, pending FROM profiles "
                "WHERE pending >= ? ORDER BY pending DESC LIMIT 1",
                (min_pending,),
            ).fetchone()
        if not row:
            return None
        job = dict(row)
        try:
            job["notes"] = json.loads(job["notes"] or "[]")
        except json.JSONDecodeError:
            job["notes"] = []
        return job

    def save_profile(self, scope, user_id, notes=None, style=None):
        """Save learned notes/style and clear the pending counter. Pass None to only reset pending."""
        with self.lock:
            if notes is not None and style is not None:
                self.conn.execute(
                    "UPDATE profiles SET notes = ?, style = ?, pending = 0, updated = ? "
                    "WHERE scope = ? AND user_id = ?",
                    (json.dumps(notes), style, int(time.time()), scope, user_id),
                )
            else:
                self.conn.execute(
                    "UPDATE profiles SET pending = 0 WHERE scope = ? AND user_id = ?",
                    (scope, user_id),
                )
            self.conn.commit()

    # ---------- server vibe (learned) ----------

    def get_vibe(self, scope) -> dict:
        with self.lock:
            row = self.conn.execute("SELECT vibe FROM scopes WHERE scope = ?", (scope,)).fetchone()
        if not row:
            return {}
        try:
            return json.loads(row["vibe"] or "{}")
        except json.JSONDecodeError:
            return {}

    def next_scope_job(self, min_pending):
        with self.lock:
            row = self.conn.execute(
                "SELECT scope, vibe, pending FROM scopes WHERE pending >= ? ORDER BY pending DESC LIMIT 1",
                (min_pending,),
            ).fetchone()
        if not row:
            return None
        job = dict(row)
        try:
            job["vibe"] = json.loads(job["vibe"] or "{}")
        except json.JSONDecodeError:
            job["vibe"] = {}
        return job

    def save_vibe(self, scope, vibe=None):
        with self.lock:
            if vibe is not None:
                self.conn.execute(
                    "UPDATE scopes SET vibe = ?, pending = 0, updated = ? WHERE scope = ?",
                    (json.dumps(vibe), int(time.time()), scope),
                )
            else:
                self.conn.execute("UPDATE scopes SET pending = 0 WHERE scope = ?", (scope,))
            self.conn.commit()

    # ---------- privacy ----------

    def forget_user(self, user_id) -> int:
        """Delete everything about a user and stop remembering them. Returns messages deleted."""
        with self.lock:
            n = self.conn.execute(
                "DELETE FROM messages WHERE user_id = ? AND is_bot = 0", (user_id,)
            ).rowcount
            # their DM scope is private to them, so clear Yuki's side of it too
            self.conn.execute("DELETE FROM messages WHERE scope = ?", (-user_id,))
            self.conn.execute("DELETE FROM profiles WHERE user_id = ?", (user_id,))
            self.conn.execute("INSERT OR IGNORE INTO optouts(user_id) VALUES (?)", (user_id,))
            self.conn.commit()
        self.optouts.add(user_id)
        return n

    def resume_user(self, user_id):
        with self.lock:
            self.conn.execute("DELETE FROM optouts WHERE user_id = ?", (user_id,))
            self.conn.commit()
        self.optouts.discard(user_id)

    def user_stats(self, scope, user_id) -> dict:
        with self.lock:
            count = self.conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE scope = ? AND user_id = ? AND is_bot = 0",
                (scope, user_id),
            ).fetchone()["c"]
        return {"messages": count, "opted_out": user_id in self.optouts}

    # ---------- housekeeping ----------

    def prune(self) -> int:
        cutoff = int(time.time()) - self.retention_days * 86400
        with self.lock:
            n = self.conn.execute("DELETE FROM messages WHERE ts < ?", (cutoff,)).rowcount
            self.conn.commit()
        if n:
            log.info("pruned %d old messages", n)
        return n
