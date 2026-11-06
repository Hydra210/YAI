import unittest
from types import SimpleNamespace

from channel_policy import conversation_allowed


class Memory:
    def __init__(self, value=None, *, fail=False):
        self.value = value
        self.fail = fail

    def fast_get(self, guild_id, key):
        if self.fail:
            raise RuntimeError("cache unavailable")
        return self.value


class ChannelPolicyTests(unittest.TestCase):
    def setUp(self):
        self.guild = SimpleNamespace(id=100, channels={})
        self.guild.get_channel = lambda ident: self.guild.channels.get(ident)
        self.category = SimpleNamespace(id=20)
        self.parent = SimpleNamespace(id=30, category_id=20, category=self.category)
        self.child = SimpleNamespace(id=31, category_id=20, category=self.category, parent_id=None)
        self.other_child = SimpleNamespace(id=32, category_id=60, category=SimpleNamespace(id=60), parent_id=None)
        self.thread = SimpleNamespace(id=40, parent_id=30, parent=self.parent, category_id=None)
        self.missing_parent_thread = SimpleNamespace(id=41, parent_id=30, parent=None, category_id=None)
        self.unrelated = SimpleNamespace(id=50, category_id=60, parent_id=None)
        self.guild.channels = {30: self.parent}

    def allowed(self, config, channel):
        return conversation_allowed(Memory(config), self.guild, channel)

    def test_allow_all_default_when_unset_or_empty(self):
        for config in (None, [], (), ""):
            with self.subTest(config=config):
                self.assertTrue(self.allowed(config, self.unrelated))

    def test_selected_channel_is_excluded_and_other_channels_remain_allowed(self):
        self.assertFalse(self.allowed(["50"], self.unrelated))
        self.assertFalse(self.allowed([50], self.unrelated))
        self.assertTrue(self.allowed(["51"], self.unrelated))

    def test_selected_category_excludes_child_channels_only(self):
        self.assertFalse(self.allowed(["20"], self.child))
        self.assertTrue(self.allowed(["20"], self.other_child))

    def test_selected_channel_or_category_excludes_threads(self):
        self.assertFalse(self.allowed(["40"], self.thread))
        self.assertFalse(self.allowed(["30"], self.thread))
        self.assertFalse(self.allowed(["20"], self.thread))

    def test_thread_matches_selected_parent_without_cached_parent(self):
        self.guild.channels.clear()
        self.assertFalse(self.allowed(["30"], self.missing_parent_thread))
        # An uncached parent cannot reveal its category, so unrelated categories remain allowed.
        self.assertTrue(self.allowed(["20"], self.missing_parent_thread))

    def test_unrelated_selected_exclusions_do_not_block(self):
        self.assertTrue(self.allowed(["30", "20"], self.unrelated))

    def test_dm_is_always_allowed(self):
        dm = SimpleNamespace(id=70)
        self.assertTrue(conversation_allowed(Memory(["999"]), None, dm))

    def test_malformed_config_fails_open_and_ignores_bad_entries(self):
        for config in ("not-a-list", {"channel": 50}, 50, [None, {}, "bad", True, 0]):
            with self.subTest(config=config):
                self.assertTrue(self.allowed(config, self.unrelated))
        self.assertTrue(self.allowed(["99", None, "bad"], self.unrelated))
        self.assertFalse(self.allowed(["50", None, "bad"], self.unrelated))
        self.assertTrue(conversation_allowed(Memory(fail=True), self.guild, self.unrelated))
        self.assertTrue(conversation_allowed(Memory(["50"]), self.guild, None))

    def test_guild_id_works_without_a_cached_guild_object(self):
        self.assertTrue(conversation_allowed(Memory(["99"]), 100, self.unrelated))
        self.assertFalse(conversation_allowed(Memory(["50"]), 100, self.unrelated))


if __name__ == "__main__":
    unittest.main()
