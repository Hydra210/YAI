"""No external Discord calls: exercise the actual aiohttp routes with fakes."""
import asyncio
import logging
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import discord
import pytest
import pytest_asyncio

import dashboard_auth as auth
import dashboard_settings as actual_settings


class Role:
    def __init__(self, role_id, permissions=0):
        self.id = role_id
        self.permissions = SimpleNamespace(value=permissions)


class Guild:
    id = 123
    name = "A server"
    icon = None
    owner_id = 99

    def __init__(self):
        self.role_ids = [456]
        self.role_permissions = 1 << 5  # MANAGE_GUILD
        self.fetch_count = 0
        self.exists = True
        self.channels = []  # Gateway cache, intentionally separate from REST.
        self.live_channels = []

    async def fetch_member(self, uid):
        self.fetch_count += 1
        if not self.exists:
            response = SimpleNamespace(status=404, reason="Not Found")
            raise discord.NotFound(response, {"message": "missing", "code": 10007})
        return SimpleNamespace(id=uid, guild=self, _roles=self.role_ids.copy(), is_timed_out=lambda: False)

    async def fetch_roles(self):
        return [Role(self.id), Role(456, self.role_permissions)]

    async def fetch_channels(self):
        return self.live_channels.copy()


class Client:
    def __init__(self):
        self.guild = Guild()
        self.guilds = [self.guild]
        self.ready = True
        self.fetch_guild_count = 0

    def is_ready(self):
        return self.ready

    def get_guild(self, gid):
        return self.guild if gid == self.guild.id and self.guild in self.guilds else None

    async def fetch_guild(self, gid):
        self.fetch_guild_count += 1
        return SimpleNamespace(id=gid, owner_id=self.guild.owner_id)


class Mod:
    def __init__(self):
        self.user_grants = set()
        self.role_grants = set()
        self.bot_owners = set()
        self.level = "manage_server"

    def can_manage(self, interaction):
        member, guild = interaction.user, interaction.guild
        if member.id == guild.owner_id or member.id in self.bot_owners:
            return True
        if member.id in self.user_grants or {r.id for r in member.roles} & self.role_grants:
            return True
        if self.level == "owner":
            return False
        p = member.guild_permissions
        if self.level == "admin":
            return p.administrator
        return p.manage_guild or p.administrator


@pytest_asyncio.fixture
async def env(monkeypatch):
    monkeypatch.setenv("DISCORD_CLIENT_ID", "1234")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "secret-not-for-browser")
    monkeypatch.setenv("DISCORD_REDIRECT_URI", "http://localhost:9999/dashboard/callback")
    client = Client()
    mod = Mod()
    context = SimpleNamespace(client=client, mod=mod)
    app = web.Application()
    dashboard = auth.setup_dashboard(app, context)
    stub = SimpleNamespace(guilds=[{"id": "123", "owner": False, "permissions": str(1 << 5)}],
                           calls=[], identity_valid=True, fail_guilds=False)

    async def discord_call(method, endpoint, **kw):
        stub.calls.append((method, endpoint, kw))
        if endpoint == "/oauth2/token":
            return {"access_token": "private-oauth-access-token", "token_type": "Bearer",
                    "expires_in": 3600, "scope": "identify guilds"}
        if endpoint == "/users/@me":
            if not stub.identity_valid:
                raise auth._Unauthenticated()
            return {"id": "10", "username": "tester", "avatar": "abc123"}
        if endpoint == "/users/@me/guilds":
            if stub.fail_guilds:
                raise auth._Unavailable()
            return stub.guilds
        raise AssertionError(endpoint)

    dashboard._discord = discord_call
    settings = SimpleNamespace(writes=[], expected=[], refresh_count=0, reauthorizations=0,
                               started=None, proceed=None)

    async def get_settings(context, guild, member):
        return {"guild": {"id": str(guild.id), "name": guild.name, "icon_url": None},
                "sections": [{"id": "s", "title": "Settings", "description": "", "fields": []}]}

    async def authorize_for_commit(reauthorize):
        assert callable(reauthorize)
        if settings.started is not None:
            settings.started.set()
            await settings.proceed.wait()
        settings.reauthorizations += 1
        return await reauthorize()

    async def update_settings(context, guild, member, changes, *, expected, reauthorize):
        guild, member = await authorize_for_commit(reauthorize)
        interaction = SimpleNamespace(guild=guild, user=member)
        admin = member.id == guild.owner_id or member.guild_permissions.administrator
        if "access_level" in changes and not admin:
            raise PermissionError("Access settings require administrator access.")
        if not context.mod.can_manage(interaction) and set(changes) != {"access_level"}:
            raise PermissionError("General settings require manager access.")
        settings.writes.append((guild.id, member.id, changes))
        settings.expected.append(expected)

    async def refresh(context, guild, member, *, reauthorize):
        guild, member = await authorize_for_commit(reauthorize)
        if not context.mod.can_manage(SimpleNamespace(guild=guild, user=member)):
            raise PermissionError("Refresh requires manager access.")
        settings.refresh_count += 1
        return "Refreshed."

    settings.get_settings = get_settings
    settings.update_settings = update_settings
    settings.refresh = refresh
    monkeypatch.setitem(__import__('sys').modules, "dashboard_settings", settings)
    http = TestClient(TestServer(app), cookie_jar=aiohttp.CookieJar(unsafe=True))
    await http.start_server()
    try:
        yield SimpleNamespace(http=http, dashboard=dashboard, client=client, mod=mod, stub=stub, settings=settings)
    finally:
        await http.close()


