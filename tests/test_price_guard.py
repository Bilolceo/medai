"""What price_guard counts as a sum of money, and which sums it lets through."""

from decimal import Decimal

import pytest

from app.services import price_guard

RULES = [
    "Faqat bemor qabul (konsultatsiya) narxini so'raganda, qabul narxi 300 000 so'm "
    "ekanini ayting.",
    "Bemor amaliyot narxini so'rasa, taxminan 30 mln so'm atrofida ekanini ayting.",
    "Bemor denervatsiya operatsiyasi narxini so'rasa, taxminan $1000 atrofida ekanini ayting.",
]


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("300 000 so'm", 300_000),
        ("300 ming", 300_000),
        ("300.000 сум", 300_000),
        ("300000 soʻm", 300_000),
        ("30 mln", 30_000_000),
        ("30 млн сум", 30_000_000),
        ("30 000 000 so‘m", 30_000_000),
        ("1,5 mln so'm", 1_500_000),
        ("200 000", 200_000),
        ("$500", 500),
        ("300 тыс. сум", 300_000),
        ("300 000 so'mdan", 300_000),
        ("1000 долларов", 1_000),
        ("1000 dollarga", 1_000),
        ("1 000 USD", 1_000),
        ("30 миллионов сум", 30_000_000),
    ],
)
def test_sums_however_they_are_written(text: str, value: int) -> None:
    assert [v for _, v in price_guard.amounts(f"Narxi {text} bo'ladi.")] == [Decimal(value)]


@pytest.mark.parametrize(
    "text",
    [
        "Telefon: +998 71 200 03 93",
        "90 123 45 67 raqamiga yozing",
        "Ish vaqti 09:00-17:00, 2026-10-06 kuni",
        "45 yoshdaman, 3 kundan beri og'riyapti",
        "1000 dan ortiq bemor",
        "Klinika metrodan 5 km uzoqda",
        "Umumiy summa ko'rikdan keyin aytiladi",
        "[[BOOK:2026-10-06T10:00|Ali Valiyev|+998901234567|prostatit]]",
        "Aniq summa ko'rikdan keyin belgilanadi.",
    ],
)
def test_what_is_not_money_is_left_alone(text: str) -> None:
    assert price_guard.amounts(text) == []


@pytest.mark.parametrize(
    "reply",
    [
        "Qabul narxi 300 000 so'm.",
        "Qabul 300 ming so'm turadi.",
        "Amaliyot taxminan 30 mln so'm atrofida.",
        "Операция стоит примерно 30 000 000 сум.",
        "Qabul 300 000, amaliyot esa taxminan 30 mln so'm.",
        "Telefon: +998 71 200 03 93, kutamiz.",
        "Denervatsiya taxminan $1000 atrofida.",
        "Денервация стоит примерно 1000 долларов.",
        "Denervatsiya narxi 1 000 USD atrofida.",
    ],
)
def test_the_sums_the_rules_give_pass_in_any_spelling(reply: str) -> None:
    assert price_guard.check(reply, RULES) == []


@pytest.mark.parametrize(
    ("reply", "caught"),
    [
        ("Buyrak UZI 200 000 so'm.", ["200 000 so'm"]),
        ("PSA 150.000 сум, qabul 300 000 so'm.", ["150.000 сум"]),
        ("Mayli, 20 mln ga qilib beramiz.", ["20 mln"]),
        ("Операция стоит 5 млн.", ["5 млн."]),
        ("Qabul 250 000.", ["250 000"]),
        ("Denervatsiya $1500 bo'ladi.", ["$1500"]),
        ("Mayli, 800 dollarga kelishamiz.", ["800 dollarga"]),
    ],
)
def test_a_sum_no_rule_gives_is_caught(reply: str, caught: list[str]) -> None:
    assert price_guard.check(reply, RULES) == caught


def test_with_no_rules_every_sum_is_caught() -> None:
    assert price_guard.check("Qabul narxi 300 000 so'm.", []) == ["300 000 so'm"]


