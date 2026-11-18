"""Dr Sultonbek, 2026-11-18 (relayed by Humoyun): after the complaint two
short questions about it -- "qachondan beri?", "davolanganmisiz, qanday
dori?" -- to draw the patient in; then the name, the place, "tushunarli,
bunday holatlar davolanadi", the times; the price once a time is chosen, half
now and the rest on the day; an operation's price only when it is asked."""

from datetime import datetime
from typing import Any

from app.services import bubbles, promo_images, scripted_flow
from app.services.appointment import CLINIC_TIMEZONE
from app.services.doctor_template import build_settings
from tests.test_operation_cards import CARDS
from tests.test_scripted_flow import FACTS

FLOWS = build_settings("urolog-intake", FACTS)["scripted_flows"]
NOW = datetime(2026, 11, 18, 8, 0, tzinfo=CLINIC_TIMEZONE)
SLOTS = [
    datetime(2026, 11, 18, 15, 0, tzinfo=CLINIC_TIMEZONE),
    datetime(2026, 11, 18, 16, 0, tzinfo=CLINIC_TIMEZONE),
    datetime(2026, 11, 19, 9, 0, tzinfo=CLINIC_TIMEZONE),
]
HANDLES = "Ha, doktor shu bilan shug'ullanadi."
SINCE = "Bu holat qachondan beri bor?"
TREATED = "Oldin bu bo'yicha davolanganmisiz yoki biror dori ichganmisiz?"
NAME = "Ismingiz nima, sizga qanday murojaat qilsam bo'ladi?"
UNDERSTOOD = (
    "Tushunarli. Bunday holatlar doktorda davolanadi, qabulda doktor hammasini batafsil "
    "tushuntiradi."
)


def _talk(messages: list[str], script: str = "uz-latn") -> list[scripted_flow.Plan | None]:
    history: list[dict[str, Any]] = []
    plans: list[scripted_flow.Plan | None] = []
    for message in messages:
        plan = scripted_flow.plan(
            FLOWS, script=script, user_message=message, history=history, now=NOW, slots=SLOTS
        )
        plans.append(plan)
        if plan is None:
            shown = "<model>"
        elif plan.reply is not None:
            shown = plan.reply
        else:
            shown = " ||| ".join(filter(None, (plan.prefix, "<model>", plan.then)))
        history += [
            {"role": "user", "content": message},
            {"role": "assistant", "content": bubbles.flatten(shown)},
        ]
    return plans


def _replies(messages: list[str], script: str = "uz-latn") -> list[str | None]:
    return [p.reply if p is not None else None for p in _talk(messages, script)]


def test_the_two_questions_come_after_the_complaint_then_name_place_and_the_times() -> None:
    replies = _replies(
        [
            "Assalomu alaykum",
            "Ereksiya muammosi bor",
            "2 yildan beri",
            "Yo'q, davolanmaganman",
            "Aziz",
            "Samarqanddanman",
            "ertaga 09:00",
        ]
    )
    assert replies[1] is not None and bubbles.parts(replies[1])[-1] == f"{HANDLES} {SINCE}"
    assert replies[2] == TREATED
    assert replies[3] == NAME
    assert replies[4] == "Aziz, qayerdansiz?"
    offer = replies[5]
    assert offer is not None and offer.startswith(UNDERSTOOD)
    assert "video qo'ng'iroq orqali onlayn" in offer and "Qaysi biri sizga qulay?" in offer
    assert "330 000" not in offer
    booked = replies[6]
    assert booked is not None
    book, price, pay = bubbles.parts(booked)
    assert book.startswith("Yaxshi, ertaga 09:00 ga")
    assert "165 000" in price and price.endswith("Qolgan qismi qabul kuni to'lanadi.")
    assert pay.endswith("sizni ertaga 09:00 ga yozib qo'yaman.")


def test_a_question_at_a_question_is_answered_and_the_next_step_follows() -> None:
    # Never the same question twice in a row (the doctor, 2026-11-11).
    plans = _talk(["Prostatit bor", "Bu davolanadimi?", "Dori bilanmi?"])
    first, second = plans[1], plans[2]
    assert first is not None and first.reply is None and first.then == TREATED
    assert second is not None and second.then == NAME


def test_answers_given_unasked_are_not_asked() -> None:
    replies = _replies(
        [
            "Assalomu alaykum, men Bobur, Namangandanman, tez tugatib qo'yaman 3 yildan beri, "
            "hech davolanmaganman"
        ]
    )
    assert replies[0] is not None and replies[0].startswith("Va alaykum") is True
    assert SINCE not in replies[0] and TREATED not in replies[0]
    assert "Bu masala bo'yicha doktor bilan video" in replies[0]


