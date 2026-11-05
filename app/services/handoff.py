"""Conversations a person has to answer, kept at the top of the dashboard.

Raised when the assistant hands a patient to the doctor -- it marks the reply
with [[DOCTOR]], or says as much in words the pattern below knows -- and when
a patient writes into a conversation staff have taken over. Cleared when a
person answers: from the dashboard, from the Instagram app itself, or by
marking it done in the dashboard after answering some other way.
"""

import re
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation

# Tolerant of what a model actually writes -- "[[DOCTOR]", "[[ doctor: ... ]]"
# -- because any variant this misses is text the patient would see.
DOCTOR_MARKER = re.compile(r"\s*\[\[\s*DOCTOR\b[^\[\]]*\]?\]?\s*", re.IGNORECASE)

# Every apostrophe Uzbek Latin is typed with: ' ʻ ʼ ‘ ’ `.
_A = "['ʻʼ‘’`]?"

# A reply that tells the patient the doctor will answer them, without the
# marker. A handover the model forgot to mark is a patient who waits for an
# answer nobody knows is owed.
# The phrasings below are the ones real replies used: "doktor bilan aniqlab
# olaman", "sizni doktorga bildiraman", "klinika botiga yubordim", "рақамни
# докторга юбордим" -- each a promise that somebody would come back, on a
# conversation nobody had been told about.
_HANDOVER_WORDS = re.compile(
    rf"(doktor|shifokor)(ning)?\s+o{_A}z(i|lari)\s+(sizga\s+)?(javob|yoz|qo{_A}ng{_A}iroq)"
    rf"|(doktor|shifokor)(ga|imizga)\s+(yetkaz|bildir|yubor|xabar\s+ber)"
    rf"|(doktor|shifokor)\s+bilan\s+aniq(lab|lashtir)"
    rf"|(klinika\s+)?bot(i|ga|iga)\s+yubor"
    r"|(доктор|шифокор)(нинг)?\s+ўз(и|лари)\s+(сизга\s+)?(жавоб|ёз|қўнғироқ)"
    r"|(доктор|шифокор)(га|имизга)\s+(етказ|билдир|юбор|хабар\s+бер)"
    r"|(доктор|шифокор)\s+билан\s+ани(қ|к)(лаб|лаштир)"
    r"|(клиника\s+)?бот(и|га|ига)\s+юбор"
    r"|врач\s+(сам|лично)\s+(вам\s+)?(ответ|напиш|позвон)"
    r"|переда(м|дим|л[аи]?)\s+(ваш\w*\s+)?(вопрос\s+|номер\s+|контакт\w*\s+)?врачу"
    r"|уточн(ю|им)\s+у\s+врача|сообщ(у|им)\s+врачу"
    # Promising to come back. The assistant only speaks when the patient
    # writes, so "Aniqlab, hozir yozaman" -- which the persona itself once
    # told it to say -- left a patient waiting on nobody. A person keeps it.
    r"|hozir\s+(aniqlab\s+)?(yozaman|yozib\s+beraman|aytaman|aytib\s+beraman"
    r"|xabar\s+beraman|bilib\s+(beraman|aytaman))"
    r"|aniqlab\s+(beraman|yozaman|aytaman|bilib)"
    rf"|bir\s+daqiqa|ko{_A}rib\s+(chiqib\s+)?(yozaman|aytaman|beramiz)"
    r"|ҳозир\s+(аниқлаб\s+)?(ёзаман|айтаман|хабар\s+бераман)|аниқлаб\s+(бераман|ёзаман|айтаман)"
    r"|бир\s+дақиқа"
    r"|сейчас\s+(уточн|узна|напиш|отвеч)|минутк|одну\s+минуту|уточню\s+и\s+(напиш|отве)"
    # Handing over to the administrator -- what the persona tells it to say to
    # "odam bilan gaplashmoqchiman". Unmarked, nobody was told and the
    # patient waited on a promise nobody knew had been made.
    r"|(admin(istrator)?|xodim)(imiz|lar(imiz)?)?\s+(ham\s+)?(shu\s+yerda\s+|hozir\s+)?"
    r"(sizga\s+|siz\s+bilan\s+)?"
    rf"(javob\s+ber|yoz|bog{_A}lan|qo{_A}ng{_A}iroq)"
    r"|admin(istrator)?(imiz)?ga\s+ula"
    r"|(админ(истратор)?|ходим)(имиз|лар(имиз)?)?\s+(ҳам\s+)?(шу\s+ерда\s+|ҳозир\s+)?"
    r"(сизга\s+|сиз\s+билан\s+)?"
    r"(жавоб\s+бер|ёз|боғлан|қўнғироқ)"
    r"|(администратор|сотрудник)\w*\s+(вам\s+|с\s+вами\s+)?(здесь\s+|сейчас\s+)?"
    r"(ответ|напиш|свяж|позвон)"
    r"|соедин\w*\s+(вас\s+)?с\s+администратор",
    re.IGNORECASE,
)


def extract(reply: str) -> tuple[str, bool]:
    """The reply without the marker, and whether it hands the patient over."""
    marked = bool(DOCTOR_MARKER.search(reply))
    clean = DOCTOR_MARKER.sub(" ", reply).strip() if marked else reply
    return clean, marked or bool(_HANDOVER_WORDS.search(clean))


def same_message(a: str | None, b: str | None) -> bool:
    """Whether two message texts are the same message, whitespace aside."""
    return " ".join((a or "").split()) == " ".join((b or "").split())


def one_of_ours(echo: str | None, recorded: str | None) -> bool:
    """Whether an echoed text is a recorded reply or one of the messages it
    went out as. A reply sent as several bubbles (app.services.bubbles) is
    kept as one row with a blank line between them, so each bubble echoes
    back as a piece of it, not the whole."""
    if same_message(echo, recorded):
        return True
    return any(same_message(echo, part) for part in (recorded or "").split("\n\n") if part.strip())


async def flag(session: AsyncSession, conversation_id: uuid.UUID) -> None:
    """Mark this conversation as waiting on a person, keeping the earliest time."""
    conversation = await session.get(Conversation, conversation_id)
    if conversation is not None and conversation.needs_doctor_since is None:
        conversation.needs_doctor_since = datetime.now(UTC)
        await session.flush()


async def clear(session: AsyncSession, conversation_id: uuid.UUID) -> None:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is not None and conversation.needs_doctor_since is not None:
        conversation.needs_doctor_since = None
        await session.flush()
