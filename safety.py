"""Guards that sit between people, the model and Discord.

Everything here is plain code (no AI, no tokens). The idea: never rely on the prompt alone to keep Yuki in line,
because anyone can talk a model into things. These checks run on what actually gets sent.

  RateGate          per-person cooldown + burst limit (stops "spam yuki until he floods the chat")
  output_filter     last check on Yuki's reply: slurs, invite links, emails / phone numbers, repeated-text floods, ping floods
  limit_pings       at most N distinct people pinged per reply, never the same person twice
  collapse_repeats  "say hi 500 times" turns into a couple of hi's
  chat_line         makes one chat message safe to paste into the prompt (can't fake extra "Name: ..." lines)
  persona_ok        stricter check on persona instructions than a few keywords
"""

from __future__ import annotations

import collections
import random
import re
import time
import unicodedata

from moderation import _INVITE, has_slur
from textcheck import _EMAIL, _PHONE, has_hate_slur, soft_norm

MAX_PINGS_PER_REPLY = 3
MAX_REPLY_PARTS = 3  # discord messages per reply (1900 chars each)


# ---------------------------------------------------------------------------------------------- rate limiting


class RateGate:
    """Sliding-window limiter per key (a user id).

    check() returns None (go ahead), "warn" (first time they go over: say something once) or "silent" (ignore them).
    Going over `burst` uses in `window` seconds mutes the key for `mute` seconds. Two uses closer than `cooldown`
    seconds are ignored without punishment."""

    def __init__(self, cooldown: float, burst: int, window: float, mute: float):
        self.cooldown, self.burst, self.window, self.mute = cooldown, burst, window, mute
        self._hits: dict = {}
        self._muted: dict = {}

    def check(self, key) -> str | None:
        now = time.time()
        if len(self._hits) > 5000:  # housekeeping so this never grows forever
            self._hits = {k: q for k, q in self._hits.items() if q and now - q[-1] < self.window}
            self._muted = {k: t for k, t in self._muted.items() if t > now}
        if now < self._muted.get(key, 0):
            return "silent"
        q = self._hits.setdefault(key, collections.deque())
        while q and now - q[0] > self.window:
            q.popleft()
        if q and now - q[-1] < self.cooldown:
            return "silent"
        if len(q) >= self.burst:
            self._muted[key] = now + self.mute
            q.clear()
            return "warn"
        q.append(now)
        return None


# ---------------------------------------------------------------------------------------------- reply checks

_PING = re.compile(r"<@!?(\d+)>")
_WORD_REPEAT = re.compile(r"(\S{1,40})(?:\s+\1){3,}")  # same word 4+ times in a row
_UNIT_REPEAT = re.compile(r"(.{3,60}?)\1{4,}", re.S)  # same chunk 5+ times back to back
_CHAR_RUN = re.compile(r"(.)\1{19,}", re.S)  # one character 20+ times
_LINE_REPEAT = re.compile(r"^(.+)$(?:\n\1$){2,}", re.M)  # same line 3+ times

_DEFLECT = ["nah i'm not saying that", "yeah no, not saying that one", "not doing that, pick something else"]


def limit_pings(text: str, cap: int = MAX_PINGS_PER_REPLY) -> str:
    """Keep the first `cap` different people pinged, drop repeats of the same person and every ping after the cap."""
    seen: list = []

    def sub(m):
        uid = m.group(1)
        if uid in seen or len(seen) >= cap:
            return ""
        seen.append(uid)
        return m.group(0)

    return re.sub(r"[ \t]{2,}", " ", _PING.sub(sub, text or ""))


def collapse_repeats(text: str) -> str:
    text = _LINE_REPEAT.sub(lambda m: m.group(1) + "\n" + m.group(1), text or "")
    text = _WORD_REPEAT.sub(lambda m: m.group(1) + " " + m.group(1), text)
    text = _UNIT_REPEAT.sub(lambda m: m.group(1) * 2, text)
    return _CHAR_RUN.sub(lambda m: m.group(1) * 20, text)


