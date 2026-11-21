"""Dr Sultonbek, 2026-11-18 (relayed by Humoyun): after the complaint two
short questions about it -- "qachondan beri?", "davolanganmisiz, qanday
dori?" -- to draw the patient in; then the name, the place, "tushunarli,
bunday holatlar davolanadi", the times; the price once a time is chosen, half
now and the rest on the day; an operation's price only when it is asked."""

import json
from datetime import datetime, time
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
FAR = (
    "Uzoqda bo'lsangiz, doktordan onlayn video konsultatsiya so'rab beraman, kelib "
    "o'tirishingiz shart bo'lmaydi. Klinikaga o'zingiz kelmoqchimisiz?"
)
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
            "Onlayn bo'lsin",
            "ertaga 09:00",
        ]
    )
    assert replies[1] is not None and bubbles.parts(replies[1])[-1] == f"{HANDLES} {SINCE}"
    assert replies[2] == TREATED
    assert replies[3] == NAME
    assert replies[4] == "Aziz, qayerdansiz?"
    offer = replies[5]
    assert offer == f"{UNDERSTOOD} {FAR}"
    assert "330 000" not in offer
    assert replies[6] is not None and replies[6].endswith("Qaysi biri sizga qulay?")
    booked = replies[7]
    assert booked is not None
    book, price, pay = bubbles.parts(booked)
    assert book == "Yaxshi, ertaga 09:00 ga onlayn konsultatsiyaga yozib qo'yishim mumkin."
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
    assert replies[0].endswith(FAR)


def test_no_more_questions_after_a_no_thank_you() -> None:
    plans = _talk(["Prostatit bor", "Hozircha kerak emas"])
    assert plans[-1] is None


def test_the_operation_price_asked_at_a_question_is_said_and_the_sequence_goes_on() -> None:
    replies = _replies(["Protez haqida ma'lumot bering", "Narxi qancha?"])
    assert replies[-1] == TREATED or (replies[-1] or "").endswith(TREATED)


def test_after_an_emergency_the_sequence_stays_out() -> None:
    history = [
        {"role": "user", "content": "Moyagim qattiq og'riyapti, shishib ketdi"},
        {"role": "assistant", "content": "Bu kechiktirib bo'lmaydigan holat, bugunoq keling."},
    ]
    plan = scripted_flow.plan(
        FLOWS, script="uz-latn", user_message="Qachon kelay?", history=history, now=NOW
    )
    assert plan is None


def test_ordinary_answers_do_not_stop_the_sequence() -> None:
    # Code review, 2026-11-18: "hozircha" and "birdan" are answers here.
    replies = _replies(["Prostatit bor", "Birdan boshlandi", "Hozircha davolanmaganman"])
    assert replies[1] == TREATED
    assert replies[2] == NAME


def test_an_age_or_a_wish_is_not_an_anamnesis_answer() -> None:
    replies = _replies(["Мне 30 лет, слабая эрекция"], "ru")
    assert replies[0] is not None and replies[0].endswith("Как давно это беспокоит?")
    replies = _replies(["Prostatitdan davolanmoqchiman", "1 yildan beri"])
    assert replies[1] == TREATED


def test_both_answered_at_the_first_question_goes_on_to_the_name() -> None:
    replies = _replies(["Prostatit bor", "2 yildan beri, hech davolanmaganman"])
    assert replies[1] == NAME


def test_a_complaint_price_is_the_consultation_not_the_operation() -> None:
    plans = _talk(["Ereksiya muammosi bor", "Varikotsele ham bor, narxi qancha?"])
    last = plans[-1]
    assert last is not None and last.reply is not None and "4 500 000" not in last.reply


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
    # The owner, 2026-11-21: "inplant narxini qancha ?" got the consultation card.
    for text in ("inplant narxini qancha ?", "Implant narxi", "Имплант нархи қанча?"):
        assert _cards(text, ["assalomu alekum"]) == ["op:falloprotez"], text


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
        {"role": "user", "content": "Operatsiya narxi qancha?"},
        {"role": "assistant", "content": "Operatsiyalar: ... Qaysi biri kerak?"},
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