async def login(env):
    response = await env.http.get("/dashboard/login", allow_redirects=False)
    assert response.status == 302
    q = parse_qs(urlsplit(response.headers["Location"]).query)
    assert q["scope"] == ["identify guilds"]
    assert q["redirect_uri"] == ["http://localhost:9999/dashboard/callback"]
    response = await env.http.get("/dashboard/callback", params={"state": q["state"][0], "code": "fake-code"},
                                  allow_redirects=False)
    assert response.status == 302
    assert response.headers["Location"] == "/dashboard"
    return q["state"][0], response


async def headers(env):
    response = await env.http.get("/dashboard/api/me")
    data = await response.json()
    assert data["authenticated"]
    return {"Origin": "http://localhost:9999", "X-CSRF-Token": data["csrf"]}


@pytest.mark.asyncio
async def test_config_fails_closed_and_only_allows_local_http(monkeypatch):
    monkeypatch.setenv("DISCORD_CLIENT_ID", "123")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "secret")
    for redirect in ["http://example.com/dashboard/callback", "https://example.com/other",
                     "https://example.com/dashboard/callback?x=y", "https://u:p@example.com/dashboard/callback",
                     "//example.com/dashboard/callback", "https://example.com/dashboard/callback#x"]:
        monkeypatch.setenv("DISCORD_REDIRECT_URI", redirect)
        assert auth._config() is None
    monkeypatch.setenv("DISCORD_REDIRECT_URI", "https://example.com/dashboard/callback")
    assert auth._config().secure
    monkeypatch.delenv("DISCORD_CLIENT_SECRET")
    assert auth._config() is None


