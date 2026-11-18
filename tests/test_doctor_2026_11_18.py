"""Dr Sultonbek, 2026-11-18 (relayed by Humoyun): after the complaint two
short questions about it -- "qachondan beri?", "davolanganmisiz, qanday
dori?" -- to draw the patient in; then the name, the place, "tushunarli,
bunday holatlar davolanadi", the times; the price once a time is chosen, half
now and the rest on the day; an operation's price only when it is asked."""

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
