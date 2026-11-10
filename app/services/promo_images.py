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
    # "kechroq" is later in the day, not the evening.
    # An evening hour only as a time -- "19:00", "soat 20", "21 da" -- not an
    # age ("20 yoshdaman") or the Beeline code in a number ("+998 20 ...").
    r"kech(?!ro)|kechqurun|kechki|ishdan\s*keyin|tunda|"
    r"\b(?:19|20|21)[:.]\d{2}\b|(?:soat|соат|в|at)\s*(?:19|20|21)\b|"
    r"\b(?:19|20|21)\s*(?:da|ga|lar|да|га|час)\b|"
    r"кеч(?!ро)|кечқурун|кечки|ишдан\s*кейин|вечер|после\s*работы|ночью",
    re.IGNORECASE,
)
_UROLOGY = re.compile(
    r"erek|potens|xohish|istak|libido|prostat|varikot|varakat|bepusht|farzand|siydik|moyak|tez\s*bo['‘ʻ’`]?shan|"
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


def wants_evening(text: str) -> bool:
    """Does the patient speak of the evening -- "kechqurun", "soat 20 da"?"""
    return bool(_EVENING.search(text))


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
_MACHINE = re.compile(r"\[\[\s*(?:CALLBACK|BOOK|DOCTOR)[^\]]*\]\]")


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
        # The words are the caption's (no call promised after hours), but the
        # markers the rest of the system reads -- a call-back, a booking, a
        # hand-over -- stay with it.
        text = _EVENING_CAPTIONS.get(script, _EVENING_CAPTIONS["uz-latn"])
        kept = " ".join(_MACHINE.findall(reply))
        return f"{text} {kept} [[IMAGE:{image['url']}|{image.get('title', '')}]]".replace("  ", " ")
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
    reply: str, image: Mapping[str, Any], script: str = "uz-latn", *, keep_text: bool = True
) -> str:
    """A scripted reply with the card: the caption goes before the step's own
    question, in the last message, so the question stays the last word. The
    step's words always stay, under the evening card too (code review,
    2026-11-07); ``keep_text`` is accepted for old callers and ignored."""
    del keep_text
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


# --- the operation cards: by name, by price, and by complaint ---
#
# Dr Sultonbek's package cards (varicocele, scrotoplasty, ligamentotomy, the
# combined operation, penile prosthesis, micro-TESE, selective denervation).
# Sent when the patient names the operation or asks the operations' prices,
# and -- the owner's order, 2026-11-09 -- when the complaint is one the
# operation treats, in the patient's own words (COMPLAINT_CARDS). Whether an
# operation is needed, and which, the doctor decides at the consultation --
# said once, under the card. The steroid and hormone card goes the same way.

