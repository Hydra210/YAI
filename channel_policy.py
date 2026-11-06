"""Guild channel allow-list policy for Yuki's conversational outputs.

The dashboard stores ``yuki_talk_channels`` as Discord channel/category ID
strings or integers. An absent, empty, or wholly malformed value is treated as
unset so existing installations continue to allow conversation everywhere.
"""
from __future__ import annotations


def _snowflake(value) -> int | None:
    """Normalize a Discord ID without trusting malformed persisted values."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        value = value.strip()
        if value.isdecimal():
            try:
                ident = int(value)
            except (TypeError, ValueError, OverflowError):
                return None
            return ident if ident > 0 else None
    return None


def _get(obj, name):
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


def _channel_ids(guild, channel) -> set[int]:
    """Collect IDs identifying this channel, its category and thread parent."""
    found: set[int] = set()

    def add(value):
        ident = _snowflake(value)
        if ident is not None:
            found.add(ident)

    add(_get(channel, "id"))
    add(_get(channel, "category_id"))
    add(_get(channel, "parent_id"))

    # For a thread, parent_id remains useful even when Discord has not cached
    # its parent. A cached parent additionally exposes its category.
    parent = _get(channel, "parent")
    parent_id = _snowflake(_get(channel, "parent_id"))
    if parent is None and parent_id is not None and guild is not None:
        for method_name in ("get_channel", "get_channel_or_thread"):
            method = _get(guild, method_name)
            if callable(method):
                try:
                    parent = method(parent_id)
                except Exception:
                    parent = None
                if parent is not None:
                    break
    if parent is not None:
        add(_get(parent, "id"))
        add(_get(parent, "category_id"))
        category = _get(parent, "category")
        add(_get(category, "id"))

    # Some channel-like wrappers expose the category object but not its ID.
    category = _get(channel, "category")
    add(_get(category, "id"))
    return found


def conversation_allowed(mem, guild, channel) -> bool:
    """Return whether Yuki may produce conversational output in ``channel``.

    DMs (no guild) are always allowed. Guilds with no valid selected IDs keep
    the legacy allow-all behavior. Selected IDs match the current channel, its
    category, a thread's parent channel, or that parent's category.
    """
    if guild is None:
        return True
    guild_id = _snowflake(_get(guild, "id")) or _snowflake(guild)
    if guild_id is None or mem is None:
        return True
    try:
        getter = _get(mem, "fast_get")
        if not callable(getter):
            return True
        selected = getter(guild_id, "yuki_talk_channels")
    except Exception:
        # Storage/cache failures must not unexpectedly silence an established bot.
        return True

    if selected is None or selected == [] or selected == () or selected == "":
        return True
    if not isinstance(selected, (list, tuple, set, frozenset)):
        return True
    selected_ids = {ident for value in selected if (ident := _snowflake(value)) is not None}
    if not selected_ids:
        return True
    if channel is None:
        return False
    try:
        return bool(selected_ids & _channel_ids(guild, channel))
    except Exception:
        # A broken channel/cache object cannot establish a positive match.
        return False