@pytest.mark.asyncio
async def test_oauth_cookie_and_browser_bound_single_use_state(env):
    response = await env.http.get("/dashboard/login", allow_redirects=False)
    q = parse_qs(urlsplit(response.headers["Location"]).query)
    cookie = response.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
    # A different browser without the state cookie cannot complete this flow.
    other = TestClient(env.http.server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await other.start_server()
    bad = await other.get("/dashboard/callback", params={"state": q["state"][0], "code": "x"}, allow_redirects=False)
    assert bad.status == 400
    assert not any(call[1] == "/oauth2/token" for call in env.stub.calls)
    await other.close()


@pytest.mark.asyncio
async def test_login_session_is_opaque_replay_fails_and_logout_revokes(env):
    state, callback = await login(env)
    cookies = callback.headers.getall("Set-Cookie")
    assert all("private-oauth-access-token" not in value for value in cookies)
    assert any("HttpOnly" in c and "SameSite=Lax" in c for c in cookies)
    resp = await env.http.get("/dashboard/api/me")
    data = await resp.json()
    assert data == {"authenticated": True, "user": {"id": "10", "username": "tester", "avatar_url":
                    "https://cdn.discordapp.com/avatars/10/abc123.png"}, "csrf": data["csrf"], "oauth_configured": True}
    assert "private-oauth-access-token" not in str(data)
    retry = await env.http.get("/dashboard/callback", params={"state": state, "code": "fake-code"}, allow_redirects=False)
    assert retry.status == 400
    assert len([c for c in env.stub.calls if c[1] == "/oauth2/token"]) == 1
    h = await headers(env)
    assert (await env.http.post("/dashboard/logout", headers=h)).status == 200
    resp = await env.http.get("/dashboard/api/me")
    assert (await resp.json())["authenticated"] is False
    assert not env.dashboard.sessions


@pytest.mark.asyncio
async def test_csrf_and_origin_required_for_every_write(env):
    await login(env)
    h = await headers(env)
    url = "/dashboard/api/guilds/123"
    for bad in [{}, {"Origin": "https://evil.example", "X-CSRF-Token": h["X-CSRF-Token"]},
                {"Origin": "http://localhost:9999", "X-CSRF-Token": "wrong"}]:
        assert (await env.http.put(url, headers=bad, json={"changes": {"x": 1}, "expected": {"x": 0}})).status == 403
        assert (await env.http.post(url + "/refresh", headers=bad)).status == 403
        assert (await env.http.post("/dashboard/logout", headers=bad)).status == 403
    assert env.settings.writes == []
    resp = await env.http.put(url, headers=h, json={"changes": {"x": 1}, "expected": {"x": 0}})
    assert resp.status == 200
    assert env.settings.writes == [(123, 10, {"x": 1})]
    assert env.settings.expected == [{"x": 0}]
    assert (await env.http.post(url + "/refresh", headers=h)).status == 200
    assert env.settings.refresh_count == 1
    assert env.settings.reauthorizations == 2


@pytest.mark.asyncio
async def test_fresh_membership_and_permissions_are_rechecked_on_each_request(env):
    await login(env)
    h = await headers(env)
    url = "/dashboard/api/guilds/123"
    assert (await env.http.get(url)).status == 200
    # Discord OAuth permission bitfield revoked even if cached/bot roles say yes.
    env.stub.guilds[0]["permissions"] = "0"
    assert (await env.http.put(url, headers=h, json={"changes": {"x": 1}, "expected": {"x": 0}})).status == 403
    assert not env.settings.writes
    # Explicit role grant still works without Manage Server.
    env.mod.role_grants.add(456)
    assert (await env.http.put(url, headers=h, json={"changes": {"x": 1}, "expected": {"x": 0}})).status == 200
    # Membership is always fetched and bot-side removal takes effect immediately.
    env.client.guild.exists = False
    assert (await env.http.put(url, headers=h, json={"changes": {"x": 2}, "expected": {"x": 1}})).status == 403
    assert len(env.settings.writes) == 1
    assert env.client.guild.fetch_count >= 4
    # Client's guild list still has a stale entry but Discord membership list doesn't.
    env.stub.guilds.clear()
    result = await (await env.http.get("/dashboard/api/guilds")).json()
    assert result["guilds"] == []


@pytest.mark.asyncio
async def test_live_role_changes_and_user_grants(env):
    await login(env)
    url = "/dashboard/api/guilds/123"
    env.client.guild.role_permissions = 0  # OAuth still reports Manage Server
    assert (await env.http.get(url)).status == 403
    env.mod.user_grants.add(10)
    assert (await env.http.get(url)).status == 200
    env.client.guild.role_ids = []
    env.mod.user_grants.clear()
    env.mod.role_grants.add(456)
    assert (await env.http.get(url)).status == 403


@pytest.mark.asyncio
async def test_admin_at_owner_only_access_gets_only_admin_settings(env):
    await login(env)
    env.mod.level = "owner"
    env.client.guild.role_permissions = 1 << 3  # ADMINISTRATOR
    env.stub.guilds[0]["permissions"] = str(1 << 3)
    h = await headers(env)
    url = "/dashboard/api/guilds/123"
    assert (await env.http.get(url)).status == 200
    assert (await env.http.put(url, headers=h, json={"changes": {"enabled": False}, "expected": {"enabled": True}})).status == 403
    assert (await env.http.put(url, headers=h, json={"changes": {"access_level": "admin"}, "expected": {"access_level": "owner"}})).status == 200


@pytest.mark.asyncio
async def test_paginated_guilds_no_truncation_and_not_ready(env):
    await login(env)
    calls = []
    original = env.dashboard._discord
    async def paging(method, endpoint, **kw):
        if endpoint != "/users/@me/guilds":
            return await original(method, endpoint, **kw)
        after = kw["params"]["after"]
        calls.append(after)
        if after == "0":
            return [{"id": str(i), "owner": False, "permissions": "0"} for i in range(1, 201)]
        return [{"id": "201", "owner": False, "permissions": "0"}]
    env.dashboard._discord = paging
    result = await env.dashboard._user_guild_ids(next(iter(env.dashboard.sessions.values())))
    assert len(result) == 201 and calls == ["0", "200"]
    env.client.ready = False
    resp = await env.http.get("/dashboard/api/guilds")
    assert resp.status == 503
    assert "not ready" in (await resp.json())["error"]


@pytest.mark.asyncio
async def test_revocation_expiration_and_upstream_errors_fail_closed(env):
    await login(env)
    env.stub.fail_guilds = True
    assert (await env.http.get("/dashboard/api/guilds")).status == 503
    env.stub.fail_guilds = False
    env.stub.identity_valid = False
    assert (await env.http.get("/dashboard/api/guilds")).status == 401
    assert not env.dashboard.sessions
    # Local expiry also invalidates the cookie, independent of Discord.
    env.stub.identity_valid = True
    await login(env)
    next(iter(env.dashboard.sessions.values()))["expires"] = time.monotonic() - 1
    assert not (await (await env.http.get("/dashboard/api/me")).json())["authenticated"]


@pytest.mark.asyncio
async def test_logout_revokes_a_request_awaiting_discord(env):
    await login(env)
    h = await headers(env)
    started, proceed = asyncio.Event(), asyncio.Event()
    original = env.dashboard._discord

    async def slow(method, endpoint, **kw):
        if endpoint == "/users/@me":
            started.set()
            await proceed.wait()
        return await original(method, endpoint, **kw)

    env.dashboard._discord = slow
    pending = asyncio.create_task(env.http.get("/dashboard/api/guilds"))
    await asyncio.wait_for(started.wait(), timeout=2)
    assert (await env.http.post("/dashboard/logout", headers=h)).status == 200
    proceed.set()
    assert (await pending).status == 401


@pytest.mark.asyncio
async def test_expired_state_is_rejected_without_token_exchange(env):
    response = await env.http.get("/dashboard/login", allow_redirects=False)
    state = parse_qs(urlsplit(response.headers["Location"]).query)["state"][0]
    env.dashboard.states[state]["expires"] = time.monotonic() - 1
    response = await env.http.get("/dashboard/callback", params={"state": state, "code": "fake"}, allow_redirects=False)
    assert response.status == 400
    assert not any(call[1] == "/oauth2/token" for call in env.stub.calls)


@pytest.mark.asyncio
async def test_discord_rate_limit_is_not_treated_as_empty_success():
    dashboard = auth._Dashboard(SimpleNamespace())
    class Response:
        status = 429
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
    dashboard.http = SimpleNamespace(request=lambda *args, **kw: Response())
    with pytest.raises(auth._Unavailable):
        await dashboard._discord("GET", "/users/@me/guilds")


def test_default_access_log_filter_suppresses_callback_query():
    filter_ = auth._CallbackLogFilter()
    secret = logging.LogRecord("aiohttp.access", logging.INFO, "", 0,
                               '"GET /dashboard/callback?code=secret&state=state HTTP/1.1"', (), None)
    ordinary = logging.LogRecord("aiohttp.access", logging.INFO, "", 0,
                                 '"GET /dashboard/api/me HTTP/1.1"', (), None)
    assert not filter_.filter(secret)
    assert filter_.filter(ordinary)


@pytest.mark.asyncio
async def test_secure_cookies_and_asset_allowlist(monkeypatch, env):
    monkeypatch.setenv("DISCORD_REDIRECT_URI", "https://dashboard.example/dashboard/callback")
    secure = auth._Dashboard(SimpleNamespace())
    response = web.Response()
    secure._cookie(response, secure.session_cookie, "opaque", 30)
    morsel = response.cookies[secure.session_cookie]
    assert secure.session_cookie.startswith("__Host-")
    assert morsel["secure"] and morsel["httponly"] and morsel["path"] == "/"
    resp = await env.http.get("/dashboard/assets/keys.html")
    assert resp.status == 404
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {}, {"changes": {}}, {"expected": {}}, {"changes": None, "expected": {}},
    {"changes": {}, "expected": None}, {"changes": [], "expected": {}},
    {"changes": {}, "expected": []}, {"changes": {}, "expected": {}, "extra": 1}, [],
])
async def test_update_requires_changes_and_expected_objects(env, body):
    await login(env)
    response = await env.http.put("/dashboard/api/guilds/123", headers=await headers(env), json=body)
    assert response.status == 400
    assert "changes and expected" in await response.text()
    assert not env.settings.writes
    assert env.settings.reauthorizations == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "refresh"])