_OPERATION_WORD = re.compile(
    r"operats|operasi|amaliyot|jarroh|marmar|операц|амалиёт|жарроҳ|хирург|"
    r"narx|qancha|нарх|қанча|цен|стоим|сколько|"
    r"nech\w{0,2}\s*pul|неч\w{0,2}\s*пул|почём|почем",
    re.IGNORECASE,
)
_OPERATION_ONLY = re.compile(r"operats|operasi|amaliyot|операц|амалиёт", re.IGNORECASE)
# "Nechpul", "неча пул", "нечпул слада": how much, as patients write it.
PRICE_WORD = re.compile(
    r"narx|qancha|нарх|қанча|цен|стоим|сколько|почём|почем|nech\w{0,2}\s*pul|неч\w{0,2}\s*пул",
    re.IGNORECASE,
)
# "Rasm qani?", "расм кани", "фото скиньте": the picture itself is asked for.
# Only with a word that asks for it: "rasm yubordim" is the patient's own photo.
_ASKS_PICTURE = re.compile(
    r"(?:rasm|rasim|surat|расм|расим|сурат|фото|foto|картин|kartin)\w*\W+(?:\w+\W+)?"
    r"(?:qani|kani|қани|кани|yo['‘ʻ’`]?q|йўқ|йук|yubor(?:ing|in|sangiz)|юбор(?:инг|ин)|"
    r"tashla(?:ng|b)|ташла(?:нг|б)|ko['‘ʻ’`]?rinma|кўринма|куринма|не\s*вид|нет|где|"
    r"скин|отправ|покаж)"
    r"|(?:qani|kani|қани|кани|где|скиньте|отправьте|покажите|yuboring|юборинг|tashlang)"
    r"\W+(?:\w+\W+)?(?:rasm|rasim|surat|расм|расим|сурат|фото|foto|картин)",
    re.IGNORECASE,
)
# A reply that points at the operation's picture: "...narxi va paketi rasmda".
_SAYS_PICTURE = re.compile(
    r"(?:narx|paket|нарх|пакет|стоимост|цен)\w*[^.!?]{0,40}"
    r"(?:rasmda|расмда|на фото|на изображении|на картинке)",
    re.IGNORECASE,
)
# topic -> (what the patient wrote, does it need an operation or price word with it)
OPERATIONS: dict[str, tuple[re.Pattern[str], bool]] = {
    "op:varikosele": (
        re.compile(r"var[iae]k[oa]t?s?[eia]l|вар[иа]к[оаэе]ц|вар[иа]к[оа]с", re.IGNORECASE),
        True,
    ),
    "op:skrotoplastika": (
        re.compile(r"skrotoplast|скротопласт|moyak\s*xaltasi\w*\s*plast", re.IGNORECASE),
        False,
    ),
    "op:ligamentotomiya": (
        re.compile(
            r"ligamentotom|лигаментотом|katt?alasht|uzaytir|кат+алашт|узайтир|увелич|удлин",
            re.IGNORECASE,
        ),
        False,
    ),
    "op:kompleks": (
        re.compile(
            r"kompleks\s*operats|комплекс\w*\s*операц|katt?alasht|uzaytir|кат+алашт|"
            r"узайтир|увелич|удлин",
            re.IGNORECASE,
        ),
        False,
    ),
    "op:falloprotez": (re.compile(r"fall?oprotez|protez|фаллопротез|протез", re.IGNORECASE), False),
    "op:micro-tese": (
        re.compile(r"m[ia]kro\W?tese|\btese\b|микро\W?тезе|\bтезе\b", re.IGNORECASE),
        False,
    ),
    "op:denervatsiya": (re.compile(r"denervats|денервац", re.IGNORECASE), False),
}
# The owner's order (2026-11-09): a complaint brings the card of the
# operation that treats it, with the note that the doctor decides at the
# consultation. Complaints with no operation (prostatitis) bring none.
COMPLAINT_CARDS: dict[str, re.Pattern[str]] = {
    # Weak erection, in the doctor's words and the patients' own:
    # "kuchim yo'q", "turmayapti", "qotmayapti", "не стоит".
    "op:falloprotez": re.compile(
        r"ere[kc]t?s?i|erekts|эрекц|ерекц|импотен|potensi|potents|потенц|"
        r"kuch(?:im|i|imiz)?\s+(?:yo['‘ʻ’`]?q|kam|pas|zaif|qolma|ketgan)|"
        r"куч(?:им|и)?\s+(?:йўқ|йук|кам|пас|заиф|қолма|колма)|"
        r"erkaklik\s+(?:kuch|quvvat|zaif)|эркаклик\s+(?:куч|қувват|заиф)|"
        r"jinsiy\s+(?:zaif|kuchsiz)|жинсий\s+(?:заиф|кучсиз)|"
        r"turma(?:yapti|yabdi|ydi|di|yotgan|yapdi)|турма(?:япти|ябди|йди|ди)|"
        r"qotma(?:yapti|ydi|di|yapdi)|қотма(?:япти|йди|ди)|котмаяпти|"
        r"ko['‘ʻ’`]?tarilma|кўтарилма|кутарилма|"
        r"qattiq\s+bo['‘ʻ’`]?lma|қаттиқ\s+бўлма|yumshab\s+qol|юмшаб\s+қол|"
        r"не\s+стоит|слабая\s+потенц|слабо\s+стоит|плохо\s+стоит",
        re.IGNORECASE,
    ),
    # Varicocele: "moyakda tomir", "tomirlar kengaygan".
    "op:varikosele": re.compile(
        # "Varakatsel" (2026-11-09) as patients spell it.
        r"var[iae]k[oa]t?s?[eia]l|вар[иа]к[оаэе]ц|вар[иа]к[оа]с|"
        r"moya[kg]\w*\s+(?:\w+\s+)?(?:tomir|vena)|моя[кг]\w*\s+(?:\w+\s+)?(?:томир|вена)|"
        r"tomir\w*\s+(?:\w+\s+)?kengay|томир\w*\s+(?:\w+\s+)?кенгай|"
        r"расширен\w*\s+вен|вен\w*\s+яичк",
        re.IGNORECASE,
    ),
    # Premature ejaculation: "tez tugatib qo'yaman", "tez ketib qolaman".
    "op:denervatsiya": re.compile(
        r"tez\s*tugat|erta\s*tugat|тез\s*тугат|эрта\s*тугат|"
        r"tez\s*bo['‘ʻ’`]?shal|тез\s*бўшал|tez\s*(?:ketib|chiqib|to['‘ʻ’`]?kil)|"
        r"тез\s*(?:кетиб|чиқиб|чикиб|тўкил)|uzoq\s+(?:chiday|bardosh\s+bera)\s*olma|"
        r"узоқ\s+чидай\s*олма|преждевремен|быстро\s*(?:конча|заканчива)|"
        r"быстрое\s*семяизверж",
        re.IGNORECASE,
    ),
    # Size: "kichkina", "kalta", "kattalashtirmoqchiman".
    "op:ligamentotomiya": re.compile(
        r"katt?alasht|uzaytir|кат+алашт|узайтир|увелич|удлин|"
        r"(?:olat|jinsiy\s*a['‘ʻ’`]?zo|penis|олат|пенис|член|razmer|размер)\w*\s+(?:\w+\s+)?"
        r"(?:kich|kalta|кич|калта|маленьк|короток|мал)",
        re.IGNORECASE,
    ),
    "op:kompleks": re.compile(
        r"katt?alasht|uzaytir|кат+алашт|узайтир|увелич|удлин|"
        r"(?:olat|jinsiy\s*a['‘ʻ’`]?zo|penis|олат|пенис|член|razmer|размер)\w*\s+(?:\w+\s+)?"
        r"(?:kich|kalta|кич|калта|маленьк|короток|мал)",
        re.IGNORECASE,
    ),
    # No children: "farzand bo'lmayapti", "bola yo'q", "уруғ йўқ".
    "op:micro-tese": re.compile(
        r"azoosperm|азооспер|bepusht|бепушт|бесплод|"
        r"(?:farzand|bola)\w*\s+(?:\w+\s+)?(?:bo['‘ʻ’`]?lma|yo['‘ʻ’`]?q|ko['‘ʻ’`]?rma)|"
        r"(?:фарзанд|бола)\w*\s+(?:\w+\s+)?(?:бўлма|булма|йўқ|йук|кўрма)|"
        r"spermatozoid\w*\s+(?:\w+\s+)?(?:yo['‘ʻ’`]?q|chiqma)|"
        r"сперматозоид\w*\s+(?:\w+\s+)?(?:йўқ|нет|чиқма)|"
        r"urug['‘ʻ’`]?\w*\s+yo['‘ʻ’`]?q|уруғ\w*\s+йўқ|не\s+можем\s+зачать|нет\s+детей|"
        # "Tuxumdan urug' topish", "тухимдан уриг топиш" (2026-11-09).
        r"ur[ui]g['‘ʻ’`]?\w*\s+(?:\w+\s+)?top|ур[уи][гғ]\w*\s+(?:\w+\s+)?топ|"
        r"sperma\w*\s+top|сперма\w*\s+топ|"
        r"(?:tux[ui]m|moyak)\w*dan\s+ur[ui]g|(?:тух[уи]м|мояк)\w*дан\s+ур[уи][гғ]",
        re.IGNORECASE,
    ),
    # Steroids and hormones (2026-11-09): the consultation's own card.
    "sport": re.compile(
        r"steroid|anabol|стероид|анабол|trenbolon|nandrolon|sustanon|тренболон|нандролон|"
        r"сустанон|testosteron|тестостерон|gormon|гормон|sport\w*\s+farm|спорт\w*\s+фарм|"
        r"kurs\w*\s+(?:qil|ich|o['‘ʻ’`]?t|tugat)|kursdan\s+keyin|курс\w*\s+(?:қил|кил|ич|ўт)|"
        r"курсдан\s+кейин|после\s+курса",
        re.IGNORECASE,
    ),
    # The scrotum: "moyak xaltasi osilgan".
    "op:skrotoplastika": re.compile(
        r"moyak\s*xalta|мояк\s*халта|мошонк|skrotum|скротум|"
        r"moya[kg]\w*\s+(?:\w+\s+)?osil|моя[кг]\w*\s+(?:\w+\s+)?осил",
        re.IGNORECASE,
    ),
}
# "Boshqa operatsiyalar narxi?": the rest of the cards, not the one talked about.
_OTHERS = re.compile(
    r"boshqa|qolgan|hamma|barcha|бошқа|қолган|колган|ҳамма|хамма|барча|други|остальн|все\b",
    re.IGNORECASE,
)
OPERATION_NAMES = re.compile(
    "|".join(pattern.pattern for pattern, _ in OPERATIONS.values()), re.IGNORECASE
)
# The model's own way of saying it: "...doktor ko'rikda aniqlanadi".
_DOCTOR_DECIDES = re.compile(
    r"ko['‘ʻ’`]?rikda\s+aniqla|кўрикда\s+аниқла|определит\s+на\s+консультации",
    re.IGNORECASE,
)
_OPERATION_NOTE = {
    "uz-latn": "Operatsiya kerakligi va usulini doktor ko'rikda aniqlaydi.",
    "uz-cyrl": "Операция кераклиги ва усулини доктор кўрикда аниқлайди.",
    "ru": "Нужна ли операция и какая, доктор определит на консультации.",
}