# --- outside Tashkent: asked, never told "kelish shart emas" (Humoyun, 2026-11-18) ---


def _to_place(*after: str, place: str = "Qozog'istondan") -> list[str | None]:
    return _replies(["Ereksiya muammosi", "1 yildan beri", "yo'q", "Aziz", place, *after])


def test_far_away_is_asked_whether_coming() -> None:
    replies = _to_place()
    assert replies[-1] == f"{UNDERSTOOD} {FAR}"
    assert "kelish shart emas" not in (replies[-1] or "")


def test_far_away_coming_is_booked_at_the_clinic() -> None:
    replies = _to_place("Ha, kelaman", "ertaga 09:00")
    assert replies[-2] is not None and replies[-2].endswith("Qaysi biri sizga qulay?")
    assert replies[-1] is not None and "klinikaga qabulga" in replies[-1]


def test_far_away_yes_is_coming() -> None:
    replies = _to_place("Ha", "ertaga 09:00")
    assert replies[-1] is not None and "klinikaga qabulga" in replies[-1]


def test_far_away_no_is_online() -> None:
    replies = _to_place("Yo'q, uzoqman", "ertaga 09:00")
    assert replies[-2] is not None and replies[-2].endswith("Qaysi biri sizga qulay?")
    assert replies[-1] is not None and "onlayn konsultatsiyaga" in replies[-1]


def test_when_can_i_come_gets_the_times_not_a_no_need() -> None:
    # The screenshot, 2026-11-18: "Qozog'istondan / Qachon kelsam bo'ladi?".
    replies = _to_place(place="Qozog'istondan. Qachon kelsam bo'ladi?")
    assert replies[-1] is not None
    assert "kelish shart emas" not in replies[-1]
    assert replies[-1].endswith("Qaysi biri sizga qulay?")
    booked = _to_place("ertaga 09:00", place="Qozog'istondan. Qachon kelsam bo'ladi?")[-1]
    assert booked is not None and "klinikaga qabulga" in booked


def test_tashkent_keeps_its_choice() -> None:
    replies = _to_place(place="Toshkent")
    assert replies[-1] is not None and replies[-1].endswith(
        "Klinikaga kelib qabulga yozilasizmi yoki doktor bilan video qo'ng'iroq orqali onlayn "
        "maslahatlashasizmi?"
    )


def test_the_screenshot_in_cyrillic_kazakhstan_when_can_i_come() -> None:
    replies = _replies(
        [
            "Эрекция муаммоси бор",
            "1 йилдан бери",
            "Йўқ",
            "Азиз",
            "Козохистондан\nКачон келсам болади?",
            "эртага 9 да",
        ],
        "uz-cyrl",
    )
    assert replies[4] is not None and "келиш шарт эмас" not in replies[4]
    assert replies[4].endswith("Қайси бири сизга қулай?")
    assert replies[5] is not None and "клиникага қабулга" in replies[5]


# --- code review, 2026-11-18 ----------------------------------------------------


def test_a_clinic_in_a_complaint_is_not_a_wish_to_come() -> None:
    replies = _replies(
        [
            "Prostatit bor",
            "1 yildan beri",
            "Ha, boshqa klinikada davolanganman",
            "Aziz",
            "Qozog'istondan",
        ]
    )
    assert replies[-1] == f"{UNDERSTOOD} {FAR}"


def test_yes_online_is_online() -> None:
    replies = _to_place("Ha, onlayn bo'lsin", "ertaga 09:00")
    assert replies[-1] is not None and "onlayn konsultatsiyaga" in replies[-1]


def test_a_real_no_after_the_question_is_not_offered_times() -> None:
    replies = _to_place("Yo'q, kerak emas")
    assert replies[-1] is None or "Qaysi biri sizga qulay?" not in replies[-1]


