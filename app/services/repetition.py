"""Whether a reply says again what this conversation has already been told.

The owner's complaint (2026-10-16): the same questions get the same answers,
word for word, as if nobody were thinking. The persona asks for fresh words
and the last replies are shown back to the model, and still a patient asking
"nima qilsam bo'ladi?" twice was sent the same sentence twice. So it is
checked here, by text, the way the price and the booking offer are: free
when nothing repeats, one rewrite when something does.

Two things count as a repeat: a whole sentence of substance (not "Salomat
bo'ling") that an earlier reply already said, or a reply that is mostly the
same words as one of the last two.
"""

import re
from collections.abc import Sequence

_MIN_SENTENCE = 30
_SAME_WORDS = 0.5

# Lines that are fine to say again: a goodbye, a thank-you, the address or a
# number the patient asked for twice.
_ALLOWED = re.compile(
    r"salomat|rahmat|marhamat|bemalol|xayr|сог\s+бўлинг|раҳмат|спасибо|до свидания"
    r"|\+?998|@\w|ko['‘ʻ’`]?chasi|кўчаси|улица",
    re.IGNORECASE,
)


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w ]", " ", text.lower()).split())


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\s+[—–]\s+", text)
    return [_norm(p) for p in parts if len(_norm(p)) >= _MIN_SENTENCE and not _ALLOWED.search(p)]


def repeated(reply: str, earlier: Sequence[str]) -> list[str]:
    """The sentences of `reply` this conversation has already been sent.

    Also returns the whole reply when it is mostly the same words as one of
    the last two replies -- the same answer reworded only at its edges.
    """
    if not earlier:
        return []
    said = {s for turn in earlier for s in _sentences(turn)}
    found = [s for s in _sentences(reply) if s in said]
    words = set(_norm(reply).split())
    if _ALLOWED.search(reply):
        # An address or a number asked for again is the same answer by nature.
        return found
    for turn in earlier[-2:]:
        other = set(_norm(turn).split())
        if len(words) >= 6 and len(words & other) / max(1, len(words | other)) > _SAME_WORDS:
            found.append(_norm(reply))
            break
    return found
