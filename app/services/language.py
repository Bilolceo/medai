import re
from collections.abc import Sequence

from app.rag.llm import ChatMessage
from app.services import message_labels

_UZBEK_CYRILLIC = frozenset("ўқғҳ")

# Letters Uzbek Cyrillic does not have. Evidence, not proof: Uzbeks write
# loanwords with them ("консультацияга") and misspell with them ("томищаман").
_RUSSIAN_ONLY = frozenset("ыщь")

# Most Uzbeks typing Cyrillic on a Russian keyboard write "к", "х", "у" for
# "қ", "ҳ", "ў", so "Рахмат" or "Канча туради" carry no Uzbek-only letter at
# all. Common whole words decide those instead.
_RUSSIAN_WORDS = frozenset(
    re.findall(
        r"\S+",
        "и в не на что как я вы мне меня с по это есть можно где когда "
        "здравствуйте привет пожалуйста спасибо сколько стоит цена хорошо "
        "нет ли уже или его её их мой моя вас нас запись "
        "записаться приём прием день добрый утро вечер хочу надо нужно "
        "какой какая какое какие почему зачем сейчас сегодня завтра здесь там "
        "очень болит болят подскажите скажите работаете работает ваш ваша "
        "записать запишите тоже только если "
        # The short replies a Russian conversation is carried on with. None
        # has a "ы" or "ь", so without them "Понятно" or "Да, давайте" in the
        # middle of a Russian chat tied, the tie went to Uzbek, and the
        # assistant answered a Russian patient in Uzbek.
        "да давайте давай понятно поняла понял ясно ладно ок окей адрес врач "
        "врача врачу доктора доктору принимает принимаете урологу уролог "
        "скок скиньте пришлите напишите позвоните перезвоните подойти прийти "
        "приду придти записали запишусь можете могу буду будет было был "
        "беспокоит болит болела почки почек камни анализ анализы стоимость",
    )
)
_UZBEK_WORDS = frozenset(
    re.findall(
        r"\S+",
        "салом ассалому ассалом алейкум рахмат рахмад рахмет ха хо йук йок "
        "бор борми йукми керак канча нарх нархи качон кайерда каерда кайерга "
        "мен ман сиз биз улар шу бу нима нега яхши илтимос ака опа ука "
        "дохтир булади булдими буладими килиш килса килиб эди экан "
        "ва лекин учун билан кейин хозир эртага бугун кеча соат манзил "
        "хам мумкин мумкинми шунга ухшаш ухшаган дори узи узим кандай канака "
        "духтир духтур доктор салом солом асалому саволим бор еди эди керакми "
        # Typed on a Russian keyboard: "Ешим 38 да", "Уруг чикмеди умуман".
        "ешим ёшим ешимда ёшимда ёшда ешда умуман чикмеди чикмади чикади "
        "оғрияпти огрияпти огрийапти куриб курсатинг беринг бериниг",
    )
)
_UZBEK_SUFFIX = re.compile(r"(лар|ларни|ларга|дан|даги|нинг|миз|сиз|ман|ган|япти|ябди|мокда)$")
# Russian verb and adjective endings Uzbek words of this length do not have:
# "принимает", "беспокоит", "давайте", "удобное". Counted like the Uzbek
# suffixes above -- one point each, and only on words longer than four letters.
_RUSSIAN_SUFFIX = re.compile(r"(ться|тся|ает|яет|ует|ите|ете|ают|яют|ешь|ого|его|ому|ая|ое|ые|ый)$")
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


# "38 да", "10 да": the Uzbek locative written apart from its number, not
# the Russian "yes". Seen live 2026-10-16: "Ешим 38 да" was answered in Russian.
_NUMBER_LOCATIVE = re.compile(r"\d\s*-?\s*да\b")


def _cyrillic_scores(lowered: str) -> tuple[int, int]:
    """Points for Uzbek and for Russian in a Cyrillic message."""
    uzbek = russian = 0
    if _NUMBER_LOCATIVE.search(lowered):
        uzbek += 2
        lowered = _NUMBER_LOCATIVE.sub(" ", lowered)
    for word in _WORD.findall(lowered):
        if word in _UZBEK_WORDS:
            uzbek += 2
        elif word in _RUSSIAN_WORDS:
            russian += 2
        elif any(letter in word for letter in _RUSSIAN_ONLY):
            russian += 1
        elif len(word) > 4 and _UZBEK_SUFFIX.search(word):
            uzbek += 1
        elif len(word) > 4 and _RUSSIAN_SUFFIX.search(word):
            russian += 1
    return uzbek, russian