def test_a_place_with_a_price_question_answers_it_and_goes_on() -> None:
    plans = _talk(
        ["Ereksiya muammosi", "1 yildan beri", "yo'q", "Aziz", "Samarqanddan. Narxi qancha?"]
    )
    last = plans[-1]
    # The price answered (in words here: no card in these facts), then the
    # question -- the place is not asked again.
    assert last is not None and last.reply is not None
    assert "330 000" in last.reply
    assert last.reply.endswith("Klinikaga o'zingiz kelmoqchimisiz?")


def test_coming_with_no_free_times_says_so() -> None:
    history: list[dict[str, Any]] = []
    plan = None
    for message in ["Ereksiya muammosi", "1 yil", "yo'q", "Aziz", "Buxoro. Qachon kelsam bo'ladi?"]:
        plan = scripted_flow.plan(
            FLOWS, script="uz-latn", user_message=message, history=history, now=NOW, slots=[]
        )
        shown = plan.reply if plan is not None and plan.reply else "<model>"
        history += [
            {"role": "user", "content": message},
            {"role": "assistant", "content": bubbles.flatten(shown)},
        ]
    assert plan is not None and plan.reply is not None
    assert "bo'sh vaqt qolmadi" in plan.reply


def test_a_place_with_another_question_lets_the_model_answer_it() -> None:
    plans = _talk(["Ereksiya muammosi", "1 yil", "yo'q", "Aziz", "Samarqanddan. Manzil qayerda?"])
    last = plans[-1]
    assert last is not None and last.reply is None and last.then is not None
    assert last.then.endswith(FAR)


# --- simulated patients, 2026-11-18 ---------------------------------------------


def test_sending_money_is_not_going_to_the_clinic() -> None:
    assert not scripted_flow._WANTS_VISIT.search("165 mingni yuboraman, karta raqamni tashlang")
    assert scripted_flow._WANTS_VISIT.search("klinikaga boraman")
    assert scripted_flow._WANTS_VISIT.search("Клиникага ўзим бораман")


def test_a_name_said_as_deb_murojaat_qiling_is_taken() -> None:
    found = scripted_flow._GIVEN_NAME.search("Ali deb murojaat qiling")
    assert found is not None and (found.group(1) or found.group(2)) == "Ali"


def test_the_card_number_asked_to_pay_is_no_request_for_a_phone() -> None:
    assert scripted_flow.contact_wish("raqam yuboring pulini tashlab qo'yaman") is None
    assert scripted_flow.contact_wish("telefonimni yuboraman") is None
    assert scripted_flow.contact_wish("doktorning raqamini bering") == "doctor"


def test_the_model_s_own_booking_and_free_times_are_dropped() -> None:
    answer = "Ha, bo'ladi. Ali, 10:30 ga yozib qo'ydim. Bugun 09:00-16:00 oralig'ida bo'ladi."
    assert scripted_flow.drop_claims(answer) == "Ha, bo'ladi."


def test_a_call_now_after_hours_gets_the_hours() -> None:
    late = datetime(2026, 11, 18, 21, 0, tzinfo=CLINIC_TIMEZONE)
    history: list[dict[str, Any]] = []
    plan = None
    for message in ["Ereksiya, 90 123 45 67 qo'ng'iroq qilinglar", "hozir qo'ng'iroq qilinglar"]:
        plan = scripted_flow.plan(
            FLOWS, script="uz-latn", user_message=message, history=history, now=late
        )
        shown = plan.reply if plan is not None and plan.reply else "<model>"
        history += [
            {"role": "user", "content": message},
            {"role": "assistant", "content": shown},
        ]
    assert plan is not None and plan.reply is not None
    assert "hozir" not in plan.reply.lower() and "09:00" in plan.reply


def test_offering_one_s_own_number_is_no_request_for_one() -> None:
    assert scripted_flow.contact_wish("ertaga bo'ladimi nomer qoldiraymi") is None
    assert scripted_flow.contact_wish("номеримни ёзиб қўяманми ёки сизда борми?") is None


