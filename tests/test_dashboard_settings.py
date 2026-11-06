import asyncio
import copy
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import dashboard_settings as ds
import memory
import moderation


class Memory:
    def __init__(self):
        self.data = {}
        self.writes = []

    def fast_get(self, gid, key, default=None):
        return self.data.get(gid, {}).get(key, default)

    def fast_set(self, gid, key, value):
        self.data.setdefault(gid, {})[key] = copy.deepcopy(value)
        self.writes.append((gid, key, copy.deepcopy(value)))

    def fast_compare_and_update(self, gid, expected, updates):
        facts = self.data.get(gid, {})
        for key, (present, value) in expected.items():
            if (key in facts) != present or (present and not memory._same_json(facts[key], value)):
                raise memory.FactConflictError("Settings changed while saving; refresh and try again.")
        self.data[gid] = {**facts, **copy.deepcopy(updates)}
        self.writes.extend((gid, key, copy.deepcopy(value)) for key, value in updates.items())


class Role:
    def __init__(self, ident, name, default=False):
        self.id, self.name, self.default = ident, name, default

    def is_default(self):
        return self.default


class Channel:
    def __init__(self, ident, name, visible=True):
        self.id, self.name, self.visible = ident, name, visible

    def permissions_for(self, member):
        return SimpleNamespace(view_channel=self.visible)


class Member:
    def __init__(self, guild, ident, *, admin=False, manage=False, roles=(), bot=False):
        self.id, self.guild, self.roles, self.bot = ident, guild, list(roles), bot
        self.guild_permissions = SimpleNamespace(administrator=admin, manage_guild=manage)


