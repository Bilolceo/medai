"""A conversation the clinic wants run word for word: Dr Sultonbek's
low-libido consultation.

The doctor dictated the administrator's lines and their order: name, place,
age, the possible causes, an online video consultation with the doctor,
today is full and what time tomorrow, then -- once a time is agreed -- the
place is held, the consultation is paid, half of it now, and the card. Given
to the model as instructions, those lines came back reworded, reordered or
skipped (the price before the name, "today is full" dropped half the time).
Here they are said as written, in the patient's own alphabet, one step at a
time.

Which step comes next is read from the transcript itself -- the lines already
sent -- so nothing new is stored and an operator's own message in between
changes nothing. The model still answers what the script does not: a
question the patient asks in the middle is answered by it, briefly, and the
step's own question follows as a message of its own. Once the place and the
card have been given, the script is done and the model carries on with the
payment as before.

A clinic without a ``scripted_flow`` setting is untouched.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from app.services import bubbles

# A complaint about desire, in any of the three alphabets, with a word that
# says it is low or gone -- "qabulga yozilish xohishim bor" is not one.
_DESIRE = re.compile(
    r"xohish|xoxish|hohish|istak|libido|mayl|хоҳиш|хохиш|ҳоҳиш|истак|либидо|майл|желани|влечени",
    re.IGNORECASE,
)
_LOW = re.compile(
    r"pas|past|yo['‘ʻ’`]?q|yoq|kam|ket|so['‘ʻ’`]?n|qolma|jinsiy|"
    r"пас|йўқ|йук|кам|кет|сўн|жинсий|нет|пропал|сниж|сниз|упал|пропад|мало",
    re.IGNORECASE,
)
# Any other complaint makes it not "only desire", which is the doctor's
# condition for the online route; the ordinary sequence answers it instead.
_OTHER_COMPLAINT = re.compile(
    r"erek|erect|potens|tez\s*bo['‘ʻ’`]?shan|og['‘ʻ’`]?ri|siydik|siyish|siya\s*olma|\bqon\b|"
    r"moyak|tuxum|prostat|varikot|bepusht|farzand|buyrak|achish|shish|"
    r"эрек|потенц|тез\s*бўшан|оғри|сийдик|сийиш|\bқон\b|мояк|простат|варикоц|фарзанд|буйрак|"
    r"\bболь|болит|боли\b|моч|кров|яичк|бесплод|ребён|ребен|эякул|семяизверж",
    re.IGNORECASE,
)
_GREETING = re.compile(r"assal|salom|ассал|салом|здравств|привет|добр", re.IGNORECASE)
_QUESTION_WORDS = re.compile(
    r"\b(nima|nega|nimaga|qancha|qanday|qayer|qachon|kim|qaysi|bo['‘ʻ’`]?ladimi|mumkinmi|bormi|"
    r"нима|нега|қанча|қандай|қаер|қачон|ким|қайси|бўладими|мумкинми|борми|"
    r"что|почему|зачем|сколько|как|где|когда|кто|какой|можно|ли)\b|\w+(mi|ми)\b",
    re.IGNORECASE,
)
_ASKS_PRICE = re.compile(
    r"narx|нарх|qancha|қанча|pul|пул|to['‘ʻ’`]?lov|тўлов|сколько|стоим|стоит|цен|оплат",
    re.IGNORECASE,
)
_CLOCK = re.compile(r"\b(\d{1,2})\s*[:.]\s*(\d{2})\b")
_HOUR = re.compile(
    r"(?:soat|соат|в|к|at)\s*(\d{1,2})\b|\b(\d{1,2})\s*(?:da|ga|lar|ларда|да|га|час|ч\b)",
    re.IGNORECASE,
)
_NAME_WORD = re.compile(r"^[A-Za-zА-Яа-яЁёЎўҚқҒғҲҳ'‘ʻ’`-]{2,20}$")
_INTRO = re.compile(r"administrator|администратор", re.IGNORECASE)

# The steps, in order, by the line that marks each as said.
_STEPS = ("ask_name", "ask_place", "ask_age", "causes", "slot_ok")
_PENDING_QUESTION = {
    "ask_name": "ask_name",
    "ask_place": "ask_place",
    "ask_age": "ask_age",
    "causes": "ask_time",
}


@dataclass(frozen=True)
class Plan:
    """What the scripted flow does with this message.

    ``reply`` is the whole answer, sent as it is. ``then`` is the step's own
    question, sent after the model has answered something off the script --
    in which case ``instruction`` is what the model is told about that
    answer.
    """

    reply: str | None = None
    then: str | None = None
    instruction: str | None = None


INSTRUCTION = (
    "THIS REPLY: answer only the patient's question in this message, in one or two short "
    "sentences. Do not greet, do not introduce yourself, do not ask anything and do not offer "
    "anything -- the next question is sent separately after your answer."
)


def _norm(text: str) -> str:
    return re.sub(r"[‘ʻ’`]", "'", text).lower()


def _said(flow: Mapping[str, Any], key: str, own: Sequence[str]) -> int:
    """How many of the assistant's turns carry this step's line, in any alphabet."""
    marks = []
    for texts in flow["texts"].values():
        # The longest stretch of the line with no placeholder in it: the
        # day and the hour change, the words around them do not.
        pieces = re.split(r"\{[^}]*\}", texts.get(key, ""))
        head = _norm(max(pieces, key=len)).strip(" ,.")[:28]
        if len(head) >= 8:
            marks.append(head)
    return sum(1 for turn in own if any(mark in _norm(turn) for mark in marks))


def _about_desire(text: str) -> bool:
    return bool(
        _DESIRE.search(text) and (_LOW.search(text) or re.search("libido|либидо", text, re.I))
    )


def _is_question(text: str) -> bool:
    return "?" in text or bool(_QUESTION_WORDS.search(text))


def _day(flow: Mapping[str, Any], texts: Mapping[str, str], now: datetime) -> str:
    """Tomorrow, or the next working day after it when tomorrow is off."""
    days = set(flow.get("work_days") or range(1, 8))
    day = now.date() + timedelta(days=1)
    if day.isoweekday() in days:
        return texts["tomorrow"]
    return texts["monday"] if day.isoweekday() == 7 and 1 in days else texts["tomorrow"]


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def parse_time(message: str) -> time | None:
    """The hour the patient named: "10:00", "soat 2 da" (14:00), "в 12"."""
    match = _CLOCK.search(message)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
    else:
        match = _HOUR.search(message)
        if not match:
            return None
        hour, minute = int(match.group(1) or match.group(2)), 0
    if 1 <= hour <= 7:
        # Nobody books a consultation for two in the morning: "soat 2" is 14:00.
        hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return time(hour, minute)


def _within(flow: Mapping[str, Any], at: time) -> bool:
    start = time.fromisoformat(flow.get("online_start") or "09:00")
    end = time.fromisoformat(flow.get("online_end") or "16:00")
    return start <= at < end


def plan(
    flow: Mapping[str, Any] | None,
    *,
    script: str,
    user_message: str,
    history: Sequence[Mapping[str, Any]] | None,
    now: datetime,
) -> Plan | None:
    """The scripted answer to this message, or None to let the model answer."""
    if not flow:
        return None
    texts = flow["texts"].get(script) or flow["texts"]["uz-latn"]
    turns = list(history or [])
    own = [turn["content"] for turn in turns if turn.get("role") == "assistant"]
    patient = [turn["content"] for turn in turns if turn.get("role") == "user"]
    # The steps count from the patient's first word about desire: an old
    # "Ismingiz nima?" from weeks ago in the same chat is not this flow's.
    first = next(
        (
            i
            for i, turn in enumerate(turns)
            if turn.get("role") == "user" and _about_desire(turn["content"])
        ),
        len(turns),
    )
    since = [turn["content"] for turn in turns[first:] if turn.get("role") == "assistant"]
    said = {key: _said(flow, key, since) for key in (*_STEPS, "paid_info", "price_early")}
    started = said["causes"] > 0 or any(_about_desire(text) for text in patient)

    if said["slot_ok"]:
        # Place held and card given: the payment is the model's to follow.
        return None
    if not started and not _about_desire(user_message):
        return None
    if any(_OTHER_COMPLAINT.search(text) for text in (*patient, user_message)):
        return None

    day = _day(flow, texts, now)

    def fill(key: str, **values: str) -> str:
        return str(texts[key]).format(day=day, Day=_cap(day), **values)

    # Where the conversation stands: the last step whose line has been said.
    done = [key for key in _STEPS if said[key]]
    pending = done[-1] if done else None
    message = user_message.strip()

    # First turn of the flow: greet and introduce if nobody has yet, then
    # ask the name.
    if pending is None:
        opening: list[str] = []
        if not own:
            opening.append(texts["greet_back"] if _GREETING.search(message) else texts["greet"])
        if not any(_INTRO.search(turn) for turn in own):
            opening.append(texts["intro"])
        parts = [" ".join(opening)] if opening else []
        if _ASKS_PRICE.search(message):
            parts.append(texts["price_early"])
        parts.append(texts["ask_name"])
        return Plan(reply=bubbles.MARKER.join(f" {p} " for p in parts).strip())

    question = _is_question(message)
    asked_twice = said[pending] >= 2

    # A price asked for at any step is answered from the script, and the
    # step's question asked again.
    if _ASKS_PRICE.search(message) and question and pending != "slot_ok":
        again = fill(_PENDING_QUESTION[pending])
        return Plan(reply=f"{texts['price_early']} {bubbles.MARKER} {again}")

    if pending == "causes":
        at = parse_time(message)
        if at is None:
            if question:
                return Plan(then=fill("ask_time"), instruction=INSTRUCTION)
            return Plan(reply=fill("time_example"))
        if not _within(flow, at):
            return Plan(reply=fill("hours"))
        clock = f"{at:%H:%M}"
        parts = [fill("slot_ok", time=clock)]
        if not said["price_early"] and not said["paid_info"]:
            parts.append(texts["paid_info"])
        parts += [fill("prepay", time=clock), texts["card"]]
        return Plan(reply=f" {bubbles.MARKER} ".join(parts))

    # A question in place of the answer: the model answers it, and the same
    # step's question follows -- once; a second time, the step is let go.
    if question and not asked_twice:
        return Plan(then=fill(_PENDING_QUESTION[pending]), instruction=INSTRUCTION)

    if pending == "ask_name":
        word = message.split()[0].strip(".,!") if message else ""
        if len(message.split()) <= 2 and _NAME_WORD.match(word):
            return Plan(reply=fill("ask_place_named", name=_cap(word)))
        return Plan(reply=fill("ask_place"))
    if pending == "ask_place":
        return Plan(reply=fill("ask_age"))
    # pending == "ask_age": the causes, the online route, today and tomorrow.
    return Plan(
        reply=f" {bubbles.MARKER} ".join((texts["causes"], texts["online"], fill("today_full")))
    )


def drop_questions(reply: str) -> str:
    """The model's off-script answer without any question of its own: the
    script's question follows it, and two questions in a row read as a form."""
    sentences = re.split(r"(?<=[.!?])\s+", reply.strip())
    kept = [s for s in sentences if not s.rstrip().endswith("?")]
    return " ".join(kept).strip() or reply.strip()