def _cyrillic_language(lowered: str) -> str:
    if any(letter in lowered for letter in _UZBEK_CYRILLIC):
        return "uz-cyrl"
    uzbek, russian = _cyrillic_scores(lowered)
    # A tie goes to Uzbek: this inbox is in Uzbekistan. Inside a conversation
    # a tie is not decided here at all -- see conversation_script.
    return "ru" if russian > uzbek else "uz-cyrl"


def _cyrillic_is_clear(lowered: str) -> bool:
    if any(letter in lowered for letter in _UZBEK_CYRILLIC):
        return True
    uzbek, russian = _cyrillic_scores(lowered)
    return uzbek != russian


# Russian typed in Latin letters, the way it is sent from a phone without a
# Cyrillic keyboard: "Zdravstvuyte, skolko stoit?". Answered in Russian.
_TRANSLIT_RUSSIAN_WORDS = frozenset(
    re.findall(
        r"\S+",
        "zdravstvuyte zdravstvuite zdrastvuyte zdravstvuy zdrasti zdraste privet "
        "spasibo pozhaluysta pojaluysta pozhalusta skolko skolka stoit stoimost "
        "mne nado nuzhno hochu khochu zapisatsya zapisat mozhno gde kogda vrach "
        "vracha dobriy dobryy dobry den utro vecher horosho khorosho ponyatno "
        "davayte priem priyom adres rabotaete u menya bolit pochki",
    )
)
# Uzbek Latin has "q", "o'", "g'", and words Russian transliteration never
# produces. Any of them settles it.
_UZBEK_LATIN_WORDS = frozenset(
    re.findall(
        r"\S+",
        "salom assalomu assalom alaykum aleykum rahmat raxmat ha xa yoq yo'q bor "
        "bormi kerak narx narxi necha qancha kancha qachon kachon qayerda qayerdasiz "
        "yaxshi iltimos men man siz biz nima nega aka opa doktor doktir duxtir "
        "bo'ladimi boladimi buladimi yozilmoqchiman yozilish qabul qabulga "
        "ertaga bugun soat manzil mumkin mumkinmi edi ekan va lekin uchun bilan "
        "slm nmagap",
    )
)
_UZBEK_LATIN_MARKS = re.compile(r"q|[og]['’ʻ‘`]|sh|ch", re.IGNORECASE)


def _latin_language(lowered: str) -> tuple[str, bool]:
    """("ru" or "uz-latn", whether the message said so clearly)."""
    words = _WORD.findall(lowered)
    russian = sum(1 for w in words if w in _TRANSLIT_RUSSIAN_WORDS)
    uzbek = sum(1 for w in words if w in _UZBEK_LATIN_WORDS)
    if russian > uzbek and not re.search(r"q|[og]['’ʻ‘`]", lowered):
        return "ru", True
    if uzbek or _UZBEK_LATIN_MARKS.search(lowered):
        return "uz-latn", True
    return "uz-latn", False


def reply_script(user_message: str) -> str:
    """Which of "uz-latn", "uz-cyrl", "ru" to answer in.

    A deliberately small rule rather than a language detector: Cyrillic is
    Uzbek or Russian by its letters and words, Latin is Uzbek unless it is
    Russian typed in Latin letters.
    """
    return _script_of(user_message)[0]


def clear_script(user_message: str) -> str | None:
    """The script this one message plainly asks for, or None when it is too
    short or too ambiguous to say ("ok", "Aziz", "93 444 44 44")."""
    message = _patient_words(user_message)
    if _letter_count(message) < _MIN_LETTERS:
        return None
    script, clear = _script_of(message)
    return script if clear else None


def _script_of(user_message: str) -> tuple[str, bool]:
    """The script for one message, and whether the message made it clear."""
    lowered = user_message.lower()
    if any("Ѐ" <= character <= "ӿ" for character in lowered):
        return _cyrillic_language(lowered), _cyrillic_is_clear(lowered)
    return _latin_language(lowered)


# Whether a message has enough letters to be evidence of an alphabet:
# "93 444 444" and "11:00" are the two most common messages this inbox gets,
# and "ok" from a patient writing Cyrillic is not a switch to Latin.
_MIN_LETTERS = 3


def _letter_count(message: str) -> int:
    return sum(len(w) for w in _WORD.findall(message))


