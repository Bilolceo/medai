"""The administrator as an agent: the model reads the whole conversation, the
doctor's rules and the clinic's services, and writes the reply itself.

The owner, 2026-11-21: the scripted flow (app.services.scripted_flow) answered
with fixed texts chosen by patterns, and every new way a patient wrote broke
it ("inplant narxi" got the consultation's price). Here the model decides
what to say -- in the doctor's order, from his rules -- and the code keeps
only the guards the model must never be trusted with:

- a sum of money no service names is never sent (asked once to correct it,
  then the sentence is dropped);
- a telephone number that is not the clinic's is never sent;
- a card is one the clinic has, sent once a conversation;
- a time offered is one the appointment book has free;
- the patient's paid booking or wish to be called is written down for the
  administrator ([[CALLBACK]]), and a hand-over flags the conversation.

A clinic uses it when its settings name an agent profile: the rules and the
services live as text in app/agent_profiles/<profile>/, which the doctor's
corrections change -- not the code.
"""

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.rag.llm import ChatMessage, LLMProvider
from app.services import bubbles, language, phone_check, phone_guard, price_guard, promo_images
from app.services.doctor_template import to_cyrillic

logger = logging.getLogger(__name__)

PROFILES = Path(__file__).resolve().parents[1] / "agent_profiles"

_CONTRACT = """\

## HOW TO ANSWER
Think about where the conversation is (what the patient has told, what you asked,
what they want now), then answer with JSON only, no other text:
{
 "messages": ["first chat bubble", "second bubble"],
 "cards": [],
 "lead": null,
 "handoff": false
}
- "messages": 1-3 short messages, in the patient's language and alphabet. Never a sum,
  a phone number or a card number that SERVICES and the rules do not give.
- "cards": the picture cards to send with this reply, by id: "day", "evening", "sport",
  "op:falloprotez", "op:varikosele", "op:skrotoplastika", "op:ligamentotomiya",
  "op:kompleks", "op:micro-tese", "op:denervatsiya". Only when the rules say so (an
  operation's price asked; the consultation price after the time is agreed or when
  asked). A card already sent (CARDS SENT below) is not sent again: say the price in
  digits without it.
- "lead": when the administrator must act, else null:
  {"phone": "<the patient's number as they wrote it>",
   "note": "<for the administrator, in Uzbek: what was agreed, e.g. ONLAYN
   KONSULTATSIYA ertaga 09:00, 50% to'landi, chekni tekshiring - ereksiya 2 yil>",
   "when": "<the agreed time or when to call>"}.
  Give it when the patient sent their number after paying, asks to be called,
  hesitates and leaves a number, or for the free prosthesis phone talk. Only with a
  real number the patient wrote.
- "handoff": true only when a person must take over now (an angry patient, a
  complaint about the clinic, a question the rules cannot answer). A lead is not a
  hand-over: false then.
"""

# "o'ttiz million so'm", "тўрт миллион беш юз минг": a sum in words, which the
# digit check cannot read -- written out by the model to get past it.
_SUM_IN_WORDS = re.compile(
    r"\b(?:bir|ikki|uch|to['‘ʻ’`]?rt|besh|olti|yetti|sakkiz|to['‘ʻ’`]?qqiz|o['‘ʻ’`]?n|yigirma|o['‘ʻ’`]?ttiz|"
    r"qirq|ellik|oltmish|yetmish|sakson|to['‘ʻ’`]?qson|yuz|бир|икки|уч|тўрт|беш|олти|етти|саккиз|тўққиз|ўн|"
    r"йигирма|ўттиз|қирқ|эллик|олтмиш|етмиш|саксон|тўқсон|юз|один|два|три|четыре|пять|шесть|семь|восемь|"
    r"девять|десять|двадцать|тридцать)\w*\s+(?:million|миллион|mln|млн|ming|минг|тысяч)",
    re.IGNORECASE,
)

