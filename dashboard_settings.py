"""Guild-scoped dashboard adapter for the bot's existing fast-memory settings.

Call these functions on the bot event loop with an authenticated, freshly resolved
Discord Member and its Guild.  This module does not authenticate HTTP requests.
Dashboard writes for the same guild are serialized, validated and committed in a
single fact transaction. Slash commands that read and then write without a
compare-and-update can still overwrite an intervening dashboard save.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import random
import re
import time
from types import SimpleNamespace

import discord
import moderation
import slang

# These are request limits, not a limit in the /yukiban command.  In particular,
# existing records are never truncated by a read or by changing another field.
MAX_BANS = 10000
MAX_NOTES = 15
MAX_NOTE_LENGTH = 200
_MAX_ID = (1 << 64) - 1
_LOCKS = {}  # (event loop, memory instance identity, guild id) -> asyncio.Lock

_BOOL_SETTINGS = {
    "enabled": ("Auto moderation", "Watch chat for enforceable server rules."),
    "exempt_staff": ("Exempt staff", "Do not moderate messages from staff members."),
    "slur_fallback": ("Always check slurs", "Check slurs even when no server rule covers them."),
    "harass_fallback": ("Always check bullying", "Check harassment even when no server rule covers it."),
    "directed_slurs": ("Flag directed allowed slurs", "Still flag allowed low-level slurs aimed at another person."),
    "reactions": ("Random emoji reactions", "Random reactions to chat; moderation warnings are separate."),
}
_ADMIN_KEYS = {"access_level", "access_roles", "access_users", "words_slur_allow"}
_SECTION_FACTS = {
    "moderation": {"settings", "staff_role_override"},
    "exemptions": {"exempt_channels", "skipped_rules", "rules"},
    "words": {"words"}, "notes": {"notes"}, "pings": {"ai_ping_policy"},
    "yuki_bans": {"yuki_server_bans"},
    "slang": {"slang_settings", "slang_terms", "slang_skipped"},
    "access": {"access"},
}
_STALE = "Settings changed since you opened them; refresh and try again."


def _check_actor(context, guild, member):
    gid = getattr(guild, "id", None)
    mid = getattr(member, "id", None)
    mguild = getattr(member, "guild", None)
    if (type(gid) is not int or not 0 < gid <= _MAX_ID or
            type(mid) is not int or not 0 < mid <= _MAX_ID or
            getattr(mguild, "id", None) != gid):
        raise PermissionError("A current member of this server is required.")
    interaction = SimpleNamespace(guild=guild, guild_id=gid, user=member)
    manage = bool(context.mod.can_manage(interaction))
    # Unlike can_admin, the dashboard deliberately does not elevate the bot's
    # global owner unless they actually own this guild or have Administrator.
    perms = getattr(member, "guild_permissions", None)
    admin = bool(mid == getattr(guild, "owner_id", None) or
                 (perms and getattr(perms, "administrator", False)))
    if not (manage or admin):
        raise PermissionError("You do not have access to this server's settings.")
    return manage, admin


def _lock(context, gid):
    key = (asyncio.get_running_loop(), id(context.mem), gid)
    return _LOCKS.setdefault(key, asyncio.Lock())


def _get(mem, gid, key, default):
    value = mem.fast_get(gid, key)
    return copy.deepcopy(value if value is not None else default)


def _field(key, label, description, kind, value, options=()):
    return {"key": key, "label": label, "description": description,
            "type": kind, "value": value, "options": list(options)}


def _option(value, label):
    return {"value": str(value), "label": str(label)}


def _ids(values):
    """Stored ids are ints in most facts, but a few older installs use strings."""
    return [str(x) for x in values if isinstance(x, (str, int)) and not isinstance(x, bool)]


def _role_options(guild, selected=(), *, access=False):
    out, seen = [], set()
    for role in getattr(guild, "roles", ()):
        value = str(role.id)
        is_default = role.is_default() if callable(getattr(role, "is_default", None)) else role.id == guild.id
        if access and is_default:
            continue
        if value not in seen:
            out.append(_option(value, role.name))
            seen.add(value)
    for value in selected:
        # Missing roles can be removed, but never add @everyone to access.
        if value not in seen and not (access and value == str(guild.id)):
            out.append(_option(value, "Unavailable role (" + value + ")"))
            seen.add(value)
    return out


def _channel_options(guild, member, selected=()):
    out, seen = [], set()
    for channel in getattr(guild, "channels", ()):
        # Do not disclose hidden channel names to delegated managers.  Existing
        # hidden selections are represented as unavailable and can be removed.
        # The auth layer supplies fresh role permissions; discord.py's usual
        # permissions_for() instead reads potentially stale cached role objects.
        try:
            perms = member.guild_permissions
            if hasattr(perms, "value") and callable(getattr(channel, "overwrites_for", None)):
                calculated = discord.Permissions(perms.value)
                if not calculated.administrator:
                    allow, deny = channel.overwrites_for(guild.default_role).pair()
                    calculated.handle_overwrite(allow.value, deny.value)
                    role_allow, role_deny = 0, 0
                    for role in member.roles:
                        if role.id != guild.id:
                            allow, deny = channel.overwrites_for(role).pair()
                            role_allow |= allow.value
                            role_deny |= deny.value
                    calculated.handle_overwrite(role_allow, role_deny)
                    allow, deny = channel.overwrites_for(member).pair()
                    calculated.handle_overwrite(allow.value, deny.value)
                visible = bool(calculated.view_channel)
            else:
                visible = bool(channel.permissions_for(member).view_channel)
        except (AttributeError, TypeError, ValueError):
            visible = False
        if visible:
            value = str(channel.id)
            if value not in seen:
                out.append(_option(value, channel.name))
                seen.add(value)
    for value in selected:
        if value not in seen:
            out.append(_option(value, "Unavailable channel (" + value + ")"))
            seen.add(value)
    return out


def _rule_token(prefix, entry, occurrence):
    raw = json.dumps([entry.get("n"), entry.get("text", ""), occurrence], ensure_ascii=False, separators=(",", ":"))
    return prefix + ":" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _rules_view(mod, gid):
    """Current rules plus unmatched saved entries; values never depend on list order."""
    rules = mod.rules(gid)[:25]
    entries = mod.skipped_entries(gid)
    options, selected, records, seen = [], [], {}, {}
    matched = set()
    for rule in rules:
        if not isinstance(rule, dict) or "n" not in rule or not isinstance(rule.get("text"), str):
            continue
        identity = (str(rule["n"]), rule["text"])
        occurrence = seen.get(identity, 0)
        seen[identity] = occurrence + 1
        token = _rule_token("rule", rule, occurrence)
        options.append(_option(token, f"{rule['n']}. {rule['text']}"))
        matches = [i for i, e in enumerate(entries) if i not in matched and isinstance(e, dict) and mod._is_skipped(rule, [e])]
        if matches:
            # Keep the original stored wording (and any future metadata) rather
            # than replacing it merely because the parsed rule was rephrased.
            matched.add(matches[0])
            records[token] = copy.deepcopy(entries[matches[0]])
            selected.append(token)
        else:
            records[token] = {"n": rule["n"], "text": rule["text"]}
    seen = {}
    for i, entry in enumerate(entries):
        if i in matched or not isinstance(entry, dict) or "n" not in entry or not isinstance(entry.get("text"), str):
            continue
        identity = (str(entry["n"]), entry["text"])
        occurrence = seen.get(identity, 0)
        seen[identity] = occurrence + 1
        token = _rule_token("saved", entry, occurrence)
        options.append(_option(token, f"Previously skipped: {entry['n']}. {entry['text']}"))
        records[token] = copy.deepcopy(entry)
        selected.append(token)
    return options, selected, records


def _sections(context, guild, member, manage, admin):
    gid, mod, mem = guild.id, context.mod, context.mem
    sections = []
    if manage:
        settings = mod.settings(gid)
        fields = [_field(k, label, description, "boolean", bool(settings[k]))
                  for k, (label, description) in _BOOL_SETTINGS.items()]
        role = _get(mem, gid, "staff_role_override", None)
        role_value = str(role) if role else ""
        fields.append(_field("staff_role_override", "Staff alert role", "Choose a role for moderation alerts, or automatic. A non-mentionable role may not notify anyone.",
                             "select", role_value, [_option("", "Automatic")] + _role_options(guild, [role_value] if role_value else [])))
        sections.append({"id": "moderation", "title": "Moderation", "description": "Configure moderation and reactions.", "fields": fields})

        channels = _ids(mod.exempt_channels(gid))
        rule_opts, chosen, _ = _rules_view(mod, gid)
        sections.append({"id": "exemptions", "title": "Moderation exemptions", "description": "Skip selected channels, categories, or rules.", "fields": [
            _field("exempt_channels", "Excluded channels and categories", "No moderation in these channels or their threads; categories include child channels. Maximum 50.", "multi_select", channels, _channel_options(guild, member, channels)),
            _field("skipped_rules", "Rules not enforced", "Yuki still knows these rules. Previously saved unmatched rules are shown so they can be retained or removed.", "multi_select", chosen, rule_opts),
        ]})
        w = mod.words(gid)
        fields = [
            _field("words_mode", "Swear filter mode", "Custom mode only checks your blocked words for swearing; slur checks are separate.", "select", str(w["mode"]), [_option("default", "Built-in list plus blocked words"), _option("custom", "Only blocked words")]),
            _field("words_allow", "Allowed swear exceptions", "One word or phrase per line. Slurs cannot be added here. Maximum 40.", "lines", list(w["allow"])),
            _field("words_block", "Blocked words", "One word or phrase per line. Maximum 40; these are checked even without a matching rule.", "lines", list(w["block"])),
        ]
        if admin:
            fields.append(_slur_field(w))
        sections.append({"id": "words", "title": "Word filters", "description": "Configure the swear filter and its exceptions.", "fields": fields})
        notes = _get(mem, gid, "notes", []) or []
        sections.append({"id": "notes", "title": "Server notes", "description": "Facts staff want Yuki to know about this server.", "fields": [
            _field("notes", "Notes", "One note per line, up to 15 notes of 200 characters each.", "lines", list(notes)),
        ]})
        policy = _get(mem, gid, "ai_ping_policy", {}) or {}
        sections.append({"id": "pings", "title": "Conversational pings", "description": "Does not change moderation alerts.", "fields": [
            _field("ping_users", "Member pings", "Allow Yuki to ping individual members in replies.", "boolean", bool(policy.get("users", True))),
            _field("ping_mass", "Mass pings", "Allow everyone/here only on staff requests and when Discord permits it.", "boolean", bool(policy.get("mass", False))),
        ]})
        bans = _get(mem, gid, "yuki_server_bans", {}) or {}
        sections.append({"id": "yuki_bans", "title": "Yuki server bans", "description": "Control access to Yuki in this server, not Discord bans.", "fields": [
            _field("yuki_server_bans", "Banned user IDs", "One numeric Discord ID per line. New bans must be current server members; existing bans can be removed after they leave.", "lines", list(bans)),
        ]})
        ss = _get(mem, gid, "slang_settings", {}) or {}
        off = _ids(ss.get("off_channels") or [])
        terms = _get(mem, gid, "slang_terms", {}) or {}
        term_options = [_option("", "Choose a learned word")]
        for term, record in sorted(terms.items()):
            if isinstance(term, str) and isinstance(record, dict) and isinstance(record.get("meaning"), str):
                term_options.append(_option(term, f"{term}: {record['meaning']}"))
        sections.append({"id": "slang", "title": "Slang learning", "description": "Control where Yuki asks about slang and forget learned words.", "fields": [
            _field("slang_enabled", "Ask about slang", "Allow occasional slang questions; a global bot configuration can still disable them.", "boolean", bool(ss.get("enabled", True))),
            _field("slang_off_channels", "Excluded channels and categories", "Never ask in these channels or their threads; categories include child channels. Maximum 50.", "multi_select", off, _channel_options(guild, member, off)),
            _field("slang_forget", "Forget a learned word", "Choose a word to forget its definition and let Yuki ask about it again.", "select", "", term_options),
            _field("slang_forget_other", "Forget another asked word", "Enter an already asked word to let Yuki ask again; this also removes its definition if present.", "text", ""),
        ]})
    elif admin:
        # Low-level slur exceptions and access policy remain administrator-only,
        # even when the dashboard user has delegated moderation access.
        sections.append({"id": "words", "title": "Word filters", "description": "Administrator-only exceptions.", "fields": [_slur_field(mod.words(gid))]})
    if admin:
        access = mod.access(gid)
        roles = _ids(access["roles"])
        sections.append({"id": "access", "title": "Command access", "description": "Only the server owner or an administrator may change access.", "fields": [
            _field("access_level", "Minimum permission", "Added roles and users may also use the mod commands.", "select", access["level"], [_option(k, v) for k, v in moderation.ACCESS_LEVELS.items()]),
            _field("access_roles", "Additional roles", "Members with these roles may use the mod commands. @everyone is not allowed. Maximum 25.", "multi_select", roles, _role_options(guild, roles, access=True)),
            _field("access_users", "Additional user IDs", "One current non-bot server member ID per line. Maximum 25.", "lines", _ids(access["users"])),
        ]})
    return sections


def _slur_field(words):
    return _field("words_slur_allow", "Allowed low-level slurs", "Administrator only. Only the fixed list can be allowed; other slurs cannot be exempted.",
                  "multi_select", list(words["slur_allow"]), [_option(x, x) for x in moderation.LOW_LEVEL_SLURS])


async def get_settings(context, guild, member):
    manage, admin = _check_actor(context, guild, member)
    icon = getattr(guild, "icon", None)
    return {"guild": {"id": str(guild.id), "name": str(guild.name), "icon_url": str(icon.url) if icon else ""},
            "sections": _sections(context, guild, member, manage, admin)}


def _section_schema(context, guild, member, manage, admin, changes, expected):
    sections = _sections(context, guild, member, manage, admin)
    by_key = {field["key"]: section for section in sections for field in section["fields"]}
    if any(key not in by_key for key in changes):
        raise PermissionError("Your permission to change these settings has expired.")
    touched = {by_key[key]["id"] for key in changes}
    schema = {field["key"]: field for section in sections for field in section["fields"]}
    # The frontend supplies original baseline values for every editable field
    # in each changed section, not just the dirty fields.
    for section in sections:
        if section["id"] not in touched:
            continue
        for field in section["fields"]:
            if field.get("disabled") or field.get("read_only"):
                continue
            key = field["key"]
            if key not in expected:
                raise ValueError("A complete settings baseline is required; refresh and try again.")
            try:
                actual_json = json.dumps(field["value"], sort_keys=True, allow_nan=False)
                expected_json = json.dumps(expected[key], sort_keys=True, allow_nan=False)
            except (TypeError, ValueError):
                raise ValueError("Invalid settings baseline; refresh and try again.") from None
            if actual_json != expected_json:
                raise ValueError(_STALE)
    return schema, touched


async def _fresh_actor(context, guild, member, reauthorize):
    if not callable(reauthorize):
        raise PermissionError("Fresh authorization is required to change settings.")
    gid, uid = getattr(guild, "id", None), getattr(member, "id", None)
    fresh_guild, fresh_member = await reauthorize()
    if getattr(fresh_guild, "id", None) != gid or getattr(fresh_member, "id", None) != uid:
        raise PermissionError("The server or member changed; please sign in again.")
    manage, admin = _check_actor(context, fresh_guild, fresh_member)
    return fresh_guild, fresh_member, manage, admin


def _fact_baseline(mem, gid, keys):
    absent = object()
    result = {}
    for key in keys:
        value = mem.fast_get(gid, key, absent)
        result[key] = (value is not absent, copy.deepcopy(value) if value is not absent else None)
    return result


def _id_string(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value):
        raise ValueError(f"{field}: enter numeric Discord IDs as strings, one per line.")
    n = int(value)
    if n <= 0 or n > _MAX_ID or str(n) != value:
        raise ValueError(f"{field}: invalid Discord ID {value!r}.")
    return value


def _list(value, field, limit):
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise ValueError(f"{field}: expected a list of strings.")
    if len(value) > limit:
        raise ValueError(f"{field}: maximum {limit} entries.")
    if any("\n" in x or "\r" in x or "\x00" in x for x in value):
        raise ValueError(f"{field}: each entry must be a single line.")
    if len(set(value)) != len(value):
        raise ValueError(f"{field}: duplicate entries are not allowed.")
    return list(value)


def _choices(value, field, options, limit):
    values = _list(value, field, limit)
    allowed = {o["value"] for o in options}
    if any(v not in allowed for v in values):
        raise ValueError(f"{field}: a selection is unavailable or belongs to another server; refresh and try again.")
    return values


async def _resolve_new_member(guild, value, field, *, allow_bot):
    uid = int(value)
    fetch = getattr(guild, "fetch_member", None)
    if callable(fetch):
        try:
            found = await fetch(uid)
        except Exception as exc:
            raise ValueError(f"{field}: unable to verify that {value} is a current server member.") from exc
    else:
        # Lightweight test doubles may only implement get_member. Real Discord
        # guilds have fetch_member and always take the live REST path above.
        getter = getattr(guild, "get_member", None)
        found = getter(uid) if callable(getter) else None
    if found is None or getattr(found, "id", None) != uid or getattr(getattr(found, "guild", None), "id", None) != guild.id:
        raise ValueError(f"{field}: {value} is not a current member of this server.")
    if not allow_bot and getattr(found, "bot", False):
        raise ValueError(f"{field}: bots cannot be given command access.")


def _word_list(raw, field):
    values = _list(raw, field, moderation.MAX_WORDS)
    result = []
    for value in values:
        word = moderation.clean_word(value)
        if not word:
            raise ValueError(f"{field}: each word must contain 2–32 valid characters.")
        if word in result:
            raise ValueError(f"{field}: duplicate words are not allowed.")
        result.append(word)
    return result


async def _persist(context, gid, baseline, writes):
    # If the caller disconnects, don't release our per-guild lock while the
    # to_thread write is still running.
    task = asyncio.create_task(context.run(context.mem.fast_compare_and_update, gid, baseline, writes))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


async def update_settings(context, guild, member, changes, expected=None, reauthorize=None):
    if not isinstance(changes, dict) or not changes or any(not isinstance(k, str) for k in changes):
        raise ValueError("Changes must be a non-empty map of setting keys to values.")
    if not isinstance(expected, dict):
        raise ValueError("A complete settings baseline is required; refresh and try again.")
    async with _lock(context, getattr(guild, "id", None)):
        manage, admin = _check_actor(context, guild, member)
        gid, mod, mem = guild.id, context.mod, context.mem
        allowed_keys = set(_BOOL_SETTINGS) | {"staff_role_override", "exempt_channels", "skipped_rules", "words_mode", "words_allow", "words_block", "notes", "ping_users", "ping_mass", "yuki_server_bans", "slang_enabled", "slang_off_channels", "slang_forget", "slang_forget_other"} | _ADMIN_KEYS
        unknown = set(changes) - allowed_keys
        if unknown:
            raise ValueError("Unknown setting: " + ", ".join(sorted(unknown)))
        if set(changes) & _ADMIN_KEYS and not admin:
            raise PermissionError("Only the server owner or an administrator can change access or allowed slurs.")
        if set(changes) - _ADMIN_KEYS and not manage:
            raise PermissionError("You do not have access to the mod commands in this server.")
        schema, touched = _section_schema(context, guild, member, manage, admin, changes, expected)
        targets = set()
        for key in changes:
            if key in _BOOL_SETTINGS:
                targets.add("settings")
            elif key.startswith("words_"):
                targets.add("words")
            elif key.startswith("access_"):
                targets.add("access")
            elif key.startswith("ping_"):
                targets.add("ai_ping_policy")
            elif key in ("slang_enabled", "slang_off_channels"):
                targets.add("slang_settings")
            elif key in ("slang_forget", "slang_forget_other"):
                targets.update(("slang_terms", "slang_skipped"))
            else:
                targets.add(key)
        dependencies = set(targets) | {"access"}
        for section_id in touched:
            dependencies.update(_SECTION_FACTS[section_id])
        baseline = _fact_baseline(mem, gid, dependencies)
        # Work on detached copies. All validation, including member resolution,
        # occurs before the first write.
        writes = {}
        def obj(key):
            if key not in writes:
                old = _get(mem, gid, key, {}) or {}
                if not isinstance(old, dict):
                    raise ValueError(f"Saved {key} is invalid; cannot safely update it.")
                writes[key] = old
            return writes[key]
        for key, value in changes.items():
            spec = schema[key]
            kind = spec["type"]
            if kind == "boolean" and type(value) is not bool:
                raise ValueError(f"{key}: expected true or false.")
            if kind in ("select", "text") and not isinstance(value, str):
                raise ValueError(f"{key}: expected text.")
            if kind == "select" and value not in {o["value"] for o in spec["options"]}:
                raise ValueError(f"{key}: selection is unavailable; refresh and try again.")
            if key in _BOOL_SETTINGS:
                obj("settings")[key] = value
            elif key in ("ping_users", "ping_mass"):
                obj("ai_ping_policy")[key.removeprefix("ping_")] = value
            elif key == "slang_enabled":
                obj("slang_settings")["enabled"] = value
            elif key == "staff_role_override":
                if value:
                    _id_string(value, key)
                    writes[key] = int(value)
                else:
                    # None has the same automatic-selection behavior as deletion.
                    writes[key] = None
            elif key in ("exempt_channels", "slang_off_channels", "access_roles"):
                limit = moderation.MAX_ACCESS if key == "access_roles" else (slang.MAX_OFF_CHANNELS if key == "slang_off_channels" else moderation.MAX_EXEMPT_CHANNELS)
                selected = _choices(value, key, spec["options"], limit)
                ids = [int(_id_string(v, key)) for v in selected]
                if key == "access_roles":
                    if guild.id in ids:
                        raise ValueError("access_roles: @everyone cannot be granted access.")
                    obj("access")["roles"] = ids
                elif key == "slang_off_channels":
                    obj("slang_settings")["off_channels"] = ids
                else:
                    writes[key] = ids
            elif key == "skipped_rules":
                selected = _choices(value, key, spec["options"], 25)
                _, _, records = _rules_view(mod, gid)
                writes[key] = [records[token] for token in selected]
            elif key in ("words_mode", "access_level"):
                obj("words" if key == "words_mode" else "access")["mode" if key == "words_mode" else "level"] = value
            elif key in ("words_allow", "words_block"):
                words = _word_list(value, key)
                if key == "words_allow":
                    if any(moderation.has_slur(w) for w in words):
                        raise ValueError("words_allow: slurs cannot be allowed as swear exceptions.")
                    existing = mod.words(gid)
                    for word in words:
                        if (word not in existing["allow"] and word not in existing["block"] and
                                not moderation.has_strong_swear(word) and not mod._profane(gid, word)):
                            raise ValueError(f"words_allow: {word!r} is not blocked by the swear filter.")
                obj("words")[key.removeprefix("words_")] = words
            elif key == "words_slur_allow":
                obj("words")["slur_allow"] = _choices(value, key, spec["options"], len(moderation.LOW_LEVEL_SLURS))
            elif key == "notes":
                notes = _list(value, key, MAX_NOTES)
                notes = [re.sub(r"\s+", " ", x).strip() for x in notes]
                if any(not x or len(x) > MAX_NOTE_LENGTH for x in notes):
                    raise ValueError("notes: each note must contain 1–200 characters.")
                writes[key] = notes
            elif key in ("access_users", "yuki_server_bans"):
                vals = _list(value, key, moderation.MAX_ACCESS if key == "access_users" else MAX_BANS)
                vals = [_id_string(v, key) for v in vals]
                if key == "access_users":
                    existing = set(_ids(mod.access(gid)["users"]))
                else:
                    existing = set((_get(mem, gid, key, {}) or {}).keys())
                for uid in vals:
                    if uid not in existing:
                        await _resolve_new_member(guild, uid, key, allow_bot=key == "yuki_server_bans")
                if key == "access_users":
                    obj("access")["users"] = [int(x) for x in vals]
                else:
                    old = _get(mem, gid, key, {}) or {}
                    now = int(time.time())
                    writes[key] = {uid: old[uid] if uid in old else {"by": member.id, "at": now} for uid in vals}
            elif key in ("slang_forget", "slang_forget_other"):
                term = value.strip().lower()
                if key == "slang_forget" and not term:
                    raise ValueError("slang_forget: choose a word to forget.")
                if not term or len(term) > 32 or any(c in term for c in "\r\n\x00"):
                    raise ValueError(f"{key}: enter a single word or phrase of at most 32 characters.")
                learned = obj("slang_terms")
                skipped = writes.get("slang_skipped", _get(mem, gid, "slang_skipped", []) or [])
                if not isinstance(skipped, list):
                    raise ValueError("Saved slang history is invalid; cannot safely update it.")
                if term not in learned and term not in skipped:
                    raise ValueError(f"{key}: Yuki has not learned or asked about that word.")
                learned.pop(term, None)
                writes["slang_skipped"] = [s for s in skipped if s != term]
        # Moving a word into one list removes it from the opposite list unless
        # both lists were explicitly edited in the same dashboard save.
        if "words" in writes:
            data = writes["words"]
            if "words_allow" in changes and "words_block" not in changes:
                newly = set(data["allow"]) - set(mod.words(gid)["allow"])
                if newly:
                    data["block"] = [w for w in data.get("block", []) if w not in newly]
            if "words_block" in changes and "words_allow" not in changes:
                newly = set(data["block"]) - set(mod.words(gid)["block"])
                if newly:
                    data["allow"] = [w for w in data.get("allow", []) if w not in newly]
            if "words_allow" in changes and "words_block" in changes and set(data.get("allow", [])) & set(data.get("block", [])):
                raise ValueError("A word cannot be both allowed and blocked.")
        guild, member, manage_now, admin_now = await _fresh_actor(context, guild, member, reauthorize)
        if (set(changes) & _ADMIN_KEYS and not admin_now) or (set(changes) - _ADMIN_KEYS and not manage_now):
            raise PermissionError("Your permission to change these settings has expired.")
        fresh_schema, _ = _section_schema(context, guild, member, manage_now, admin_now, changes, expected)
        # Revalidate options against the freshly resolved guild and member.
        for key, value in changes.items():
            spec = fresh_schema[key]
            if spec["type"] == "select" and value not in {o["value"] for o in spec["options"]}:
                raise ValueError(f"{key}: selection is unavailable; refresh and try again.")
            if spec["type"] == "multi_select":
                allowed = {o["value"] for o in spec["options"]}
                if any(item not in allowed for item in value):
                    raise ValueError(f"{key}: a selection is unavailable; refresh and try again.")
        await _persist(context, gid, baseline, writes)
        return await get_settings(context, guild, member)


async def refresh(context, guild, member, reauthorize=None):
    manage, _ = _check_actor(context, guild, member)
    if not manage:
        raise PermissionError("You do not have access to the mod commands in this server.")
    async with _lock(context, guild.id):
        # Resolve live membership/permissions only after waiting for the lock.
        guild, member, manage, _ = await _fresh_actor(context, guild, member, reauthorize)
        if not manage:
            raise PermissionError("You do not have access to the mod commands in this server.")
        try:
            await context.mod.refresh_roles(guild)
        except Exception as exc:
            raise RuntimeError("Role refresh failed. Please try again.") from exc
        try:
            rules = await context.mod.refresh_rules(guild, force=True)
        except Exception as exc:
            raise RuntimeError("Roles were refreshed, but rules refresh failed. Please try again.") from exc
        context.mod._next_rules[guild.id] = time.time() + random.uniform(4, 10) * 3600
        # refresh_rules returns human-readable strings that may include channel
        # names. Never send the unfiltered string (including failure statuses).
        if isinstance(rules, str) and rules.startswith("read ") and " rules from #" in rules:
            return "Roles and rules refreshed."
        if isinstance(rules, str) and "hasn't changed, still " in rules:
            return "Roles refreshed; rules have not changed."
        if isinstance(rules, str) and rules.startswith("couldn't find a public rules channel"):
            return "Roles refreshed, but no public rules channel was found."
        if isinstance(rules, str) and "discord wouldn't let me read it" in rules:
            return "Roles refreshed, but the rules channel could not be read."
        if isinstance(rules, str) and "has nothing readable in it" in rules:
            return "Roles refreshed, but no readable rules were found."
        if isinstance(rules, str) and "the ai gave me junk" in rules:
            return "Roles refreshed, but the rules could not be parsed. Please try again."
        return "Roles refreshed, but the rules refresh did not report success. Please try again."
