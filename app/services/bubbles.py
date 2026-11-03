"""One reply, several messages.

A clinic administrator texting a patient sends the introduction and then
the question as two bubbles, not one block. The assistant marks the break
with ``|||`` and delivery sends each part as a message of its own, in
order. The transcript keeps the reply as one row with the parts on their
own paragraphs, so the dashboard and the model's history read it as one
turn.
"""

import re

MARKER = "|||"
_BREAK = re.compile(r"\s*\|\|\|\s*")


def parts(text: str) -> list[str]:
    """The messages to send, in order; never empty when ``text`` has words."""
    pieces = [piece.strip() for piece in _BREAK.split(text)]
    return [piece for piece in pieces if piece] or [text.strip()]


def flatten(text: str) -> str:
    """The reply as one block, a blank line where the breaks were."""
    return "\n\n".join(parts(text)) if MARKER in text else text