_CORRECT_PRICE = (
    "Your last answer wrote a sum no service gives: {sums}. Write the answer again, same JSON, "
    "using only the prices in SERVICES, in digits (or no sum at all). Keep the cards and the lead."
)


@dataclass
class Answer:
    messages: list[str] = field(default_factory=list)
    cards: list[str] = field(default_factory=list)
    lead: dict[str, str] | None = None
    handoff: bool = False


@lru_cache
def load_profile(name: str) -> tuple[str, str]:
    """The profile's rules and services, as written."""
    folder = PROFILES / name
    return (folder / "rules.md").read_text(), (folder / "services.md").read_text()


def profile_of(settings: Mapping[str, Any] | None) -> str | None:
    """The agent profile a clinic's settings name, if it exists."""
    name = str((settings or {}).get("agent_profile") or "").strip()
    if not name or not re.fullmatch(r"[a-z0-9_-]+", name) or not (PROFILES / name).is_dir():
        return None
    return name


def sent_cards(images: Sequence[Mapping[str, Any]], own_turns: Sequence[str]) -> list[str]:
    """The cards this conversation already carried, by id."""
    return [
        str(image.get("topic"))
        for image in images
        if any(promo_images.sent_line(str(image.get("title", ""))) in turn for turn in own_turns)
    ]


def _book(slots: Sequence[datetime], now_local: datetime) -> str:
    """The free times, day by day, as the prompt shows them."""
    lines = [f"Right now: {now_local:%A %Y-%m-%d %H:%M} (Tashkent)."]
    if not slots:
        lines.append("Nothing is free in the coming week — offer to take their number for a call.")
        return "\n".join(lines)
    by_day: dict[str, list[str]] = {}
    for slot in slots:
        days = (slot.date() - now_local.date()).days
        label = {0: "TODAY (bugun)", 1: "TOMORROW (ertaga)"}.get(days, f"{slot:%A %Y-%m-%d}")
        by_day.setdefault(label, []).append(f"{slot:%H:%M}")
    if (slots[0].date() - now_local.date()).days > 0:
        lines.append("Today is full (bugungi joylar to'lgan).")
    lines += [f"- {day}: {', '.join(times[:8])}" for day, times in list(by_day.items())[:4]]
    return "\n".join(lines)


def system_prompt(
    profile: str,
    *,
    now_local: datetime,
    slots: Sequence[datetime],
    cards_sent: Sequence[str],
) -> str:
    rules, services = load_profile(profile)
    state = (
        "\n\n## THE APPOINTMENT BOOK (consultation times; only these exist)\n"
        + _book(slots, now_local)
        + "\n\n## CARDS SENT in this conversation\n"
        + (", ".join(cards_sent) if cards_sent else "none")
    )
    return f"{rules}\n\n{services}{state}\n{_CONTRACT}"


def parse(raw: str) -> Answer | None:
    """The model's JSON as an Answer; None if it is not usable."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    messages = [str(m).strip() for m in data.get("messages") or [] if str(m).strip()]
    if not messages:
        return None
    cards = [str(c).strip() for c in data.get("cards") or [] if isinstance(c, str)]
    lead = data.get("lead")
    if isinstance(lead, dict) and str(lead.get("phone") or "").strip():
        lead = {k: str(lead.get(k) or "").strip() for k in ("phone", "note", "when")}
    else:
        lead = None
    return Answer(
        messages=messages[:3], cards=cards, lead=lead, handoff=data.get("handoff") is True
    )


async def _ask(provider: LLMProvider, system: str, conversation: list[ChatMessage]) -> str:
    """The model's answer, held to JSON where the provider can."""
    as_json = getattr(provider, "generate_json", None)
    if as_json is not None:
        return str(await as_json(system, conversation))
    return await provider.generate(system, conversation)


_SCRIPT_NAMES = {
    "uz-latn": "Uzbek in LATIN letters",
    "uz-cyrl": "Uzbek in CYRILLIC letters (ўқғҳ) -- not Russian",
    "ru": "Russian",
}