def test_a_time_typed_without_a_colon() -> None:
    assert scripted_flow.parse_time("juma 1030 qulayroq") == time(10, 30)
    assert scripted_flow.parse_time("narxi 330000 so'm") is None


def test_the_model_s_free_times_are_dropped() -> None:
    assert scripted_flow.drop_claims("Ислом, 10:00 ёки 14:00 га тушишингиз мумкин.") == ""


def test_the_gynaecology_line_is_said_once() -> None:
    plans = _talk(["Hayzim 2 oydan beri kelmayapti", "Tushundim rahmat ginekologga yozaman"])
    assert plans[0] is not None and plans[0].reply is not None and "ginekolog" in plans[0].reply
    assert plans[1] is None


def test_one_cyrillic_letter_does_not_switch_the_alphabet() -> None:
    from app.services.language import reply_script

    assert (
        reply_script("men baribir ko‘rikkа kelmoqchiman to‘lovni qilib chekni yuboraman")
        == "uz-latn"
    )
    assert (
        reply_script("ertaga 10 30 qulay qanday qilib pul yuboraman кейин qilib berasizmi")
        == "uz-latn"
    )
    assert reply_script("Простатит бор, эртага келаман") == "uz-cyrl"


def test_the_evening_offer_names_no_price_before_the_time() -> None:
    for texts in FLOWS["intake"]["texts"].values():
        assert "990" not in texts["evening_ok"].format(price="990 000 so'm")


# --- the owner's test, 2026-11-19 ------------------------------------------------


def test_the_card_that_did_not_show_is_sent_again() -> None:
    day = {"topic": "day", "title": "Kunduzgi konsultatsiya: narx va tartib", "url": "d"}
    own = ["Konsultatsiya narxi va tartibi rasmda.\n\n📎 Kunduzgi konsultatsiya: narx va tartib"]
    again = promo_images.resend_last(
        [day],
        user_message="Бошкаттан юборинг корнмаяпти",
        own_turns=own,
        reply="Расмни қайта юбордим.",
    )
    assert again == day
    assert (
        promo_images.resend_last([day], user_message="rahmat", own_turns=own, reply="Mayli.")
        is None
    )


def test_chek_alone_asks_for_the_receipt_not_says_it_came() -> None:
    assert scripted_flow._PAID.search("Чек")
    assert not scripted_flow._PAID.search("chekni keyin yuboraman")


def test_cyrillic_uzbek_without_uzbek_letters_is_uzbek() -> None:
    from app.services.language import reply_script

    assert (
        reply_script("Нечга келсам болади? Йоки онлаен консультацияни нечга килсак болади")
        == "uz-cyrl"
    )
    assert reply_script("Сколько стоит консультация?") == "ru"


# --- local run, 2026-11-19 (no OpenAI) -----------------------------------------


def test_how_long_it_has_been_is_not_when_to_ring() -> None:
    assert scripted_flow._when("3 yil") is None
    assert scripted_flow._when("yarim yildan beri") is None
    assert scripted_flow._when("ertaga 11 da") == "ertaga 11 da"


def test_when_can_i_come_at_the_times_step_gets_the_times() -> None:
    plans = _talk(["Ereksiya", "1 yil", "yo'q", "Aziz", "Toshkent", "Nechga kelsam bo'ladi?"])
    last = plans[-1]
    assert last is not None and last.reply is not None and "bo'sh vaqtlar bor" in last.reply


def test_the_card_asked_again_after_the_booking() -> None:
    plans = _talk(
        [
            "Ereksiya",
            "1 yil",
            "yo'q",
            "Aziz",
            "Samarqand",
            "onlayn",
            "ertaga 09:00",
            "raqam yuboring pulini tashlab qo'yaman",
        ]
    )
    last = plans[-1]
    assert last is not None and last.reply is not None
    assert last.reply.startswith("To'lov uchun karta:") and "ertaga 09:00" in last.reply


# --- live med.uz.ai chat and the negative run, 2026-11-19 ---------------------

