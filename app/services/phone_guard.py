"""Telephone numbers a reply may carry: the clinic's, and no other.

The doctor's personal number was handed out for a week -- by a rule, then by
the model copying it from earlier turns of the same conversation after the
rule was gone (live, 15-19 October 2026). The owner's word on 2026-10-21:
that number no longer exists, and no number but the clinic's two is ever
to be written. A rule asks the model; this makes sure of it on the way out.

Any Uzbek number in a reply that is not one of the clinic's is replaced
with the clinic's first number, so the sentence around it still reads --
"qo'ng'iroq qiling: +998 70 310 40 40". With no clinic numbers configured,
the stray number is simply taken out.
"""

import logging
import re

logger = logging.getLogger(__name__)

# +998 XX XXX XX XX in the shapes people type it: spaces, dashes, brackets,
# the country code with or without the plus, or none at all (nine digits
# starting with an operator code).
_NUMBER = re.compile(
    r"(?<!\d)(?:\+?998[\s\-()]*)?(\d{2})[\s\-()]*(\d{3})[\s\-]*(\d{2})[\s\-]*(\d{2})(?!\d)"
)
# The hidden markers the model writes for the clinic -- [[BOOK:...|phone|...]],
# [[CALL:...]] -- carry the patient's own number; they are not shown to
# anybody and are left as they are.
_MARKER = re.compile(r"(\[\[.*?\]\])", re.DOTALL)
_SEPARATOR = re.compile(r"\s*[,;]\s*|\s+(?:yoki|или|или же|va|и)\s+", re.IGNORECASE)
_UZ_OPERATOR_CODES = frozenset(
    {"20", "33", "50", "55", "60", "61", "62", "65", "66", "67", "69", "70",
     "71", "73", "74", "75", "77", "78", "79", "88", "90", "91", "93", "94", "95", "97", "98", "99"}
)  # fmt: skip


def digits_of(number: str) -> str:
    """The nine subscriber digits: "+998 93 051 25 96" -> "930512596"."""
    digits = re.sub(r"\D", "", number)
    if len(digits) == 12 and digits.startswith("998"):
        digits = digits[3:]
    return digits


def allowed_numbers(clinic_phone_numbers: str | None) -> list[str]:
    """The clinic's numbers as written in its settings, in order."""
    if not clinic_phone_numbers:
        return []
    found = []
    for part in _SEPARATOR.split(clinic_phone_numbers):
        match = _NUMBER.search(part or "")
        if match:
            found.append(match.group(0).strip())
    return found


def strip(reply: str, clinic_phone_numbers: str | None) -> str:
    """The reply with every number that is not the clinic's taken out."""
    allowed = allowed_numbers(clinic_phone_numbers)
    allowed_digits = {digits_of(n) for n in allowed}
    stand_in = allowed[0] if allowed else ""

    def replace(match: re.Match[str]) -> str:
        digits = digits_of(match.group(0))
        if len(digits) != 9 or digits[:2] not in _UZ_OPERATOR_CODES:
            return match.group(0)  # a price, a date, an order number
        if digits in allowed_digits:
            return match.group(0)
        logger.warning("foreign_phone_number_removed", extra={"number": digits})
        return stand_in

    cleaned = "".join(
        part if part.startswith("[[") else _NUMBER.sub(replace, part)
        for part in _MARKER.split(reply)
    )
    if cleaned == reply:
        return reply
    # Two clinic numbers side by side after a replacement read as a slip.
    if stand_in:
        cleaned = re.sub(
            rf"{re.escape(stand_in)}(\s*(?:yoki|или|va|и|,|/)\s*){re.escape(stand_in)}",
            stand_in,
            cleaned,
        )
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


def foreign_numbers(text: str, allowed_digits: set[str]) -> list[str]:
    """The Uzbek numbers written in ``text`` that are not the clinic's, as
    nine digits each. The same reading of "a number" as ``strip``, so what the
    guard removes from the bot's replies is what the weekly report counts in
    the operators'. Hidden markers are skipped: they carry the patient's own
    number."""
    found: list[str] = []
    for part in _MARKER.split(text):
        if part.startswith("[["):
            continue
        for match in _NUMBER.finditer(part):
            digits = digits_of(match.group(0))
            if (
                len(digits) == 9
                and digits[:2] in _UZ_OPERATOR_CODES
                and digits not in allowed_digits
            ):
                found.append(digits)
    return found
