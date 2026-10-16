"""Which sums of money a reply may say: only the ones the clinic's rules give.

The persona forbids prices, and the clinic's rules may name a few it does
want said ("qabul 300 000 so'm", "amaliyot taxminan 30 mln"). Told that in
the prompt, the model still read leave to quote one price as leave to quote
the knowledge base's: asked "UZI narxi qancha?", it answered with the
uploaded price list two times in eight. So it is checked here, by pattern,
the way app.services.medical_safety checks a prescription.

What counts as a sum: a number with a unit of money or a multiplier after it
("300 ming", "30 mln", "250 000 so'm", "$500"), or a number grouped in
thousands that ends in zeros ("200 000"). That is how prices are written
here, and it leaves alone what else a reply carries numbers for: telephone
numbers ("+998 71 200 03 93"), times, dates, ages.

A sum is allowed when the same amount -- not the same spelling -- appears in
one of the clinic's rules: "300 ming" in a reply matches "300 000 so'm" in a
rule.
"""

import re
from collections.abc import Iterable, Sequence
from decimal import Decimal, InvalidOperation

_MULTIPLIERS = {
    "ming": 1_000,
    "минг": 1_000,
    "тыс": 1_000,
    "тысяч": 1_000,
    "k": 1_000,
    "к": 1_000,
    "mln": 1_000_000,
    "million": 1_000_000,
    "миллион": 1_000_000,
    "млн": 1_000_000,
    "mlrd": 1_000_000_000,
    "milliard": 1_000_000_000,
    "млрд": 1_000_000_000,
    "миллиард": 1_000_000_000,
}
# Words take their endings ("so'mdan", "долларов", "millionga", "тысячи");
# "k", "sum" and the codes do not, so "5 km" and "summa" stay words.
_CURRENCY = (
    r"(?:so['‘ʻ’`]?m|сўм|сум|dollar|доллар|руб|rubl)\w*" r"|(?:sum|uzs|usd|eur)(?!\w)|\$|€|₽"
)
_LETTERS = ("k", "к")
_MULTIPLIER = (
    "(?:"
    + "|".join(
        sorted((re.escape(w) for w in _MULTIPLIERS if w not in _LETTERS), key=len, reverse=True)
    )
    + r")\w*|[kк](?!\w)"
)
_NUMBER = r"\d+(?:[ \u00a0\u202f.,]\d{3})*(?:[.,]\d{1,2})?"

# A number followed by a multiplier and/or a currency, or a currency sign in
# front of one.
_WITH_UNIT = re.compile(
    rf"(?<![\w+])(?P<cur>\$|€|₽)?\s?(?P<num>{_NUMBER})\s*"
    rf"(?:(?P<mul>{_MULTIPLIER})\.?)?\s*(?P<unit>{_CURRENCY})?",
    re.IGNORECASE,
)
# A sum in words, "ikki yuz ming so'm" / "двести тысяч": the rules give theirs
# in digits, so one in words is never theirs, whatever it adds up to.
_NUMBER_WORDS = (
    r"bir|ikki|uch|to['‘ʻ’`]?rt|besh|olti|yetti|sakkiz|to['‘ʻ’`]?qqiz|o['‘ʻ’`]?n|yigirma"
    r"|o['‘ʻ’`]?ttiz|qirq|ellik|oltmish|yetmish|sakson|to['‘ʻ’`]?qson|yuz"
    r"|бир|икки|уч|тўрт|беш|олти|етти|саккиз|тўққиз|ўн|йигирма|ўттиз|қирқ|эллик|юз"
    r"|один|одна|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять|двадцать"
    r"|тридцать|сорок|пятьдесят|сто|двести|триста|четыреста|пятьсот|полтор\w*"
)
_SPELLED = re.compile(
    rf"(?<!\w)(?:(?:{_NUMBER_WORDS})\s+)*(?:{_NUMBER_WORDS})\s+"
    rf"(?:(?:{_MULTIPLIER})\s*(?:{_CURRENCY})?|(?:{_CURRENCY}))",
    re.IGNORECASE,
)
# Never one of the rules' amounts: a sum in words is always caught.
_UNKNOWN = Decimal(-1)
# "200 000" with nothing after it: thousands groups ending in 000.
_BARE = re.compile(r"(?<![\w+\d])\d{1,3}(?:[ \u00a0\u202f.,]\d{3})*[ \u00a0\u202f.,]000(?![\d])")