_BOOKED = [
    "Assalomu alaykum",
    "Ereksiya",
    "1 yil",
    "yo'q",
    "Akbar",
    "Xivadan",
    "onlayn",
    "ertaga 09:00",
    "to'ladim",
    "+998 94 571 99 69",
]


def test_a_patient_booked_this_morning_who_greets_again_starts_again() -> None:
    replies = _replies(
        [
            *_BOOKED,
            "Assalomu alaykum",
            "Ereksiya bo'lmayapti",
            "5 yil",
            "yo'q",
            "Serik",
            "Toshkent",
            "O'zim kelaman\nNechida kelsam bo'ladi?",
        ]
    )
    assert replies[9] is not None and "yozilasiz" in replies[9]
    assert replies[10] is not None and "Sizga qanday yordam" in replies[10]
    assert "Men doktor" not in replies[10]
    assert replies[11] is not None and "qachondan beri" in replies[11]
    assert replies[13] is not None and "Ismingiz" in replies[13]
    assert replies[14] is not None and "qayerdansiz" in replies[14].lower()
    assert replies[16] is not None and "bo'sh vaqtlar bor" in replies[16]


def test_a_question_after_the_booking_does_not_start_again() -> None:
    plans = _talk([*_BOOKED, "Bu onlayn konsultatsiyami yoki borishim kerakmi?"])
    assert plans[-1] is None


def test_the_number_corrected_after_the_booking_is_confirmed_again() -> None:
    plans = _talk([*_BOOKED, "+998 90 111 22 33"])
    last = plans[-1]
    assert last is not None and last.reply is not None
    assert "yozilasiz" in last.reply and "CALLBACK:+998 90 111 22 33" in last.reply


def test_an_insult_is_no_name() -> None:
    plans = _talk(["Salom", "Ereksiya", "1 yil", "yo'q", "Ahmoq bot"])
    last = plans[-1]
    assert last is None or last.reply is None or "Ahmoq" not in last.reply


def test_a_closed_day_is_said_closed_not_the_hour_taken() -> None:
    replies = _replies(
        [
            "Salom",
            "Bepushtlik",
            "3 yil",
            "yo'q",
            "Dilshod",
            "Toshkent",
            "klinikaga kelaman",
            "yakshanba 10:00",
        ]
    )
    assert replies[-1] is not None and "band" not in replies[-1]
    assert "bo'sh vaqt qolmagan" in replies[-1]


# --- the operation the model read, however spelt (the owner, 2026-11-21) ---


def _read(**fields: Any) -> Any:
    from app.services import message_reader

    return message_reader.parse(json.dumps(fields))


def test_the_reader_keeps_only_the_doctors_operations() -> None:
    reading = _read(operations=["op:falloprotez", "op:heart", 5, "op:falloprotez"])
    assert reading is not None and reading.operations == ("op:falloprotez",)
    assert _read(operations="op:falloprotez").operations == ()


def test_an_operation_only_the_model_understood_brings_its_card() -> None:
    # No pattern knows these words; the model said which operation.
    message = "jinsiy a'zoga moslama qo'ydirish necha pul turadi"
    assert _cards(message) == []
    found = promo_images.operations_asked(
        CARDS, user_message=message, own_turns=[], named_now=["op:falloprotez"]
    )
    assert [c["topic"] for c in found] == ["op:falloprotez"]


def test_an_operation_only_the_model_understood_is_priced_as_that_operation() -> None:
    flows = build_settings(
        "urolog-intake",
        {**FACTS, "PROMO_OP_FALLOPROTEZ_URL": "u", "OP_PRICE_FALLOPROTEZ": "25 000 000 so'm"},
    )["scripted_flows"]
    history = [
        {"role": "user", "content": "assalomu alekum"},
        {"role": "assistant", "content": "Va alaykum assalom. Sizga qanday yordam bera olamiz?"},
    ]
    message = "moslama qo'ydirish necha pul turadi?"
    plain = scripted_flow.plan(
        flows,
        script="uz-latn",
        user_message=message,
        history=history,
        now=NOW,
        reading=_read(asks_price=True, complaint_kind="urology"),
    )
    read = scripted_flow.plan(
        flows,
        script="uz-latn",
        user_message=message,
        history=history,
        now=NOW,
        reading=_read(asks_price=True, complaint_kind="urology", operations=["op:falloprotez"]),
    )
    named = scripted_flow.plan(
        flows,
        script="uz-latn",
        user_message="Falloprotez narxi qancha?",
        history=history,
        now=NOW,
        reading=_read(asks_price=True, complaint_kind="urology"),
    )
    assert plain is not None and read is not None and named is not None
    # Without the model's word: the consultation price. With it: as if named.
    assert "330 000" in (plain.reply or "")
    assert "330 000" not in (read.reply or "") and not read.cards
    assert read.reply == named.reply