@pytest.mark.parametrize("revocation", ["permission", "membership", "logout", "csrf"])
async def test_queued_write_reauthorizes_and_denies_revocation(env, operation, revocation):
    await login(env)
    h = await headers(env)
    env.settings.started, env.settings.proceed = asyncio.Event(), asyncio.Event()
    url = "/dashboard/api/guilds/123"
    if operation == "update":
        pending = asyncio.create_task(env.http.put(url, headers=h,
                                      json={"changes": {"x": 1}, "expected": {"x": 0}}))
    else:
        pending = asyncio.create_task(env.http.post(url + "/refresh", headers=h))
    try:
        await asyncio.wait_for(env.settings.started.wait(), timeout=2)
        if revocation == "permission":
            env.client.guild.role_permissions = 0
            env.stub.guilds[0]["permissions"] = "0"
        elif revocation == "membership":
            env.client.guild.exists = False
        elif revocation == "logout":
            assert (await env.http.post("/dashboard/logout", headers=h)).status == 200
        else:
            next(iter(env.dashboard.sessions.values()))["csrf"] = "rotated"
        env.settings.proceed.set()
        response = await asyncio.wait_for(pending, timeout=2)
        assert response.status == (401 if revocation == "logout" else 403)
        assert env.settings.reauthorizations == 1
        assert env.settings.writes == []
        assert env.settings.refresh_count == 0
    finally:
        env.settings.proceed.set()
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_queued_admin_change_checks_current_field_authority(env):
    await login(env)
    h = await headers(env)
    env.client.guild.role_permissions = (1 << 3) | (1 << 5)
    env.stub.guilds[0]["permissions"] = str((1 << 3) | (1 << 5))
    env.settings.started, env.settings.proceed = asyncio.Event(), asyncio.Event()
    pending = asyncio.create_task(env.http.put("/dashboard/api/guilds/123", headers=h,
                                  json={"changes": {"access_level": "admin"},
                                        "expected": {"access_level": "owner"}}))
    try:
        await asyncio.wait_for(env.settings.started.wait(), timeout=2)
        # Manager access remains, but this particular field needs Administrator.
        env.client.guild.role_permissions = 1 << 5
        env.stub.guilds[0]["permissions"] = str(1 << 5)
        env.settings.proceed.set()
        assert (await asyncio.wait_for(pending, timeout=2)).status == 403
        assert not env.settings.writes
        assert env.settings.reauthorizations == 1
    finally:
        env.settings.proceed.set()
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "refresh"])
@pytest.mark.parametrize("different", ["guild", "user"])
async def test_reauthorization_must_preserve_guild_and_user(env, operation, different):
    await login(env)
    h = await headers(env)
    original = env.dashboard._guild
    calls = 0

    async def mismatched(request, write=False):
        nonlocal calls
        calls += 1
        guild, member = await original(request, write=write)
        if calls == 2:
            if different == "guild":
                guild = SimpleNamespace(id=124)
            else:
                member = SimpleNamespace(id=11)
        return guild, member

    env.dashboard._guild = mismatched
    url = "/dashboard/api/guilds/123"
    if operation == "update":
        response = await env.http.put(url, headers=h, json={"changes": {"x": 1}, "expected": {"x": 0}})
    else:
        response = await env.http.post(url + "/refresh", headers=h)
    assert response.status == 403
    assert calls == 2
    assert not env.settings.writes and env.settings.refresh_count == 0


