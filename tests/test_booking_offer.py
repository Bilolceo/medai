"""app.services.booking_offer: the offer to book is made once a conversation.

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
        # Dry run, 2026-10-16: the call-back offer made a second time.
        (
            "Bu operatsiya taxminan 1000 dollar atrofida. Aniq summani doktor ko'rikdan keyin "
            "aytadi. Xohlasangiz ism va telefon raqamingizni qoldiring, operator qulay vaqtda "
            "qo'ng'iroq qiladi.",
            "Bu operatsiya taxminan 1000 dollar atrofida. Aniq summani doktor ko'rikdan keyin "
            "aytadi.",
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


@pytest.mark.parametrize(
    "turn",
    [
        "Ismingiz va telefoningizni qoldiring, operator qulay vaqtda qo‘ng‘iroq qiladi.",
        "Исм ва телефонингизни қолдирсангиз, оператор қўнғироқ қилади.",
        "Если хотите записаться, оставьте имя, номер телефона — оператор свяжется.",
    ],
)
def test_the_call_back_offer_in_its_other_wordings_is_seen(turn: str) -> None:
    """Simulation, 2026-10-16: each of these went out twice in one chat."""
    assert booking_offer.offered_before([turn])


def test_an_offer_already_made_is_seen() -> None:
    assert booking_offer.offered_before(
        ["Va alaykum assalom.", "Doktor ko'rikda aytadi. Qabulga yozilmoqchimisiz?"]
    )
    # The doctor's own wording, as a statement rather than a question.
    assert booking_offer.offered_before(
        [
            "Batafsil ma'lumot kerak bo'lsa, ismingiz va raqamingizni qoldiring — operator "
            "qulay vaqtda qo'ng'iroq qiladi."
        ]
    )
    assert not booking_offer.offered_before(["Doktor qabuli 300 000 soʻm."])


@pytest.mark.parametrize(
    ("turns", "no"),
    [
        (["Yo'q, rahmat"], True),
        (["Kirak mas"], True),
        (["Hozircha bormayman"], True),
        (["Нет, не надо"], True),
        (["Qabul narxi qancha?"], False),
        (["Rahmat"], False),
    ],
)
def test_a_patient_who_said_no(turns: list[str], no: bool) -> None:
    assert booking_offer.declined(turns) is no


def test_a_prescription_written_before_is_not_asking_to_book() -> None:
    """Live, 2026-10-16: "dori yozib bergandi" (the doctor wrote a
    prescription) was read as "yozib qo'ying" (book me), and the call-back
    offer went out a second time."""
    assert not booking_offer.wants_to_book(
        "Hozirgi vaziyatda nima qilsam buladi",
        ["2 oychala bulib woldi duxtirga borgandim ukkol dori yozib bergandi"],
    )
    assert booking_offer.wants_to_book("Qabulga yozib qo'ying")


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (
            "Tushundim, noxush holat. Men doktor Temurning administratoriman. Doktor qayta "
            "ko‘rishi kerak.",
            "Tushundim, noxush holat. Doktor qayta ko‘rishi kerak.",
        ),
        (
            "Salom, men doktor Temurning administratoriman — qanday yordam kerak?",
            "Qanday yordam kerak?",
        ),
        (
            "Здравствуйте. Я администратор доктора Темура. Доктор занимается этим.",
            "Здравствуйте. Доктор занимается этим.",
        ),
        # Nothing but the introduction: sent as it is rather than nothing.
        ("Men doktor Temurning administratoriman.", "Men doktor Temurning administratoriman."),
    ],
)
def test_a_second_introduction_is_taken_out(reply: str, expected: str) -> None:
    assert booking_offer.drop_intro(reply) == expected


def test_an_introduction_already_made_is_seen() -> None:
    assert booking_offer.introduced_before(["Salom, men doktor Temurning administratoriman."])
    assert not booking_offer.introduced_before(["Doktor qabuli 300 000 soʻm."])


@pytest.mark.parametrize(
    ("message", "hello"),
    [
        ("Salom", True),
        ("Assalomu alaykum aka yaxshimisiz", True),
        ("Здравствуйте", True),
        ("Ассалому алайкум", True),
        ("salom narxi qancha", False),
        ("prostatit davolaysizlarmi", False),
    ],
)
def test_a_bare_hello_has_asked_nothing_yet(message: str, hello: bool) -> None:
    """Simulation, 2026-10-16: "Salom" was answered with the call-back offer;
    the doctor's rule is to answer first, then offer."""
    assert booking_offer.only_hello(message) is hello


def test_the_plain_offer_asking_name_number_and_time_counts_as_the_offer() -> None:
    offer = (
        "Mutaxassisimiz qo'ng'iroq qilib batafsil aytib beradi -- ismingiz, raqamingiz va "
        "qaysi vaqtda qo'ng'iroq qilsak qulay ekanini yozing."
    )
    assert booking_offer.offered_before([offer])


def test_asking_the_time_after_a_number_is_kept() -> None:
    """Once they gave a number, "when shall we ring?" is the follow-up, not a second offer."""
    follow_up = "Rahmat, Jasur. Qaysi vaqtda qo'ng'iroq qilsak qulay?"
    assert booking_offer.strip(follow_up) == follow_up
