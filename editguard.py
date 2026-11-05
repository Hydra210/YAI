"""Edit-trick guard: stops people from getting Yuki to agree to something harmless and then editing their message.

The trick: "yuki say yes if 2+2=4" -> Yuki says "yes" -> the person edits their message to something bad, so it looks
like Yuki agreed to the bad thing. Everything here is plain code (no AI, no tokens), same idea as safety.py.

How it works (bot.py wires this in):
  begin(message)        called when Yuki starts answering a message. Remembers what it said at that moment.
  add_sent(...)         called for every message Yuki sends back, as it is sent.
  on_edit(...)          called for every edit of a message Yuki answered (works in servers AND DMs, cached or not).
                        If the edit really changes the message, Yuki's answers get deleted at once and he calls it out.
  was_edited(...)       checked before Yuki sends, so an edit during his "thinking" time cancels the answer.

Tiny typo fixes (a letter or a capital or some punctuation) are ignored so normal people aren't annoyed.
Any other change counts, on purpose: the safe side to be wrong on is deleting an answer, not keeping one.
"""

from __future__ import annotations

import os
import random
import re
import time
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from moderation import _INVITE, has_slur
from textcheck import _EMAIL, _PHONE, find_links, has_hate_slur, has_strong_swear

WINDOW = float(os.getenv("EDIT_GUARD_WINDOW_SECONDS", "3600"))  # how long after answering an edit still counts (default 1 hour)
MAX_TRACKED = 3000
MAX_JUDGES = 3  # how many times one message may be judged; edits past that count as a trick (stops edit-spamming to burn tokens)
MIN_TYPO_WORD = 4  # only words this long may be "fixed" by a one-letter typo edit ("no" -> "so" flips the meaning, "recieve" -> "receive" doesn't)
ENABLED = os.getenv("EDIT_GUARD", "1").strip().lower() not in ("0", "false", "off", "no")

CALLOUTS = [
    "nice try. i saw that edit, so i deleted my answer",
    "not gonna fall for the edit trick lol, nice try",
    "you edited that after i answered. nope, nice try",
    "caught the edit. my answer's gone, nice try",
    "ohh so you edited it after i replied? yeah i'm not falling for that one",
    "edit trick? in this economy? nice try",
    "i see you. editing your message after i answer doesn't work on me",
    "bro really edited his message after i answered. deleted, nice try",
]

CALLOUTS_BEFORE_ANSWER = [  # the edit landed while he was still thinking, so there is nothing to delete yet
    "i saw you edit that. not answering it, nice try",
    "editing your message while i'm thinking? nice try, i'm not falling for it",
    "not gonna fall for the edit trick lol, nice try",
]

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass
class Entry:
    channel_id: int
    original: str  # the raw text as it was when Yuki started answering
    created: float = field(default_factory=time.time)
    sent_ids: list = field(default_factory=list)
    edited: bool = False  # judged a trick: don't send (more) answers to this message
    called_out: bool = False
    reply_text: str = ""  # what Yuki answered (or is about to), so the judge can see what the edit would make look bad
    judging: bool = False  # an edit of this message is being judged right now
    judged: int = 0
    latest: str = ""  # the newest text seen while judging


_tracked: dict[int, Entry] = {}


# ------------------------------------------------------------------------------------------------ comparing text


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").casefold()


def _words(text: str) -> list:
    return _WORD.findall(_fold(text))


def _osa(a: str, b: str) -> int:
    """Edit distance where swapping two neighbouring letters ("teh" -> "the") counts as one change."""
    la, lb = len(a), len(b)
    d = [[0] * (lb + 1) for _ in range(la + 1)]
    for i in range(la + 1):
        d[i][0] = i
    for j in range(lb + 1):
        d[0][j] = j
    for i in range(1, la + 1):
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[la][lb]


