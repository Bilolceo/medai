"""What a patient's message says, read by the model rather than by patterns.

The scripted flows (app.services.scripted_flow) decide what to say next and
say it in the doctor's words; to do that they need to know what the patient
wrote: a complaint, a name, where they are, a number, a time, a question, a
no. Patterns caught the tidy cases and missed the rest -- "rassiyada",
"Toshkendaman lekin hozr rassiyada", "men Otabek, Andijondanman, raqamim
..., ertaga 10 da" -- each new spelling another pattern. The model reads
them as a person would. It is asked for facts only, as JSON; it never
writes the reply. If the call fails or the answer is not JSON, the flows
fall back to their patterns, as before.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.rag.llm import ChatMessage, LLMProvider

logger = logging.getLogger(__name__)

KINDS = ("none", "urology", "libido_only", "sport", "emergency")

_PROMPT = """\
You read one message a patient wrote to a urologist-andrologist's Instagram \
administrator in Uzbekistan, and say what is in it. Patients write Uzbek \
(Latin or Cyrillic) or Russian, with typos, dialect and no punctuation: \
"rassiyada" is Russia, "km bn gaplashvoma" is "who am I talking to", \
"ishvoht" is "working hours", "toshkendaman" is "I am from Tashkent". \
You are given the administrator's last message, so a bare "Akmal" after \
"Ismingiz nima?" is a name and "Buxoro" after "Qayerdansiz?" is a place.

Answer with JSON only, exactly these keys:
{
 "greeting_only": true if the message is only a greeting or small talk,
 "complaint_kind": "none" | "urology" (any urological or andrological \
problem: erection, potency, premature ejaculation, prostate, urination, \
testicles, varicocele, infertility, infection, stones, circumcision, \
hormones, testosterone, a wish to be examined) | "libido_only" (ONLY low \
or lost sexual desire, nothing else) | "sport" (anabolic steroids, a \
steroid course, bodybuilding, sport, testosterone injections) | "emergency" (sudden severe pain or \
swelling, bleeding, cannot urinate, high fever),
 "complaint": the complaint in a few of the patient's own words, or null,
 "name": the patient's first name if they give it, capitalised and in the \
alphabet they wrote it in ("Руслан" stays "Руслан"), or null -- never a \
place, a country or a word like "siz", "ha", "men",
 "place": the city or country they are in or from, or null,
 "abroad": true if they are outside Uzbekistan now,
 "phone": the telephone number exactly as typed, or null,
 "time": when they want to be called or seen, in their words without the \
number (e.g. "ertaga 10 da", "tushdan keyin", "16:00 da", "qachon bo'lsa ham"), or null,
 "time_hhmm": that time as HH:MM on a 24-hour clock if it names an hour \
("soat 2" in the day is 14:00, "kechki 8" is 20:00), or null,
 "age": their age as a number if given, or null,
 "question": the question they ask that needs an answer, or null,
 "asks_price": true if they ask the price or cost,
 "wants_evening": true if they want the evening, after work, or an hour \
after 16:00,
 "refuses": true if they decline (no, not now, I will write myself, I won't \
give my number),
 "who_are_you": true if they ask who they are talking to or whether this is \
a bot,
 "messenger": "telegram" | "whatsapp" | null if they name one,
 "yes": true if the message is a bare yes / agreement
}"""


@dataclass(frozen=True)
class Reading:
    greeting_only: bool = False
    complaint_kind: str = "none"
    complaint: str | None = None
    name: str | None = None
    place: str | None = None
    abroad: bool = False
    phone: str | None = None
    time: str | None = None
    time_hhmm: str | None = None
    age: int | None = None
    question: str | None = None
    asks_price: bool = False
    wants_evening: bool = False
    refuses: bool = False
    who_are_you: bool = False
    messenger: str | None = None
    yes: bool = False
    raw: dict[str, Any] = field(default_factory=dict, compare=False)


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def parse(payload: str) -> Reading | None:
    """The model's JSON as a Reading; None if it is not usable."""
    match = re.search(r"\{.*\}", payload, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    kind = data.get("complaint_kind")
    hhmm = _text(data.get("time_hhmm"))
    if hhmm and not re.fullmatch(r"\d{1,2}:\d{2}", hhmm):
        hhmm = None
    age = data.get("age")
    messenger = _text(data.get("messenger"))
    return Reading(
        greeting_only=data.get("greeting_only") is True,
        complaint_kind=kind if kind in KINDS else "none",
        complaint=_text(data.get("complaint")),
        name=_text(data.get("name")),
        place=_text(data.get("place")),
        abroad=data.get("abroad") is True,
        phone=_text(data.get("phone")),
        time=_text(data.get("time")),
        time_hhmm=hhmm,
        age=age if isinstance(age, int) and 0 < age < 120 else None,
        question=_text(data.get("question")),
        asks_price=data.get("asks_price") is True,
        wants_evening=data.get("wants_evening") is True,
        refuses=data.get("refuses") is True,
        who_are_you=data.get("who_are_you") is True,
        messenger=messenger.lower() if messenger else None,
        yes=data.get("yes") is True,
        raw=data,
    )


async def read(provider: LLMProvider, message: str, last_bot_message: str | None) -> Reading | None:
    """What the message says, or None when the model could not be asked."""
    shown = (
        f"Administrator's last message: {last_bot_message or '(none -- first message)'}\n"
        f"Patient's message: {message}"
    )
    try:
        answer = await provider.generate(_PROMPT, [ChatMessage(role="user", content=shown)])
    except Exception:
        logger.exception("message_reader_failed")
        return None
    reading = parse(answer)
    if reading is None:
        logger.warning("message_reader_unparsed", extra={"answer": answer[:200]})
    return reading