class Guild:
    def __init__(self, ident=100):
        self.id, self.name, self.owner_id = ident, "Example", 101
        self.icon = SimpleNamespace(url="https://example.test/icon.png")
        self.roles = [Role(ident, "@everyone", True), Role(301, "Moderators"), Role(302, "Members")]
        self.channels = [Channel(401, "general"), Channel(402, "category"), Channel(403, "private-staff", False)]
        self.members = {}

    def get_member(self, ident):
        return self.members.get(ident)

    async def fetch_member(self, ident):
        if ident not in self.members:
            raise LookupError("not found")
        return self.members[ident]


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.g = Guild()
        self.mem = Memory()

        async def run(fn, *args):
            await asyncio.sleep(0)
            return fn(*args)

        self.run = run
        self.mod = moderation.Mod(None, self.mem, None, run)
        self.ctx = SimpleNamespace(mem=self.mem, mod=self.mod, run=run)
        self.admin = Member(self.g, 111, admin=True)
        self.owner = Member(self.g, 101)
        self.manager = Member(self.g, 112, manage=True)
        self.normal = Member(self.g, 113)
        self.bot = Member(self.g, 114, bot=True)
        for m in (self.admin, self.owner, self.manager, self.normal, self.bot):
            self.g.members[m.id] = m
        self.gid = self.g.id
        self.mem.data = {
            0: {"groq_keys": ["SECRET_KEY"], "yuki_global_bans": {"777": {"by": 999}}},
            self.gid: {
                "private_personal_memories": ["PRIVATE NOTE"],
                "settings": {"enabled": False, "future_setting": "untouched"},
                "rules": [{"n": 1, "text": "Be respectful", "category": "harassment"},
                          {"n": 2, "text": "No advertising", "category": "invites"}],
                "rules_meta": {"channel_id": 401, "channel": "general"},
                "skipped_rules": [{"n": 1, "text": "Be respectful", "extra": "keep"},
                                  {"n": 9, "text": "Old rule"}],
                "exempt_channels": [401, 403],
                "staff_role_override": 301,
                "words": {"allow": ["shit"], "block": ["foobar"], "mode": "default", "slur_allow": ["retard"], "future": 7},
                "notes": ["server note"],
                "access": {"level": "manage_server", "roles": [302], "users": [113], "future": "keep"},
                "ai_ping_policy": {"users": False, "mass": True, "future": 8},
                "yuki_server_bans": {"999": {"by": 444, "at": 42, "reason": "unchanged"}},
                "slang_settings": {"enabled": False, "off_channels": [402], "future": 6},
                "slang_terms": {"glorp": {"meaning": "cool thing", "ts": 100, "future": 2},
                                "blip": {"meaning": "small thing", "ts": 101}},
                "slang_skipped": ["glorp", "blip", "alreadyasked"],
            },
            200: {"notes": ["OTHER SERVER NOTE"], "settings": {"enabled": True}},
        }

    async def fields(self, member=None):
        result = await ds.get_settings(self.ctx, self.g, member or self.admin)
        return {f["key"]: f for s in result["sections"] for f in s["fields"]}

    async def update(self, context, guild, member, changes, expected=None, reauthorize=None):
        if expected is None:
            snapshot = await ds.get_settings(context, guild, member)
            expected = {f["key"]: copy.deepcopy(f["value"])
                        for section in snapshot["sections"] for f in section["fields"]}
        if reauthorize is None:
            async def reauthorize():
                return guild, member
        return await ds.update_settings(context, guild, member, changes,
                                        expected=expected, reauthorize=reauthorize)

    async def refresh(self, context, guild, member, reauthorize=None):
        if reauthorize is None:
            async def reauthorize():
                return guild, member
        return await ds.refresh(context, guild, member, reauthorize=reauthorize)

    async def test_schema_defaults_mapping_and_no_private_or_global_data(self):
        result = await ds.get_settings(self.ctx, self.g, self.admin)
        fields = await self.fields()
        expected = {"enabled", "exempt_staff", "slur_fallback", "harass_fallback", "directed_slurs", "reactions",
                    "yuki_talk_channels", "staff_role_override", "exempt_channels", "skipped_rules", "rule_annotations", "notes", "access_level", "access_roles", "access_users",
                    "words_mode", "words_allow", "words_block", "words_slur_allow", "ping_users", "ping_mass", "yuki_server_bans",
                    "slang_enabled", "slang_off_channels", "slang_forget", "slang_forget_other"}
        self.assertEqual(expected, set(fields))
        self.assertEqual(result["guild"], {"id": "100", "name": "Example", "icon_url": "https://example.test/icon.png"})
        self.assertFalse(fields["enabled"]["value"])
        for key in ("exempt_staff", "slur_fallback", "harass_fallback", "directed_slurs", "reactions"):
            self.assertTrue(fields[key]["value"])
        self.assertEqual(fields["access_users"]["value"], ["113"])
        self.assertEqual(fields["exempt_channels"]["value"], ["401", "403"])
        self.assertFalse(fields["slang_enabled"]["value"])
        self.assertEqual(fields["slang_off_channels"]["value"], ["402"])
        self.assertEqual(fields["words_slur_allow"]["value"], ["retard"])
        self.assertEqual(fields["ping_users"]["value"], False)
        self.assertEqual(fields["ping_mass"]["value"], True)
        self.assertEqual(fields["yuki_server_bans"]["value"], ["999"])
        self.assertEqual(fields["yuki_talk_channels"]["value"], [])
        self.assertEqual(fields["yuki_talk_channels"]["type"], "multi_select")
        self.assertEqual(fields["rule_annotations"]["type"], "rules_editor")
        self.assertEqual(fields["rule_annotations"]["value"][0]["source_channel"], "#general")
        self.assertEqual(fields["rule_annotations"]["value"][0]["text"], "Be respectful")
        data = json.dumps(result)
        for secret in ("SECRET_KEY", "PRIVATE NOTE", "OTHER SERVER NOTE", "private-staff", '"reason"', '"at"'):
            self.assertNotIn(secret, data)
        for section in result["sections"]:
            self.assertTrue(all(k in section for k in ("id", "title", "description", "fields")))
            for field in section["fields"]:
                self.assertTrue(all(k in field for k in ("key", "label", "description", "type", "value", "options")))
                self.assertTrue(all(isinstance(o["value"], str) and isinstance(o["label"], str) for o in field["options"]))
                if field["type"] == "boolean": self.assertIs(type(field["value"]), bool)
                if field["type"] in ("select", "text"): self.assertIs(type(field["value"]), str)
                if field["type"] in ("multi_select", "lines"): self.assertTrue(all(isinstance(v, str) for v in field["value"]))
                if field["type"] == "rules_editor": self.assertTrue(all(isinstance(v, dict) for v in field["value"]))

    async def test_default_values_without_facts(self):
        self.mem.data[self.gid] = {}
        fields = await self.fields()
        self.assertTrue(fields["enabled"]["value"])
        self.assertTrue(fields["ping_users"]["value"])
        self.assertFalse(fields["ping_mass"]["value"])
        self.assertTrue(fields["slang_enabled"]["value"])
        self.assertEqual(fields["access_level"]["value"], "manage_server")
        self.assertEqual(fields["words_mode"]["value"], "default")
        self.assertEqual(fields["staff_role_override"]["value"], "")

    async def test_permissions_and_cross_guild(self):
        # Delegates may manage ordinary controls but never access or slur exceptions.
        fields = await self.fields(self.normal)
        self.assertIn("enabled", fields)
        self.assertNotIn("access_level", fields)
        self.assertNotIn("words_slur_allow", fields)
        with self.assertRaises(PermissionError):
            await self.update(self.ctx, self.g, self.normal, {"access_level": "owner"})
        with self.assertRaises(PermissionError):
            await self.update(self.ctx, self.g, self.normal, {"words_slur_allow": []})
        stranger = Member(self.g, 500)
        with self.assertRaises(PermissionError):
            await ds.get_settings(self.ctx, self.g, stranger)
        other = Guild(200)
        foreign_admin = Member(other, self.admin.id, admin=True)
        for call in (ds.get_settings, ds.refresh):
            with self.assertRaises(PermissionError):
                await call(self.ctx, self.g, foreign_admin)
        with self.assertRaises(PermissionError):
            await self.update(self.ctx, self.g, foreign_admin, {"enabled": False})
        self.assertEqual(self.mem.writes, [])

    async def test_owner_only_level_does_not_elevate_admin_for_general_settings(self):
        self.mem.data[self.gid]["access"]["level"] = "owner"
        fields = await self.fields(self.admin)
        self.assertNotIn("enabled", fields)
        self.assertIn("access_level", fields)
        self.assertIn("words_slur_allow", fields)
        with self.assertRaises(PermissionError):
            await self.update(self.ctx, self.g, self.admin, {"enabled": True})
        await self.update(self.ctx, self.g, self.admin, {"words_slur_allow": ["fag"]})
        self.assertEqual(self.mem.fast_get(self.gid, "words")["slur_allow"], ["fag"])
        with self.assertRaises(PermissionError):
            await self.refresh(self.ctx, self.g, self.admin)
        await self.update(self.ctx, self.g, self.admin, {"access_level": "manage_server"})
        self.assertIn("enabled", await self.fields(self.admin))

    async def test_global_bot_owner_is_not_automatically_guild_admin(self):
        self.mod.is_bot_owner = lambda uid: uid == self.normal.id
        # Remove delegation; bot owner may manage general settings via command parity.
        self.mem.data[self.gid]["access"]["users"] = []
        fields = await self.fields(self.normal)
        self.assertIn("enabled", fields)
        self.assertNotIn("access_level", fields)
        with self.assertRaises(PermissionError):
            await self.update(self.ctx, self.g, self.normal, {"words_slur_allow": []})

    async def test_unknown_or_invalid_input_rejected_without_writes(self):
        bad = [{"global_bans": ["777"]}, {"groq_keys": []}, {"enabled": "true"}, {"enabled": 1},
               {"words_mode": "anything"}, {"access_level": "superuser"}, {"notes": "hello"},
               {"notes": ["a\nb"]}, {"notes": ["x" * 201]}, {"notes": ["x"] * 16},
               {"exempt_channels": [401]}, {"exempt_channels": ["9999"]},
               {"staff_role_override": "9999"}, {"skipped_rules": ["0"]},
               {"access_roles": ["100"]}, {"access_roles": ["9999"]},
               {"access_users": ["112x"]}, {"access_users": ["0112"]}, {"access_users": ["114"]},
               {"access_users": ["999999"]}, {"yuki_server_bans": ["-42"]},
               {"words_block": ["x"]}, {"words_block": ["x" * 33]},
               {"words_allow": ["foobarzzz"]}, {"words_allow": ["nigger"]},
               {"words_slur_allow": ["nigger"]}, {"words_slur_allow": ["REtard"]},
               {"slang_off_channels": ["403"]}, {"slang_forget": "not-in-list"},
               {"slang_forget_other": "unknown"}, {}, {"enabled": True, "notes": ["x" * 201]}]
        for change in bad:
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    await self.update(self.ctx, self.g, self.admin, change)
        self.assertEqual(self.mem.writes, [])

    async def test_cross_guild_role_channel_member_and_id_validation(self):
        other = Guild(200)
        other.members[600] = Member(other, 600)
        # Even a compromised or incorrect resolver must not return a foreign member.
        self.g.members[600] = other.members[600]
        for field, value in [("access_users", ["600"]), ("yuki_server_bans", ["600"]),
                             ("exempt_channels", ["999"]), ("access_roles", ["999"])]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                await self.update(self.ctx, self.g, self.admin, {field: value})
        for value in ["0", "-1", "１２３", "18446744073709551616", "1e4"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                await self.update(self.ctx, self.g, self.admin, {"yuki_server_bans": [value]})

    async def test_updates_preserve_unknown_members_and_settings(self):
        await self.update(self.ctx, self.g, self.admin, {"enabled": True, "reactions": False, "ping_users": True,
                                                                  "slang_enabled": True, "slang_off_channels": ["401"],
                                                                  "words_mode": "custom", "access_level": "admin"})
        facts = self.mem.data[self.gid]
        self.assertEqual(facts["settings"], {"enabled": True, "reactions": False, "future_setting": "untouched"})
        self.assertEqual(facts["ai_ping_policy"], {"users": True, "mass": True, "future": 8})
        self.assertEqual(facts["slang_settings"], {"enabled": True, "off_channels": [401], "future": 6})
        self.assertEqual(facts["words"]["mode"], "custom")
        self.assertEqual(facts["words"]["slur_allow"], ["retard"])
        self.assertEqual(facts["words"]["future"], 7)
        self.assertEqual(facts["access"], {"level": "admin", "roles": [302], "users": [113], "future": "keep"})
        self.assertEqual(self.mem.data[0]["groq_keys"], ["SECRET_KEY"])
        self.assertEqual(self.mem.data[200]["notes"], ["OTHER SERVER NOTE"])
        self.assertTrue(all(gid == self.gid for gid, _, _ in self.mem.writes))
        self.assertEqual(self.mod.settings(self.gid)["enabled"], True)

    async def test_staff_role_exempt_channels_and_notes(self):
        await self.update(self.ctx, self.g, self.manager, {"staff_role_override": "302", "exempt_channels": ["402"],
                                                                    "notes": ["  abc   def  "]})
        self.assertEqual(self.mem.fast_get(self.gid, "staff_role_override"), 302)
        self.assertEqual(self.mod.exempt_channels(self.gid), [402])
        self.assertEqual(self.mem.fast_get(self.gid, "notes"), ["abc def"])
        await self.update(self.ctx, self.g, self.manager, {"staff_role_override": "", "exempt_channels": []})
        self.assertIsNone(self.mem.fast_get(self.gid, "staff_role_override"))
        self.assertEqual(self.mod.exempt_channels(self.gid), [])

    async def test_yuki_conversation_scope_accepts_only_visible_server_locations(self):
        for value in (["403"], ["9999"]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                await self.update(self.ctx, self.g, self.manager, {"yuki_talk_channels": value})
        self.assertEqual(self.mem.writes, [])
        await self.update(self.ctx, self.g, self.manager, {"yuki_talk_channels": ["401", "402"]})
        self.assertEqual(self.mem.fast_get(self.gid, "yuki_talk_channels"), [401, 402])

    async def test_talk_location_options_label_categories_filter_voice_and_hide_unavailable_names(self):
        self.g.channels[1].type = SimpleNamespace(name="category")
        voice = Channel(405, "private-voice")
        voice.type = SimpleNamespace(name="voice")
        self.g.channels.append(voice)
        options = (await self.fields())["yuki_talk_channels"]["options"]
        labels = {option["value"]: option["label"] for option in options}
        self.assertEqual(labels["402"], "category (category)")
        self.assertNotIn("405", labels)
        self.mem.data[self.gid]["yuki_talk_channels"] = [403]
        field_json = json.dumps((await self.fields())["yuki_talk_channels"])
        self.assertIn("Unavailable channel (403)", field_json)
        self.assertNotIn("private-staff", field_json)

    async def test_rule_annotations_correct_quoted_text_and_enforcement_category(self):
        self.mem.data[self.gid]["skipped_rules"] = []
        rules = copy.deepcopy((await self.fields())["rule_annotations"]["value"])
        first = next(rule for rule in rules if rule["n"] == 1)
        second = next(rule for rule in rules if rule["n"] == 2)
        first["correction"] = "Be respectful to other members."
        first["guidance"] = "Remind them calmly and explain this covers personal insults."
        second["category_override"] = "none"
        await self.update(self.ctx, self.g, self.manager, {"rule_annotations": rules})

        saved = self.mem.fast_get(self.gid, "rule_annotations")
        self.assertEqual(len(saved), 2)
        self.assertEqual(saved[0]["text"], "Be respectful")
        effective = {rule["n"]: rule for rule in self.mod.effective_rules(self.gid)}
        self.assertEqual(effective[1]["text"], first["correction"])
        self.assertEqual(effective[1]["guidance"], first["guidance"])
        self.assertEqual(effective[2]["category"], "none")
        self.assertEqual(self.mod.enforced(self.gid)["harassment"]["text"], first["correction"])
        self.assertNotIn("invites", self.mod.enforced(self.gid))

        # Parser punctuation/rephrasing retains a conservative annotation match.
        self.mem.data[self.gid]["rules"][0]["text"] = "Be respectful."
        refreshed = {rule["n"]: rule for rule in self.mod.effective_rules(self.gid)}
        self.assertEqual(refreshed[1]["text"], first["correction"])

    async def test_rule_annotations_validate_limits_and_keep_unmatched_entries_visible(self):
        rules = copy.deepcopy((await self.fields())["rule_annotations"]["value"])
        rules[0]["correction"] = "Correct interpretation"
        await self.update(self.ctx, self.g, self.manager, {"rule_annotations": rules})
        before = copy.deepcopy(self.mem.fast_get(self.gid, "rule_annotations"))
        bad = copy.deepcopy((await self.fields())["rule_annotations"]["value"])
        bad[0]["guidance"] = "x" * (ds.MAX_RULE_GUIDANCE_LENGTH + 1)
        with self.assertRaises(ValueError):
            await self.update(self.ctx, self.g, self.manager, {"rule_annotations": bad})
        self.assertEqual(self.mem.fast_get(self.gid, "rule_annotations"), before)

        self.mem.data[self.gid]["rules"][0] = {"n": 1, "text": "A completely different rule", "category": "none"}
        editor = (await self.fields())["rule_annotations"]["value"]
        self.assertTrue(any(entry["stale"] and entry["text"] == "Be respectful" for entry in editor))
        stale = next(entry for entry in editor if entry["stale"])
        stale["correction"] = ""
        stale["guidance"] = ""
        stale["category_override"] = "auto"
        await self.update(self.ctx, self.g, self.manager, {"rule_annotations": editor})
        self.assertEqual(self.mem.fast_get(self.gid, "rule_annotations"), [])

    async def test_skips_save_wording_and_match_after_reorder_or_reparse(self):
        fields = await self.fields()
        rule = fields["skipped_rules"]
        self.assertEqual(len(rule["value"]), 2)  # includes unmatched historical rule
        new_rule = next(o["value"] for o in rule["options"] if "advertising" in o["label"])
        chosen = rule["value"] + [new_rule]
        await self.update(self.ctx, self.g, self.manager, {"skipped_rules": chosen})
        saved = self.mod.skipped_entries(self.gid)
        self.assertIn({"n": 1, "text": "Be respectful", "extra": "keep"}, saved)
        self.assertIn({"n": 9, "text": "Old rule"}, saved)
        self.assertIn({"n": 2, "text": "No advertising"}, saved)
        self.assertTrue(self.mod._is_skipped({"n": 1, "text": "Be respectful to others"}, saved))
        fields = await self.fields()
        removed = [v for v in fields["skipped_rules"]["value"] if v != new_rule]
        await self.update(self.ctx, self.g, self.manager, {"skipped_rules": removed})
        self.assertFalse(self.mod._is_skipped({"n": 2, "text": "No advertising"}, self.mod.skipped_entries(self.gid)))

    async def test_stale_rule_token_rejected(self):
        rule = (await self.fields())["skipped_rules"]
        token = rule["options"][1]["value"]
        self.mem.data[self.gid]["rules"][1]["text"] = "New rule entirely"
        with self.assertRaises(ValueError):
            await self.update(self.ctx, self.g, self.manager, {"skipped_rules": [token]})
        self.assertEqual(self.mem.writes, [])

    async def test_word_moving_and_limits(self):
        await self.update(self.ctx, self.g, self.manager, {"words_allow": ["shit", "foobar"]})
        w = self.mod.words(self.gid)
        self.assertEqual(w["allow"], ["shit", "foobar"])
        self.assertEqual(w["block"], [])
        await self.update(self.ctx, self.g, self.manager, {"words_block": ["shit", "hello"]})
        self.assertEqual(self.mod.words(self.gid)["allow"], ["foobar"])
        self.assertEqual(self.mod.words(self.gid)["block"], ["shit", "hello"])
        before = len(self.mem.writes)
        with self.assertRaises(ValueError):
            await self.update(self.ctx, self.g, self.manager, {"words_allow": ["shit"], "words_block": ["shit"]})
        with self.assertRaises(ValueError):
            await self.update(self.ctx, self.g, self.manager, {"words_block": [f"word{i}" for i in range(41)]})
        self.assertEqual(len(self.mem.writes), before)

    async def test_access_and_large_ids(self):
        large = 1234567890123456789
        self.g.members[large] = Member(self.g, large)
        await self.update(self.ctx, self.g, self.admin, {"access_roles": ["301"], "access_users": [str(large)]})
        self.assertEqual(self.mod.access(self.gid)["roles"], [301])
        self.assertEqual(self.mod.access(self.gid)["users"], [large])
        self.assertEqual((await self.fields())["access_users"]["value"], [str(large)])
        with self.assertRaises(ValueError):
            await self.update(self.ctx, self.g, self.admin, {"access_users": [str(large)] * 26})

    async def test_bans_preserve_metadata_departed_members_and_add_new(self):
        await self.update(self.ctx, self.g, self.manager, {"yuki_server_bans": ["999", "113"]})
        bans = self.mem.fast_get(self.gid, "yuki_server_bans")
        self.assertEqual(bans["999"], {"by": 444, "at": 42, "reason": "unchanged"})
        self.assertEqual(bans["113"]["by"], self.manager.id)
        self.assertIs(type(bans["113"]["at"]), int)
        await self.update(self.ctx, self.g, self.manager, {"yuki_server_bans": ["113"]})
        self.assertNotIn("999", self.mem.fast_get(self.gid, "yuki_server_bans"))
        self.assertIn("777", self.mem.fast_get(0, "yuki_global_bans"))

    async def test_slang_forget_preserves_other_terms_and_skipped(self):
        fields = await self.fields()
        self.assertIn({"value": "glorp", "label": "glorp: cool thing"}, fields["slang_forget"]["options"])
        await self.update(self.ctx, self.g, self.manager, {"slang_forget": "glorp"})
        self.assertEqual(self.mem.fast_get(self.gid, "slang_terms"), {"blip": {"meaning": "small thing", "ts": 101}})
        self.assertEqual(self.mem.fast_get(self.gid, "slang_skipped"), ["blip", "alreadyasked"])
        await self.update(self.ctx, self.g, self.manager, {"slang_forget_other": " AlreadyAsked "})
        self.assertEqual(self.mem.fast_get(self.gid, "slang_skipped"), ["blip"])
        self.assertEqual((await self.fields())["slang_forget"]["value"], "")

    async def test_sequential_dashboard_updates_preserve_siblings(self):
        # Each save uses a newly loaded baseline, as the browser does after a
        # successful write. Concurrent stale editors are rejected separately.
        await self.update(self.ctx, self.g, self.manager, {"enabled": True})
        await self.update(self.ctx, self.g, self.manager, {"reactions": False})
        await self.update(self.ctx, self.g, self.manager, {"ping_users": True})
        await self.update(self.ctx, self.g, self.manager, {"ping_mass": False})
        await self.update(self.ctx, self.g, self.admin, {"access_level": "admin"})
        await self.update(self.ctx, self.g, self.admin, {"access_roles": ["301"]})
        self.assertEqual(self.mem.fast_get(self.gid, "settings"), {"enabled": True, "reactions": False, "future_setting": "untouched"})
        self.assertEqual(self.mem.fast_get(self.gid, "ai_ping_policy"), {"users": True, "mass": False, "future": 8})
        self.assertEqual(self.mem.fast_get(self.gid, "access")["level"], "admin")
        self.assertEqual(self.mem.fast_get(self.gid, "access")["roles"], [301])

    async def test_stale_dashboard_editor_cannot_overwrite_newer_settings(self):
        snapshot = await ds.get_settings(self.ctx, self.g, self.manager)
        expected = {field["key"]: copy.deepcopy(field["value"])
                    for section in snapshot["sections"] for field in section["fields"]}
        await self.update(self.ctx, self.g, self.manager, {"notes": ["first editor"]})
        with self.assertRaisesRegex(ValueError, "changed since you opened"):
            await ds.update_settings(self.ctx, self.g, self.manager,
                                     {"notes": ["stale editor"]}, expected=expected,
                                     reauthorize=self.reauthorize_actor)
        self.assertEqual(self.mem.fast_get(self.gid, "notes"), ["first editor"])

    async def reauthorize_actor(self):
        return self.g, self.manager

    async def test_external_conflict_during_member_resolution_fails_without_write(self):
        # The caller waited on REST verification while a slash command changed
        # the same fact. The dashboard must not overwrite the intervening edit.
        ready, resume = asyncio.Event(), asyncio.Event()
        new = Member(self.g, 555)
        async def fetch(ident):
            ready.set()
            await resume.wait()
            return new
        self.g.fetch_member = fetch
        task = asyncio.create_task(self.update(self.ctx, self.g, self.admin, {"access_users": ["555"]}))
        await ready.wait()
        self.mem.data[self.gid]["access"]["future"] = "changed elsewhere"
        resume.set()
        with self.assertRaisesRegex(ValueError, "changed while saving"):
            await task
        self.assertEqual(self.mem.fast_get(self.gid, "access")["future"], "changed elsewhere")
        self.assertEqual(self.mem.writes, [])

    async def test_permission_revoked_during_member_resolution(self):
        ready, resume = asyncio.Event(), asyncio.Event()
        new = Member(self.g, 555)
        async def fetch(ident):
            ready.set()
            await resume.wait()
            return new
        self.g.fetch_member = fetch
        task = asyncio.create_task(self.update(self.ctx, self.g, self.normal, {"yuki_server_bans": ["555"]}))
        await ready.wait()
        self.mem.data[self.gid]["access"]["users"] = []
        self.mem.data[self.gid]["access"]["roles"] = []
        resume.set()
        with self.assertRaises(PermissionError):
            await task
        self.assertEqual(self.mem.writes, [])

    async def test_channel_options_use_fresh_permissions_and_overwrites(self):
        # Cached permissions_for lies; the authenticated member's fresh roles
        # plus the channel's overwrite determine whether the name is shown.
        self.g.default_role = self.g.roles[0]
        current = discord.Permissions.none()
        current.view_channel = False
        self.normal.guild_permissions = current
        class LiveChannel:
            id = 888
            name = "sensitive"
            def permissions_for(self, member):
                return SimpleNamespace(view_channel=True)
            def overwrites_for(self, obj):
                return discord.PermissionOverwrite()
        self.g.channels.append(LiveChannel())
        self.assertNotIn("sensitive", json.dumps(await self.fields(self.normal)))
        current.view_channel = True
        self.assertIn("sensitive", json.dumps(await self.fields(self.normal)))

    async def test_limits_and_no_cross_field_writes_on_validation_failure(self):
        channels = [Channel(1000 + i, f"channel-{i}") for i in range(51)]
        roles = [Role(2000 + i, f"role-{i}") for i in range(26)]
        self.g.channels.extend(channels)
        self.g.roles.extend(roles)
        cases = [
            {"yuki_talk_channels": [str(c.id) for c in channels]},
            {"exempt_channels": [str(c.id) for c in channels]},
            {"slang_off_channels": [str(c.id) for c in channels]},
            {"access_roles": [str(r.id) for r in roles]},
            {"notes": [str(i) for i in range(16)]},
            {"yuki_server_bans": [str(i) for i in range(1, ds.MAX_BANS + 2)]},
            {"enabled": True, "notes": ["too long " * 30]},
        ]
        for case in cases:
            with self.subTest(field=next(iter(case))), self.assertRaises(ValueError):
                await self.update(self.ctx, self.g, self.admin, case)
        self.assertEqual(self.mem.writes, [])

    async def test_refresh_calls_actual_mod_methods_with_force(self):
        self.mod.refresh_roles = AsyncMock(return_value="roles read")
        self.mod.refresh_rules = AsyncMock(return_value="read 3 rules from #private-channel")
        output = await self.refresh(self.ctx, self.g, self.manager)
        self.assertEqual(output, "Roles and rules refreshed.")
        self.assertNotIn("private-channel", output)
        self.mod.refresh_roles.assert_awaited_once_with(self.g)
        self.mod.refresh_rules.assert_awaited_once_with(self.g, force=True)
        self.assertIn(self.gid, self.mod._next_rules)


if __name__ == "__main__":
    unittest.main()