def is_real_change(before: str, after: str) -> bool:
    """True if the edit changes what the message says. Case, spacing, punctuation, emoji and one-letter typo fixes in
    longer words don't count. Adding, removing or swapping a word (or any short word) does."""
    if before == after:
        return False
    a, b = _words(before), _words(after)
    if a == b:
        return False
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        old, new = a[i1:i2], b[j1:j2]
        if tag != "replace" or len(old) != len(new):
            return True  # a word was added or removed
        for x, y in zip(old, new):
            if len(x) < MIN_TYPO_WORD or len(y) < MIN_TYPO_WORD or _osa(x, y) > 1 or x.isdigit() or y.isdigit():
                return True
    return False


# ------------------------------------------------------------------------------------------------ tracking


def _prune(now: float) -> None:
    if len(_tracked) < MAX_TRACKED:
        return
    for k in [k for k, e in _tracked.items() if now - e.created > WINDOW]:
        del _tracked[k]
    if len(_tracked) >= MAX_TRACKED:  # still full: drop the oldest quarter
        for k, _ in sorted(_tracked.items(), key=lambda kv: kv[1].created)[: MAX_TRACKED // 4]:
            del _tracked[k]


def begin(message_id: int, channel_id: int, content: str) -> Entry:
    now = time.time()
    _prune(now)
    entry = _tracked[message_id] = Entry(channel_id, content or "", created=now)
    return entry


def get(message_id: int) -> Entry | None:
    return _tracked.get(message_id)


def was_edited(message_id: int) -> bool:
    e = _tracked.get(message_id)
    return bool(e and e.edited)


def add_sent(message_id: int, sent_id: int) -> bool:
    """Remember one message Yuki sent for this trigger. Returns True if the trigger was already caught being edited,
    meaning the caller should delete `sent_id` right away."""
    e = _tracked.get(message_id)
    if e is None:
        return False
    e.sent_ids.append(sent_id)
    return e.edited


def set_reply(message_id: int, reply: str) -> None:
    e = _tracked.get(message_id)
    if e is not None:
        e.reply_text = reply or ""


def on_edit(message_id: int, new_content: str) -> Entry | None:
    """Call for every edit. Returns the entry when the CALLER now has to judge it (a real change, inside the window,
    nobody judging yet). While a judgement is running, newer edits are just stored and picked up by next_pending()."""
    if not ENABLED:
        return None
    e = _tracked.get(message_id)
    if e is None or e.called_out:
        return None
    if time.time() - e.created > WINDOW:
        _tracked.pop(message_id, None)
        return None
    if e.judging:
        e.latest = new_content
        return None
    if not is_real_change(e.original, new_content):
        return None
    e.judging = True
    e.latest = new_content
    return e


def next_pending(e: Entry, judged_content: str) -> str | None:
    """After an OK verdict: the newer text that arrived during the judgement and still needs judging, else None
    (and the entry is free to be judged again on the next edit)."""
    if e.latest != judged_content and is_real_change(e.original, e.latest):
        return e.latest
    e.judging = False
    return None


def flag(e: Entry) -> None:
    """The edit was judged a trick: stop answering this message and remember we called it out."""
    e.edited = True
    e.called_out = True
    e.judging = False


def too_many_judges(e: Entry) -> bool:
    e.judged += 1
    return e.judged > MAX_JUDGES


def obviously_bad(before: str, after: str) -> bool:
    """Plain-code check, no AI: did the edit ADD a slur, strong swear, invite, email, phone number or link?"""
    def flags(t: str) -> set:
        out = set()
        if has_slur(t) or has_hate_slur(t):
            out.add("slur")
        if has_strong_swear(t):
            out.add("swear")
        if _INVITE.search(t):
            out.add("invite")
        if _EMAIL.search(t) or _PHONE.search(t):
            out.add("personal")
        if find_links(t):
            out.add("link")
        return out
    return bool(flags(after) - flags(before))


def callout(deleted: bool = True) -> str:
    return random.choice(CALLOUTS if deleted else CALLOUTS_BEFORE_ANSWER)
