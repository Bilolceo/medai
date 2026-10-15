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
    r"|приём\s+на|прием\s+на"
    r"|buyurtma|буюртма|zakaz|заказ|olib\s+bor|олиб\s+бор|kelsak|келсак|boraylik|борайлик"
    r"|ko['‘ʻ’`]?rsatsam|кўрсатсам|ko['‘ʻ’`]?rikka\s+(?:kel|bor)|кўрикка\s+(?:кел|бор)",
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
    r"|yozaymi|ёзайми|yozmoqchimisiz|ёзмоқчимисиз"
    r"|(?:qabul|ko['‘ʻ’`]?rik|konsultatsiya|қабул|кўрик|консультац)\w*[^.!?]*(?:xohlaysizmi|хоҳлайсизми)[^.!?]*\?"
    r"|qo['‘ʻ’`]?ng['‘ʻ’`]?iroq\s+qilish\w*\s+xohlaysizmi|kela\s+olasizmi|кела\s+оласизми"
    r"|кўриб\s+(?:чиқиш|кўриш)ни\s+хоҳлайсизми"
    r"|qachon\s+kelishingiz\s+qulay[^.!?]*\?|qaysi\s+kun(?:i)?\s+(?:kelish\w*\s+)?qulay[^.!?]*\?",
    re.IGNORECASE,
)

_SENTENCE = re.compile(r"[^.!?]*[.!?]+|[^.!?]+$")

# What a reply opens with before it says anything: a greeting, the
# administrator introducing themselves. A cut that leaves only these has
# taken the answer with it.
_OPENING = re.compile(
    r"^\s*(?:va\s+alaykum|assalom|ва\s+алайкум|ассалом|здравствуйте|добрый|salom|салом"
    r"|men\s+doktor|мен\s+доктор|я\s+администратор)",
    re.IGNORECASE,
)


def _without_offer(sentence: str) -> str:
    """The sentence, its offer clause, or nothing.

    "Doktor ko'rib chiqadi — ko'rib chiqishni xohlaysizmi?" keeps what comes
    before the dash; a sentence that is only the offer is dropped whole.
    """
    if not _OFFER.search(sentence):
        return sentence
    head = re.split(r"\s+[—–-]\s+|;\s+", sentence)[0]
    if head != sentence and not _OFFER.search(head + "?") and len(head.split()) >= 3:
        return head.rstrip(" ,") + "."
    return ""


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
    kept = [k for k in (_without_offer(s) for s in sentences) if k]
    if kept == sentences or all(_OPENING.search(s) for s in kept):
        return reply
    text = "".join(kept).strip()
    return " ".join([text, *markers]) if markers else text
