"""The clinic's price cards, sent as pictures when a patient asks the price.

Dr Sultonbek gave three cards: the daytime urology and andrology
consultation (330 000 so'm), the daytime anabolic-steroid and hormone one
(500 000 so'm), and the evening online one (990 000 so'm, 19:00-22:00).
Which one a patient gets is decided here, by pattern, rather than left to
the model -- the same way prices themselves are policed in
app.services.price_guard:

* evening -- the patient asked about the evening or after work;
* steroid -- the conversation is about steroids, a course, hormones, sport;
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
_STEROID = re.compile(
    r"steroid|anabol|kurs\b|kursdan|testosteron|gormon|sport|trenbolon|nandrolon|sustanon|"
    r"стероид|анабол|курс\b|тестостерон|гормон|спорт|тренболон|нандролон|сустанон",
    re.IGNORECASE,
)
_UROLOGY = re.compile(
    r"erek|potens|xohish|istak|libido|prostat|varikot|bepusht|farzand|siydik|moyak|tez\s*bo['‘ʻ’`]?shan|"
    r"erkak|urolog|androlog|"
    r"эрек|потенц|хоҳиш|хохиш|истак|либидо|простат|варикоц|фарзанд|сийдик|мояк|"
    r"влечен|желани|бесплод|моч|яичк|уролог|андролог",
    re.IGNORECASE,
)
# The reply saying the card is there: "narxlari rasmda", "на картинке".
_POINTS_AT_PICTURE = re.compile(r"rasmda|расмда|на\s*картинке|на\s*изображени", re.IGNORECASE)
_ASKED_TOPIC = re.compile(r"qaysi\s+masala|қайси\s+масала|по\s+какому\s+вопросу", re.IGNORECASE)


def sent_line(title: str) -> str:
    return f"📎 {title}"


def choose(
    images: Sequence[Mapping[str, Any]] | None,
    *,
    user_message: str,
    patient_turns: Sequence[str],
    own_turns: Sequence[str],
    reply: str,
) -> Mapping[str, Any] | None:
    """The card to send with this reply, or None."""
    if not images:
        return None
    by_topic = {str(image.get("topic")): image for image in images}
    asked_topic_last = bool(own_turns) and bool(_ASKED_TOPIC.search(own_turns[-1]))
    wants = price_guard.asks_price(user_message, list(patient_turns)) or asked_topic_last
    if not wants:
        return None
    recent = [*patient_turns[-3:], user_message]
    everything = [*patient_turns, user_message]
    if any(_EVENING.search(text) for text in recent):
        topic = "evening"
    elif any(_STEROID.search(text) for text in everything):
        topic = "steroid"
    elif any(_UROLOGY.search(text) for text in everything) or _POINTS_AT_PICTURE.search(reply):
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


def attach(reply: str, image: Mapping[str, Any], script: str = "uz-latn") -> str:
    """The reply with the card under it, and with nothing in it that says
    the price comes some other way."""
    caption = _CAPTIONS.get(script, _CAPTIONS["uz-latn"])
    text = _PRICE_BY_PHONE.sub(" ", reply)
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