def output_filter(reply: str) -> str:
    """Final check on what Yuki is about to send. The input side has always been moderated; this is the output side."""
    reply = reply or ""
    if has_slur(reply) or has_hate_slur(reply):
        return random.choice(_DEFLECT)
    reply = _INVITE.sub("[invite removed]", reply)
    reply = _EMAIL.sub("[email hidden]", reply)
    reply = _PHONE.sub("[number hidden]", reply)
    reply = collapse_repeats(reply)
    return limit_pings(reply)


# ---------------------------------------------------------------------------------------------- prompt input


def chat_line(text: str, limit: int = 600) -> str:
    """One chat message (or name) made safe for the prompt: newlines can't be used to fake another speaker."""
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f\u200b-\u200f\u2028\u2029\u2060\ufeff]", "", str(text or ""))
    text = re.sub(r"\s*[\r\n]+\s*", " \u23ce ", text).strip()
    return text[:limit]


# ---------------------------------------------------------------------------------------------- personas

_PERSONA_BAD = re.compile(
    r"ignor\w* .{0,20}(?:instruction|rule|prompt|guideline|polic)|(?:system|developer) (?:prompt|message)|jailbreak|"
    r"do anything now|developer mode|god mode|"
    r"no (?:rule|restriction|filter|limit|censor)|un(?:filtered|censored|restricted|moderated)|"
    r"(?:disable|turn off|bypass|remove|drop|forget|override|stop following) .{0,20}(?:safety|filter|rule|restriction|guardrail|moderation|polic|instruction)|"
    r"run_?python|(?:call|run|use) (?:the |your |any |all )?(?:tools?|skills?)\b|run (?:this |the |my |some )?(?:code|command|script)|reveal .{0,20}(?:prompt|instruction|memory|key|token|password)|"
    r"(?:pretend|act|behave) .{0,25}(?:staff|admin|mod(?:erator)?|owner|patrick|creator)|"
    r"you (?:are|have|can) .{0,12}(?:no limits|no rules|allowed to say anything|root|admin)|"
    r"slur|n word|racis|nazi|hitler|\bsex\w*|nsfw|porn|nude|"
    r"spam|flood|\d{2,} times|(?:ping|tag|mention) .{0,25}(?:everyone|here|every|always|constantly|repeatedly|over and over)|"
    r"@ ?(?:everyone|here)|every (?:single )?(?:message|reply|response) .{0,25}(?:ping|tag|mention)|"
    r"(?:harass|bully|insult|threaten|dox|doxx)",
    re.I,
)


# same idea with every non-letter removed, so "ig-nore", "i.g.n.o.r.e", "ignore_all" can't dodge the check
_PERSONA_COMPACT = re.compile(
    r"ignore(?:all|any|your|the|previous|prior|above|earlier)*(?:instruction|rule|prompt|guideline|polic)|"
    r"systemprompt|developermessage|jailbreak|doanythingnow|developermode|godmode|"
    r"nofilter|norules|norestriction|nolimits|unfiltered|uncensored|unrestricted|"
    r"(?:disable|bypass|override|removesafety|turnoff)(?:your|the)?(?:safety|filter|rule|restriction|guardrail|moderation)|"
    r"nword|slur|nazi|hitler|spam|flood|@everyone|@here"
)


def persona_ok(instruction: str) -> bool:
    """False when a persona line tries to change rules, tools or safety instead of just style. Run on save AND on load."""
    raw = unicodedata.normalize("NFKC", str(instruction or ""))
    if _PERSONA_BAD.search(raw):
        return False
    squashed = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9@ ]+", " ", soft_norm(raw))).strip()  # "i g n o r e", "ign0re"
    if _PERSONA_BAD.search(squashed):
        return False
    compact = re.sub(r"[^a-z0-9@]+", "", soft_norm(raw))
    return not _PERSONA_COMPACT.search(compact)
