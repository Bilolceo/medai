"""app.services.booking_offer: an appointment is offered only when asked for.

The replies below are the model's own, from dry runs against the live
prompt on 2026-10-14.
"""

import pytest

from app.services import booking_offer


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (
            "Assalomu alaykum. Dorilar faqat doktor ko‘rigidan keyin belgilanadi. "
            "Qabulga yozilmoqchimisiz?",
            "Assalomu alaykum. Dorilar faqat doktor ko‘rigidan keyin belgilanadi.",
        ),
        (
            "Kechirasiz, dori ayta olmaymiz. Xohlasangiz doktor ko‘rigiga yozib qo‘yaymi?",
            "Kechirasiz, dori ayta olmaymiz.",
        ),
        (
            "Dori haqida maslahat bera olmayman. Xohlasangiz qabulga yozib qo‘yaymi yoki "
            "avval Telegram orqali bog‘lanishni xohlaysizmi (@Temur_Akhmadaliev)?",
            "Dori haqida maslahat bera olmayman.",
        ),
        (
            "Tashxisni faqat ko'rikda aytadi. Agar qabulga yozilmoqchi boʻlsangiz, xabar bering.",
            "Tashxisni faqat ko'rikda aytadi.",
        ),
        ("Записать вас на приём? Врач всё расскажет.", "Врач всё расскажет."),
        (
            "Доктор бу билан шуғулланади. Қабулга ёзиб қўяйми?",
            "Доктор бу билан шуғулланади.",
        ),
        # Live, 2026-10-15: each of these ended a reply to a patient who had
        # not asked to come.
        (
            "Doktor bu bilan shug'ullanadi. Kela olasizmi yoki klinikadan qo'ng'iroq "
            "qilishni xohlaysizmi?",
            "Doktor bu bilan shug'ullanadi.",
        ),
        (
            "Тушунаман. Доктор Темур болани кўриб чиқиши мумкин — кўриб чиқишни хоҳлайсизми?",
            "Тушунаман. Доктор Темур болани кўриб чиқиши мумкин.",
        ),
        (
            "Doktor ko'rikda tushuntiradi. Eringiz ko'rik uchun kelishini xohlaysizmi — "
            "konsultatsiyaga yozaymi? Klinikamiz: +998 70 310 40 40.",
            "Doktor ko'rikda tushuntiradi. Klinikamiz: +998 70 310 40 40.",
        ),
        (
            "Doktor bu bilan shug'ullanadi. Siz ukangizni qabulga yozmoqchimisiz?",
            "Doktor bu bilan shug'ullanadi.",
        ),
    ],
)
def test_an_unasked_offer_is_taken_off(reply: str, expected: str) -> None:
    assert booking_offer.strip(reply) == expected


@pytest.mark.parametrize(
    "reply",
    [
        "Doktor qabuli 300 000 soʻm.",
        "Aniq sababni doktor ko'rikda aytadi.",
        "Ertaga 11:00 ga yozib qo'ydim.",
        # Offering the doctor's Telegram is not offering a booking.
        "Doktor bilan Telegram orqali bog'lanishni xohlaysizmi?",
        # Live, 2026-10-15: cutting the offer left only the greeting, and the
        # patient was sent "Men doktor Temurning administratoriman." alone.
        "Assalomu alaykum. Men doktor Temurning administratoriman. Qabulga yozilmoqchimisiz?",
        # A reply that is nothing but the offer is left alone: silence is worse.
        "Qabulga yozilmoqchimisiz?",
    ],
)
def test_everything_else_is_left_alone(reply: str) -> None:
    assert booking_offer.strip(reply) == reply


def test_a_marker_survives_the_cut() -> None:
    reply = "Buni doktor aytadi. Qabulga yozilmoqchimisiz? [[DOCTOR]]"
    assert booking_offer.strip(reply) == "Buni doktor aytadi. [[DOCTOR]]"


@pytest.mark.parametrize(
    ("message", "history", "wants"),
    [
        ("Qabulga yozilmoqchiman", (), True),
        ("Qachon kelsam bo'ladi?", (), True),
        ("Ertaga bo'sh vaqt bormi?", (), True),
        ("Хочу записаться", (), True),
        ("Qanday qilib buyurtma berish mumkin?", (), True),
        ("O'g'limni ko'rsatsam bo'ladimi?", (), True),
        ("11:00", ("Qabulga yozilmoqchiman",), True),
        ("Prostatitga qaysi dori ichsam bo'ladi?", (), False),
        ("Qabul narxi qancha?", (), False),
        ("Menda nima kasallik?", ("Salom",), False),
    ],
)
def test_who_has_asked_to_book(message: str, history: tuple[str, ...], wants: bool) -> None:
    assert booking_offer.wants_to_book(message, history) is wants
