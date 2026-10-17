"""Keep the offer to book to once a conversation.

The doctor's rule (2026-10-16): after answering, the assistant may offer an
appointment -- or a call back, with the patient's name, number and a good
time -- once. Saying it again in every reply ("keling, keling") reads as
begging, and a patient who has said no is not asked again. The model,
told so, still repeated it; so it is checked here, by pattern, the way
app.services.price_guard checks a sum.

The first offer in a conversation goes out. A later one -- or any after the
patient declined -- has its sentence removed, never the whole reply: a cut
that would leave only the greeting sends the reply as written.
"""

import re
from collections.abc import Sequence

# The patient has said they want to come, be booked, or know when they can.
_WANTS_TO_BOOK = re.compile(
    r"yozil|yozib\s+qo['‘ʻ’`]?y|yozdir|ёзил|ёзиб\s+қўй|ёздир|запиш|записа|запись"
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
    r"|(?:raqam|telefon)\w*ni\s+qoldir\w*|(?:рақам|телефон)\w*ни\s+қолдир\w*"
    # The plain style says "yozing" where it said "qoldiring", and asks again
    # as "telefon raqamingizni yozasizmi?" / "raqamingiz yo'qmi?" (2026-10-16).
    r"|(?:ism\w*\s+(?:va|,)\s+)(?:telefon|raqam)\w*(?:\s+raqam\w*)?(?:ni)?\s+yoz\w*"
    r"|(?:исм\w*\s+(?:ва|,)\s+)(?:телефон|рақам)\w*(?:\s+рақам\w*)?(?:ни)?\s+ёз\w*"
    r"|(?:telefon|raqam)\w*\s+(?:\w+\s+)?(?:yozasizmi|yo['‘ʻ’`]?qmi)"
    r"|(?:оставьте|напишите)[^.!?]*(?:номер|телефон)"
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


# The patient has turned the offer down.
_DECLINED = re.compile(
    r"\byo['‘ʻ’`]?q\b|kerak\s+(?:emas|mas)|kirak\s+mas|hozircha|kelolmayman|kela\s+olmayman"
    r"|йўқ|керак\s+(?:эмас|мас)|ҳозирча|келолмайман|\bнет\b|не\s+надо|не\s+нужно|пока\s+нет",
    re.IGNORECASE,
)


def offered_before(assistant_turns: Sequence[str]) -> bool:
    """Whether an earlier reply in this conversation already made the offer."""
    return any(_OFFER.search(turn) for turn in assistant_turns)


def declined(patient_turns: Sequence[str]) -> bool:
    """Whether the patient has said no to coming or being called."""
    return any(_DECLINED.search(turn) for turn in patient_turns)


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


# The administrator introducing themselves -- "Men doktor Temurning
# administratoriman" -- which the persona allows once a conversation.
_INTRO = re.compile(
    r"(?:men|мен)\s+(?:doktor|доктор)\s+\w+\s+(?:administrator|администратор)\w*"
    r"|я\s+администратор\s+доктора\s+\w+",
    re.IGNORECASE,
)


_GREETING_LEAD = re.compile(
    r"^\W*(?:va\s+alaykum\s+assalom|assalomu\s+alaykum|salom|ва\s+алайкум\s+ассалом"
    r"|ассалому\s+алайкум|салом|здравствуйте|добрый\s+\w+)\W*",
    re.IGNORECASE,
)


def introduced_before(assistant_turns: Sequence[str]) -> bool:
    return any(_INTRO.search(turn) for turn in assistant_turns)


def drop_intro(reply: str) -> str:
    """The reply without the self-introduction, for a conversation that has
    already had it. A sentence that was only the introduction (and perhaps a
    greeting) goes; one with more in it keeps the rest."""
    if not _INTRO.search(reply):
        return reply
    kept: list[str] = []
    for sentence in _SENTENCE.findall(reply):
        if not _INTRO.search(sentence):
            kept.append(sentence)
            continue
        rest = _GREETING_LEAD.sub("", _INTRO.sub("", sentence)).strip(" ,.;:—–-\n")
        if len(re.findall(r"\w+", rest)) >= 2:
            kept.append(" " + rest[0].upper() + rest[1:] + ("" if rest[-1] in ".!?" else "."))
    text = "".join(kept).strip()
    return text or reply


# A message that only says hello -- nothing has been asked yet, so there is
# nothing to have answered before the one offer is made.
_ONLY_HELLO = re.compile(
    r"^\W*(?:assalomu?\s*alaykum|assalom\s*aleykum|salom(?:\s*alaykum)?|ассалому?\s*алайкум|салом"
    r"|здравствуйте|привет|добрый\s+\w+|hello|hi)\W*(?:aka|opa|doktor|доктор|ака)?\W*"
    r"(?:yaxshimisiz|qalaysiz|яхшимисиз|қалайсиз)?\W*$",
    re.IGNORECASE,
)


def only_hello(message: str) -> bool:
    return bool(_ONLY_HELLO.match(message.strip()))
