"""Yuki's version, update log and "what can you do" notes.

This is the one place to edit when you ship something:
  1. bump VERSION (and VERSION_NOTE, the one-liner shown next to it)
     version format is always 1.MAJOR.MINOR.PATCH, four numbers (e.g. 1.15.0.0): bump MAJOR for big releases, MINOR for new features,
     PATCH for small fixes, and reset the numbers to its right to 0
  2. add a new entry at the TOP of CHANGELOG
Yuki reads this through the `whats_new` and `about_yuki` skills (so he can answer "what's new" / "what can you do" in
his own words), and people can see it with `/about`. Keep entries short and factual: he only knows what is written here,
so if a feature isn't listed he'll say he doesn't know about it rather than guess.
"""

from __future__ import annotations

VERSION = "1.15.1.1"
VERSION_DATE = "2026-10-25"
VERSION_NOTE = "he always knows his real version number now"

# newest first. "date" may be empty if you don't know it.
CHANGELOG = [
    {
        "version": "1.15.1.1",
        "date": "2026-10-25",
        "title": "version fix",
        "changes": [
            "he always knows his real version now, so he can't repeat an old number from earlier in the chat or guess one.",
        ],
    },
    {
        "version": "1.15.1.0",
        "date": "2026-10-25",
        "title": "emoji reactions switch",
        "changes": [
            "staff can turn his random emoji reactions off (or back on) for their server with `/mod setting` > reactions. "
            "his ⚠️ warning reaction on rule breaks is separate and stays.",
        ],
    },
    {
        "version": "1.15.0.1",
        "date": "2026-10-25",
        "title": "no more random rule quotes",
        "changes": [
            "when staff exempt a rule or a word (`/mod skiprules`, `/mod words`) he still steps in if it's aimed at a person, "
            "but he no longer quotes a rule for it. before, he'd grab whatever rule looked closest, which was often a random one.",
            "real rules are still quoted as normal when the server actually has a matching rule that isn't exempt.",
        ],
    },
    {
        "version": "1.15.0.0",
        "date": "2026-10-25",
        "title": "channel exceptions, statuses, search, update log",
        "changes": [
            "staff can pick channels (or whole categories) yuki never moderates with `/mod channels` (add, remove, list, clear). "
            "nothing posted there is checked and threads inside them count too. he still chats there.",
            "custom statuses are back to the lazy deadpan kind (like \"doomscrolling\"): they come from a big hand-written list, "
            "with the model only writing one now and then in the same style.",
            "web search no longer gives up when one source blocks him (the 403 thing): he tries several in a row (Google through Serper, Tavily, Brave, "
            "SearXNG if the owner set them up, then DuckDuckGo and Wikipedia).",
            "opening links is more reliable: he looks like a normal browser and retries when a site blocks him once. "
            "some sites still refuse bots completely.",
            "added this update log, a version number, and `/about` so anyone can see what's new and what he can do.",
        ],
    },
    {
        "version": "earlier",
        "date": "",
        "title": "everything before 1.15.0.0 and the update log",
        "changes": [
            "talks when mentioned, with `/yuki`, in DMs, or when a message starts with \"yuki\".",
            "remembers each server in a database: searches old messages, keeps notes on people and servers, learns how each person talks.",
            "reads the server rules and reminds people who break them (slurs, bullying, swearing, links, invites, personal info, spam, caps, mass pings), "
            "with `/mod` tools for staff.",
            "tools for rules, channels, roles, pinned messages, user info, images (convert, resize, memes, looking at pictures), time, math, web search and links.",
            "personas: tell him how to act with you and it only applies to you.",
            "random custom statuses, emoji reactions, and a running roast feud with one specific person.",
        ],
    },
]

