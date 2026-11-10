"""Discord OAuth2 and HTTP routes for the Yuki dashboard.

Call setup_dashboard(app, context) before starting the aiohttp application. The
context is the bot module and must expose client and mod. The companion
``dashboard_settings`` module supplies get_settings, update_settings and refresh.

Configuration: DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET and an exact absolute
DISCORD_REDIRECT_URI ending in /dashboard/callback. HTTPS is mandatory except
for localhost/loopback development. Sessions and OAuth state are in-memory,
per-process and deliberately do not survive a restart. No signing secret is
needed: cookies are opaque random keys, with all data retained server-side.
Run only one instance or arrange sticky routing; a durable shared session store
would be needed for multi-instance operation. The reverse proxy must preserve
HTTPS externally, and HTTP access logs must not record OAuth callback query
strings (e.g. use aiohttp AppRunner(access_log=None)).
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import time
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit

import aiohttp
from aiohttp import web
import discord


log = logging.getLogger(__name__)
_BASE = Path(__file__).resolve().parent / "web"
_API = "https://discord.com/api/v10"
_AUTHORIZE = "https://discord.com/oauth2/authorize"
_STATE_TTL = 600
_SESSION_TTL = 8 * 60 * 60
_LIMIT = 10000
_MAX_STATES_PER_PEER = 20
_TIMEOUT = 10
_MAX_BODY = 300 * 1024
_MAX_GUILD_PAGES = 50
_ID_RE = re.compile(r"^[0-9]{1,20}$")
_HASH_RE = re.compile(r"^[A-Za-z0-9_]{1,128}$")


class _CallbackLogFilter(logging.Filter):
    """Suppress default aiohttp access lines that could contain an OAuth code.

    This cannot control custom loggers, proxies, CDNs, or tracing middleware;
    those must be configured separately to omit callback query strings.
    """
    def filter(self, record):
        try:
            return "/dashboard/callback" not in record.getMessage()
        except Exception:
            return False


class _Unauthenticated(Exception):
    pass


class _Unavailable(Exception):
    pass


class _Forbidden(Exception):
    pass


def _origin(url):
    """Normalize an origin; return None for malformed/non-HTTP URLs."""
    try:
        p = urlsplit(url)
        if (p.scheme not in ("http", "https") or not p.hostname or p.username is not None
                or p.password is not None or p.path not in ("", "/") or p.query or p.fragment):
            return None
        host = p.hostname.lower()
        port = p.port or (443 if p.scheme == "https" else 80)
        return p.scheme, host, port
    except (ValueError, AttributeError):
        return None


def _config():
    client_id = os.getenv("DISCORD_CLIENT_ID", "").strip()
    secret = os.getenv("DISCORD_CLIENT_SECRET", "").strip()
    redirect = os.getenv("DISCORD_REDIRECT_URI", "").strip()
    try:
        p = urlsplit(redirect)
        # No query, fragment, credentials, relative path, or open redirect.
        origin = _origin(f"{p.scheme}://{p.netloc}")
        if (not _ID_RE.fullmatch(client_id) or int(client_id) <= 0 or not secret or not origin
                or p.path != "/dashboard/callback" or p.query or p.fragment
                or any(ch.isspace() for ch in redirect)
                or (p.scheme == "http" and p.hostname not in ("localhost", "127.0.0.1", "::1"))):
            return None
        return SimpleNamespace(client_id=client_id, secret=secret, redirect=redirect,
                               origin=origin, secure=p.scheme == "https")
    except (ValueError, TypeError):
        return None


def _id(value):
    s = str(value)
    if not _ID_RE.fullmatch(s) or int(s) <= 0:
        raise _Unavailable()
    return s


def _public_user(data):
    if not isinstance(data, dict):
        raise _Unavailable()
    uid = _id(data.get("id"))
    username = data.get("username")
    if not isinstance(username, str) or not username or len(username) > 256:
        raise _Unavailable()
    avatar = data.get("avatar")
    avatar_url = None
    if isinstance(avatar, str) and _HASH_RE.fullmatch(avatar):
        ext = "gif" if avatar.startswith("a_") else "png"
        avatar_url = f"https://cdn.discordapp.com/avatars/{uid}/{avatar}.{ext}"
    return {"id": uid, "username": username, "avatar_url": avatar_url}


def _guild_info(guild):
    gid = _id(guild.id)
    icon = getattr(guild, "icon", None)
    key = getattr(icon, "key", None) if icon else None
    url = None
    if isinstance(key, str) and _HASH_RE.fullmatch(key):
        ext = "gif" if key.startswith("a_") else "png"
        url = f"https://cdn.discordapp.com/icons/{gid}/{key}.{ext}"
    return {"id": gid, "name": str(guild.name), "icon_url": url}


class _MemberView:
    """Member proxy with roles and guild permissions based on fresh REST data.

    discord.py's fetch_member does not refresh its cached Guild's roles. Its
    normal Member.roles and guild_permissions would consequently still use
    stale role objects. Preserve the rest of the Member interface by proxying.
    """
    def __init__(self, member, roles, permissions):
        self._member = member
        self.roles = roles
        self.guild_permissions = permissions

    def __getattr__(self, name):
        return getattr(self._member, name)


class _GuildView:
    """Guild facade with REST-fresh roles and channel permission overwrites."""
    def __init__(self, guild, roles, channels):
        self._guild = guild
        self.roles = roles
        self.channels = channels
        self.default_role = next(role for role in roles if int(role.id) == int(guild.id))

    def __getattr__(self, name):
        # Bound guild methods remain available to the settings/refresh adapter.
        return getattr(self._guild, name)


class _Dashboard:
    def __init__(self, context):
        self.context = context
        self.config = _config()
        self.states = {}
        self.sessions = {}
        self.http = None
        self.guild_semaphore = asyncio.Semaphore(8)
        self.session_cookie = "__Host-yai_dashboard" if self.config and self.config.secure else "yai_dashboard"
        self.state_cookie = "__Host-yai_oauth_state" if self.config and self.config.secure else "yai_oauth_state"

    async def lifecycle(self, _app):
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=_TIMEOUT),
                                          trust_env=False)
        try:
            yield
        finally:
            await self.http.close()
            self.sessions.clear()
            self.states.clear()

    def _clean(self):
        now = time.monotonic()
        for store in (self.states, self.sessions):
            for key, item in list(store.items()):
                if item["expires"] <= now:
                    store.pop(key, None)

    def _cookie(self, response, name, value, max_age):
        response.set_cookie(name, value, max_age=max_age, path="/", httponly=True,
                            secure=bool(self.config and self.config.secure), samesite="Lax")

    def _delete_cookie(self, response, name):
        # The same name/path must be used as on set; keep the prefix requirements.
        response.set_cookie(name, "", max_age=0, expires="Thu, 01 Jan 1970 00:00:00 GMT",
                            path="/", httponly=True,
                            secure=bool(self.config and self.config.secure), samesite="Lax")

    def _require_config(self):
        if not self.config:
            raise _Unavailable()

    def _session(self, request):
        self._require_config()
        self._clean()
        token = request.cookies.get(self.session_cookie, "")
        if not token or len(token) > 128:
            raise _Unauthenticated()
        session = self.sessions.get(token)
        if not session:
            raise _Unauthenticated()
        return token, session

    def _still_valid(self, token, session):
        if self.sessions.get(token) is not session or session["expires"] <= time.monotonic():
            self.sessions.pop(token, None)
            raise _Unauthenticated()

    def _csrf(self, request, session):
        # Require both a canonical Origin and an unpredictable per-session header.
        # Referer/Host/X-Forwarded-* are intentionally not trusted as substitutes.
        header = request.headers.get("X-CSRF-Token", "")
        if (_origin(request.headers.get("Origin", "")) != self.config.origin
                or not header or len(header) > 128
                or not hmac.compare_digest(header.encode(), session["csrf"].encode())):
            raise _Forbidden()

    async def _discord(self, method, endpoint, **kwargs):
        """Make requests only to fixed Discord endpoints. Never log response bodies."""
        try:
            async with self.http.request(method, _API + endpoint, allow_redirects=False, **kwargs) as resp:
                if resp.status in (401, 403):
                    raise _Unauthenticated()
                if resp.status == 429 or resp.status < 200 or resp.status >= 300:
                    raise _Unavailable()
                return await resp.json(content_type=None)
        except (_Unauthenticated, _Unavailable):
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError):
            raise _Unavailable() from None

    async def _identity(self, token, session):
        try:
            data = await self._discord("GET", "/users/@me", headers={"Authorization": "Bearer " + session["access_token"]})
            user = _public_user(data)
            if user["id"] != session["user"]["id"]:
                raise _Unauthenticated()
            self._still_valid(token, session)
            session["user"] = user
            return user
        except _Unauthenticated:
            self.sessions.pop(token, None)
            raise

    async def _user_guild_ids(self, session):
        headers = {"Authorization": "Bearer " + session["access_token"]}
        guilds = {}
        after = "0"
        for _ in range(_MAX_GUILD_PAGES):
            data = await self._discord("GET", "/users/@me/guilds", headers=headers,
                                       params={"limit": "200", "after": after})
            if not isinstance(data, list) or len(data) > 200:
                raise _Unavailable()
            page_ids = []
            for item in data:
                if not isinstance(item, dict):
                    raise _Unavailable()
                gid = _id(item.get("id"))
                permissions = item.get("permissions")
                if (not isinstance(permissions, str) or not _ID_RE.fullmatch(permissions)
                        or not isinstance(item.get("owner"), bool) or gid in guilds):
                    raise _Unavailable()
                guilds[gid] = {"permissions": int(permissions), "owner": item["owner"]}
                page_ids.append(gid)
            if len(data) < 200:
                return guilds
            next_after = str(max(int(x) for x in page_ids))
            if int(next_after) <= int(after):
                raise _Unavailable()
            after = next_after
        # Never silently pretend the result is complete.
        raise _Unavailable()

    def _ready(self):
        try:
            return bool(self.context.client.is_ready())
        except Exception:
            return False

    async def _authorized(self, gid, user_id, oauth_guild):
        """Fetch live membership, roles and channel overwrites for settings access."""
        if not self._ready():
            raise _Unavailable()
        client = self.context.client
        guild = client.get_guild(int(gid))
        if guild is None:
            raise _Forbidden()
        try:
            async def fetch_live():
                async with self.guild_semaphore:
                    tasks = [asyncio.create_task(client.fetch_guild(int(gid))),
                             asyncio.create_task(guild.fetch_member(int(user_id))),
                             asyncio.create_task(guild.fetch_roles()),
                             asyncio.create_task(guild.fetch_channels())]
                    try:
                        return await asyncio.gather(*tasks)
                    finally:
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
            fresh_guild, member, roles, channels = await asyncio.wait_for(fetch_live(), timeout=_TIMEOUT)
            # A changed guild owner in a lagging cache makes can_manage/can_admin
            # unsafe; fail closed until the gateway cache catches up.
            if (int(fresh_guild.id) != int(gid) or int(member.id) != int(user_id)
                    or not getattr(fresh_guild, "owner_id", None)
                    or fresh_guild.owner_id != guild.owner_id
                    or oauth_guild["owner"] != (int(user_id) == int(fresh_guild.owner_id))
                    or client.get_guild(int(gid)) is not guild or not self._ready()):
                raise _Unavailable()
            role_map = {int(role.id): role for role in roles}
            role_ids = {int(r) for r in member._roles}
            role_ids.add(int(gid))  # @everyone
            if not role_ids.issubset(role_map):
                raise _Unavailable()
            member_roles = [role_map[r] for r in role_ids]
            perms = discord.Permissions.none()
            for role in member_roles:
                perms.value |= int(role.permissions.value)
            if int(user_id) == int(fresh_guild.owner_id) or perms.administrator:
                perms = discord.Permissions.all()
            elif member.is_timed_out():
                perms.value &= discord.Permissions._timeout_mask()
            # Both independent live sources must agree on Discord permissions;
            # user/role/bot-owner grants still flow through Mod.can_manage.
            if not oauth_guild["owner"]:
                perms.value &= oauth_guild["permissions"]
            view = _MemberView(member, member_roles, perms)
            manager = self.context.mod.can_manage(SimpleNamespace(guild=guild, guild_id=guild.id, user=view))
            # Some admins can change restricted fields even when the /mod
            # access level is owner-only. The settings module separately checks
            # manager vs administrator permissions for every individual field.
            if not (manager or oauth_guild["owner"] or perms.administrator):
                raise _Forbidden()
            return _GuildView(guild, roles, channels), view
        except (_Forbidden, _Unavailable):
            raise
        except discord.NotFound:
            raise _Forbidden() from None
        except (discord.Forbidden, discord.HTTPException, asyncio.TimeoutError):
            raise _Unavailable() from None
        except Exception as exc:
            # Keep the denial fail-closed, but retain a traceback so unexpected
            # Discord/cache state can be diagnosed without logging user tokens.
            log.exception("dashboard guild authorization failed (unexpected %s)",
                          type(exc).__name__)
            raise _Unavailable() from None

    async def _identity_and_guilds(self, request):
        token, session = self._session(request)
        await self._identity(token, session)
        if not self._ready():
            raise _Unavailable()
        try:
            guild_ids = await self._user_guild_ids(session)
        except _Unauthenticated:
            self.sessions.pop(token, None)
            raise
        self._still_valid(token, session)
        return session, guild_ids

    async def page(self, _request):
        path = _BASE / "dashboard.html"
        if not path.is_file():
            raise _Unavailable()
        return web.FileResponse(path)

    async def asset(self, request):
        name = request.match_info["name"]
        if name not in ("dashboard.css", "dashboard.js"):
            raise web.HTTPNotFound()
        path = _BASE / name
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def login(self, request):
        self._require_config()
        self._clean()
        # Only the socket peer is authoritative. Forwarded headers and
        # request.remote can be overridden by an untrusted client/middleware.
        transport = request.transport
        peername = transport.get_extra_info("peername") if transport else None
        try:
            if not isinstance(peername, tuple) or not peername:
                raise ValueError("missing peer")
            peer = ipaddress.ip_address(peername[0])
            peer = str(peer.ipv4_mapped or peer) if isinstance(peer, ipaddress.IPv6Address) else str(peer)
        except (ValueError, TypeError):
            raise _Unavailable() from None
        if len(self.states) >= _LIMIT:
            raise web.HTTPTooManyRequests(text="Too many pending Discord login attempts. Please wait and try again.",
                                          headers={"Retry-After": str(_STATE_TTL)})
        if sum(item.get("peer") == peer for item in self.states.values()) >= _MAX_STATES_PER_PEER:
            raise web.HTTPTooManyRequests(text="Too many pending Discord login attempts from your connection. Please wait and try again.",
                                          headers={"Retry-After": str(_STATE_TTL)})
        state, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.states[state] = {"browser": browser, "peer": peer, "expires": time.monotonic() + _STATE_TTL}
        params = {"client_id": self.config.client_id, "response_type": "code",
                  "redirect_uri": self.config.redirect, "scope": "identify guilds", "state": state}
        response = web.Response(status=302, headers={"Location": _AUTHORIZE + "?" + urlencode(params)})
        self._cookie(response, self.state_cookie, browser, _STATE_TTL)
        return response

    async def callback(self, request):
        self._require_config()
        self._clean()
        state = request.query.getall("state", [])
        code = request.query.getall("code", [])
        browser = request.cookies.get(self.state_cookie, "")
        # Pop before any awaits, making state single-use even for a failed callback.
        item = self.states.pop(state[0], None) if len(state) == 1 and len(state[0]) <= 128 else None
        valid = (item and browser and len(browser) <= 128
                 and hmac.compare_digest(browser.encode(), item["browser"].encode()))
        if not valid or len(code) != 1 or not code[0] or len(code[0]) > 2048 or request.query.get("error"):
            response = web.Response(text="Discord login failed. Please try again.", status=400)
            self._delete_cookie(response, self.state_cookie)
            return response
        try:
            data = await self._discord("POST", "/oauth2/token", data={
                "client_id": self.config.client_id, "client_secret": self.config.secret,
                "grant_type": "authorization_code", "code": code[0], "redirect_uri": self.config.redirect,
            }, headers={"Accept": "application/json"})
            access_token = data.get("access_token") if isinstance(data, dict) else None
            expires_in = data.get("expires_in") if isinstance(data, dict) else None
            scope = data.get("scope", "") if isinstance(data, dict) else ""
            if (not isinstance(access_token, str) or not access_token or len(access_token) > 4096
                    or not isinstance(expires_in, (int, float)) or isinstance(expires_in, bool)
                    or not math.isfinite(expires_in) or expires_in <= 0
                    or not isinstance(scope, str) or not {"identify", "guilds"}.issubset(scope.split())
                    or str(data.get("token_type", "")).lower() != "bearer"):
                raise _Unavailable()
            user = _public_user(await self._discord("GET", "/users/@me", headers={"Authorization": "Bearer " + access_token}))
            self._clean()
            if len(self.sessions) >= _LIMIT:
                raise _Unavailable()
            # Prevent session fixation and replace any old local session.
            self.sessions.pop(request.cookies.get(self.session_cookie, ""), None)
            key, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            lifetime = min(int(expires_in), _SESSION_TTL)
            if lifetime <= 0:
                raise _Unavailable()
            self.sessions[key] = {"user": user, "access_token": access_token,
                                  "csrf": csrf, "expires": time.monotonic() + lifetime}
            response = web.Response(status=302, headers={"Location": "/dashboard"})
            self._cookie(response, self.session_cookie, key, lifetime)
        except (_Unauthenticated, _Unavailable):
            response = web.Response(text="Discord login could not be completed. Please try again.", status=503)
        self._delete_cookie(response, self.state_cookie)
        return response

    async def logout(self, request):
        key, session = self._session(request)
        self._csrf(request, session)
        self.sessions.pop(key, None)
        response = web.json_response({"message": "Logged out."})
        self._delete_cookie(response, self.session_cookie)
        self._delete_cookie(response, self.state_cookie)
        return response

    async def me(self, request):
        if not self.config:
            return web.json_response({"authenticated": False, "user": None, "csrf": None,
                                      "oauth_configured": False})
        try:
            token, session = self._session(request)
            user = await self._identity(token, session)
        except _Unauthenticated:
            return web.json_response({"authenticated": False, "user": None, "csrf": None,
                                      "oauth_configured": True})
        return web.json_response({"authenticated": True, "user": user, "csrf": session["csrf"],
                                  "oauth_configured": True})

    async def statistics(self, request):
        """Return the bot-wide guild inventory; this endpoint is read-only."""
        token, session = self._session(request)
        await self._identity(token, session)
        if not self._ready():
            raise _Unavailable()
        guilds = [_guild_info(guild) for guild in list(self.context.client.guilds)]
        guilds.sort(key=lambda guild: (guild["name"].casefold(), guild["id"]))
        self._still_valid(token, session)
        return web.json_response({"total": len(guilds), "guilds": guilds})

    async def guilds(self, request):
        session, ids = await self._identity_and_guilds(request)
        client = self.context.client
        candidates = [g for g in list(client.guilds) if str(g.id) in ids]
        semaphore = asyncio.Semaphore(8)

        async def check(guild):
            async with semaphore:
                try:
                    authorized, _ = await self._authorized(str(guild.id), session["user"]["id"], ids[str(guild.id)])
                    return _guild_info(authorized)
                except _Forbidden:
                    return None
        # If Discord is unavailable for any candidate, return 503 instead of a
        # misleading partial list. Revoked access/not-a-member is safely omitted.
        tasks = [asyncio.create_task(check(g)) for g in candidates]
        try:
            results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=45)
        except asyncio.TimeoutError:
            raise _Unavailable() from None
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        token = request.cookies.get(self.session_cookie, "")
        self._still_valid(token, session)
        return web.json_response({"guilds": [g for g in results if g is not None]})

    async def _guild(self, request, write=False):
        token, session = self._session(request)
        if write:
            self._csrf(request, session)
        await self._identity(token, session)
        if not self._ready():
            raise _Unavailable()
        gid = request.match_info["guild_id"]
        if not _ID_RE.fullmatch(gid) or int(gid) <= 0:
            raise _Forbidden()
        try:
            ids = await self._user_guild_ids(session)
        except _Unauthenticated:
            self.sessions.pop(token, None)
            raise
        if gid not in ids:
            raise _Forbidden()
        guild, member = await self._authorized(gid, session["user"]["id"], ids[gid])
        self._still_valid(token, session)
        if write:
            self._csrf(request, session)
        return guild, member

    async def snapshot(self, request):
        guild, member = await self._guild(request)
        import dashboard_settings
        return web.json_response(await dashboard_settings.get_settings(self.context, guild, member))

    async def update(self, request):
        # Reject unverified clients before reading, then resolve fresh access
        # after the entire body arrives (which can be arbitrarily delayed).
        _, session = self._session(request)
        self._csrf(request, session)
        if request.content_length is not None and request.content_length > _MAX_BODY:
            raise web.HTTPRequestEntityTooLarge(max_size=_MAX_BODY, actual_size=request.content_length)
        if request.content_type != "application/json":
            raise web.HTTPBadRequest(text="Expected JSON.")
        async def read_body():
            parts, size = [], 0
            async for chunk in request.content.iter_chunked(8192):
                size += len(chunk)
                if size > _MAX_BODY:
                    raise web.HTTPRequestEntityTooLarge(max_size=_MAX_BODY, actual_size=size)
                parts.append(chunk)
            return b"".join(parts)
        try:
            raw = await asyncio.wait_for(read_body(), timeout=_TIMEOUT)
        except asyncio.TimeoutError:
            raise web.HTTPRequestTimeout() from None
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise web.HTTPBadRequest(text="Invalid JSON.") from None
        if (not isinstance(body, dict) or set(body) != {"changes", "expected"}
                or not isinstance(body["changes"], dict) or not isinstance(body["expected"], dict)):
            raise web.HTTPBadRequest(text="Expected changes and expected objects.")
        guild, member = await self._guild(request, write=True)
        token, session = self._session(request)
        original_guild_id, original_user_id = _id(guild.id), _id(member.id)

        async def reauthorize():
            nonlocal guild, member
            self._still_valid(token, session)
            fresh_guild, fresh_member = await self._guild(request, write=True)
            self._still_valid(token, session)
            self._csrf(request, session)
            if _id(fresh_guild.id) != original_guild_id or _id(fresh_member.id) != original_user_id:
                raise _Forbidden()
            guild, member = fresh_guild, fresh_member
            return guild, member

        import dashboard_settings
        await dashboard_settings.update_settings(self.context, guild, member, body["changes"],
                                                 expected=body["expected"], reauthorize=reauthorize)
        return web.json_response(await dashboard_settings.get_settings(self.context, guild, member))

    async def refresh(self, request):
        guild, member = await self._guild(request, write=True)
        token, session = self._session(request)
        original_guild_id, original_user_id = _id(guild.id), _id(member.id)

        async def reauthorize():
            nonlocal guild, member
            self._still_valid(token, session)
            fresh_guild, fresh_member = await self._guild(request, write=True)
            self._still_valid(token, session)
            self._csrf(request, session)
            if _id(fresh_guild.id) != original_guild_id or _id(fresh_member.id) != original_user_id:
                raise _Forbidden()
            guild, member = fresh_guild, fresh_member
            return guild, member

        import dashboard_settings
        message = await dashboard_settings.refresh(self.context, guild, member, reauthorize=reauthorize)
        return web.json_response({"message": str(message)})


@web.middleware
async def _security(request, handler):
    if not (request.path == "/dashboard" or request.path.startswith("/dashboard/")):
        return await handler(request)
    try:
        response = await handler(request)
    except _Unauthenticated:
        response = web.json_response({"error": "Authentication required. Please log in again."}, status=401)
    except _Forbidden:
        response = web.json_response({"error": "Access denied or invalid request verification."}, status=403)
    except _Unavailable:
        response = web.json_response({"error": "Dashboard or Discord is unavailable, or the bot is not ready. Please try again."}, status=503)
    except PermissionError:
        response = web.json_response({"error": "You do not have permission to change these settings."}, status=403)
    except ValueError as exc:
        # Settings module validation messages are intended for clients, but cap
        # length and do not reveal non-string/internal exception representations.
        message = str(exc)[:300] or "Invalid settings."
        response = web.json_response({"error": message}, status=400)
    except web.HTTPException as exc:
        response = web.Response(status=exc.status, headers=exc.headers, body=exc.body)
    except Exception as exc:
        log.warning("dashboard request failed (%s)", type(exc).__name__)
        response = web.json_response({"error": "Dashboard request failed."}, status=500)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data: https://cdn.discordapp.com https://media.discordapp.net; "
        "connect-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'"
    )
    return response


def setup_dashboard(app, context):
    """Register dashboard routes on an aiohttp Application, before startup."""
    dashboard = _Dashboard(context)
    access_logger = logging.getLogger("aiohttp.access")
    if not any(isinstance(f, _CallbackLogFilter) for f in access_logger.filters):
        access_logger.addFilter(_CallbackLogFilter())
    app.middlewares.append(_security)
    app.cleanup_ctx.append(dashboard.lifecycle)
    app.router.add_get("/dashboard", dashboard.page)
    app.router.add_get("/dashboard/assets/{name}", dashboard.asset)
    app.router.add_get("/dashboard/login", dashboard.login)
    app.router.add_get("/dashboard/callback", dashboard.callback)
    app.router.add_post("/dashboard/logout", dashboard.logout)
    app.router.add_get("/dashboard/api/me", dashboard.me)
    app.router.add_get("/dashboard/api/statistics", dashboard.statistics)
    app.router.add_get("/dashboard/api/guilds", dashboard.guilds)
    app.router.add_get("/dashboard/api/guilds/{guild_id}", dashboard.snapshot)
    app.router.add_put("/dashboard/api/guilds/{guild_id}", dashboard.update)
    app.router.add_post("/dashboard/api/guilds/{guild_id}/refresh", dashboard.refresh)
    return dashboard
