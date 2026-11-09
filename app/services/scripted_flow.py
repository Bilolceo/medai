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
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from typing import Any

from app.services import bubbles, phone_check, promo_images

# A complaint about desire, in any of the three alphabets, with a word that
# says it is low or gone -- "qabulga yozilish xohishim bor" is not one.
_DESIRE = re.compile(
    r"xohish|xoxish|hohish|istak|libido|mayl|qiziqish|havas|хоҳиш|хохиш|ҳоҳиш|истак|либидо|майл|"
    r"қизиқиш|желани|влечени|интерес",
    re.IGNORECASE,
)
_LOW = re.compile(
    r"pas|past|yo['‘ʻ’`]?q|yoq|kam|ket|so['‘ʻ’`]?n|qolma|jinsiy|xotin|"
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
# "15 00 ga": the colon left out (live chat, 2026-11-07).
_CLOCK_SPACED = re.compile(r"(?<!\d)([01]?\d|2[0-3])\s(00|15|30|45)(?!\d)")
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
    # Said before the model's answer, as its own message: the greeting and
    # introduction on a first contact that opens with a question.
    prefix: str | None = None
    # Whether a price card may go with this reply (app.services.promo_images).
    cards: bool = False
    # The card the complaint itself calls for: "day" (urology and andrology)
    # or "evening" (steroids and sport -- the evening online consultation).
    card_topic: str | None = None


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
        head = _norm(max(pieces, key=len)).strip(" ,.")[:60]
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
    message = _PHONE_DIGITS.sub(" ", message)
    match = _CLOCK.search(message) or _CLOCK_SPACED.search(message)
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


def _within(flow: Mapping[str, Any], at: time, prefix: str = "online") -> bool:
    start, end = flow.get(f"{prefix}_start"), flow.get(f"{prefix}_end")
    if not start or not end:
        return False
    return time.fromisoformat(start) <= at < time.fromisoformat(end)


_EVENING_WORD = re.compile(r"kech(?!ro)|кеч(?!ро)|вечер", re.IGNORECASE)


_TASHKENT = re.compile(r"to[sš]h?kent|tashkent|тошкент|ташкент|toshken|тошкен", re.IGNORECASE)
# Words that open a sentence and are no name: "Men", "Menda", "Salom".
_NOT_NAMES = re.compile(
    r"^(?:men|menda|mening|menga|meni|bizda|biz|salom|assalom\w*|asalom\w*|ha|xa|yo['‘ʻ’`]?q|"
    r"doktor|aka|uka|bro|xohish\w*|hohish\w*|ok|rahmat|мен|менда|менинг|салом|ассалом\w*|"
    r"ҳа|ха|йўқ|доктор|ака|ука|хоҳиш\w*|хохиш\w*|я|у|меня|здравствуйте|привет|да|нет)$",
    re.IGNORECASE,
)


def _libido_name(message: str) -> str | None:
    """The name a message opens with: "Sardor", "Sardor menda xohish yo'q"."""
    words = message.split()
    word = words[0].strip(".,!?") if words else ""
    if not _NAME_WORD.match(word) or _NOT_NAMES.match(word):
        return None
    if len(words) > 2 and not (
        words[0].endswith(",")
        or re.match(r"(?:men|menda|mening|menga|мен|менда|менинг|менга|я|у)$", words[1], re.I)
    ):
        # "Bilmayman nima qilishni": a sentence, not a name and a sentence.
        return None
    if _DESIRE.search(word) or _ABROAD.search(word) or _REGIONS.search(word):
        return None
    if _TASHKENT.search(word):
        return None
    return _cap(word)


def plan_libido(
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
        named = _libido_name(message)
        if named and own and _said(flow, "ask_name", own[-1:]):
            # "Sardor, menda xohish yo'q" to "Ismingiz nima?": the name is given.
            parts.append(fill("ask_place_named", name=named))
        else:
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
        if _EVENING_WORD.search(message) and at.hour < 12:
            # "kechki 8" is eight in the evening.
            at = at.replace(hour=at.hour + 12)
        clock = f"{at:%H:%M}"
        if _within(flow, at, "evening") and "paid_info_evening" in texts:
            # The evening hours cost more, and say why, whatever was said before.
            parts = [
                fill("slot_ok", time=clock),
                texts["paid_info_evening"],
                fill("prepay_evening", time=clock),
                texts["card"],
            ]
            return Plan(reply=f" {bubbles.MARKER} ".join(parts))
        if not _within(flow, at):
            return Plan(reply=fill("hours"))
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
        if _ABROAD.search(message) or _REGIONS.search(message) or _TASHKENT.search(message):
            # "Toshkenda" is the place, never a name.
            return Plan(reply=fill("ask_age"))
        named = _libido_name(message)
        if named:
            return Plan(reply=fill("ask_place_named", name=named))
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


# --- the ordinary sequence: any complaint, or a wish to be seen ----------------

_COMPLAINT = re.compile(
    r"erek|erect|potens|tez\s*bo['‘ʻ’`]?shan|tez\s*tugat|erta\s*tugat|og['‘ʻ’`]?ri|siydik|siyish|siya\s*olma|\bqon\b|"
    r"moyak|tuxum|prostat|varikot|bepusht|farzand|buyrak|achish|shish|sunnat|"
    r"vodyanka|gidrotsel|gidrosel|водянк|гидроцел|гидросел|"
    r"yuqumli|infeksiya|tahlil|spermogramma|gormon|testosteron|"
    r"muammo|kasal|shikoyat|davola|qabul|yozil|ko['‘ʻ’`]?rik|konsultatsiya|"
    r"эрек|потенц|оғри|сийдик|мояк|простат|варикоц|фарзанд|буйрак|гормон|"
    r"муаммо|касал|шикоят|даволан|қабул|ёзил|кўрик|консультац|"
    r"бол[иья]|моч|кров|яичк|бесплод|эякул|семяизверж|простатит|инфекц|анализ|"
    r"проблем|лечени|запис|приём|прием",
    re.IGNORECASE,
)
# Spelled as people type it: "rassiyada", "rosiyada", "moskvada", "korеyada".
_ABROAD = re.compile(
    r"r[ao]s+i[iy]?a|russia|moskva|maskva|koreya|koriya|turkiya|turtsiya|qozog['‘ʻ’`]?iston|"
    r"kazaxstan|amerika|ameriqa|usa\b|dubay|dubai|bae\b|germaniya|polsha|angliya|yaponiya|"
    r"chet\s*el|sankt|piter|yevropa|evropa|musofir|uzoqda|uzoqdaman|"
    r"р[ао]сси|москв|корея|корея|турци|туркия|казахстан|қозоғистон|америк|дубай|"
    r"германи|польш|англи|чет\s*эл|за\s*границ|питер|европ|мусофир|узоқда|узокда",
    re.IGNORECASE,
)
_REFUSE = re.compile(
    r"\byo['‘ʻ’`]?q\b|kerak\s*emas|bermayman|keyin|o['‘ʻ’`]?zim|shart\s*emas|"
    r"\bйўқ\b|керак\s*эмас|бермайман|кейин|\bнет\b|не\s*надо|потом|сам\b",
    re.IGNORECASE,
)
_PHONE_DIGITS = re.compile(r"\+?\d[\d\s()\-]{7,}\d")
_TIME_WORDS = re.compile(
    r"\d|ertalab|tush|kech|hozir|bugun|ertaga|istalgan|xohlagan|dushanba|seshanba|chorshanba|"
    r"payshanba|juma|shanba|soat|эртала|туш|кеч|ҳозир|бугун|эртага|исталган|соат|"
    r"утр|обед|вечер|сейчас|сегодня|завтра|любое|час",
    re.IGNORECASE,
)
# Words that ask to be seen without saying what for.
_JUST_A_VISIT = re.compile(
    r"qabul|yozil|ko['‘ʻ’`]?rik|konsultatsiya|narx|qancha|қабул|ёзил|кўрик|консультац|нарх|қанча|"
    r"запис|приём|прием|цен|стоим|сколько",
    re.IGNORECASE,
)
_INTAKE_STEPS = (
    "who_choice",
    "ask_name",
    "ask_place",
    "ask_topic",
    "contact_phone",
    "online_choice",
    "offer_slots",
    "book_ok",
    "hesitate_ask",
    "admin_busy",
    "ask_phone",
    "ask_time",
    "confirm",
    "paid_confirm",
)
_WANTS_ONLINE = re.compile(
    r"onla[iy]?e?n|online|онла[йи]?е?н|yozil|yozing|yozib|ёзил|ёзинг|запиш|\bha\b|\bxa\b|\bҳа\b|\bха\b|\bда\b|"
    r"mayli|xo['‘ʻ’`]?p|майли|хўп|хорошо|давайте",
    re.IGNORECASE,
)
_WANTS_CALL = re.compile(
    r"qo['‘ʻ’`]?ng['‘ʻ’`]?iroq|telefon|administrator|qilsin|қўнғироқ|телефон|администратор|"
    r"звон|позвон|перезвон",
    re.IGNORECASE,
)
# Uzbek regions and towns: a place, so "qayerdansiz?" is not asked again.
_REGIONS = re.compile(
    r"samarqand|buxoro|andijon|namangan|farg['‘ʻ’`]?ona|qo['‘ʻ’`]?qon|qashqadaryo|qarshi|"
    r"surxondaryo|termiz|jizzax|sirdaryo|guliston|navoiy|xorazm|urganch|xiva|nukus|"
    r"qoraqalpog|chirchiq|olmaliq|angren|bekobod|viloyat|"
    r"самарқанд|бухоро|андижон|наманган|фарғона|қўқон|қашқадарё|қарши|сурхондарё|термиз|"
    r"жиззах|сирдарё|гулистон|навоий|хоразм|урганч|нукус|вилоят|"
    r"самарканд|бухар|андижан|наманган|фергана|коканд|карши|термез|джизак|навои|ургенч|"
    r"нукус|област",
    re.IGNORECASE,
)
# Gynaecology is the model's: it sends her to a gynaecologist.
_WOMAN = re.compile(
    r"ayolman|xotin\s*kishiman|hayz|homilador|bachadon|tuxumdon|аёлман|ҳайз|ҳомиладор|"
    r"я\s+женщина|месячн|беременн|матк|яичник",
    re.IGNORECASE,
)
_GYNAECOLOGY = re.compile(
    r"hayz|homilador|bachadon|tuxumdon|ginekolog|ҳайз|ҳомиладор|бачадон|гинеколог|"
    r"месячн|беременн|матк|яичник|гинеколог",
    re.IGNORECASE,
)
# "Manzil qayerda?": asked with other things, it is answered with them.
_ASKS_ADDRESS = re.compile(
    r"manzil|adres|qayerda\s+joylash|klinika\s+qayerda|qayerdasiz|манзил|адрес|"
    r"қаерда\s+жойлаш|каерда\s+жойлаш|клиника\s+(?:қаерда|каерда|где)|где\s+находит",
    re.IGNORECASE,
)
# "Dollar kursi qancha?": a price, but not ours.
_NOT_OUR_PRICE = re.compile(r"dollar|kurs|valyuta|ob-?havo|доллар|курс|валют|погод", re.IGNORECASE)
# "Klinikaga kelaman": a visit in person, not online.
_WANTS_VISIT = re.compile(
    r"kel(?:aman|ib|amiz|sam|ish)|klinika|oflayn|offline|borib|boraman|"
    r"кел(?:аман|иб|амиз|сам)|клиник|офлайн|очно|приду|приеду|приехать|прийти",
    re.IGNORECASE,
)
# "Doktor bilan qanday bog'lansam bo'ladi?", "raqamini bering": the way to the
# doctor is the booking here, not the clinic's numbers.
_CONTACT = re.compile(
    r"bog['‘ʻ’`]?lan|aloqa|raqam|nomer|telefon|telegram|tel\b|"
    r"боғлан|алоқа|рақам|номер|телефон|телеграм|связат|связь",
    re.IGNORECASE,
)
# "Doktor bilan gaplashmoqchiman": wants the doctor or the administrator.
_WANTS_PERSON = re.compile(
    r"(?:doktor|vrach|shifokor|admin\w*|operator)\w*\s+bilan\s+gaplash|gaplashmoqchi|"
    r"(?:доктор|врач|шифокор|админ\w*|оператор)\w*\s+билан\s+гаплаш|гаплашмоқчи|"
    r"поговорить|связаться",
    re.IGNORECASE,
)
# Who the patient asking for a number wants: the administrator or the doctor.
_ASKS_ADMIN = re.compile(r"admin|operator|админ|оператор", re.IGNORECASE)
_ASKS_DOCTOR = re.compile(
    r"doktor|doxtir|vrach|shifokor|sultonbek|доктор|врач|шифокор|султонбек", re.IGNORECASE
)
_NO_NUMBER = re.compile(
    r"berma|bermay|qoldirma|yozma(?:yman|y)|kerak\s*emas|бермай|берма|қолдирма|не\s*(?:дам|хочу|буду)",
    re.IGNORECASE,
)
# "Karta raqami": the payment, not a contact.
_CARD_NUMBER = re.compile(r"karta|карт", re.IGNORECASE)


def contact_wish(text: str) -> str | None:
    """Asked for a number or a person: "admin", "doctor", or "ask" (which one
    is not said); None for anything else."""
    if phone_check.read(text) is not None or _CARD_NUMBER.search(text):
        return None
    if _NO_NUMBER.search(text):
        # "Raqam bermayman": a no, not a request for a number.
        return None
    if not (_CONTACT.search(text) or _WANTS_PERSON.search(text)):
        return None
    if _ASKS_ADMIN.search(text):
        return "admin"
    if _ASKS_DOCTOR.search(text):
        return "doctor"
    return "ask"


# "O'ylab ko'raman", "qimmat ekan": not yet -- the number is asked, once.
_HESITATE = re.compile(
    r"o['‘ʻ’`]?ylab|keyinroq|keyin\s+(?:yozaman|o['‘ʻ’`]?zim|bog['‘ʻ’`]?lanaman)|qimmat|"
    r"hozircha|pulim\s+yo['‘ʻ’`]?q|maslahatlash|ўйлаб|кейинроқ|қиммат|ҳозирча|"
    r"подумаю|дорого|позже|пока\s+не",
    re.IGNORECASE,
)
_PAID = re.compile(
    r"to['‘ʻ’`]?ladim|to['‘ʻ’`]?lov\s+qildim|o['‘ʻ’`]?tkazdim|tashladim|тўладим|ўтказдим|"
    r"оплатил|перевел|перевёл|отправил",
    re.IGNORECASE,
)
_TODAY = re.compile(r"bugun|бугун|сегодня", re.IGNORECASE)
_TOMORROW = re.compile(r"ertaga|эртага|завтра", re.IGNORECASE)
_TELEGRAM = re.compile(r"telegram|telegramm|телеграм|tg\b", re.IGNORECASE)
_WHATSAPP = re.compile(r"whats\s*app|vatsap|vatsapp|ватсап|вацап|уотсап", re.IGNORECASE)
_YES = re.compile(r"^\s*(ha|xa|bor|ҳа|ха|бор|да|есть)\b", re.IGNORECASE)
# An emergency is the model's to answer -- today, the phone, come in -- never
# a form to fill in.
_EMERGENCY = re.compile(
    r"birdan|qattiq\s*og['‘ʻ’`]?ri|kuchli\s*og['‘ʻ’`]?ri|shishib|shishdi|qon\s*ket|siya\s*olma|"
    r"siyolma|isitma|harorat|chidab\s*bo['‘ʻ’`]?lma|"
    r"бирдан|қаттиқ\s*оғри|кучли\s*оғри|шишди|қон\s*кет|сия\s*олма|иситма|ҳарорат|"
    r"внезапн|сильн\w*\s*бол|кровотеч|не\s*могу\s*помочит|температур|опух",
    re.IGNORECASE,
)
# "Ismim Bobur", "men Otabek", "меня зовут Руслан": the name, given unasked.
_GIVEN_NAME = re.compile(
    r"(?i:ismim|men|исмим|мен|меня\s+зовут|я)\s+([A-ZА-ЯЎҚҒҲ][a-zа-яёўқғҳ'‘ʻ’`]{1,19})(?=[\s,.!]|$)"
)


def _phone(text: str) -> str | None:
    match = _PHONE_DIGITS.search(text)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(0))
    return match.group(0).strip() if len(digits) >= 9 else None


_CALL_ME = re.compile(
    r"\s*(?:menga\s+)?(?:qo['‘ʻ’`]?ng['‘ʻ’`]?iroq\s+(?:qiling(?:lar)?|qilinglar|qilsangiz)|"
    r"телефон\s+қилинг|қўнғироқ\s+қилинг|позвоните(?:\s+мне)?|перезвоните)\s*",
    re.IGNORECASE,
)
_NOT_TIME = re.compile(r"raqam|рақам|номер|telefon|телефон|ism|исм", re.IGNORECASE)


def _when(text: str) -> str | None:
    """What the patient said about the time -- "ertaga 10 da", "tushdan
    keyin" -- out of a message that may also carry the number, the name and
    the complaint."""
    rest = _PHONE_DIGITS.sub(" ", text)
    pieces = []
    for part in re.split(r"[,;.\n]+", rest):
        part = _CALL_ME.sub(" ", part)
        part = re.sub(r"\s+", " ", part).strip(" -")
        if not part or not _TIME_WORDS.search(part):
            continue
        if _COMPLAINT.search(part) or _NOT_TIME.search(part) or _GREETING.search(part):
            continue
        pieces.append(part)
    return ", ".join(pieces) or None


def _greeting_only(text: str) -> bool:
    words = re.findall(r"\w+", text)
    return bool(_GREETING.search(text)) and len(words) <= 5 and not _COMPLAINT.search(text)


@dataclass(frozen=True)
class _Understood:
    """The current message, as the model read it -- or, without a reading,
    as the patterns do."""

    complaint: bool
    greeting_only: bool
    question: bool
    asks_price: bool
    refuses: bool
    abroad: bool
    name: str | None
    when: str | None
    messenger: str | None
    yes: bool
    complaint_words: str | None


def _understand(message: str, reading: Any, *, asked_name: bool) -> _Understood:
    if reading is None:
        # Several messages at once ("Сардор / Операция нархи / Водянка"):
        # the name is the first of them.
        first_line = message.strip().splitlines()[0] if message.strip() else ""
        word = first_line.split()[0].strip(".,!") if first_line.split() else ""
        name = None
        if (
            asked_name
            and len(first_line.split()) <= 2
            and _NAME_WORD.match(word)
            and not _GREETING.search(word)
        ):
            name = _cap(word)
        return _Understood(
            complaint=bool(_COMPLAINT.search(message)),
            greeting_only=_greeting_only(message),
            question=_is_question(message),
            asks_price=bool(_ASKS_PRICE.search(message)) and _is_question(message),
            refuses=bool(_REFUSE.search(message)) and not _is_question(message),
            abroad=bool(_ABROAD.search(message)),
            name=name,
            when=_when(message),
            messenger=(
                "WhatsApp"
                if _WHATSAPP.search(message)
                else ("Telegram" if _TELEGRAM.search(message) else None)
            ),
            yes=bool(_YES.search(message)) and not _is_question(message),
            complaint_words=None,
        )
    name = reading.name
    if name and (_ABROAD.search(name) or _COMPLAINT.search(name)):
        name = None
    lines = message.strip().splitlines()
    if name is None and asked_name and len(lines) > 1:
        # Several messages at once, the name first: "Сардор / Операция нархи / Водянка".
        first = lines[0].split()
        if len(first) == 1 and _NAME_WORD.match(first[0]) and not _COMPLAINT.search(first[0]):
            name = _cap(first[0])
    when = reading.time or None
    if when and _PHONE_DIGITS.search(when):
        when = _when(when)
    if when:
        # "ertaga 10 da qo'ng'iroq qiling" is "ertaga 10 da".
        when = re.sub(r"\s+", " ", _CALL_ME.sub(" ", when)).strip(" ,.") or None
    messenger = {"telegram": "Telegram", "whatsapp": "WhatsApp"}.get(reading.messenger or "")
    return _Understood(
        complaint=reading.complaint_kind in ("urology", "libido_only", "sport")
        or bool(_COMPLAINT.search(message)),
        greeting_only=reading.greeting_only and reading.complaint_kind == "none",
        question=bool(reading.question) or reading.who_are_you,
        asks_price=reading.asks_price,
        refuses=reading.refuses,
        abroad=reading.abroad,
        name=name,
        when=when,
        messenger=messenger,
        yes=reading.yes,
        complaint_words=reading.complaint,
    )


_WEEKDAYS = {
    "uz-latn": ["dushanba", "seshanba", "chorshanba", "payshanba", "juma", "shanba", "yakshanba"],
    "uz-cyrl": ["душанба", "сешанба", "чоршанба", "пайшанба", "жума", "шанба", "якшанба"],
    "ru": [
        "в понедельник",
        "во вторник",
        "в среду",
        "в четверг",
        "в пятницу",
        "в субботу",
        "в воскресенье",
    ],
}


def _label(texts: Mapping[str, str], day: Any, now: datetime) -> str:
    """ "bugun", "ertaga", or the weekday for a day further on."""
    if day == now.date():
        return texts["today"]
    if day == now.date() + timedelta(days=1):
        return texts["tomorrow"]
    script = "ru" if texts["at"] == "в" else ("uz-cyrl" if texts["today"] == "бугун" else "uz-latn")
    return str(_WEEKDAYS[script][day.weekday()])


def _slot_words(texts: Mapping[str, str], slots: Sequence[datetime], now: datetime) -> str:
    """ "Bugun 14:00 va 15:30 da, ertaga 10:00 va 11:30 da": two a day, today
    and the next day with room, spread rather than back to back."""
    by_day: dict[Any, list[datetime]] = {}
    for slot in slots:
        by_day.setdefault(slot.date(), []).append(slot)
    parts = []
    for day in sorted(by_day)[:2]:
        times = by_day[day]
        picked = [times[0]]
        later = [t for t in times if (t - times[0]).total_seconds() >= 90 * 60]
        if later:
            picked.append(later[0])
        elif len(times) > 1:
            picked.append(times[-1])
        word = _label(texts, day, now)
        clock = f" {texts['and']} ".join(f"{t:%H:%M}" for t in picked)
        if texts["at"] == "в":
            parts.append(f"{word} в {clock}" if not word.startswith("в") else f"{word} {clock}")
        else:
            parts.append(f"{word} {clock} {texts['at']}")
    text = ", ".join(parts)
    return text[:1].upper() + text[1:]


_DAY_NAMES = (
    (0, r"du?shanba|душанба|понедельн"),
    (1, r"se[yi]?shanba|сей?шанба|вторник"),
    (2, r"chorshanba|чоршанба|сред[ауы]"),
    (3, r"payshanba|пайшанба|четверг"),
    (4, r"\bjuma|жума|пятниц"),
    (5, r"(?<![a-zа-яёўқғҳ'])shanba|(?<![a-zа-яёўқғҳ])шанба|суббот"),
    (6, r"yakshanba|якшанба|воскресен"),
)
_LATER = re.compile(r"kechroq|kechro|keyinroq|кечроқ|кечро|кейинроқ|позже|попозже", re.IGNORECASE)
_DAY_AFTER = re.compile(r"indin|индин|послезавтра", re.IGNORECASE)


def _named_day(message: str, now: datetime) -> Any:
    """The day the patient named without an hour: "seshanbada-chi?", "ertaga"."""
    for weekday, pattern in _DAY_NAMES:
        if re.search(pattern, message, re.IGNORECASE):
            ahead = (weekday - now.weekday()) % 7 or 7
            return now.date() + timedelta(days=ahead)
    if _DAY_AFTER.search(message):
        return now.date() + timedelta(days=2)
    if _TOMORROW.search(message):
        return now.date() + timedelta(days=1)
    if _TODAY.search(message):
        return now.date()
    return None


def _chosen_slot(
    message: str, at: time, slots: Sequence[datetime], now: datetime
) -> datetime | None:
    """The free slot the patient named, on the day they said ("bugun",
    "ertaga", "seshanba") or else the soonest."""
    named = _named_day(message, now)
    days = [named] if named is not None else sorted({s.date() for s in slots})
    for day in days:
        for slot in slots:
            if slot.date() == day and slot.time() == at:
                return slot
    return None


def _day_word(texts: Mapping[str, str], when: datetime, now: datetime) -> str:
    return _label(texts, when.date(), now)


def plan_intake(
    flow: Mapping[str, Any] | None,
    *,
    script: str,
    user_message: str,
    history: Sequence[Mapping[str, Any]] | None,
    now: datetime,
    reading: Any = None,
    slots: Sequence[datetime] | None = None,
) -> Plan | None:
    """Dr Sultonbek's ordinary sequence, said as written: the doctor handles
    it, name, place, telephone and a time, then the operator will ring.

    Starts at the patient's first complaint or wish to be seen; until then
    only a bare greeting is answered here (with the introduction). Leaves
    the conversation to the model once the call is confirmed, once the
    evening card has been sent, or when the patient says no.
    """
    if not flow:
        return None
    texts = flow["texts"].get(script) or flow["texts"]["uz-latn"]
    turns = list(history or [])
    own = [turn["content"] for turn in turns if turn.get("role") == "assistant"]
    message = user_message.strip()
    introduced = any(_INTRO.search(turn) for turn in own)

    def opening() -> str | None:
        parts: list[str] = []
        if not own:
            parts.append(texts["greet_back"] if _GREETING.search(message) else texts["greet"])
        if not introduced:
            parts.append(texts["intro"])
        return " ".join(parts) or None

    last_own = own[-1] if own else ""
    if _GYNAECOLOGY.search(message) and texts.get("gynaecology"):
        # Not the doctor's field, and no gynaecologist is promised here.
        gyn_lead = " ".join(
            filter(
                None,
                (
                    (
                        (texts["greet_back"] if _GREETING.search(message) else texts["greet"])
                        if not own
                        else None
                    ),
                    texts["intro"] if not introduced else None,
                ),
            )
        )
        body = texts["gynaecology"]
        return Plan(reply=f"{gyn_lead} {bubbles.MARKER} {body}" if gyn_lead else body)
    if _WOMAN.search(message):
        return None
    u = _understand(message, reading, asked_name=bool(_said(flow, "ask_name", [last_own])))
    # Where the sequence began: the patient's first complaint by pattern, or
    # -- for one only the model understood -- the turn before the bot first
    # said "Ha, doktor shu bilan shug'ullanadi".
    by_pattern = next(
        (
            i
            for i, turn in enumerate(turns)
            if turn.get("role") == "user" and _COMPLAINT.search(turn["content"])
        ),
        None,
    )
    by_own_line = next(
        (
            max(i - 1, 0)
            for i, turn in enumerate(turns)
            if turn.get("role") == "assistant" and _said(flow, "handles", [turn["content"]])
        ),
        None,
    )
    # The doctor's order is name, place, complaint: the sequence begins with
    # the conversation, whatever the first message is -- a greeting, "how do
    # I reach the doctor", a price.
    by_name_line = next(
        (
            max(i - 1, 0)
            for i, turn in enumerate(turns)
            if turn.get("role") == "assistant"
            and any(
                _said(flow, key, [turn["content"]])
                for key in ("ask_name", "who_choice", "admin_busy", "doctor_consult")
            )
        ),
        None,
    )
    starts = [i for i in (by_pattern, by_own_line, by_name_line) if i is not None]
    first = min(starts) if starts else 0
    since_turns = turns[first:]
    since = [turn["content"] for turn in since_turns if turn.get("role") == "assistant"]
    said = {key: _said(flow, key, since) for key in _INTAKE_STEPS}
    # "Telefon raqamingizni yozing" asked again is the same step.
    said["ask_phone"] += _said(flow, "ask_phone_again", since)
    said["online_choice"] += _said(flow, "choice_again", since) + _said(flow, "visit_choice", since)
    said["offer_slots"] += _said(flow, "slot_taken", since) + _said(flow, "offer_evening", since)
    if said["paid_confirm"] or _said(flow, "hesitate_thanks", since):
        return None
    corrected = said["confirm"] and phone_check.read(message) is not None
    # The evening card sent for an evening wish hands the conversation to the
    # model; the flow's own evening offer does not.
    evening_handover = re.compile(
        r"ish vaqtidan keyin doktor bilan faqat onlayn|иш вақтидан кейин доктор билан фақат|"
        r"после рабочего времени — только онлайн",
        re.IGNORECASE,
    )
    if (said["confirm"] and not corrected) or any(evening_handover.search(t) for t in since):
        return None

    patient_since = [t["content"] for t in since_turns if t.get("role") == "user"] + [message]
    # What the call is about: the patient's complaint as the model put it, or
    # the message that began the sequence -- never the one with the number.
    complaint = (
        (u.complaint_words if len(patient_since) == 1 else None)
        or next((t for t in patient_since if _COMPLAINT.search(t)), None)
        or patient_since[0]
    )
    if contact_wish(complaint) and not _COMPLAINT.search(complaint):
        complaint = ""
    reason = " ".join(complaint.split()[:12]) or texts["default_reason"]
    question = u.question
    asks_price = u.asks_price and not _NOT_OUR_PRICE.search(message)
    sport_words = re.compile(flow.get("sport") or r"(?!x)x", re.IGNORECASE)
    sport = (reading is not None and reading.complaint_kind == "sport") or any(
        sport_words.search(t) for t in patient_since
    )
    card_topic = "evening" if sport else "day"

    def given_name() -> str | None:
        if u.name and reading is not None:
            return u.name
        for text in patient_since:
            for found in _GIVEN_NAME.finditer(text):
                word = found.group(1)
                # "Men Rossiyadaman", "men Toshkentdanman": a place, not a name.
                if re.search(r"(man|dan|da|ман|дан|да)$", word.lower()) or _ABROAD.search(word):
                    continue
                if _COMPLAINT.search(word):
                    continue
                return _cap(word)
        return None

    def name_part() -> str:
        if given_name():
            return f", {given_name()}"
        # The answer to "Ismingiz nima?": the patient message right after it.
        for i, turn in enumerate(since_turns):
            if turn.get("role") == "assistant" and _said(flow, "ask_name", [turn["content"]]):
                answer = next(
                    (t["content"] for t in since_turns[i + 1 :] if t.get("role") == "user"), ""
                )
                word = answer.split()[0].strip(".,!") if answer.split() else ""
                if len(answer.split()) <= 2 and _NAME_WORD.match(word):
                    return f", {_cap(word)}"
        return ""

    def earlier_when() -> str | None:
        return next((w for t in reversed(patient_since) if (w := _when(t))), None)

    def confirm(phone: str, when: str) -> Plan:
        at = parse_time(when)
        # "16:00" is inside "09:00 dan 16:00 gacha" for a call.
        callable_at = at is None or _within(flow, at) or f"{at:%H:%M}" == flow.get("online_end")
        if not callable_at or _EVENING_WORD.search(when):
            return Plan(reply=texts["operator_hours"])
        if re.fullmatch(r"\d{1,2}(?:[:.]\d{2})?", when.strip()):
            # A bare "16:00" reads as "16:00 da" / "в 16:00".
            when = texts.get("at_clock", "{t}").format(t=when.strip())
        text = texts["confirm"].format(name_part=name_part(), when=when, shown=phone)
        note = f"{reason} -- 50% oldindan"
        if said["admin_busy"]:
            note = f"ADMINISTRATOR BILAN GAPLASHMOQCHI -- {reason}"
        return Plan(reply=f"{text} [[CALLBACK:{phone}|{note}|{when}]]")

    def foreign(found: phone_check.Phone, said_now: str) -> Plan:
        app = u.messenger
        if app is None:
            if u.yes:
                return Plan(reply=texts["ask_app"])
            return Plan(reply=texts["phone_foreign"].format(shown=found.shown))
        when = earlier_when() or texts["any_time"]
        text = texts["foreign_confirm"].format(
            name_part=name_part(), when=when, app=app, shown=found.shown
        )
        note = f"{reason} (chet el, {app}) -- 50% oldindan"
        return Plan(reply=f"{text} [[CALLBACK:{found.shown}|{note}|{when}]]")

    def take_phone() -> Plan | None:
        found = phone_check.read(message)
        if found is None and u.when and _said(flow, "operator_hours", since[-1:]):
            # The number came with an hour the operators do not work; now the
            # hour is fixed, the number already given stands.
            earlier = next(
                (
                    f.shown
                    for t in reversed(patient_since[:-1])
                    if (f := phone_check.read(t)) and f.kind == phone_check.UZ
                ),
                None,
            )
            if earlier:
                return confirm(earlier, u.when)
        if found is None:
            # "Telegram bor" after the foreign number was pointed out.
            last_number = next(
                (f for t in reversed(patient_since[:-1]) if (f := phone_check.read(t))), None
            )
            pointed_out = _said(flow, "phone_foreign", since[-1:]) or _said(
                flow, "ask_app", since[-1:]
            )
            if last_number is not None and last_number.kind == phone_check.FOREIGN and pointed_out:
                return foreign(last_number, message)
            return None
        if found.kind == phone_check.FOREIGN:
            return foreign(found, message)
        if found.kind != phone_check.UZ:
            key = {
                phone_check.SHORT: "phone_short",
                phone_check.LONG: "phone_long",
                phone_check.BAD_CODE: "phone_bad_code",
            }[found.kind]
            return Plan(reply=texts[key].format(shown=found.shown))
        # A time said with the number that had to be retyped still stands.
        when = u.when or earlier_when()
        return confirm(found.shown, when) if when else Plan(reply=texts["ask_time"])

    # The first turn of the sequence.
    done = [key for key in _INTAKE_STEPS if said[key]]
    pending = done[-1] if done else None
    if (
        pending == "book_ok"
        and since
        and any(_said(flow, key, since[-1:]) for key in ("offer_slots", "slot_taken", "day_full"))
    ):
        # Times offered again after a booking line: choosing among them again.
        pending = "offer_slots"
    if pending in ("admin_busy", "ask_phone") and said["admin_busy"] and since:
        # Back in the doctor's sequence after the administrator's line: the
        # step just asked is the one answered.
        aliases = {
            "ask_place": ("ask_place_named",),
            "online_choice": ("visit_choice", "choice_again"),
            "offer_slots": ("slot_taken", "day_full", "offer_evening"),
        }
        last_step = next(
            (
                key
                for key in reversed(_INTAKE_STEPS)
                if key not in ("admin_busy", "ask_phone", "who_choice")
                and any(_said(flow, k, since[-1:]) for k in (key, *aliases.get(key, ())))
            ),
            None,
        )
        if last_step is not None:
            pending = last_step

    def named_complaint(text: str) -> bool:
        if sport_words.search(text) or promo_images.OPERATION_NAMES.search(text):
            return True
        if (
            reading is not None
            and text == message
            and reading.complaint_kind in ("urology", "libido_only", "sport")
        ):
            return True
        return bool(_COMPLAINT.search(_JUST_A_VISIT.sub(" ", text)))

    def answer_after(*keys: str) -> str | None:
        """The patient's message right after the bot's latest line for one of these
        steps: asked twice, the second answer counts ("klinikaga" after the card)."""
        for i in range(len(since_turns) - 1, -1, -1):
            turn = since_turns[i]
            if turn.get("role") == "assistant" and any(
                _said(flow, key, [turn["content"]]) for key in keys
            ):
                return next(
                    (t["content"] for t in since_turns[i + 1 :] if t.get("role") == "user"),
                    message,
                )
        return None

    def question_only(text: str) -> bool:
        """Only the question, no answer to the step: "Narxi qancha?"."""
        rest = re.sub(r"[^\w\s]", " ", _ASKS_PRICE.sub(" ", text))
        return len(rest.split()) <= 2

    def complaint_known() -> bool:
        return bool(_said(flow, "handles", since)) or any(named_complaint(t) for t in patient_since)

    def known_place() -> str | None:
        """Where the patient said they are, in any message so far."""
        tashkent = re.compile(flow.get("tashkent") or "toshkent", re.IGNORECASE)
        for text in reversed(patient_since):
            if _ABROAD.search(text) or _REGIONS.search(text) or tashkent.search(text):
                return str(text)
        if reading is not None and reading.place:
            return str(reading.place)
        return None

    def abroad_said() -> bool:
        return u.abroad or any(_ABROAD.search(t) for t in patient_since)

    def choice(place: str | None, abroad: bool = False) -> str:
        """Online or in the clinic, by where the patient is."""
        if sport:
            # Sport pharmacology: online only, in the evening.
            return str(texts["offer_evening"])
        place = place or ""
        tashkent = re.search(flow.get("tashkent") or "toshkent", place, re.IGNORECASE)
        if tashkent and not abroad and not abroad_said():
            return str(texts["visit_choice"])
        return str(texts["online_choice"])

    def choice_again() -> str:
        """The choice asked again: the clinic only for Tashkent."""
        place = answer_after("ask_place", "ask_place_named") or known_place()
        offer = choice(place)
        if offer == texts["visit_choice"]:
            return str(texts["choice_again"])
        if offer == texts["online_choice"]:
            # Only its question: "Onlayn konsultatsiyaga yozib qo'yaymi?".
            return re.split(r"(?<=[.!?])\s+", str(offer).strip())[-1]
        return str(offer)

    def phone_given() -> bool:
        return any(
            (f := phone_check.read(t)) is not None
            and f.kind in (phone_check.UZ, phone_check.FOREIGN)
            for t in patient_since
        )

    def next_after_complaint(place: str | None, abroad: bool = False) -> str:
        # Asked for the doctor: talking to the doctor is the consultation
        # (2026-11-07 tasks), booked here like any other.
        return choice(place, abroad)

    def doctor_path() -> str:
        """To talk to the doctor is to book the consultation, half paid ahead;
        then the next of name, place, complaint."""
        return f"{texts['doctor_consult']} {sequence_step()}"

    def sequence_step() -> str:
        """The next of name, place, complaint -- or the choice."""
        named = given_name()
        place = known_place()
        if not named and not said["ask_name"] and not name_part():
            step = texts["ask_name"]
        elif not place:
            step = texts["ask_place_named"].format(name=named) if named else texts["ask_place"]
        elif not complaint_known():
            step = texts["ask_topic"]
        else:
            step = choice(place, abroad_said())
        return str(step)

    def contact_reply(wish: str) -> str:
        if wish == "admin":
            return str(texts["admin_busy"])
        if wish == "doctor":
            return doctor_path()
        return str(texts["who_choice"])

    def after_place(place: str, abroad: bool = False) -> Plan:
        if not complaint_known():
            return Plan(reply=texts["ask_topic"])
        return Plan(reply=next_after_complaint(place, abroad))

    def in_person() -> bool:
        """Did the patient choose to come to the clinic?"""
        if sport:
            return False
        chosen = answer_after("online_choice", "visit_choice", "choice_again")
        replies = [chosen] if chosen else []
        if pending in ("online_choice", "offer_slots"):
            replies.append(message)
        return any(_WANTS_VISIT.search(t) for t in replies)

    if pending is None:
        lead = opening()
        if not named_complaint(message):
            # No complaint yet: the name first, as the doctor asks. "Doktor bilan
            # qanday bog'lansam bo'ladi?" is answered with the booking here,
            # never with the clinic's numbers.
            body = texts["ask_name"]
            named = given_name()
            if named:
                # "Men Bobur, Rossiyadaman": the name and maybe the place.
                body = (
                    texts["ask_topic"]
                    if known_place()
                    else texts["ask_place_named"].format(name=named)
                )
            wish = contact_wish(message)
            if wish:
                body = contact_reply(wish)
            elif question and not asks_price and not u.greeting_only:
                return Plan(prefix=lead, then=body, instruction=INSTRUCTION)
            plan_body = f"{lead} {bubbles.MARKER} {body}" if lead else body
            return Plan(reply=plan_body, cards=asks_price)
        early = take_phone()
        if early is not None and early.reply is not None:
            # All of it in one message: complaint, number, time.
            body = f"{texts['handles']} {early.reply}"
            return Plan(reply=f"{lead} {bubbles.MARKER} {body}" if lead else body)
        wish = contact_wish(message)
        if wish:
            # "Prostatit bor, doktor raqamini bering": who, then the sequence.
            # "Doktor bilan gaplashmoqchiman" is no complaint, whatever the
            # model read into it (mock run, 2026-11-08).
            body = contact_reply(wish)
            if _COMPLAINT.search(message) or promo_images.OPERATION_NAMES.search(message):
                body = f"{texts['handles']} {body}"
            return Plan(reply=f"{lead} {bubbles.MARKER} {body}" if lead else body)
        named = given_name()
        place = known_place()
        if named and place:
            # "Salom men Bobur, Rossiyadaman, ereksiya muammosi": straight to the choice.
            step = f"{texts['handles']} {next_after_complaint(place)}"
        elif named:
            step = f"{texts['handles']} {texts['ask_place_named'].format(name=named)}"
        else:
            step = f"{texts['handles']} {texts['ask_name']}"
        if question and not asks_price and not (named and place):
            return Plan(prefix=lead, then=step, instruction=INSTRUCTION)
        body = f"{lead} {bubbles.MARKER} {step}" if lead else step
        return Plan(reply=body, cards=asks_price, card_topic=card_topic)

    prices = flow.get("prices") or {}
    suffix = {"uz-cyrl": "_cyr", "ru": "_ru"}.get(script, "")

    def price_of(kind: str) -> tuple[str, str]:
        entry = prices.get(kind) or prices.get("uro") or {}
        return entry.get("price" + suffix, ""), entry.get("prepay" + suffix, "")

    def offer_slots(lead: str | None = None) -> Plan:
        if sport:
            # Steroids and sport: the doctor sees these online, in the evening.
            return Plan(reply=texts["offer_evening"])
        free = list(slots or [])
        if not free:
            return Plan(reply=texts["no_slots"])
        day = _named_day(message, now)
        if day is not None:
            # "Seshanbada-chi?": that day's times, or none that day.
            on_day = [s for s in free if s.date() == day]
            if not on_day:
                text = texts["day_full"].format(
                    day=_label(texts, day, now),
                    slots=_slot_words(texts, free, now),
                )
                return Plan(reply=text[:1].upper() + text[1:])
            free = on_day
        if _LATER.search(message):
            # "Kechroqi yo'qmi?": the latest times of the days on offer.
            by_day: dict[Any, list[datetime]] = {}
            for slot in free:
                by_day.setdefault(slot.date(), []).append(slot)
            free = [t for times in by_day.values() for t in times[-3:]]
        text = texts["offer_slots"].format(slots=_slot_words(texts, free, now))
        return Plan(reply=f"{lead} {text}" if lead else text)

    def bad_number(found: phone_check.Phone) -> Plan:
        key = {
            phone_check.SHORT: "phone_short",
            phone_check.LONG: "phone_long",
            phone_check.BAD_CODE: "phone_bad_code",
        }[found.kind]
        return Plan(reply=texts[key].format(shown=found.shown))

    if pending == "who_choice":
        # "Doktor bilan" / "admin bilan": the answer to who.
        wish = contact_wish(message) or (
            "admin"
            if _ASKS_ADMIN.search(message)
            else "doctor" if _ASKS_DOCTOR.search(message) else None
        )
        if wish in ("admin", "doctor"):
            return Plan(reply=contact_reply(wish))
        if phone_check.read(message) is not None:
            # A number straight away: the administrator rings.
            pending = "admin_busy"
            said["admin_busy"] = 1
        elif said["who_choice"] < 2 and not named_complaint(message):
            return Plan(reply=texts["who_choice"])
        else:
            return Plan(reply=doctor_path())
    if (
        pending in ("admin_busy", "ask_phone")
        and said["admin_busy"]
        and phone_check.read(message) is None
        and not question
    ):
        # No number for the administrator: asked once more, then -- or on a
        # no, a name, or a complaint -- on with the doctor's sequence.
        complaint_now = named_complaint(message)
        words = message.split()
        if (
            len(words) == 1
            and not complaint_now
            and _NAME_WORD.match(words[0].strip(".,!"))
            and not known_place()
            and not _GREETING.search(message)
        ):
            # "Bobur": the name, so the place next.
            return Plan(reply=texts["ask_place_named"].format(name=_cap(words[0].strip(".,!"))))
        if said["ask_phone"] or u.refuses or complaint_now:
            step = sequence_step()
            return Plan(reply=f"{texts['handles']} {step}" if complaint_now else step)
        return Plan(reply=texts["ask_phone_again"])
    wish = contact_wish(message)
    if (
        pending in ("ask_name", "ask_place", "ask_topic", "online_choice", "offer_slots")
        and wish
        and _said(flow, "who_choice", since) + _said(flow, "doctor_consult", since) < 3
    ):
        # "Doktor raqamini bering", "admin bilan gaplashsam": who, or that
        # person's way -- never a number, and never taken for a name.
        if wish == "doctor" and pending in ("ask_name", "ask_place", "ask_topic"):
            if _said(flow, "doctor_consult", since):
                return Plan(reply=texts[pending])
            return Plan(reply=f"{texts['doctor_consult']} {texts[pending]}")
        if wish == "doctor" and _said(flow, "doctor_consult", since):
            return Plan(reply=choice(known_place(), abroad_said()))
        return Plan(reply=contact_reply(wish))

    # --- asked for the doctor: their number after the complaint, passed on ---
    if pending == "contact_phone":
        found = phone_check.read(message)
        place = answer_after("ask_place", "ask_place_named") or known_place()
        if found is not None and found.kind in (phone_check.UZ, phone_check.FOREIGN):
            thanks = texts["contact_thanks"].format(name_part=name_part())
            note = f"DOKTOR BILAN GAPLASHMOQCHI -- {reason}"
            marker = f"[[CALLBACK:{found.shown}|{note}|{texts['any_time']}]]"
            return Plan(reply=f"{thanks} {marker} {bubbles.MARKER} {choice(place)}")
        if found is not None:
            return bad_number(found)
        if not question:
            # No number: on to the consultation all the same.
            return Plan(reply=choice(place))

    # --- not yet: the number, once, so the administrator can ring ---
    if (
        pending == "book_ok"
        and phone_check.read(message) is None
        and not _PAID.search(message)
        and (
            parse_time(message) is not None
            or _named_day(message, now) is not None
            or _LATER.search(message)
        )
    ):
        # "Seshanbada-chi?" after the card: another time, booked the same way.
        pending = "offer_slots"
    if pending == "hesitate_ask" and (
        parse_time(message) is not None
        or _named_day(message, now) is not None
        or _LATER.search(message)
    ):
        # Not a no after all: a day or an hour -- back to the times.
        pending = "offer_slots"
    if pending == "hesitate_ask":
        found = phone_check.read(message)
        if found is not None and found.kind in (phone_check.UZ, phone_check.FOREIGN):
            thanks = texts["hesitate_thanks"].format(name_part=name_part())
            note = f"IKKILANDI, qayta qo'ng'iroq -- {reason}"
            return Plan(reply=f"{thanks} [[CALLBACK:{found.shown}|{note}|{texts['any_time']}]]")
        if found is not None:
            return bad_number(found)
        return None
    if (
        pending == "online_choice"
        and named_complaint(message)
        and not question
        and not _WANTS_VISIT.search(message)
        and not _WANTS_ONLINE.search(message)
        and parse_time(message) is None
        and phone_check.read(message) is None
    ):
        # "Ereksiya yo'q" after the offer: the complaint, not a no.
        place = answer_after("ask_place", "ask_place_named") or known_place()
        return Plan(reply=f"{texts['handles']} {choice(place)}", card_topic=card_topic)
    hesitant = (
        (_HESITATE.search(message) or (u.refuses and _REFUSE.search(message)))
        and not _PAID.search(message)
        # "Raqam bermayman": the number is refused, so not asked for again.
        and not (_CONTACT.search(message) and _NO_NUMBER.search(message))
    )
    if (
        pending in ("online_choice", "offer_slots", "book_ok")
        and hesitant
        and not phone_given()
        and phone_check.read(message) is None
        and parse_time(message) is None
    ):
        return Plan(reply=texts["hesitate_ask"])

    # --- the booking: a time, the price, half of it, the card ---
    if pending == "book_ok":
        found = phone_check.read(message)
        if found is not None and found.kind not in (phone_check.UZ, phone_check.FOREIGN):
            key = {
                phone_check.SHORT: "phone_short",
                phone_check.LONG: "phone_long",
                phone_check.BAD_CODE: "phone_bad_code",
            }[found.kind]
            return Plan(reply=texts[key].format(shown=found.shown))
        if found is None and not _PAID.search(message):
            return None
        if found is None:
            return Plan(reply=texts["paid_ask"])
        offer = next(t for t in reversed(since) if _said(flow, "book_ok", [t]))
        clock = re.search(r"\b(\d{1,2}:\d{2})\b", offer)
        names = [texts["today"], texts["tomorrow"], *_WEEKDAYS["uz-latn"], *_WEEKDAYS["uz-cyrl"]]
        names += _WEEKDAYS["ru"]
        # Longest first: "yakshanba" and "dushanba" before the "shanba" inside them.
        names = sorted(names, key=len, reverse=True)
        day = next(
            (n for n in names if re.search(re.escape(n), offer, re.IGNORECASE)), texts["tomorrow"]
        )
        at = clock.group(1) if clock else ""
        visit = any(_norm(t.get("mode_visit", "#")) in _norm(offer) for t in flow["texts"].values())
        text = texts["paid_confirm"].format(name_part=name_part(), day=day, time=at)
        where = "KLINIKA" if visit else "ONLAYN"
        if found.kind == phone_check.FOREIGN:
            where += " (chet el raqami: Telegram/WhatsApp)"
        note = (
            f"{where} {day} {at} -- 50% oldindan, chekni tekshiring, "
            f"qabuldan 1-2 soat oldin qo'ng'iroq ({reason})"
        )
        return Plan(reply=f"{text} [[CALLBACK:{found.shown}|{note}|{day} {at}]]")

    if (
        pending == "online_choice"
        and _WANTS_CALL.search(message)
        and phone_check.read(message) is None
        and not _WANTS_ONLINE.search(message)
    ):
        # "Qo'ng'iroq qilinglar": the call, as the patient asks.
        return Plan(reply=texts["ask_phone"])
    if (
        pending in ("offer_slots", "online_choice")
        and parse_time(message) is None
        and (_named_day(message, now) is not None or _LATER.search(message))
        and not _WANTS_CALL.search(message)
    ):
        # "Seshanbada-chi?", "kechroq yo'qmi?": the free times that answer it.
        return offer_slots()
    if pending in ("offer_slots", "online_choice") and (
        not question or (parse_time(message) is not None and not asks_price)
    ):
        # "15:00 ga?" is a time, question mark or not.
        at = parse_time(message)
        if (
            at is not None
            and pending == "offer_slots"
            or (at is not None and pending == "online_choice" and not _WANTS_CALL.search(message))
        ):
            if _EVENING_WORD.search(message) and at.hour < 12:
                at = at.replace(hour=at.hour + 12)
            kind = "uro"
            mode = texts["mode_visit"] if in_person() else texts["mode_online"]
            if sport and not _within(flow, at, "evening"):
                return Plan(reply=texts["offer_evening"])
            if _within(flow, at, "evening"):
                evening_day = _named_day(message, now) or now.date() + timedelta(days=1)
                day = _label(texts, evening_day, now)
                mode = texts["mode_online"]
                price, prepay = price_of("evening")
                line = texts["price_line_evening"].format(price=price, prepay=prepay)
            else:
                slot = _chosen_slot(message, at, list(slots or []), now)
                if slot is None:
                    others = [s for s in (slots or [])]
                    if not others:
                        return Plan(reply=texts["no_slots"])
                    text = texts["slot_taken"].format(
                        time=f"{at:%H:%M}", slots=_slot_words(texts, others, now)
                    )
                    return Plan(reply=text)
                day = _day_word(texts, slot, now)
                price, prepay = price_of(kind)
                line = texts["price_line"].format(price=price, prepay=prepay)
            book = texts["book_ok"].format(day=day, time=f"{at:%H:%M}", mode=mode)
            return Plan(reply=f" {bubbles.MARKER} ".join((book, line, texts["card_line"])))

    no_number = phone_check.read(message) is None
    if pending == "online_choice" and not question and no_number:
        if _WANTS_CALL.search(message):
            return Plan(reply=texts["ask_phone"])
        if _WANTS_VISIT.search(message) or _WANTS_ONLINE.search(message) or u.yes:
            return offer_slots()
        if said["online_choice"] < 2 and not u.refuses:
            return Plan(reply=choice_again())
    if (
        pending == "offer_slots"
        and not question
        and not u.refuses
        and no_number
        and said["offer_slots"] < 2
    ):
        return offer_slots()

    if pending == "ask_topic" and not question and (not u.refuses or named_complaint(message)):
        # "Ereksiya yo'q" is the complaint, not a no.
        place = answer_after("ask_place", "ask_place_named") or known_place()
        return Plan(
            reply=f"{texts['handles']} {next_after_complaint(place)}", card_topic=card_topic
        )
    if pending == "ask_name" and not u.name and named_complaint(message) and not question:
        # "Prostatit" in answer to "Ismingiz nima?": the complaint came late.
        return Plan(reply=f"{texts['handles']} {texts['ask_name']}")

    # A number, whenever it comes -- or a correction of it -- is checked and
    # moves straight on to the time and the call.
    early = take_phone()
    callable_steps = (
        "admin_busy",
        "ask_name",
        "ask_place",
        "online_choice",
        "offer_slots",
        "ask_phone",
        "ask_time",
    )
    if early is not None and (pending in callable_steps or corrected):
        return early

    if asks_price and pending == "ask_place" and not question_only(message):
        # "Toshkentdanman, narxi qancha?": the place is answered; the card goes.
        step_plan = after_place(message, abroad=u.abroad)
        return Plan(reply=step_plan.reply, cards=True)
    asks_about_operation = question and re.search(r"operats|amaliyot|операц|амалиёт", message, re.I)
    card_sent = any(
        "📎" in line and "paket" not in line for turn in own for line in turn.splitlines()
    )
    op_cards = [
        line.split("📎", 1)[1].strip()
        for turn in own
        for line in turn.splitlines()
        if "📎" in line and "paket" in line
    ]
    op_prices = flow.get("operation_prices") or {}
    # The operation itself asked about before -- not a card that came with a
    # complaint (2026-11-09: "Narhi qancha" after "ereksiya" got the prosthesis).
    operation_asked = any(
        re.search(r"operats|operasi|amaliyot|операц|амалиёт", t, re.I)
        or any(
            pattern.search(t)
            and not (
                (complaint := promo_images.COMPLAINT_CARDS.get(topic)) is not None
                and complaint.search(t)
            )
            for topic, (pattern, _) in promo_images.OPERATIONS.items()
        )
        for t in patient_since
    )
    if asks_price and op_cards and operation_asked and not asks_about_operation:
        # "1- komponentli protez narxi?" again: the operation's price, in words.
        lines = [
            op_prices[t].get(script) or op_prices[t]["uz-latn"] for t in op_cards if t in op_prices
        ]
        if lines:
            again = {
                "online_choice": choice_again(),
                "offer_slots": choice_again(),
                "ask_place": texts["ask_place"],
                "ask_name": texts["ask_name"],
            }.get(pending or "", "")
            body = " ".join(lines) + " " + texts["op_first_consult"]
            return Plan(reply=f"{body} {again}".strip())
    if asks_price and not asks_about_operation and card_sent and complaint_known():
        # The card went already; asked again ("Пули нечпул?"), the price is
        # said in words -- never the step's question alone (live chat, 2026-11-07).
        price, prepay = price_of("evening" if sport else "uro")
        key = "price_answer_evening" if sport else "price_answer"
        again = {
            "online_choice": choice_again(),
            "offer_slots": choice_again(),
            "ask_place": texts["ask_place"],
            "ask_name": texts["ask_name"],
        }.get(pending or "", "")
        return Plan(reply=f"{texts[key].format(price=price, prepay=prepay)} {again}".strip())
    if asks_price and not asks_about_operation:
        again = {
            "ask_name": (
                texts["ask_place_named"].format(name=u.name) if u.name else texts["ask_name"]
            ),
            "ask_place": texts["ask_place"],
            "ask_topic": texts["ask_topic"],
            "online_choice": choice_again(),
            "offer_slots": choice_again(),
            "ask_phone": texts["ask_phone_again"],
            "ask_time": texts["ask_time"],
        }.get(pending, texts["ask_phone_again"])
        return Plan(reply=again, cards=True)

    if early is not None and pending == "ask_time" and phone_check.read(message) is not None:
        return early
    if pending == "ask_time":
        phone = next(
            (
                f.shown
                for t in reversed(patient_since)
                if (f := phone_check.read(t)) and f.kind == phone_check.UZ
            ),
            None,
        )
        when = u.when
        if phone and when and not question:
            return confirm(phone, when)
        if question:
            return Plan(then=texts["ask_time"], instruction=INSTRUCTION)
        return None

    no_name = re.search(
        r"ayt(?:ma|mas)|berma|shart\s*emas|kerak\s*emas|айтма|берма|не\s*(?:скажу|хочу|буду)",
        message,
        re.IGNORECASE,
    )
    if (
        pending == "ask_name"
        and (u.refuses or no_name)
        and re.search(r"ism|исм|им[яе]", message, re.I)
    ):
        # "Ismimni aytmayman": on without it.
        return Plan(reply=texts["ask_place"])
    if u.refuses and not question:
        # "Yo'q", "keyin o'zim yozaman": respected, by the model, once.
        return None
    if question and pending == "ask_topic" and named_complaint(message):
        # "Varikotsele, operatsiya qancha?": the complaint is given with the
        # question, so the model answers and the next step follows.
        place = answer_after("ask_place", "ask_place_named") or known_place()
        return Plan(then=next_after_complaint(place), instruction=INSTRUCTION)
    if question:
        # The model answers; the step's question follows -- twice at most,
        # then the next step's, so a patient full of questions is not asked
        # the same thing a third time, and a question is never taken for a name.
        if pending in ("online_choice", "offer_slots"):
            if said[pending] >= 2:
                return None
            # Outside Tashkent the clinic was never offered: online again.
            offer = choice_again()
            again = (
                texts["offer_slots"].format(slots=_slot_words(texts, list(slots or []), now))
                if pending == "offer_slots" and slots
                else offer
            )
            return Plan(then=again, instruction=INSTRUCTION)
        if said[pending] < 2:
            again = (
                texts["ask_phone_again"]
                if pending in ("ask_phone", "admin_busy")
                else texts[pending]
            )
        else:
            if pending in (
                "ask_phone",
                "ask_topic",
                "contact_phone",
                "hesitate_ask",
                "admin_busy",
                "who_choice",
            ):
                return None
            again = {
                "ask_name": texts["ask_place"],
                "ask_place": texts["ask_topic"],
            }[pending]
        return Plan(then=again, instruction=INSTRUCTION)

    if pending == "ask_name" and u.greeting_only and (not u.name or _GREETING.search(u.name)):
        # "Ассаламу алейкум" after "Ismingiz nima?": not a name.
        return Plan(reply=texts["ask_name"])
    if pending == "ask_name":
        place = known_place()
        if place:
            # The place came earlier or with the name: not asked again.
            return after_place(place, abroad=abroad_said())
        if u.name:
            return Plan(reply=texts["ask_place_named"].format(name=u.name))
        return Plan(reply=texts["ask_place"])
    told_online = any(
        _said(flow, "abroad_online", [turn]) or _said(flow, "online_choice", [turn])
        for turn in since
    )
    if pending in ("ask_phone", "ask_time") and u.abroad and not told_online:
        # "Hozir men Rossiyadaman", said after the place question was answered.
        return Plan(reply=texts["online_choice"])
    if pending == "ask_place":
        # Then the complaint, if not yet said; then online or the clinic --
        # booked and half paid here, so the administrator only checks the
        # receipt and rings to confirm before the visit.
        if named_complaint(message) and not known_place():
            # "Prostatit bor" in answer to "qayerdansiz?": taken, the place asked again.
            return Plan(reply=f"{texts['handles']} {texts['ask_place']}", card_topic=card_topic)
        return after_place(message, abroad=abroad_said())
    if pending in ("ask_phone", "admin_busy"):
        if said["ask_phone"] >= 2:
            return None
        return Plan(reply=texts["ask_phone_again"])
    return None


def finished(flows: Mapping[str, Any] | None, history: Sequence[Mapping[str, Any]] | None) -> bool:
    """Has the intake sequence ended in this conversation -- the call
    confirmed, the payment confirmed, the hesitant patient's number taken?
    Then nothing scripted is coming, and the message need not be read for it."""
    intake = (flows or {}).get("intake")
    if not intake:
        return False
    own = [t["content"] for t in history or [] if t.get("role") == "assistant"]
    return any(_said(intake, key, own) for key in ("confirm", "paid_confirm", "hesitate_thanks"))


def drop_repeats(step: str, answer: str) -> str:
    """The step's line without a sentence the model's answer already said
    ("Ha, doktor shu bilan shug'ullanadi." twice, live replay 2026-11-07)."""
    said = {_norm(x).strip(" .!?") for x in re.findall(r"[^.!?]+[.!?]?", answer)}
    kept = [
        x.strip() for x in re.findall(r"[^.!?]+[.!?]?", step) if _norm(x).strip(" .!?") not in said
    ]
    return " ".join(kept) or step


def plan(
    flows: Mapping[str, Any] | None,
    *,
    script: str,
    user_message: str,
    history: Sequence[Mapping[str, Any]] | None,
    now: datetime,
    reading: Any = None,
    slots: Sequence[datetime] | None = None,
) -> Plan | None:
    """The scripted answer from whichever flow this conversation is in."""
    if not flows:
        return None
    if _EMERGENCY.search(user_message) or (
        reading is not None and reading.complaint_kind == "emergency"
    ):
        return None
    libido = flows.get("libido")
    if libido:
        patient = [t["content"] for t in history or [] if t.get("role") == "user"]
        in_libido = any(_about_desire(t) for t in (*patient, user_message)) and not any(
            _OTHER_COMPLAINT.search(t) for t in (*patient, user_message)
        )
        if in_libido:
            return plan_libido(
                libido, script=script, user_message=user_message, history=history, now=now
            )
    result = plan_intake(
        flows.get("intake"),
        script=script,
        user_message=user_message,
        history=history,
        now=now,
        reading=reading,
        slots=slots,
    )
    result = _price_in_words(result, flows.get("intake"), script, user_message, history)
    return _with_address(result, flows.get("intake"), script, user_message, history)


def _price_in_words(
    result: Plan | None,
    flow: Mapping[str, Any] | None,
    script: str,
    user_message: str,
    history: Sequence[Mapping[str, Any]] | None,
) -> Plan | None:
    """With no consultation price card (Dr Sultonbek took his two away,
    2026-11-07), the price asked for is said in words, with the 50% ahead,
    before the step's question -- the operation cards still go."""
    if result is None or not result.cards or result.reply is None or not flow:
        return result
    if flow.get("price_cards", True):
        return result
    texts = flow["texts"].get(script) or flow["texts"]["uz-latn"]
    patient = [t["content"] for t in history or [] if t.get("role") == "user"]
    sport_words = re.compile(flow.get("sport") or r"(?!x)x", re.IGNORECASE)
    evening = any(
        sport_words.search(t) or promo_images.wants_evening(t) for t in (*patient, user_message)
    )
    entry = (flow.get("prices") or {}).get("evening" if evening else "uro") or {}
    suffix = {"uz-cyrl": "_cyr", "ru": "_ru"}.get(script, "")
    price, prepay = entry.get("price" + suffix, ""), entry.get("prepay" + suffix, "")
    if not price:
        return Plan(reply=result.reply, card_topic=result.card_topic)
    line = texts["price_answer_evening" if evening else "price_answer"].format(
        price=price, prepay=prepay
    )
    parts = result.reply.split(bubbles.MARKER)
    sentences = [x.strip() for x in re.findall(r"[^.!?]+[.!?]?", parts[-1]) if x.strip()]
    if len(sentences) > 1:
        last = " ".join([*sentences[:-1], line, sentences[-1]])
    else:
        last = f"{line} {' '.join(sentences)}".strip()
    parts[-1] = (" " if len(parts) > 1 else "") + last
    return Plan(reply=bubbles.MARKER.join(parts).strip(), card_topic=result.card_topic)


def _with_address(
    result: Plan | None,
    flow: Mapping[str, Any] | None,
    script: str,
    user_message: str,
    history: Sequence[Mapping[str, Any]] | None,
) -> Plan | None:
    """A scripted reply to a message that also asked the address, with the
    address in it -- the Instagram buttons send "narxi?", "manzil qayerda?"
    and "qanday bog'lanaman?" together (real chats, 2026-11-06)."""
    if result is None or result.reply is None or not flow:
        return result
    if not _ASKS_ADDRESS.search(user_message):
        return result
    texts = flow["texts"].get(script) or flow["texts"]["uz-latn"]
    line = texts.get("address")
    if not line or _norm(line)[:30] in _norm(result.reply):
        return result
    own = [t["content"] for t in history or [] if t.get("role") == "assistant"]
    if any(_norm(line)[:30] in _norm(t) for t in own[-2:]):
        return result
    parts = result.reply.split(bubbles.MARKER)
    parts[-1] = f" {line} {parts[-1].strip()}" if len(parts) > 1 else f"{line} {parts[-1].strip()}"
    return replace(result, reply=bubbles.MARKER.join(parts).strip())