def talks_about(topic: str, text: str) -> bool:
    """Does ``text`` name this card's operation, or the complaint it treats?"""
    rule = OPERATIONS.get(topic)
    complaint = COMPLAINT_CARDS.get(topic)
    return bool(rule and rule[0].search(text)) or bool(complaint and complaint.search(text))


def operations_asked(
    images: Sequence[Mapping[str, Any]] | None,
    *,
    user_message: str,
    own_turns: Sequence[str],
    patient_turns: Sequence[str] = (),
    reply: str = "",
) -> list[Mapping[str, Any]]:
    """The operation cards this message asks for, not sent before -- by name,
    or all of them for "operatsiya narxlari qancha?" naming none. "Rasm qani?"
    after operations were talked about, or a reply that says the price is in
    the picture, sends the ones named so far (all of them, none named)."""
    general = bool(_OPERATION_ONLY.search(user_message)) and bool(PRICE_WORD.search(user_message))
    talked = [*patient_turns, user_message]

    operations_talked = any(
        _OPERATION_ONLY.search(t)
        or OPERATION_NAMES.search(t)
        or any(c.search(t) for c in COMPLAINT_CARDS.values())
        for t in talked
    ) or any(_SAYS_PICTURE.search(t) for t in own_turns)
    if (_ASKS_PICTURE.search(user_message) and operations_talked) or _SAYS_PICTURE.search(reply):
        earlier = [
            image
            for image in images or []
            if any(talks_about(str(image.get("topic", "")), t) for t in talked)
        ]
        pool = earlier or [i for i in images or [] if str(i.get("topic", "")) in OPERATIONS]
        unsent = [
            image
            for image in pool
            if not any(sent_line(str(image.get("title", ""))) in turn for turn in own_turns)
        ]
        if not unsent and _ASKS_PICTURE.search(user_message):
            # "Rasm qani?" after they went out: not seen, so sent again.
            return earlier or pool
        if unsent or not general or (earlier and not _OTHERS.search(user_message)):
            return unsent
    named = [
        image
        for image in images or []
        if (rule := OPERATIONS.get(str(image.get("topic", "")))) is not None
        and rule[0].search(user_message)
    ]
    if general and not named and not _OTHERS.search(user_message):
        # "Нечпул операция" after one operation was talked about: that one's
        # card, not all seven (2026-11-09) -- and none again once it went.
        before = [
            image
            for image in images or []
            if str(image.get("topic", "")) in OPERATIONS
            and any(talks_about(str(image.get("topic", "")), t) for t in talked)
        ]
        if before:
            return [
                image
                for image in before
                if not any(sent_line(str(image.get("title", ""))) in turn for turn in own_turns)
            ]
    found = []
    for image in images or []:
        topic = str(image.get("topic", ""))
        rule = OPERATIONS.get(topic)
        complaint = COMPLAINT_CARDS.get(topic)
        if rule is None:
            # A card for a complaint only (steroids and hormones), sent once.
            if (
                complaint is not None
                and complaint.search(user_message)
                and not any(sent_line(str(image.get("title", ""))) in t for t in own_turns)
            ):
                found.append(image)
            continue
        pattern, needs_word = rule
        if not (complaint is not None and complaint.search(user_message)):
            if not pattern.search(user_message) and not (general and not named):
                continue
            if needs_word and not _OPERATION_WORD.search(user_message):
                continue
        if any(sent_line(str(image.get("title", ""))) in turn for turn in own_turns):
            continue
        found.append(image)
    return found