def _value(number: str, multiplier: str | None) -> Decimal | None:
    digits = re.sub(r"[ \u00a0\u202f]", "", number)
    # "1,5 mln" / "1.5 mln": a decimal separator before one or two digits;
    # "250.000" / "250,000": a thousands separator before three.
    parts = re.split(r"[.,]", digits)
    if len(parts) == 2 and len(parts[1]) in (1, 2):
        text = f"{parts[0]}.{parts[1]}"
    else:
        text = "".join(parts)
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    if multiplier:
        word = multiplier.lower()
        stem = max((w for w in _MULTIPLIERS if word.startswith(w)), key=len)
        value *= _MULTIPLIERS[stem]
    return value


def amounts(text: str) -> list[tuple[str, Decimal]]:
    """Every sum of money in `text`, as written and as a number (-1 for one
    written in words)."""
    found: list[tuple[str, Decimal]] = []
    taken: list[tuple[int, int]] = []
    for match in _WITH_UNIT.finditer(text):
        if not (match.group("cur") or match.group("mul") or match.group("unit")):
            continue
        value = _value(match.group("num"), match.group("mul"))
        if value is not None:
            found.append((match.group(0).strip(), value))
            taken.append(match.span())
    for match in _SPELLED.finditer(text):
        found.append((match.group(0).strip(), _UNKNOWN))
        taken.append(match.span())
    for match in _BARE.finditer(text):
        if any(start <= match.start() < end for start, end in taken):
            continue
        value = _value(match.group(0), None)
        if value is not None:
            found.append((match.group(0), value))
    return found


def allowed(rules: Iterable[str]) -> set[Decimal]:
    """The sums the clinic's rules name."""
    return {value for rule in rules for _, value in amounts(rule)}


def check(reply: str, rules: Sequence[str]) -> list[str]:
    """The sums in `reply` that no rule gives, as written. Empty when fine."""
    permitted = allowed(rules)
    return [written for written, value in amounts(reply) if value not in permitted]


# Words a patient asking a price uses. "Denervatsiya qilasizlarmi?" is not
# one, and the live model answered it with the price two times in three
# although the rule said only when asked -- so a sum is let through only
# when the message being answered asks for one.
_ASKS_PRICE = re.compile(
    r"narx|нарх|qancha|қанча|ne\w{0,3}\s*pul|не\w{0,3}\s*пул|\bpul|\bпул|xarajat|харажат|расход|to['‘ʻ’`]?lov|тўлов"
    r"|сколько|стоим|стоит|цен[аыуе]|поч[её]м|chegirma|чегирма|скидк|arzon|арзон"
    r"|qimmat|қиммат|price|cost|how\s+much|dollar|доллар|so['‘ʻ’`]m|сўм|\bсум|mln|млн|\$",
    re.IGNORECASE,
)


# "Operatsiya-chi?" / "А операция?" right after a price question asks the
# same thing about something else, and has no price word of its own.
# "Rahmat" after a price question closes it rather than naming a service.
_ACK = re.compile(
    r"^\W*(?:rahmat|рахмат|раҳмат|xop|xo['‘ʻ’`]?p|хоп|хўп|ok|ок|ha|ҳа|ха|yaxshi|яхши|mayli|майли"
    r"|tushundim|тушундим|спасибо|понятно|хорошо|ладно)\b",
    re.IGNORECASE,
)
_FOLLOW_UP = re.compile(r"chi\b|чи\b|^\s*[аa]\s", re.IGNORECASE)


def asks_price(message: str, previous: str | Sequence[str] | None = None) -> bool:
    """Whether the patient's message asks what something costs.

    `previous` is the patient's message before it, or the last few: a short
    follow-up to a price question ("Operatsiya-chi?") asks one too -- and so
    does a follow-up to a follow-up ("narxi qancha" / "qabul" /
    "operatsiyachi"), which is why it looks back up to three messages.
    """
    if _ASKS_PRICE.search(message):
        return True
    earlier = [previous] if isinstance(previous, str) else list(previous or [])[-3:]
    words = len(message.split())
    short_follow_up = _FOLLOW_UP.search(message) or (words <= 2 and not _ACK.search(message))
    return bool(
        words <= 6 and short_follow_up and any(_ASKS_PRICE.search(text) for text in earlier)
    )


SAFE_REPLIES = {
    "uz-latn": "Aniq narxni klinikaga qo'ng'iroq qilib bilsangiz bo'ladi.",
    "uz-cyrl": "Аниқ нархни клиникага қўнғироқ қилиб билсангиз бўлади.",
    "ru": "Точную цену скажут по телефону клиники.",
}
