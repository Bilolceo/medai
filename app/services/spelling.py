"""Misspellings the model has been seen to make, put right before sending.

A patient reads a clinic that cannot spell its own introduction as careless.
Only words seen wrong in real replies are listed, each with the one spelling
it was meant to be; nothing here guesses.
"""

import re

# Seen live, 2026-10-16.
_FIXES: dict[str, str] = {
    "администраториаман": "администраториман",
    "administratoriaman": "administratoriman",
    "ekkan": "ekan",
}

# "ekkan" is also a real word ("planted"); it is a typo only after a word
# describing something -- "ta'sirli ekkan", "yaxshi ekkan".
_AFTER_DESCRIPTION = {"ekkan"}
_DESCRIBING = re.compile(r"(?:li|siz|shi|ri|da|dan|gan|kan|qan|ham)$", re.IGNORECASE)

_WORD = re.compile(r"\w+", re.UNICODE)


def fix(text: str) -> str:
    """`text` with the known misspellings corrected, keeping capitalisation."""
    out: list[str] = []
    last = 0
    previous = ""
    for match in _WORD.finditer(text):
        word = match.group(0)
        right = _FIXES.get(word.lower())
        if right is not None and (
            word.lower() not in _AFTER_DESCRIPTION or _DESCRIBING.search(previous)
        ):
            if word[:1].isupper():
                right = right[:1].upper() + right[1:]
            out.append(text[last : match.start()] + right)
            last = match.end()
        previous = word
    out.append(text[last:])
    return "".join(out)
