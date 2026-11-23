"""Tiny DB-API compatibility layer for Yuki's Cloudflare D1 Worker.

The Worker URL and bearer key are supplied only through service environment
variables. Discord snowflakes are sent as decimal strings to avoid JS precision
loss, while ordinary counters/timestamps remain integers.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request


try:
    import psycopg2 as _psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    _psycopg2 = None
    class RealDictCursor:
        pass



def Binary(value: bytes):
    if not os.getenv("YUKI_MEMORY_URL") and _psycopg2 is not None:
        return _psycopg2.Binary(bytes(value))
    return base64.b64encode(bytes(value)).decode("ascii")


def _safe(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and abs(value) > 9_007_199_254_740_991:
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, (list, tuple)):
        return json.dumps([str(v) if isinstance(v, int) else v for v in value], separators=(",", ":"))
    return value


def _normalise_sql(sql: str, params):
    raw = sql
    if "pg_advisory_xact_lock" in raw:
        return None, []
    # SQLite/D1 has no row-level FOR UPDATE; each individual statement is atomic.
    sql = re.sub(r"\s+FOR\s+UPDATE\b", "", sql, flags=re.I)
    sql = re.sub(r"octet_length\s*\(\s*data\s*\)", "length(data)", sql, flags=re.I)
    params = list(params or ())
    # Translate Postgres array membership to SQLite JSON1.
    # Use an explicit scan so each array parameter is JSON encoded in order.
    pattern = re.compile(r"=\s*ANY\(\s*%s(?:\s*::\s*(?:bigint|text)\[\])?\s*\)", re.I)
    if pattern.search(sql):
        converted = []
        p_index = 0
        for match in pattern.finditer(sql):
            # Parameter order is preserved by the original SQL; array expressions
            # consume the next parameter at their location.
            converted.append(match)
        # Rebuild and transform matching parameter values by position.
        pieces, last = [], 0
        for match in pattern.finditer(sql):
            pieces.append(sql[last:match.start()])
            pieces.append("IN (SELECT value FROM json_each(?))")
            last = match.end()
        pieces.append(sql[last:])
        sql = "".join(pieces)
        # The only array parameters in Yuki's statements are the matching ANY args.
        params = [json.dumps([str(v) for v in p], separators=(",", ":")) if isinstance(p, (list, tuple)) else p for p in params]
    sql = sql.replace("%s", "?")
    return sql, [_safe(p) for p in params]


class D1Cursor:
    def __init__(self, connection, cursor_factory=None):
        self.connection = connection
        self.dict_rows = cursor_factory is RealDictCursor
        self.rows = []
        self.index = 0
        self.rowcount = 0
        self.lastrowid = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=()):
        query, values = _normalise_sql(sql, params)
        self.rows, self.index, self.rowcount, self.lastrowid = [], 0, 0, None
        if query is None:
            return self
        payload = json.dumps({"sql": query, "params": values}, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.connection.url,
            data=payload,
            headers={
                "Authorization": "Bearer " + self.connection.api_key,
                "Content-Type": "application/json",
                "User-Agent": "Yuki-Memory/1.0 (+https://exedevelopement.com)",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"Cloudflare D1 request failed ({exc.code}): {detail}") from exc
        except Exception as exc:
            raise RuntimeError(f"Cloudflare D1 request failed: {type(exc).__name__}: {exc}") from exc
        if not result.get("success"):
            raise RuntimeError("Cloudflare D1 query failed: " + str(result.get("error", result))[:500])
        self.rows = result.get("results") or []
        meta = result.get("meta") or {}
        self.rowcount = int(meta.get("changes") or 0)
        self.lastrowid = meta.get("last_row_id")
        cooked = []
        for row in self.rows:
            item = dict(row)
            for key, value in list(item.items()):
                if key in {"id", "scope", "channel_id", "user_id", "message_id"} and isinstance(value, str):
                    if re.fullmatch(r"-?\d+", value):
                        try:
                            item[key] = int(value)
                        except ValueError:
                            pass
                    elif key == "scope" and value in {"0.0", "-0.0"}:
                        # D1 can return numeric zero from a TEXT scope column as
                        # "0.0". Normalize it so global facts (scope 0) are
                        # available through fast_get(0) after a restart.
                        item[key] = 0
                if key == "data" and isinstance(value, str):
                    try:
                        item[key] = base64.b64decode(value)
                    except Exception:
                        pass
            cooked.append(item)
        self.rows = cooked
        return self

    def fetchall(self):
        rows = self.rows[self.index:]
        self.index = len(self.rows)
        if self.dict_rows:
            return [dict(row) for row in rows]
        return [tuple(row.values()) for row in rows]

    def fetchone(self):
        if self.index >= len(self.rows):
            return None
        row = self.rows[self.index]
        self.index += 1
        return dict(row) if self.dict_rows else tuple(row.values())


class D1Connection:
    def __init__(self, url=None, api_key=None):
        self.url = (url or os.getenv("YUKI_MEMORY_URL", "")).strip().rstrip("/")
        self.api_key = (api_key or os.getenv("YUKI_MEMORY_API_KEY", "")).strip()
        if not self.url or not self.url.startswith("https://") or not self.api_key:
            raise RuntimeError("YUKI_MEMORY_URL and YUKI_MEMORY_API_KEY must be configured for persistent Cloudflare D1 storage.")
        self.closed = 0
        self.autocommit = True

    def cursor(self, cursor_factory=None):
        return D1Cursor(self, cursor_factory)

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        self.closed = 1

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
        return False


def connect(*_args, **_kwargs):
    if os.getenv("YUKI_MEMORY_URL") and os.getenv("YUKI_MEMORY_API_KEY"):
        return D1Connection()
    legacy_url = (os.getenv("DATABASE_URL") or "").strip()
    if legacy_url and _psycopg2 is not None:
        return _psycopg2.connect(legacy_url, connect_timeout=10)
    raise RuntimeError("Configure YUKI_MEMORY_URL and YUKI_MEMORY_API_KEY for Cloudflare D1, or DATABASE_URL temporarily for the legacy Postgres fallback.")