# "**op:falloprotez — Penile prosthesis …: 30 000 000 so'm**": which card a
# sum belongs to, read from the services text itself.
# Signs that cannot wait: two of them, or one of the gravest. The model is
# told what to say; this is the net under it -- an emergency is never left to
# a weak answer (a local model once echoed the patient's own words back).
_ALARM = re.compile(
    r"birdan|qattiq\s*og['‘ʻ’`]?ri|kuchli\s*og['‘ʻ’`]?ri|shishib|shishdi|qon\s*ket|siya\s*olma|"
    r"siyolma|isitma|harorat|chidab\s*bo['‘ʻ’`]?lma|"
    r"бирдан|қаттиқ\s*оғри|кучли\s*оғри|шишди|шишиб|қон\s*кет|сия\s*олма|иситма|ҳарорат|"
    r"внезапн|сильн\w*\s*бол|кровотеч|не\s*могу\s*помочит|температур|опух",
    re.IGNORECASE,
)
_GRAVE = re.compile(
    r"qon\s*ket|siya\s*olma|siyolma|қон\s*кет|сия\s*олма|кровотеч|не\s*могу\s*помочит",
    re.IGNORECASE,
)
_EMERGENCY_SAID = re.compile(
    r"kechiktirib\s+bo['‘ʻ’`]?lmaydigan|кечиктириб\s+бўлмайдиган|откладывать|неотложн|bugunoq|бугуноқ",
    re.IGNORECASE,
)


# "Pulimni qaytaring", "aldadinglar", "верните деньги": a complaint about the
# clinic -- a person takes it, whatever the model thought.
_COMPLAINT_ABOUT_US = re.compile(
    r"pul\w*\s+qaytar|aldad|aldov|firibgar|shikoyat\s+qil(aman|amiz)|sudga|"
    r"пул\w*\s+қайтар|пул\w*\s+кайтар|алдад|фирибгар|"
    r"верните\s+деньг|обман|мошенни|жалоб|в\s+суд",
    re.IGNORECASE,
)


def is_emergency(message: str) -> bool:
    alarms = {m.group(0).lower() for m in _ALARM.finditer(message)}
    return bool(_GRAVE.search(message)) or len(alarms) >= 2


@lru_cache
def emergency_lines(profile: str) -> dict[str, str]:
    path = PROFILES / profile / "emergency.json"
    return json.loads(path.read_text()) if path.is_file() else {}


# A word with Latin letters slipped into Cyrillic ("Oldин").
_MIXED_WORD = re.compile(
    r"\b\w*[a-zA-Z]\w*[а-яёўқғҳА-ЯЁЎҚҒҲ]\w*\b|\b\w*[а-яёўқғҳА-ЯЁЎҚҒҲ]\w*[a-zA-Z]\w*\b"
)


def _tidy_cyrillic(text: str) -> str:
    """Mixed-alphabet words made Cyrillic, and the model's habitual slip in
    the introduction ("администраторииман", "администратори ман")."""
    text = _MIXED_WORD.sub(lambda m: to_cyrillic(m.group(0)), text)
    return re.sub(r"администратор+(?:и+\s*|\s+)?ман\b", "администраториман", text)


def _same(a: str, b: str) -> bool:
    return re.sub(r"\W+", " ", a).strip().lower() == re.sub(r"\W+", " ", b).strip().lower()


def _repeats(answer: "Answer", own: Sequence[str], asked_again: bool = False) -> bool:
    """The whole reply word for word the bot's last one: stuck in a loop --
    unless the patient asked the very same thing again."""
    if not own or asked_again:
        return False
    last = promo_images.MARKER.sub("", own[-1]).split("📎")[0]
    return _same(" ".join(answer.messages), last)


def _echoes(answer: "Answer", message: str) -> bool:
    """A message that only repeats the patient's own words back."""
    said = re.sub(r"\W+", " ", message).strip().lower()
    return len(said) > 8 and any(
        re.sub(r"\W+", " ", m).strip().lower() == said for m in answer.messages
    )


