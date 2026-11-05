"""The clinic's price cards, sent as pictures when a patient asks the price.

Dr Sultonbek gave two cards: the daytime urology and andrology consultation
(330 000 so'm) and the evening online one (990 000 so'm, 19:00-22:00).
Which one a patient gets is decided here, by pattern, rather than left to
the model -- the same way prices themselves are policed in
app.services.price_guard:

* evening -- the patient asked about the evening or after work;
* day -- any urology or andrology complaint, and the default when the
  reply itself points at the picture.

Each card goes once per conversation. The reply carries it as a marker,
``[[IMAGE:<url>|<title>]]``; delivery sends the text and then the picture,
and the transcript keeps "📎 <title>" in its place, which is also how the
next turn knows the card has been sent.
"""

import re
from collections.abc import Mapping, Sequence
from typing import Any

from app.services import price_guard

MARKER = re.compile(r"\[\[\s*IMAGE\s*:\s*(?P<url>[^|\]\s]+)\s*(?:\|\s*(?P<title>[^\]]*))?\]\]")

_EVENING = re.compile(
    r"kech|kechqurun|kechki|ishdan\s*keyin|tunda|\b(19|20|21)[:.]?\d{0,2}\b|"
    r"кеч|кечқурун|кечки|ишдан\s*кейин|вечер|после\s*работы|ночью",
    re.IGNORECASE,
)
_UROLOGY = re.compile(
    r"erek|potens|xohish|istak|libido|prostat|varikot|bepusht|farzand|siydik|moyak|tez\s*bo['‘ʻ’`]?shan|"
    r"erkak|urolog|androlog|gormon|testosteron|"
    r"эрек|потенц|хоҳиш|хохиш|истак|либидо|простат|варикоц|фарзанд|сийдик|мояк|"
    r"влечен|желани|бесплод|моч|яичк|уролог|андролог|гормон|тестостерон",
    re.IGNORECASE,
)
# The reply saying the card is there: "narxlari rasmda", "на картинке".
_POINTS_AT_PICTURE = re.compile(r"rasmda|расмда|на\s*картинке|на\s*изображени", re.IGNORECASE)
_ASKED_TOPIC = re.compile(r"qaysi\s+masala|қайси\s+масала|по\s+какому\s+вопросу", re.IGNORECASE)


def sent_line(title: str) -> str:
    return f"📎 {title}"


# Wanting a visit, a call or a time -- "qachon", "soat 19 da", "yozilmoqchiman".
# Asking the hours ("ish vaqtingiz?") is not asking for a visit: words of
# time alone do not count, only a visit, a booking or a call.
_WANTS_VISIT = re.compile(
    r"qabul|yozil|yozib|ko['‘ʻ’`]?rik|konsult|qo['‘ʻ’`]?ng['‘ʻ’`]?iroq|"
    r"қабул|ёзил|кўрик|консульт|қўнғироқ|"
    r"запис|приём|прием|консульт|позвон",
    re.IGNORECASE,
)
# The clinic's daytime hours: outside them the evening card is the one on offer.
DAY_START_HOUR, DAY_END_HOUR = 9, 16


def choose(
    images: Sequence[Mapping[str, Any]] | None,
    *,
    user_message: str,
    patient_turns: Sequence[str],
    own_turns: Sequence[str],
    reply: str,
    hour: int | None = None,
) -> Mapping[str, Any] | None:
    """The card to send with this reply, or None.

    ``hour`` is the clinic's local hour now: a patient wanting a visit after
    the day's hours is offered the evening one, since that is the only one
    there is then.
    """
    if not images:
        return None
    by_topic = {str(image.get("topic")): image for image in images}
    asked_topic_last = bool(own_turns) and bool(_ASKED_TOPIC.search(own_turns[-1]))
    recent = [*patient_turns[-3:], user_message]
    everything = [*patient_turns, user_message]
    # The evening card is for a patient who wants the evening -- said so, or
    # named an evening hour. Writing in the evening is not that: tomorrow's
    # daytime consultation is still there for them (live test, 2026-11-05).
    del hour
    evening_wish = bool(_EVENING.search(user_message))
    wants = (
        price_guard.asks_price(user_message, list(patient_turns))
        or asked_topic_last
        or (evening_wish and bool(_WANTS_VISIT.search(user_message)))
    )
    if not wants:
        return None
    if evening_wish or any(_EVENING.search(text) for text in recent):
        topic = "evening"
    elif any(_SPORT.search(text) for text in everything):
        # Steroids and sport are seen in the evening, online.
        topic = "evening"
    elif any(_UROLOGY.search(text) for text in everything) or _POINTS_AT_PICTURE.search(reply):
        topic = "day"
    elif asked_topic_last and price_guard.asks_price(user_message, list(patient_turns)):
        # Asked which matter, and answered with the price question again: the
        # doctor is a urologist-andrologist, so his ordinary card it is.
        topic = "day"
    else:
        return None
    image = by_topic.get(topic)
    if image is None:
        return None
    already = sent_line(str(image.get("title", "")))
    if any(already in turn for turn in own_turns):
        return None
    return image


