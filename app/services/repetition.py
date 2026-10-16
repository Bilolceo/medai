"""Whether a reply says again what this conversation has already been told.

The owner's complaint (2026-10-16): the same questions get the same answers,
word for word, as if nobody were thinking. The persona asks for fresh words
and the last replies are shown back to the model, and still a patient asking
"nima qilsam bo'ladi?" twice was sent the same sentence twice. So it is
checked here, by text, the way the price and the booking offer are: free
when nothing repeats, one rewrite when something does.

Two things count as a repeat: a sentence of substance (not "Salomat
bo'ling") that an earlier reply already said -- word for word or with a word
or two changed around it -- or a reply that is mostly the same words as one
of the last two.

The address, the phone and the doctor's Telegram are said again only when
the patient asks for them again. Live, 2026-10-16: "Doktor bilan Telegram
orqali bog'lanishingiz mumkin: @Temur_Akhmadaliev" closed three replies in a
row to somebody who had asked about something else each time.
"""

import re
from collections.abc import Sequence

_MIN_SENTENCE = 30
_SAME_WORDS = 0.5

# Sharing a sentence's words this much, and at least this many of them,
# makes it the same sentence said again.
_SAME_SENTENCE = 0.8
_SHARED_WORDS = 5

# Lines that are fine to say again: a goodbye, a thank-you.
_ALLOWED = re.compile(
    r"salomat|rahmat|marhamat|bemalol|xayr|сог\s+бўлинг|раҳмат|спасибо|до свидания",
    re.IGNORECASE,
)

# Where the clinic is and how to reach it: said again only when asked again.
_CONTACT = re.compile(r"\+?998|@\w|ko['‘ʻ’`]?chasi|кўчаси|улица", re.IGNORECASE)
_ASKS_CONTACT = re.compile(
    r"\btg\b|telegram|телеграм|nomer|raqam|telefon|номер|рақам|телефон|manzil|манзил|адрес"
    r"|adres|address|qayer|қаер|где|lokats|локац|bog['‘ʻ’`]?lan|боғлан|связ|ko['‘ʻ’`]?cha",
    re.IGNORECASE,
)

# A reply that opens with the same acknowledgement as the one before it
# reads as a template: "Tushundim." three times in a row (live, 2026-10-16).
_ACK_OPENING = re.compile(
    r"^\s*(tushundim|tushunaman|tushunarli|albatta|тушундим|тушунаман|тушунарли|понимаю|понятно)"
    r"\s*[.!,—–-]?\s+",
    re.IGNORECASE,
)


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w ]", " ", text.lower()).split())


def _sentences(text: str, *, contact_ok: bool) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\s+[—–]\s+", text)
    return [
        _norm(p)
        for p in parts
        if len(_norm(p)) >= _MIN_SENTENCE
        and not _ALLOWED.search(p)
        and not (contact_ok and _CONTACT.search(p))
    ]


def _same(sentence: str, other: str) -> bool:
    if sentence == other:
        return True
    a, b = set(sentence.split()), set(other.split())
    shared = len(a & b)
    return shared >= _SHARED_WORDS and shared / min(len(a), len(b)) >= _SAME_SENTENCE


def asks_contact(message: str) -> bool:
    """Whether the patient is asking where the clinic is or how to reach it."""
    return bool(_ASKS_CONTACT.search(message))


def repeated(reply: str, earlier: Sequence[str], asked: str = "") -> list[str]:
    """The sentences of `reply` this conversation has already been sent.

    Also returns the whole reply when it is mostly the same words as one of
    the last two replies -- the same answer reworded only at its edges.
    `asked` is the patient's message: asking for the address or the number
    again makes saying it again the answer.
    """
    if not earlier:
        return []
    contact_ok = asks_contact(asked)
    said = [s for turn in earlier for s in _sentences(turn, contact_ok=contact_ok)]
    found = [s for s in _sentences(reply, contact_ok=contact_ok) if any(_same(s, o) for o in said)]
    if contact_ok and _CONTACT.search(reply):
        # An address or a number asked for again is the same answer by nature.
        return found
    words = set(_norm(reply).split())
    for turn in earlier[-2:]:
        other = set(_norm(turn).split())
        if len(words) >= 6 and len(words & other) / max(1, len(words | other)) > _SAME_WORDS:
            found.append(_norm(reply))
            break
    return found


def drop_echoed_opening(reply: str, earlier: Sequence[str]) -> str:
    """`reply` without its "Tushundim." when the last reply opened the same way."""
    if not earlier or not _ACK_OPENING.match(earlier[-1]):
        return reply
    match = _ACK_OPENING.match(reply)
    if match is None:
        return reply
    rest = reply[match.end() :]
    if len(rest.split()) < 3:
        # "Tushundim, rahmat." is the whole reply; nothing to open instead.
        return reply
    return rest[:1].upper() + rest[1:]