@pytest.mark.parametrize(
    "message",
    [
        "Qabul narxi qancha?",
        "Konsultatsiya necha pul?",
        "doktorga ko'rinish qancha turadi",
        "denervatsiya narxi",
        "Денервация операцияси нархи қанча?",
        "Сколько стоит денервация?",
        "Цена операции?",
        "Operatsiyani 20 mln ga qilib bering",
        "Chegirma bormi?",
        "How much is the consultation?",
        "Kansultatsiya nechpul",
        "Shu newpul implant",
        "Ака хамси бўлиб қанча расход кетади",
    ],
)
def test_a_patient_asking_a_price(message: str) -> None:
    assert price_guard.asks_price(message)


@pytest.mark.parametrize(
    "message",
    [
        "Denervatsiya operatsiyasini qilasizlarmi?",
        "Denervatsiya qanday o'tadi, og'riqli bo'ladimi?",
        "Tez bo'shalish bo'yicha shifokorga yozilmoqchiman",
        "Assalomu alaykum",
        "Ertaga qabulga yozilsam bo'ladimi?",
        "Ismim Ali, 90 123 45 67, prostatit bo'yicha",
        "Manzilingiz qayerda?",
        "Ish vaqtingiz qanday?",
    ],
)
def test_a_patient_not_asking_one(message: str) -> None:
    assert not price_guard.asks_price(message)


@pytest.mark.parametrize(
    "reply",
    [
        "UZI 200к",
        "₽5000",
        "€800",
        "UZI 200 тыс",
        "Analiz 80 ming",
        "Narxi 25-35 mln",
        "200 000сум",
        "1.000.000 so'm",
        "ikki yuz ming so'm",
        "двести тысяч сум",
        "o'ttiz million",
        "полтора миллиона",
    ],
)
def test_found_by_the_audit_however_it_is_written(reply: str) -> None:
    assert price_guard.check(reply, RULES)


@pytest.mark.parametrize(
    "reply",
    [
        "Tel: +998 (71) 200-03-93",
        "Natija 3-5 kunda tayyor bo'ladi, 1-2 soat davom etadi.",
        "100% tiklanish kafolatlanmaydi",
        "Chilonzor 1-kvartal, 15-uy, 2-qavat, kabinet 205",
        "Shifokor 20 yillik tajribaga ega",
        "PSA 4 ng/ml dan yuqori bo'lsa",
        "Bir kun, ikki hafta yoki uch marta -- yuz foiz o'n kun ichida",
        "Ming rahmat!",
        "[[CALLBACK:+998 90 123 45 67|qabul|ertaga 10 da]]",
        "0,3 mln so'm",  # the consultation, written another way
        "300 000 UZS",
    ],
)
def test_left_alone_by_the_audit(reply: str) -> None:
    assert price_guard.check(reply, RULES) == []


@pytest.mark.parametrize(
    ("message", "previous", "asks"),
    [
        ("Operatsiya-chi?", "Qabul narxi qancha?", True),
        ("operatsiyachi", "Qabul narxi qancha?", True),
        ("А денервация?", "Сколько стоит приём?", True),
        ("Имплант хақида?", "Ака хамси бўлиб қанча расход кетади", True),
        # Simulation, 2026-10-16: a five-word Russian follow-up.
        ("а операция при раннем семяизвержении?", "сколько стоит консультация", True),
        ("Rahmat", "Qabul narxi qancha?", False),
        ("Yaxshi, ertaga yozilaman", "Qabul narxi qancha?", False),
        ("Denervatsiya ham qilasizlarmi?", "Qabul narxi qancha?", False),
        ("Operatsiya-chi?", None, False),
        ("Operatsiya-chi?", "Salom", False),
    ],
)
def test_a_short_follow_up_to_a_price_question_asks_one_too(
    message: str, previous: str | None, asks: bool
) -> None:
    assert price_guard.asks_price(message, previous) is asks


def test_a_follow_up_to_a_follow_up_still_asks_the_price() -> None:
    """Simulation, 2026-10-16: "narxi qancha" / "qabul" / "operatsiyachi" --
    the third message was answered without the rule's price because only the
    message right before it ("qabul") was looked at."""
    assert price_guard.asks_price("operatsiyachi", ["narxi qancha", "qabul"])
    assert price_guard.asks_price("implant-chi", ["narxi qancha", "qabul", "operatsiyachi"])
    assert not price_guard.asks_price("rahmat", ["narxi qancha", "qabul"])