def conversation_script(history: Sequence[ChatMessage] | None, user_message: str) -> str:
    """The alphabet this conversation is being held in.

    Per conversation rather than per message: a patient who wrote Latin
    throughout and then sent a telephone number was answered in Cyrillic.
    The patient's own words decide it, newest first; what the assistant
    wrote does not, or one reply in the wrong alphabet would justify the
    next.
    """
    written = [_patient_words(user_message)]
    if history:
        written.extend(
            _patient_words(m["content"]) for m in reversed(history) if m.get("role") == "user"
        )
    # The newest message that says clearly which language it is in decides.
    # "Понятно", "Aziz" or "ok" in the middle of a conversation do not: they
    # leave it in the language the patient was already writing.
    first_guess: str | None = None
    for message in written:
        if _letter_count(message) < _MIN_LETTERS:
            continue
        script, clear = _script_of(message)
        if clear:
            return script
        if first_guess is None:
            first_guess = script
    return first_guess or reply_script(written[0])


# Labels the system puts in the transcript in its own words -- "🎤 Ovozli
# xabar", "📷 Rasm yubordi", "💬 Izoh: ..." -- are not the patient writing,
# and their Latin once had a patient writing Cyrillic answered in Latin.
# See app.services.message_labels.
_COMMENT_PREFIX = re.compile(r"^\s*💬\s*Izoh:\s*")


def _patient_words(message: str) -> str:
    if message_labels.is_label(message):
        return ""
    return _COMMENT_PREFIX.sub("", message)


# Latin words a reply in Cyrillic may still carry as they are: service
# names patients know in Latin. Handles, links and markers are removed
# before counting, and so are short capitalised abbreviations (UZI, PSA).
_LATIN_ALLOWED = frozenset(
    {"telegram", "whatsapp", "instagram", "direct", "zoom", "google", "youtube", "online"}
)
_NOT_WORDS = re.compile(r"@\w+|https?://\S+|\[\[[^\]]*\]\]|\b[A-Z]{2,5}\b")
_LATIN = re.compile(r"[A-Za-z]")
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
# Words in the other alphabet a reply may carry before it is a reply in the
# wrong one: a stray loanword in a long answer, not a sentence -- and not one
# word of five, which is how "Qaysi телефон рақамга" looked.
_FOREIGN_SHARE = 0.15


def _word_alphabets(text: str) -> tuple[int, int]:
    """How many words are written in Latin, and how many in Cyrillic."""
    latin_words = cyrillic_words = 0
    for word in text.split():
        latin = len(_LATIN.findall(word))
        cyrillic = len(_CYRILLIC.findall(word))
        if latin + cyrillic < 3:
            continue
        if latin > cyrillic:
            lowered = word.lower()
            if any(lowered.startswith(name) for name in _LATIN_ALLOWED):
                continue
            latin_words += 1
        else:
            cyrillic_words += 1
    return latin_words, cyrillic_words


# English the model falls back to when it cannot place a patient: "Hi.
# What do you need help with?" to "Nmagap", "Hello." in front of Russian. No
# reply to this inbox is meant to carry any of them.
_ENGLISH_WORDS = frozenset(
    {
        "hello",
        "hi",
        "hey",
        "how",
        "can",
        "help",
        "you",
        "your",
        "what",
        "the",
        "please",
        "thanks",
        "thank",
        "sorry",
        "welcome",
        "need",
        "would",
        "like",
        "we",
        "are",
    }
)
# One word spelled in both alphabets: "келishingiz", "Темуr".
_MIXED_WORD = re.compile(r"(?=\w*[A-Za-z])(?=\w*[Ѐ-ӿ])\w+")


def writes_english(message: str) -> bool:
    """Whether the patient wrote in English -- then an English reply is right."""
    words = re.findall(r"[a-z]+", message.lower())
    return sum(1 for word in words if word in _ENGLISH_WORDS) >= 2


def reply_fits(reply: str, script: str, *, allow_english: bool = False) -> bool:
    """Whether the assistant's reply is written in the language and alphabet
    it was told to use."""
    text = _NOT_WORDS.sub(" ", reply)
    if _MIXED_WORD.search(text):
        return False
    if not allow_english and any(
        word in _ENGLISH_WORDS for word in re.findall(r"[a-z]+", text.lower())
    ):
        return False
    latin, cyrillic = _word_alphabets(text)
    words = latin + cyrillic
    if not words:
        return True
    foreign = cyrillic if script == "uz-latn" else latin
    if foreign >= 2 or foreign / words > _FOREIGN_SHARE:
        return False
    if script == "uz-latn":
        return True
    lowered = text.lower()
    if script == "ru":
        return not any(letter in lowered for letter in _UZBEK_CYRILLIC) and (
            _cyrillic_language(lowered) == "ru" or len(lowered.split()) < 4
        )
    return _cyrillic_language(lowered) == "uz-cyrl"
