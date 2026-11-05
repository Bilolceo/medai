"""Is the number a patient left one the operators can ring?

The operators ring Uzbek numbers only. A patient types the number by hand,
and a digit dropped or doubled is a call that never connects -- found out a
day later, when the patient has gone elsewhere. A patient abroad leaves a
number the operators cannot ring at all. So the number is read here before
it is accepted:

* an Uzbek number: +998 and nine digits, the first two a known operator or
  area code -- accepted, and shown back in one shape (+998 90 123 45 67);
* nearly one: the right start but a digit short or over, or a code no
  operator uses -- the patient is asked to check it, with an example;
* foreign: another country code -- the patient is asked for Telegram or
  WhatsApp on it, or for a number in Uzbekistan.

Codes: Beeline 90, 91, 20; Ucell 93, 94, 50; Uzmobile 95, 99, 77; Mobiuz
88, 97; Perfectum 98; Humans 33; and the fixed-line area codes (55, 6x, 7x).
Numbers move between operators, so the code says only that it is Uzbek.
"""

import re
from dataclasses import dataclass

from app.services.patient_profile import _UZ_OPERATOR_CODES

# A run of digits with the separators people type between them.
_RUN = re.compile(r"(?:\+|00)?\d[\d\s().\-‐-―]{5,}\d")

UZ = "uz"
FOREIGN = "foreign"
SHORT = "short"
LONG = "long"
BAD_CODE = "bad_code"


@dataclass(frozen=True)
class Phone:
    kind: str
    # The number as the operators will see it: "+998 90 123 45 67", or the
    # foreign one with its plus.
    shown: str
    digits: int = 0


def _uz_shown(nine: str) -> str:
    return f"+998 {nine[:2]} {nine[2:5]} {nine[5:7]} {nine[7:]}"


def read(text: str) -> Phone | None:
    """The number in this message and what it is, or None if there is none."""
    match = _RUN.search(text)
    if match is None:
        return None
    raw = match.group(0)
    plus = raw.startswith("+") or raw.startswith("00")
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("00"):
        digits = digits[2:]

    if digits.startswith("998") and (plus or len(digits) >= 10):
        rest = digits[3:]
        return _uz(rest)
    if plus:
        if 8 <= len(digits) <= 15:
            return Phone(FOREIGN, "+" + digits, len(digits))
        return Phone(SHORT, "+" + digits, len(digits))
    # No country code.
    if len(digits) == 11 and digits[0] in "78":
        # Russia or Kazakhstan, typed the way it is dialled there.
        return Phone(FOREIGN, "+7" + digits[1:], 11)
    if len(digits) == 10 and digits[0] == "8" and digits[1:3] in _UZ_OPERATOR_CODES:
        # The old domestic "8" in front: "8 90 123 45 67".
        return _uz(digits[1:])
    if len(digits) in (9, 8, 10):
        return _uz(digits)
    if len(digits) in (6, 7):
        return Phone(SHORT, digits, len(digits))
    return None


def _uz(nine: str) -> Phone:
    if len(nine) < 9:
        return Phone(SHORT, "+998 " + nine, len(nine))
    if len(nine) > 9:
        return Phone(LONG, "+998 " + nine, len(nine))
    if nine[:2] not in _UZ_OPERATOR_CODES:
        return Phone(BAD_CODE, _uz_shown(nine), 9)
    return Phone(UZ, _uz_shown(nine), 9)