@lru_cache
def card_prices(profile: str) -> dict[int, str]:
    """Each card's own price as a number -> the card: the first sum of each
    "- **<card> — …" entry in the services text."""
    _, services = load_profile(profile)
    found: dict[int, str] = {}
    for entry in re.split(r"\n(?=- \*\*)", services):
        head = re.match(r"- \*\*(day|evening|sport|op:[a-z-]+)\b", entry)
        first = re.search(r"(\d{1,3}(?: \d{3})+) so'm", entry)
        if head and first:
            found.setdefault(int(first.group(1).replace(" ", "")), head.group(1))
    return found


def _price_problems(answer: Answer, allowed_sums: Sequence[str]) -> list[str]:
    text = " ".join(answer.messages)
    return [
        *price_guard.check(text, allowed_sums),
        *(f"{m.group(0)} (in words -- write the digits)" for m in _SUM_IN_WORDS.finditer(text)),
    ]


async def decide(
    provider: LLMProvider,
    *,
    profile: str,
    user_message: str,
    history: Sequence[ChatMessage] | None,
    now_local: datetime,
    slots: Sequence[datetime],
    images: Sequence[Mapping[str, Any]],
    script: str | None = None,
) -> Answer | None:
    """Ask the model, check its answer, and correct the price once."""
    own = [str(t["content"]) for t in history or [] if t.get("role") == "assistant"]
    cards_sent = sent_cards(images, own)
    system = system_prompt(profile, now_local=now_local, slots=slots, cards_sent=cards_sent)
    if script in _SCRIPT_NAMES:
        system += f"\n## THE PATIENT WRITES IN: {_SCRIPT_NAMES[script]} -- answer in it.\n"
    conversation: list[ChatMessage] = [
        *(history or []),
        ChatMessage(role="user", content=user_message),
    ]
    patient_before = [str(t["content"]) for t in history or [] if t.get("role") == "user"]
    asked_again = bool(patient_before) and _same(patient_before[-1], user_message)
    raw = await _ask(provider, system, conversation)
    answer = parse(raw)
    if answer is not None and (_echoes(answer, user_message) or _repeats(answer, own, asked_again)):
        logger.warning("agent_echoed_or_repeated")
        raw = await _ask(
            provider,
            system
            + "\n\nYour draft repeated the patient's words or your own last reply word for word."
            " Answer what the patient just wrote and move the conversation forward.",
            conversation,
        )
        answer = parse(raw)
        if answer is not None and (
            _echoes(answer, user_message) or _repeats(answer, own, asked_again)
        ):
            # Stuck twice: not sent -- the caller answers this one another way.
            logger.warning("agent_stuck")
            return None
    if answer is None:
        logger.warning("agent_unparsed", extra={"raw": raw[:300]})
        raw = await _ask(provider, system + "\n\nAnswer with the JSON object only.", conversation)
        answer = parse(raw)
        if answer is None:
            return None

    # The patient's alphabet: a reply in the other one is written again, once.
    wrote = language.clear_script(" ".join(answer.messages))
    if (
        script in _SCRIPT_NAMES
        and wrote
        and wrote != script
        and {script, wrote} != {"uz-cyrl", "ru"}
    ):
        logger.warning("agent_wrong_script", extra={"want": script, "got": wrote})
        again = parse(
            await _ask(
                provider,
                system + f"\n\nWrite the whole answer in {_SCRIPT_NAMES[script]}.",
                conversation,
            )
        )
        if again is not None and language.clear_script(" ".join(again.messages)) in (None, script):
            answer = again
    if script == "ru":
        answer.messages = [re.sub(r"(?i)\bso['‘ʻ’`]m\b|\bсўм\b", "сум", m) for m in answer.messages]
    answer.messages = [_tidy_cyrillic(m) for m in answer.messages]

    rules, services = load_profile(profile)
    allowed = [rules, services]
    wrong = _price_problems(answer, allowed)
    if wrong:
        logger.warning("agent_price_corrected", extra={"sums": wrong})
        retry = parse(
            await _ask(
                provider,
                system,
                [
                    *conversation,
                    ChatMessage(role="assistant", content=raw),
                    ChatMessage(role="user", content=_CORRECT_PRICE.format(sums=", ".join(wrong))),
                ],
            )
        )
        if retry is not None and not _price_problems(retry, allowed):
            answer = retry
        else:
            # Still wrong: the messages carrying a made-up sum are not sent.
            answer.messages = [
                m for m in answer.messages if not _price_problems(Answer(messages=[m]), allowed)
            ] or answer.messages[:0]
            if not answer.messages:
                return None

    # A price said goes with its card: "30 000 000 so'm" in words, and the
    # prosthesis card under it -- the model forgot it a time in twenty.
    priced = card_prices(profile)
    for _, value in price_guard.amounts(" ".join(answer.messages)):
        card = priced.get(int(value))
        if card is not None and card not in answer.cards:
            answer.cards.append(card)
    if is_emergency(user_message) and not any(_EMERGENCY_SAID.search(m) for m in answer.messages):
        line = emergency_lines(profile).get(script or "uz-latn") or emergency_lines(profile).get(
            "uz-latn"
        )
        if line:
            logger.warning("agent_emergency_net")
            answer.messages = [line]
            answer.cards = []
            answer.handoff = True
    # An operation's card goes with a price: asked by the patient, or said in
    # the reply. "Varikotsele operatsiyasini qilasizlarmi?" gets no card.
    patient = [str(t["content"]) for t in history or [] if t.get("role") == "user"]
    price_asked = price_guard.asks_price(user_message, patient[-1:]) or bool(
        price_guard.amounts(" ".join(answer.messages))
    )
    if not price_asked:
        answer.cards = [c for c in answer.cards if not c.startswith("op:")]
    # A lead only with a whole number: "+998 90 12" is asked again, not sent on.
    if answer.lead is not None:
        number = phone_check.read(answer.lead.get("phone", ""))
        if number is None or number.kind not in (phone_check.UZ, phone_check.FOREIGN):
            logger.warning("agent_lead_bad_number")
            answer.lead = None
    if _COMPLAINT_ABOUT_US.search(user_message):
        answer.handoff = True
    known = {str(i.get("topic")) for i in images}
    answer.cards = [c for c in dict.fromkeys(answer.cards) if c in known and c not in cards_sent][
        :2
    ]
    return answer