_CAPTIONS = {
    "uz-latn": "Konsultatsiya narxi va tartibi rasmda.",
    "uz-cyrl": "Консультация нархи ва тартиби расмда.",
    "ru": "Цена и порядок консультации — на картинке.",
}
# "Narxlarni operatorimiz qo'ng'iroqda aytib beradi": said with the card
# under it, it contradicts the card.
_PRICE_BY_PHONE = re.compile(
    r"[^.!?|]*(?:narx|нарх|цен|стоим)[^.!?|]*(?:operator|оператор|telefon|телефон|qo['‘ʻ’`]?ng['‘ʻ’`]?iroq|қўнғироқ|звон)[^.!?|]*[.!?]?"
    r"|[^.!?|]*(?:operator|оператор)[^.!?|]*(?:narx|нарх|цен|стоим)[^.!?|]*[.!?]?",
    re.IGNORECASE,
)


_EVENING_CAPTIONS = {
    "uz-latn": (
        "Ish vaqtidan keyin doktor bilan faqat onlayn konsultatsiya bor, 19:00 dan 22:00 gacha"
        " -- narxi va tartibi rasmda. Qaysi vaqt sizga qulay?"
    ),
    "uz-cyrl": (
        "Иш вақтидан кейин доктор билан фақат онлайн консультация бор, 19:00 дан 22:00 гача"
        " -- нархи ва тартиби расмда. Қайси вақт сизга қулай?"
    ),
    "ru": (
        "После рабочего времени — только онлайн-консультация с доктором, с 19:00 до 22:00;"
        " цена и порядок — на картинке. Какое время вам удобно?"
    ),
}
_SENTENCE = re.compile(r"[^.!?]+[.!?]?")


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[‘ʻ’`]", "'", text).lower().split()).strip(" .!?")


def attach(
    reply: str,
    image: Mapping[str, Any],
    script: str = "uz-latn",
    own_turns: Sequence[str] = (),
) -> str:
    """The reply with the card under it, and with nothing in it that says
    the price comes some other way, that was said word for word last time,
    or -- under the evening card -- that promises a call after hours."""
    evening = image.get("topic") == "evening"
    if evening:
        text = _EVENING_CAPTIONS.get(script, _EVENING_CAPTIONS["uz-latn"])
        return f"{text} [[IMAGE:{image['url']}|{image.get('title', '')}]]"
    caption = _CAPTIONS.get(script, _CAPTIONS["uz-latn"])
    text = _PRICE_BY_PHONE.sub(" ", reply)
    if own_turns:
        said = {_norm(s) for s in _SENTENCE.findall(own_turns[-1]) if len(_norm(s)) > 12}
        text = " ".join(s.strip() for s in _SENTENCE.findall(text) if _norm(s) not in said)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    if not _POINTS_AT_PICTURE.search(text):
        lead = re.match(r"^(?:Ha|Ҳа|Да)[^.!?]*[.!?]\s*", text)
        cut = lead.end() if lead else 0
        text = f"{text[:cut]}{caption} {text[cut:]}".strip()
    return f"{text} [[IMAGE:{image['url']}|{image.get('title', '')}]]"


def split(text: str) -> tuple[str, list[str]]:
    """The text without its picture markers, and the pictures' addresses."""
    urls = [match.group("url") for match in MARKER.finditer(text)]
    return MARKER.sub("", text).strip(), urls


def for_transcript(text: str) -> str:
    """The reply as it is kept: each picture as a line naming it."""
    return MARKER.sub(
        lambda m: "\n\n" + sent_line((m.group("title") or "rasm").strip()), text
    ).strip()


_EVENING_SHORT = {
    "uz-latn": "Kechki onlayn konsultatsiya (19:00-22:00) narxi va tartibi rasmda.",
    "uz-cyrl": "Кечки онлайн консультация (19:00-22:00) нархи ва тартиби расмда.",
    "ru": "Цена и порядок вечерней онлайн-консультации (19:00-22:00) — на картинке.",
}
_SPORT = re.compile(
    r"steroid|anabol|sport|bodibild|bodybuild|\bzal\b|trenbolon|nandrolon|sustanon|kurs\b|kursdan|"
    r"стероид|анабол|спорт|бодибилд|курс\b|курсдан|тренболон|нандролон|сустанон|качалк",
    re.IGNORECASE,
)


def attach_scripted(
    reply: str, image: Mapping[str, Any], script: str = "uz-latn", *, keep_text: bool = False
) -> str:
    """A scripted reply with the card: the caption goes before the step's own
    question, in the last message, so the question stays the last word.
    ``keep_text`` keeps the step's words under the evening card too -- the
    card sent with a steroid or sport complaint, not an evening wish."""
    if image.get("topic") == "evening" and not keep_text:
        return attach(reply, image, script)
    captions = _EVENING_SHORT if image.get("topic") == "evening" else _CAPTIONS
    caption = captions.get(script, captions["uz-latn"])
    parts = reply.split("|||")
    last = parts[-1].strip()
    sentences = _SENTENCE.findall(last)
    if len(sentences) > 1:
        last = " ".join(s.strip() for s in sentences[:-1]) + f" {caption} " + sentences[-1].strip()
    else:
        last = f"{caption} {last}"
    parts[-1] = " " + last.strip()
    text = "|||".join(parts).strip()
    return f"{text} [[IMAGE:{image['url']}|{image.get('title', '')}]]"


def for_topic(
    images: Sequence[Mapping[str, Any]] | None, topic: str, *, own_turns: Sequence[str]
) -> Mapping[str, Any] | None:
    """The card for this topic, sent with the reply to the patient's complaint
    -- the doctor wants the right one in front of them from the start -- once
    a conversation."""
    image = next((i for i in images or [] if i.get("topic") == topic), None)
    if image is None:
        return None
    already = sent_line(str(image.get("title", "")))
    if any(already in turn for turn in own_turns):
        return None
    return image