# --- live med.uz.ai chat, 2026-11-21: prices asked mid-sequence ---

_FAR_CHAT = [
    ("user", "Ассалом алейкум"),
    (
        "assistant",
        "Ва алайкум ассалом. Мен доктор Султонбекнинг администраториман, исмим Дилдора.\n\nСизга қандай ёрдам бера оламиз?",  # noqa: E501
    ),
    ("user", "Варик нархи"),
    (
        "assistant",
        "Ҳа, доктор шу билан шуғулланади. Бу ҳолат қачондан бери бор?\n\n📎 Varikosele operatsiyasi: narx va paket",  # noqa: E501
    ),
    ("user", "Канака услугалариз бор озз"),
    (
        "assistant",
        "Бу қабулда уролог-андролог масалалари кўрилади.\n\nОлдин бу бўйича даволанганмисиз ёки бирор дори ичганмисиз?",  # noqa: E501
    ),
    ("user", "Йок"),
    ("assistant", "Исмингиз нима, сизга қандай мурожаат қилсам бўлади?"),
    ("user", "Сардор"),
    ("assistant", "Сардор, қаердансиз?"),
    ("user", "Сирдаредан"),
    (
        "assistant",
        "Тушунарли. Бизда бундай операциялар қилинади, қабулда доктор ҳаммасини батафсил тушунтиради. Узоқда бўлсангиз, доктордан онлайн видео консультация сўраб бераман, келиб ўтиришингиз шарт бўлмайди. Клиникага ўзингиз келмоқчимисиз?",  # noqa: E501
    ),
]


def _far_history() -> list[dict[str, str]]:
    return [{"role": r, "content": c} for r, c in _FAR_CHAT]


def test_the_price_asked_at_the_clinic_question_is_answered() -> None:
    history = _far_history()
    assert "Клиникага ўзингиз келмоқчимисиз" in history[-1]["content"]
    plan = scripted_flow.plan(
        FLOWS,
        script="uz-cyrl",
        user_message="Тез бошаниш даволаш нечпул",
        history=history,
        now=NOW,
        reading=_read(asks_price=True, complaint_kind="urology"),
    )
    assert plan is not None and plan.reply is not None
    # The price: its card, not the same question again.
    assert plan.cards or "330 000" in plan.reply
    assert plan.reply != history[-1]["content"]


def test_an_operation_named_after_its_price_was_asked_is_priced() -> None:
    history = _far_history()
    plan = scripted_flow.plan(
        FLOWS,
        script="uz-cyrl",
        user_message="Денервация",
        history=history,
        now=NOW,
        reading=_read(asks_price=True, complaint_kind="urology", operations=["op:denervatsiya"]),
    )
    assert plan is not None and plan.reply is not None
    assert plan.reply != history[-1]["content"]


def test_an_unnamed_operation_priced_mid_sequence_asks_which() -> None:
    history = _far_history()[:6]
    plan = scripted_flow.plan(
        FLOWS,
        script="uz-cyrl",
        user_message="Энг киммат операция нечпул",
        history=history,
        now=NOW,
        reading=_read(asks_price=True, operation_unnamed=True),
    )
    assert plan is not None and plan.reply is not None
    assert "Қайси бири" in plan.reply or "Qaysi biri" in plan.reply