def test_no_more_questions_after_a_no_thank_you() -> None:
    plans = _talk(["Prostatit bor", "Hozircha kerak emas"])
    assert plans[-1] is None


def test_the_operation_price_asked_at_a_question_is_said_and_the_sequence_goes_on() -> None:
    replies = _replies(["Protez haqida ma'lumot bering", "Narxi qancha?"])
    assert replies[-1] == TREATED or (replies[-1] or "").endswith(TREATED)


def test_after_an_emergency_the_sequence_stays_out() -> None:
    plans = _talk(["Moyagim qattiq og'riyapti, shishib ketdi", "Qachon kelay?"])
    assert plans == [None, None]


def test_the_price_asked_at_a_question_brings_the_price_and_the_next_question() -> None:
    plans = _talk(["Prostatit bor", "Konsultatsiya narxi qancha?"])
    last = plans[-1]
    # No price card in these facts: the price in words, then the next question.
    assert last is not None and last.reply is not None
    assert last.reply.endswith(TREATED) and "330 000" in last.reply


def test_steroids_get_their_own_second_question() -> None:
    replies = _replies(["Steroid kurs qilganman, testosteron tushdi", "3 oy oldin"])
    assert replies[1] == "Hozir biror dori yoki gormon ichyapsizmi?"


def test_an_operation_is_answered_with_we_do_such_operations() -> None:
    replies = _replies(
        ["Varikotsele operatsiyasi kerak", "1 yildan beri", "yo'q", "Sardor", "Buxoro"]
    )
    assert replies[-1] is not None and replies[-1].startswith(
        "Tushunarli. Bizda bunday operatsiyalar qilinadi"
    )


def test_the_name_given_with_the_complaint_is_not_asked_again() -> None:
    replies = _replies(["Men Bobur, prostatit bor", "yarim yil", "antibiotik ichganman"])
    assert replies[2] == "Bobur, qayerdansiz?"


def test_cyrillic_questions() -> None:
    replies = _replies(["Простатит бор", "1 йил"], "uz-cyrl")
    assert replies[0] is not None and replies[0].endswith("Бу ҳолат қачондан бери бор?")
    assert replies[1] == "Олдин бу бўйича даволанганмисиз ёки бирор дори ичганмисиз?"


# --- an operation's card: only when its price is asked --------------------------


def _cards(message: str, patient: list[str] | None = None) -> list[str]:
    found = promo_images.operations_asked(
        CARDS, user_message=message, own_turns=[], patient_turns=patient or []
    )
    return [str(c["topic"]) for c in found]


def test_a_complaint_alone_brings_no_operation_card() -> None:
    assert _cards("Ereksiya muammosi bor") == []
    assert _cards("Varikotsele operatsiyasi kerak") == []
    assert _cards("Farzand bo'lmayapti") == []


def test_the_price_asked_brings_the_operation_card() -> None:
    assert _cards("Falloprotez narxi qancha?") == ["op:falloprotez"]
    assert _cards("Varikotsele operatsiyasi necha pul?") == ["op:varikosele"]
    # "Operatsiya narxi?" then "falloprotez": the price was the question before.
    assert _cards("Falloprotez", ["Operatsiya narxi qancha?"]) == ["op:falloprotez"]


def test_the_price_after_an_operation_was_named_is_that_operation() -> None:
    assert _cards("Narxi qancha?", ["Protez haqida ma'lumot bering"]) == ["op:falloprotez"]
    # The consultation's price asked outright: not the operation's.
    assert _cards("Konsultatsiya narxi qancha?", ["Protez haqida ma'lumot bering"]) == []


def test_the_price_asked_after_an_operation_card_is_that_operation() -> None:
    flows = build_settings(
        "urolog-intake",
        {**FACTS, "PROMO_OP_VARIKOSELE_URL": "u", "OP_PRICE_VARIKOSELE": "4 500 000 so'm"},
    )["scripted_flows"]
    history = [
        {"role": "user", "content": "Varikotsele"},
        {
            "role": "assistant",
            "content": f"{HANDLES} {SINCE}\n\n📎 Varikosele operatsiyasi: narx va paket",
        },
    ]
    plan = scripted_flow.plan(
        flows, script="uz-latn", user_message="narxi qancha dedim", history=history, now=NOW
    )
    assert plan is not None and plan.reply is not None
    assert "4 500 000" in plan.reply and "330 000" not in plan.reply
    assert plan.reply.endswith(TREATED)