ABOUT = {
    "overview": (
        "yuki is a super casual discord bot that uses ai for everything and talks like his creator. he reads a lot of what's said in a server, remembers it, "
        "and has real tools for looking stuff up. now and then he reacts to a message with an emoji (staff can turn that off with `/mod setting`). people can @mention him, use /yuki, dm him, or start a message with 'yuki'."
    ),
    "talk": (
        "ways to talk to him: @mention him in a server, `/yuki question: ...`, dm him (he answers every dm), or start a message with 'yuki'. "
        "`/memory` shows or wipes what he knows about you, `/persona` shows or resets how he acts with you, `/meme` makes captioned images, "
        "and `/about` shows this info."
    ),
    "memory": (
        "memory: he keeps messages per server in a database, searches old ones (like 'what did bob say about valorant'), and writes short "
        "notes about each person and server so he picks up how everyone talks. `/memory show` lists what he knows about you, `/memory forget` "
        "wipes it and stops him remembering you, `/memory resume` turns it back on. messages older than 90 days are pruned and he doesn't "
        "keep sensitive stuff (health, religion, politics, money, personal info) in his notes."
    ),
    "moderation": (
        "moderation: he reads the server's rules himself and reminds people who break the ones code can check: slurs, bullying and insults "
        "aimed at people, swearing, links, invites, personal info (emails, phones, ips), mass pings, spam, all caps. he replies, quotes the exact rule "
        "and pings staff. he never deletes anything and skips staff. the checks are plain code, not ai. staff manage it with `/mod`: status, test, "
        "refresh, skiprules (rules he shouldn't enforce), words (swear exceptions), channels (channels he never moderates), access (who can use /mod), "
        "setting, note, staffrole. it can't catch sarcasm, images or other languages."
    ),
    "skills": (
        "tools: read the server rules, pinned messages, channels, roles, user and server info, search old messages, recall what he knows about someone, "
        "look at images (rate, roast, describe), convert and resize images, make memes, tell the time, do math, search the web for news and current stuff, "
        "open links and read them. there's also a sandboxed python fallback if the owner turned it on. if he isn't sure he can do something he checks his tool list "
        "instead of guessing."
    ),
    "personas": (
        "personas: tell him 'talk like a pirate', 'call me boss', 'keep it short', 'be nicer to me' and he saves it just for you. nobody else sees or gets it. "
        "`/persona` shows it or resets it."
    ),
    "privacy": (
        "privacy: memory is per server and dms are private to each person. tools only read channels the person asking can see. edited messages update and "
        "deleted ones are removed. `/memory forget` wipes your stuff. rule-break records only keep what and when, not the message text."
    ),
    "limits": (
        "limits: he can be wrong, his image vision is basic, his rule checks are heuristics, "
        "and web search depends on the sources the owner set up."
    ),
}

TOPICS = tuple(ABOUT)


def version_line() -> str:
    return f"v{VERSION} ({VERSION_DATE}): {VERSION_NOTE}"


def format_changelog(count: int = 1, limit: int = 2300) -> str:
    """The newest `count` versions as plain text for Yuki (or for /about)."""
    count = max(1, min(int(count or 1), len(CHANGELOG)))
    parts = [f"current version: {version_line()}"]
    for e in CHANGELOG[:count]:
        v = e["version"]
        head = (f"v{v}" if v[:1].isdigit() else v) + (f" ({e['date']})" if e.get("date") else "") + f": {e['title']}"
        parts.append(head + "\n" + "\n".join(f"- {c}" for c in e["changes"]))
    return "\n\n".join(parts)[:limit]


def about_text(topic: str | None = None, limit: int = 2300) -> str:
    t = (topic or "").strip().lower()
    if t in ABOUT:
        return f"{ABOUT[t]}\n\n(version {VERSION})"[:limit]
    if t == "version":
        return version_line()
    # no topic / "all": the short tour
    keys = ("overview", "talk", "memory", "moderation", "skills")
    return ("\n\n".join(ABOUT[k] for k in keys) + f"\n\n(version {VERSION}. other topics: " + ", ".join(k for k in TOPICS if k not in keys) + ")")[:limit]
