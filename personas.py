"""Per-person personas for Yuki.

A persona is a short list of standing instructions one person gave Yuki about how to act *with them*
("talk like a pirate", "call me boss", "keep it to one line", "be nicer to me"). They are stored per Discord
user id and only ever injected into the prompt when that same person is the one Yuki is replying to, so one
person's persona never leaks into how he talks to anyone else.

Storage:
  - Postgres only via DATABASE_URL. Missing database configuration is a startup error;
    there is deliberately no SQLite or local-file fallback.

All methods are synchronous and thread safe; call them with asyncio.to_thread from the bot. Reads are served
from an in-memory cache, so normal chat never waits on the database.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time

log = logging.getLogger("yuki.personas")

MAX_DIRECTIVES = 6
MAX_DIRECTIVE_CHARS = 240

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_WS = re.compile(r"\s+")


def clean_directive(text) -> str:
    text = _CONTROL.sub("", str(text or ""))
    text = _WS.sub(" ", text).strip().strip('"').strip()
    return text[:MAX_DIRECTIVE_CHARS]


class PersonaStore:
    def __init__(self, database_url: str | None = None):
        self.lock = threading.Lock()
        self.cache: dict[int, list[str]] = {}
        self.url = (database_url or "").strip()
        if not self.url:
            raise RuntimeError("DATABASE_URL is required. Persona storage is Postgres-only; no local SQLite fallback.")
        self.pg = None
        self._down_until = 0.0
        self.backend = "postgres"
        if not self._connect():
            raise RuntimeError("Could not initialize persistent Postgres persona storage.")
        log.info("persona store ready (Postgres only)")

    # ---------- connection ----------

    def _connect(self) -> bool:
        """(Re)open the database. Returns False (and logs) if it can't, instead of crashing the bot."""
        try:
            if self.url:
                import psycopg2

                if self.pg is not None:
                    try:
                        self.pg.close()
                    except Exception:
                        pass
                self.pg = psycopg2.connect(self.url, connect_timeout=5)
                self.pg.autocommit = True
                with self.pg.cursor() as cur:
                    cur.execute(
                        "CREATE TABLE IF NOT EXISTS personas ("
                        " user_id BIGINT PRIMARY KEY,"
                        " directives TEXT NOT NULL DEFAULT '[]',"
                        " updated BIGINT NOT NULL DEFAULT 0)"
                    )
            return True
        except Exception:
            log.exception("persona store couldn't connect to %s", self.backend)
            self.pg = None
            self._down_until = time.time() + 60
            return False

    def _execute(self, sql_pg: str, sql_lite: str, args: tuple, fetch: bool = False):
        """Run one statement, reconnecting once if the Postgres connection dropped."""
        for attempt in (1, 2):
            try:
                if self.pg is None or self.pg.closed:
                    if time.time() < self._down_until or not self._connect():
                        raise ConnectionError("postgres unavailable")
                with self.pg.cursor() as cur:
                    cur.execute(sql_pg, args)
                    return cur.fetchall() if fetch else None
            except Exception:
                if attempt == 2:
                    raise
                self.pg = None  # force a reconnect on the next loop

    # ---------- api ----------

    def get(self, user_id: int) -> list[str]:
        """The person's current directives (empty list = default Yuki)."""
        with self.lock:
            if user_id in self.cache:
                return list(self.cache[user_id])
            try:
                rows = self._execute(
                    "SELECT directives FROM personas WHERE user_id = %s",
                    "SELECT directives FROM personas WHERE user_id = ?",
                    (user_id,), fetch=True,
                )
            except Exception as e:
                log.warning("couldn't load persona for %s (%s), using default", user_id, type(e).__name__)
                return []  # not cached, so we retry next time
            directives: list[str] = []
            if rows:
                raw = rows[0][0]
                try:
                    data = json.loads(raw or "[]")
                    directives = [d for d in (clean_directive(x) for x in data) if d][:MAX_DIRECTIVES]
                except (json.JSONDecodeError, TypeError):
                    directives = []
            self.cache[user_id] = directives
            return list(directives)

    def _save(self, user_id: int, directives: list[str]) -> bool:
        """Write to the cache first (so it applies immediately), then the database. False = db write failed."""
        self.cache[user_id] = list(directives)
        payload = json.dumps(directives)
        now = int(time.time())
        try:
            if not directives:
                self._execute(
                    "DELETE FROM personas WHERE user_id = %s", "DELETE FROM personas WHERE user_id = ?", (user_id,)
                )
            else:
                self._execute(
                    "INSERT INTO personas(user_id, directives, updated) VALUES (%s, %s, %s) "
                    "ON CONFLICT (user_id) DO UPDATE SET directives = EXCLUDED.directives, updated = EXCLUDED.updated",
                    "INSERT INTO personas(user_id, directives, updated) VALUES (?, ?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET directives = excluded.directives, updated = excluded.updated",
                    (user_id, payload, now),
                )
            return True
        except Exception as e:
            log.warning("couldn't save persona for %s (%s)", user_id, type(e).__name__)
            return False

    def add(self, user_id: int, instruction: str, replace: bool = False) -> tuple[list[str], bool, str]:
        """Add (or, with replace=True, swap in) one instruction.

        Returns (directives_now, saved_to_db, note). `note` explains what happened for the model to relay.
        """
        directive = clean_directive(instruction)
        if len(directive) < 3:
            return self.get(user_id), True, "that instruction was empty"
        with self.lock:
            current = [] if replace else self._current(user_id)
            note = ""
            if directive.lower() in (d.lower() for d in current):
                note = "already had that one"
            else:
                current.append(directive)
                if len(current) > MAX_DIRECTIVES:
                    dropped = current.pop(0)
                    note = f"was at the limit so the oldest one got dropped ({dropped[:60]})"
            ok = self._save(user_id, current)
            return list(current), ok, note

    def _current(self, user_id: int) -> list[str]:
        """Cached directives, loading from the database on a cache miss. Caller holds the lock."""
        if user_id not in self.cache:
            self.cache[user_id] = self._load_unlocked(user_id)
        return list(self.cache[user_id])

    def _load_unlocked(self, user_id: int) -> list[str]:
        try:
            rows = self._execute(
                "SELECT directives FROM personas WHERE user_id = %s",
                "SELECT directives FROM personas WHERE user_id = ?",
                (user_id,), fetch=True,
            )
            if rows:
                data = json.loads(rows[0][0] or "[]")
                return [d for d in (clean_directive(x) for x in data) if d][:MAX_DIRECTIVES]
        except Exception as e:
            log.warning("couldn't load persona for %s (%s)", user_id, type(e).__name__)
        return []

    def remove(self, user_id: int, match: str | None = None) -> tuple[int, bool]:
        """Remove directives containing `match` (case-insensitive), or everything if match is empty.

        Returns (how_many_removed, saved_to_db)."""
        with self.lock:
            current = self._current(user_id)
            if not current:
                return 0, True
            needle = clean_directive(match).lower() if match else ""
            if needle:
                kept = [d for d in current if needle not in d.lower()]
            else:
                kept = []
            removed = len(current) - len(kept)
            if removed == 0:
                return 0, True
            ok = self._save(user_id, kept)
            return removed, ok

    def delete_user(self, user_id: int) -> bool:
        """Wipe everything (used by /memory forget)."""
        with self.lock:
            return self._save(user_id, [])