def attach_operations(
    reply: str,
    cards: Sequence[Mapping[str, Any]],
    script: str = "uz-latn",
    own_turns: Sequence[str] = (),
) -> str:
    """The reply with the operation cards under it, and -- once a
    conversation -- that the doctor decides at the consultation."""
    if not cards:
        return reply
    note = _OPERATION_NOTE.get(script, _OPERATION_NOTE["uz-latn"])
    said = not any(str(c.get("topic", "")).startswith("op:") for c in cards) or (
        any(_norm(note) in _norm(turn) for turn in own_turns)
        or _norm(note) in _norm(reply)
        or bool(_DOCTOR_DECIDES.search(reply))
    )
    text, existing = reply, ""
    first = MARKER.search(text)
    if first:
        text, existing = text[: first.start()].rstrip(), " " + text[first.start() :].strip()
    if not said:
        # Before the last message's own question, so the question stays last.
        parts = text.split("|||")
        sentences = [x.strip() for x in _SENTENCE.findall(parts[-1]) if x.strip()]
        if len(sentences) > 1:
            last = " ".join([*sentences[:-1], note, sentences[-1]])
        else:
            last = f"{note} {' '.join(sentences)}".strip()
        parts[-1] = (" " if len(parts) > 1 else "") + last
        text = "|||".join(parts)
    markers = " ".join(f"[[IMAGE:{c['url']}|{c.get('title', '')}]]" for c in cards)
    return f"{text}{existing} {markers}".strip()