@pytest.mark.asyncio
async def test_update_accepts_10000_large_ids_and_bounds_streamed_body(env):
    await login(env)
    h = await headers(env)
    url = "/dashboard/api/guilds/123"
    ids = [str(10**19 + i) for i in range(10_000)]
    # The standard aiohttp app/runner limit is above the explicitly streamed cap.
    assert env.http.server.app._client_max_size >= auth._MAX_BODY
    response = await env.http.put(url, headers=h, json={"changes": {"yuki_server_bans": ids},
                                                       "expected": {"yuki_server_bans": []}})
    assert response.status == 200
    assert env.settings.writes[-1][2]["yuki_server_bans"] == ids
    assert env.settings.expected[-1] == {"yuki_server_bans": []}

    # Reject both declared oversized lengths and chunked bodies with no length.
    response = await env.http.put(url, headers={**h, "Content-Type": "application/json"},
                                  data=b" " * (auth._MAX_BODY + 1))
    assert response.status == 413

    async def oversized():
        for _ in range((auth._MAX_BODY // 8192) + 1):
            yield b" " * 8192

    response = await env.http.put(url, headers={**h, "Content-Type": "application/json"}, data=oversized())
    assert response.status == 413
    assert len(env.settings.writes) == 1


@pytest.mark.asyncio
async def test_oauth_state_peer_limit_ignores_spoofed_forwarded_headers(env):
    for i in range(auth._MAX_STATES_PER_PEER):
        response = await env.http.get("/dashboard/login", allow_redirects=False,
                                      headers={"X-Forwarded-For": f"198.51.100.{i + 1}"})
        assert response.status == 302
    assert len(env.dashboard.states) == auth._MAX_STATES_PER_PEER
    response = await env.http.get("/dashboard/login", allow_redirects=False,
                                  headers={"X-Forwarded-For": "203.0.113.99", "Forwarded": "for=203.0.113.99"})
    assert response.status == 429
    assert "Too many pending Discord login attempts" in await response.text()
    assert response.headers["Retry-After"] == str(auth._STATE_TTL)
    assert len(env.dashboard.states) == auth._MAX_STATES_PER_PEER
    next(iter(env.dashboard.states.values()))["expires"] = time.monotonic() - 1
    assert (await env.http.get("/dashboard/login", allow_redirects=False)).status == 302
    assert len(env.dashboard.states) == auth._MAX_STATES_PER_PEER


@pytest.mark.asyncio
async def test_oauth_state_limit_is_per_actual_peer_and_global(monkeypatch, env):
    class Transport:
        def __init__(self, peer):
            self.peer = peer
        def get_extra_info(self, name):
            assert name == "peername"
            return (self.peer, 12345)

    monkeypatch.setattr(auth, "_MAX_STATES_PER_PEER", 1)
    monkeypatch.setattr(auth, "_LIMIT", 2)
    def request(peer):
        return SimpleNamespace(transport=Transport(peer), headers={"X-Forwarded-For": "203.0.113.5"})

    assert (await env.dashboard.login(request("192.0.2.1"))).status == 302
    # An IPv4-mapped IPv6 peer is the same peer, not a new quota.
    with pytest.raises(web.HTTPTooManyRequests):
        await env.dashboard.login(request("::ffff:192.0.2.1"))
    assert (await env.dashboard.login(request("192.0.2.2"))).status == 302
    with pytest.raises(web.HTTPTooManyRequests):
        await env.dashboard.login(request("192.0.2.3"))
    with pytest.raises(auth._Unavailable):
        await env.dashboard.login(SimpleNamespace(transport=None, headers={}))


@pytest.mark.asyncio
async def test_snapshot_uses_live_channel_overwrites_and_roles(env):
    await login(env)
    view_bit = 1 << 10
    env.client.guild.role_permissions |= view_bit
    env.stub.guilds[0]["permissions"] = str((1 << 5) | view_bit)

    class Channel:
        id = 777
        name = "private-channel"
        def __init__(self, hidden):
            self.hidden = hidden
        def overwrites_for(self, obj):
            if self.hidden and obj.id == 123:
                return discord.PermissionOverwrite(view_channel=False)
            return discord.PermissionOverwrite()

    env.client.guild.channels = [Channel(hidden=False)]  # Stale cache claims visible.
    env.client.guild.live_channels = [Channel(hidden=True)]

    async def snapshot(context, guild, member):
        assert guild.id == 123 and guild.owner_id == 99
        assert guild.fetch_member.__self__ is env.client.guild
        assert guild.default_role.id == guild.id
        assert {role.id for role in guild.roles} == {123, 456}
        return {"options": actual_settings._channel_options(guild, member, ["777"])}

    env.settings.get_settings = snapshot
    response = await env.http.get("/dashboard/api/guilds/123")
    assert response.status == 200
    assert (await response.json())["options"] == [{"value": "777", "label": "Unavailable channel (777)"}]
    env.client.guild.live_channels = [Channel(hidden=False)]
    response = await env.http.get("/dashboard/api/guilds/123")
    assert (await response.json())["options"] == [{"value": "777", "label": "private-channel"}]


@pytest.mark.asyncio
async def test_failing_live_channel_lookup_fails_closed(env):
    await login(env)
    async def fail():
        raise RuntimeError("Discord unavailable")
    env.client.guild.fetch_channels = fail
    assert (await env.http.get("/dashboard/api/guilds/123")).status == 503
