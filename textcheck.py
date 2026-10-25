"""Plain-Python text checks for Yuki's moderation. No AI, no network, no tokens: just regex and a little context scoring.

What's here
  - analyze_harassment(): bullying / personal attacks / telling people to hurt themselves / threats, scored 0-3
  - has_hate_slur():      slurs other than the n-word (that one lives in moderation.py)
  - has_strong_swear():   strong swearing in general (for servers whose rules ban cursing)
  - find_links() / has_personal_info()

The hard part of bullying detection is not the word list, it's WHO the words are aimed at. "this game is fucking trash" is fine,
"you worthless piece of shit" is not. So an insult only counts when it is aimed at someone: the message says "you/u/ur", or it
mentions / replies to another member, or it is an order like "shut up". Banter markers (lol, lmao, jk...) and friendly words
(love you, thanks) turn a hit down a notch, and negations ("you're not stupid") cancel it. Repetition then turns it back up
(see Mod._harassment in moderation.py), because bullying is a pattern.

Severity: 0 nothing | 1 mild (react only, escalates if repeated) | 2 reminder + staff ping | 3 serious (self-harm bait, threats)
"""

from __future__ import annotations

import codecs
import os
import re
import unicodedata

# ---------------------------------------------------------------------------------------------- normalizing

_MENTION_TOKEN = re.compile(r"<(?:@[!&]?|#|a?:\w+:)\d*>?")
_ZERO_WIDTH = re.compile("[\u200b-\u200f\u2060\ufeff\u00ad]")
_HOMOGLYPHS = str.maketrans(
    {
        "а": "a", "е": "e", "і": "i", "ї": "i", "ı": "i", "о": "o", "с": "c", "р": "p", "ѕ": "s", "ј": "j", "у": "y",
        "х": "x", "ɡ": "g", "ɢ": "g", "н": "n", "г": "r", "η": "n", "ι": "i", "α": "a", "ε": "e", "ɪ": "i", "ɑ": "a", "ο": "o",
    }
)
_INNER_LEET = {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i", "|": "i", "+": "t"}
_INNER_LEET_RE = re.compile(r"(?<=[a-z])[013457@$!|+](?=[a-z])")  # only INSIDE words, so "stupid!" stays "stupid!"
_SPACED_LETTERS = re.compile(r"(?<![a-z0-9])((?:[a-z][ .\-_]){2,}[a-z])(?![a-z0-9])")  # "f u c k", "f.u.c.k"
_REPEATS = re.compile(r"([a-z])\1{2,}")


def soft_norm(text: str) -> str:
    """Lowercase, drop mentions/emoji tags, undo lookalike letters and in-word leetspeak, squash fuuuuck -> fuuck."""
    t = _MENTION_TOKEN.sub(" ", (text or "")[:1000])
    t = unicodedata.normalize("NFKD", t.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = _ZERO_WIDTH.sub("", t).translate(_HOMOGLYPHS)
    t = _INNER_LEET_RE.sub(lambda m: _INNER_LEET[m.group(0)], t)
    t = _SPACED_LETTERS.sub(lambda m: re.sub(r"[ .\-_]", "", m.group(1)), t)
    return _REPEATS.sub(r"\1\1", t)


def _b(pattern: str) -> str:
    """Wrap in word boundaries that also treat digits as word characters."""
    return rf"(?<![a-z0-9])(?:{pattern})(?![a-z0-9])"


# ---------------------------------------------------------------------------------------------- word shapes
# vowels can be masked (f*ck, sh#t) and letters stretched (fuuuck): the text is already squashed by soft_norm

_FUCK = r"f+(?:[u*#xv]+c*|c+)k+"
_FUCKW = rf"{_FUCK}(?:ing|in|ed|er|ers|s|face|head|wit|boy)?"
_SHIT = r"s+h+[i*#]+t+(?:s|ty|ting|ted|head|bag|face|show|hole|stain)?"
_BITCH = r"b+[i*#]*a?t+c+h+(?:es|y|ing)?"
_CUNT = r"c+[u*#]+n+t+s?"
_ASSHOLE = (
    r"(?:a+s+s+|a+r+s+e+)[\s\-_.]?(?:h+o+l+e+|w+i+p+e+|h+a+t+|c+l+o+w+n+|f+a+c+e+|b+a+g+)s?"
    r"|(?:dumb|jack|dip|stupid|lame)[\s\-_.]?a+s+s+(?:es)?"
)
_DICKHEAD = r"d+[i*#]+c+k+[\s\-_.]?(?:h+e+a+d+|f+a+c+e+|w+a+d+|b+a+g+)s?"
_SHITHEAD = r"s+h+[i*#]+t+[\s\-_.]?(?:h+e+a+d+|b+a+g+|f+a+c+e+|s+t+a+i+n+)s?"
_MOTHERFUCKER = rf"m+o+t+h+e+r+[\s\-_.]?{_FUCK}(?:er|ers|ing|in)?"
_WHORE = r"w+h+[o*#]+r+e+s?"
_SLUT = r"s+l+[u*#]+t+(?:s|ty)?"

# ---------------------------------------------------------------------------------------------- hate slurs (besides the n-word)
# kept rot13-encoded so the source file itself stays clean; they are decoded once at import
_HATE_ROT13 = [
    "snttbg", "snt", "ergneq", "ergneqrq", "genaal", "xvxr", "puvax", "fcvp", "jrgonpx", "pbba", "tbbx", "gbjryurnq", "cnxv",
]
_HATE_WORDS = [codecs.decode(w, "rot13") for w in _HATE_ROT13]
_HATE_RE = re.compile(
    _b("|".join("".join(f"{re.escape(c)}+" for c in w) + "s?" for w in _HATE_WORDS))
)
_HATE_IDIOM = re.compile(r"chink\s+in\s+(?:the|his|her|their|our|your|my)\b")  # "a chink in the armor"

_extra = [w.strip().lower() for w in os.getenv("MOD_EXTRA_BLOCKED", "").split(",") if w.strip()]
_EXTRA_RE = re.compile(_b("|".join(re.escape(w) for w in _extra))) if _extra else None


def has_hate_slur(text: str) -> bool:
    t = soft_norm(text)
    if _HATE_IDIOM.search(t):
        t = _HATE_IDIOM.sub(" ", t)
    return bool(_HATE_RE.search(t) or (_EXTRA_RE and _EXTRA_RE.search(t)))


# ---------------------------------------------------------------------------------------------- swearing

_SWEAR = re.compile(
    _b("|".join([_FUCKW, _SHIT, _BITCH, _CUNT, _ASSHOLE, _DICKHEAD, _SHITHEAD, _MOTHERFUCKER, _WHORE, _SLUT]))
)


def has_strong_swear(text: str) -> bool:
    return bool(_SWEAR.search(soft_norm(text)))


# ---------------------------------------------------------------------------------------------- bullying / harassment

_YOU = re.compile(_b(r"you+|u+|ya+|yu+|youre|ur|your|urself|yourself"))
_FIRST = re.compile(_b(r"i|im|i'm|ive|i've|me|my|myself|we|our"))
_NEG = re.compile(r"(?:not|n't|never|aint|ain't|no|dont|don't|isnt|isn't|wasnt|wasn't|arent|aren't)\b")
_BANTER = re.compile(r"(?<![a-z0-9])(?:lol|lmao|lmfao|rofl|ha(?:ha)+|hehe|jk|j/k|just\s+kidding|joking|kidding|xd)(?![a-z0-9])|[😂💀🤣😭😹]")
_FRIENDLY = re.compile(_b(r"love|luv|miss|thanks?|thank|proud|appreciate|bestie|best\s+friend|fam"))
_GAME = re.compile(
    _b(r"game|games|valorant|fortnite|minecraft|roblox|csgo|cs2|apex|league|dota|ranked|rank|1v1|lobby|round|match|spawn|clutch|"
       r"gg|ez|respawn|bedwars|arena|boss|duel|pvp|raid|aim|kd")
)

_SELFHARM = re.compile(
    "|".join(
        [
            _b(r"k+y+s+"),
            _b(r"(?:kill|hang|off|end|neck|unalive|rope|drown)\s+(?:yourself|yourselves|urself|ur\s*self|your\s+self)"),
            _b(r"(?:i\s+)?hope\s+(?:you|u|ya)\s+(?:die|get\s+cancer|rot|choke|burn|suffer|get\s+hit\s+by)"),
            _b(r"(?:you|u|ya)\s+(?:deserve|should)\s+(?:to\s+)?(?:die|be\s+dead|rot|disappear)"),
            _b(r"drink\s+bleach"),
        ]
    )
)
_GODIE = re.compile(_b(r"go\s+(?:and\s+)?(?:die|rot)(?:\s+in\s+a\s+(?:hole|ditch|fire))?"))
_THREAT = re.compile(
    "|".join(
        [
            _b(
                r"(?:i\s*'?\s*ll|i\s*'?\s*m\s+(?:gonna|going\s+to)|i\s+will|ima|imma|gonna|going\s+to|wanna|want\s+to)\s+"
                r"(?:kill|murder|shoot|stab|beat|hurt|rape|find|dox|swat|hunt|end)\s+(?:you|u|ya|ur|your|him|her|them)"
            ),
            _b(r"i\s+know\s+where\s+(?:you|u)\s+live"),
            _b(r"dox+(?:ing|ed)?\s+(?:you|u)"),
        ]
    )
)

_FUCK_YOU = re.compile(
    "|".join(
        [
            rf"(?<![a-z0-9]){_FUCK}(?:ing|in)?\s*(?:you+|u+|ya+|yu+|ur|your|urself|yourself|off+)(?![a-z0-9])",
            _b(r"(?:eat|choke\s+on|suck|lick)\s+(?:a\s+)?(?:shit|dick|cock|balls|my\s+(?:dick|cock|balls|ass))"),
            _b(r"(?:go\s+to\s+hell|piss\s+off|screw\s+you)"),
        ]
    )
)
_PUTDOWN = re.compile(
    "|".join(
        [
            _b(r"(?:no\s*one|nobody|noone|everyone)\s+(?:likes?|wants?|cares?\s+about|loves?|hates?)\s+(?:you|u|ya)(?:\s+here)?"),
            _b(r"(?:you|u|ya)\s+(?:are|re|r)?\s*(?:a\s+)?(?:waste\s+of\s+(?:space|oxygen|air|skin)|worthless|good\s+for\s+nothing)"),
            _b(r"waste\s+of\s+(?:space|oxygen|air|skin)"),
        ]
    )
)
_ORDER = re.compile(_b(r"shut\s+(?:the\s+(?:fuck|hell|f)\s+)?up|stfu|shut\s+it|get\s+lost|get\s+the\s+fuck\s+out"))

_STRONG_INSULT = re.compile(
    _b(
        "|".join(
            [
                _BITCH, _CUNT, _ASSHOLE, _DICKHEAD, _SHITHEAD, _MOTHERFUCKER, _WHORE, _SLUT,
                r"s+k+a+n+k+s?", r"b+a+s+t+a+r+d+s?", r"s+c+u+m+(?:bag)?s?", r"t+w+a+t+s?", r"w+a+n+k+e+r+s?",
                r"t+o+s+s+e+r+s?", r"b+e+l+l+e+n+d+s?", r"p+r+i+c+k+s?",
                r"piece\s+of\s+(?:shit|crap|garbage|trash)", r"worthless", r"waste\s+of\s+(?:space|oxygen|air|skin)",
            ]
        )
    )
)
_MILD_INSULT = re.compile(
    _b(
        r"idiots?|morons?|imbeciles?|stupid|dumb|dumbo|dummy|losers?|trash|garbage|pathetic|ugly|fat|disgusting|useless|"
        r"brain\s?dead|clowns?|bozos?|creeps?|freaks?|degenerates?|lunatics?|psychos?|incels?"
    )
)
_SUCK = re.compile(_b(r"(?:you|u|ya)\s+(?:suck|stink)"))


def _negated(t: str, start: int) -> bool:
    return bool(_NEG.search(t[max(0, start - 14) : start]))


def analyze_harassment(text: str, directed_ctx: bool = False) -> tuple[int, str]:
    """(severity 0-3, short reason). directed_ctx = the message mentions or replies to another member."""
    t = soft_norm(text)
    if len(t.strip()) < 3:
        return 0, ""
    banter = bool(_BANTER.search(t))

    if _SELFHARM.search(t):
        return 3, "told someone to hurt or kill themselves"
    if _GODIE.search(t):
        if _GAME.search(t) or banter:
            return 1, "told someone to go die (probably game banter)"
        return 3, "told someone to hurt or kill themselves"
    if _THREAT.search(t):
        return (1, "threat in a game context") if _GAME.search(t) else (3, "made a threat")

    you = bool(_YOU.search(t))
    order = bool(_ORDER.search(t))
    directed = you or directed_ctx or order

    sev, why = 0, ""

    def bump(s: int, w: str):
        nonlocal sev, why
        if s > sev:
            sev, why = s, w

    if _FUCK_YOU.search(t):
        bump(2, "told someone off with strong swearing")
    if _PUTDOWN.search(t):
        bump(2, "put someone down (said nobody likes them / they're worthless)")
    if directed:
        m = _STRONG_INSULT.search(t)
        if m and not _negated(t, m.start()):
            bump(2, "called someone a strong insult")
        m = _MILD_INSULT.search(t)
        if m and not _negated(t, m.start()) and not (_FIRST.search(t) and not you):
            bump(1, "insulted someone")
        if _SUCK.search(t):
            bump(1, "insulted someone")
        if order:
            bump(1, "told someone to shut up")

    # lol / lmao / "love you" turn a hit down one notch (friends joke like this); repeating it turns it back up in the Mod
    if sev == 2 and (banter or _FRIENDLY.search(t)):
        sev, why = 1, why + " (probably joking)"
    return sev, why


# ---------------------------------------------------------------------------------------------- links and personal info

_URL = re.compile(r"(?:https?://|www\.)[^\s<>()\"']+", re.IGNORECASE)
_DEFAULT_LINK_ALLOW = [
    "tenor.com", "giphy.com", "cdn.discordapp.com", "media.discordapp.net", "images-ext-1.discordapp.net",
    "images-ext-2.discordapp.net", "discord.com", "discordapp.com",
]
_extra_allow = [d.strip().lower() for d in os.getenv("MOD_LINK_ALLOW", "").split(",") if d.strip()]
LINK_ALLOW = _DEFAULT_LINK_ALLOW + _extra_allow


def find_links(text: str, allow=None) -> list:
    """URLs in the text whose domain is not on the allow list (gifs and discord's own links are fine by default)."""
    allow = LINK_ALLOW if allow is None else allow
    bad = []
    for u in _URL.findall(text or ""):
        host = re.sub(r"^(?:https?://|www\.)", "", u, flags=re.IGNORECASE).split("/")[0].split(":")[0].lower().lstrip("www.")
        if not any(host == d or host.endswith("." + d) for d in allow):
            bad.append(u)
    return bad


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]{2,}")
_PHONE = re.compile(r"(?<![\d.])(?:\+?\d{1,2}[\s.-])?(?:\(\d{3}\)[\s.-]?|\d{3}[\s.-])\d{3}[\s.-]?\d{4}(?![\d.])")
_IPV4 = re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])")


def has_personal_info(text: str) -> bool:
    t = text or ""
    return bool(_EMAIL.search(t) or _PHONE.search(t) or _IPV4.search(t))