def render(
    answer: Answer,
    *,
    images: Sequence[Mapping[str, Any]],
    clinic_phone_numbers: str | None,
    patient_texts: Sequence[str] = (),
) -> str:
    """The answer as the rest of the system reads a reply: bubbles split by
    |||, the cards as [[IMAGE]] markers, the lead as [[CALLBACK]], a
    hand-over as [[DOCTOR]]."""
    # The patient's own number may be said back to them ("+998 93 ... ga
    # qo'ng'iroq qilamiz"); any other number that is not the clinic's goes.
    theirs = [n for t in patient_texts for n in phone_guard.allowed_numbers(t)]
    allowed = ", ".join(filter(None, [clinic_phone_numbers or "", *theirs]))
    text = f" {bubbles.MARKER} ".join(phone_guard.strip(m, allowed) for m in answer.messages)
    by_topic = {str(i.get("topic")): i for i in images}
    for topic in answer.cards:
        image = by_topic[topic]
        text += f" [[IMAGE:{image['url']}|{image.get('title', '')}]]"
    if answer.lead:
        lead = answer.lead
        note = (lead.get("note") or "AGENT").replace("|", "/").replace("]", ")")[:280]
        when = (lead.get("when") or "").replace("|", "/").replace("]", ")")[:90]
        text += (
            f" [[CALLBACK:{lead['phone']}|{note}|{when}]]"
            if when
            else (f" [[CALLBACK:{lead['phone']}|{note}]]")
        )
    if answer.handoff:
        text += " [[DOCTOR]]"
    return text.strip()
