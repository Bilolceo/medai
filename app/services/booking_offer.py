"""Take an unasked "shall I book you?" off the end of a reply.

The clinic's rule is that the assistant offers an appointment only once the
patient has said they want one. The persona says so, the rules say so, and
the medical contract says so -- and asked "Prostatitga qaysi dori?", the
model still closed five refusals in eight with "Qabulga yozilmoqchimisiz?".
So it is checked here, by pattern, the way app.services.price_guard checks a
sum: free when the patient asked to come or the reply offers nothing, and a
sentence removed -- never a whole reply replaced -- when it does.

Only a sentence that *asks* is removed ("…yozilmoqchimisiz?", "…yozib
qo'yaymi?", "Записать вас?"). A statement such as "doktor ko'rikda aytadi"
is the answer itself and stays.
"""

import re
from collections.abc import Sequence

# The patient has said they want to come, be booked, or know when they can.
_WANTS_TO_BOOK = re.compile(
    r"yozil|yozib|yozdir|ёзил|ёзиб|ёздир|запиш|записа|запись"
    r"|qachon\s+(?:kel|bor)|қачон\s+(?:кел|бор)|когда\s+(?:можно\s+)?(?:прий|подой|зайт)"
    r"|bo['‘ʻ’`]?sh\s+vaqt|бўш\s+вақт|свободн"
    r"|qabulga\s+(?:kel|bor)|қабулга\s+(?:кел|бор)|kelmoqchi|келмоқчи|borsam|борсам"
    r"|приём\s+на|прием\s+на",
    re.IGNORECASE,
)

# A sentence that asks the patient whether to book them or take their number.
_OFFER = re.compile(
    r"yozilmoqchimisiz|yozilasizmi|yozib\s+qo['‘ʻ’`]?yaymi|yozib\s+qo['‘ʻ’`]?yishimni"
    r"|ёзилмоқчимисиз|ёзиласизми|ёзиб\s+қўяйми"
    r"|записать\s+вас|хотите\s+записаться|записаться\s+на\s+при[её]м\s*\?"
    r"|(?:qabul|ko['‘ʻ’`]?rik)\w*\s+(?:\w+\s+){0,3}(?:yozil|yozib)\w*[^.!?]*\?"
    r"|raqamingizni\s+qoldir\w*[^.!?]*\?"
    r"|yozilmoqchi\s+bo['‘ʻ’`]?l(?:sangiz|angiz)|ёзилмоқчи\s+бўлсангиз|если\s+хотите\s+записаться"
    r"|qachon\s+kelishingiz\s+qulay[^.!?]*\?|qaysi\s+kun(?:i)?\s+(?:kelish\w*\s+)?qulay[^.!?]*\?",
    re.IGNORECASE,
)

_SENTENCE = re.compile(r"[^.!?]*[.!?]+|[^.!?]+$")


def wants_to_book(message: str, history: Sequence[str] = ()) -> bool:
    """Whether the patient, now or earlier, has asked to come or be booked."""
    return any(_WANTS_TO_BOOK.search(text) for text in (*history, message))


def strip(reply: str) -> str:
    """The reply without any sentence that asks whether to book them.

    Markers ([[DOCTOR]], [[CALLBACK:...]]) and everything else are kept. A
    reply that would be left empty is returned unchanged -- an offer is a
    smaller fault than silence.
    """
    markers = re.findall(r"\[\[[^\]]*\]\]", reply)
    body = re.sub(r"\s*\[\[[^\]]*\]\]", "", reply)
    sentences = [s for s in _SENTENCE.findall(body) if s.strip()]
    kept = [s for s in sentences if not _OFFER.search(s)]
    if len(kept) == len(sentences) or not kept:
        return reply
    text = "".join(kept).strip()
    return " ".join([text, *markers]) if markers else text
