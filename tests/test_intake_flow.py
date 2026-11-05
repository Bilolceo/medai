"""Dr Sultonbek's ordinary sequence, in the doctor's order: name, place, the
complaint; then online or the clinic, a free time, the price, half of it paid
ahead, the card and the receipt -- booked here, so the administrator only
checks the receipt and rings 1-2 hours before. Said as written."""

from datetime import datetime
from typing import Any

from app.services import bubbles, scripted_flow
from app.services.appointment import CLINIC_TIMEZONE
from app.services.doctor_template import build_settings
from tests.test_scripted_flow import FACTS

FLOWS = build_settings("urolog-intake", FACTS)["scripted_flows"]
NOW = datetime(2026, 11, 3, 11, 0, tzinfo=CLINIC_TIMEZONE)


def _talk(messages: list[str], script: str = "uz-latn") -> list[str]:
    history: list[dict[str, Any]] = []
    replies: list[str] = []
    for message in messages:
        plan = scripted_flow.plan(
            FLOWS, script=script, user_message=message, history=history, now=NOW
        )
        if plan is None:
            reply = "<model>"
        elif plan.reply is not None:
            reply = plan.reply
        else:
            reply = "".join(
                filter(None, (plan.prefix and plan.prefix + " ||| ", "<model> ||| ", plan.then))
            )
        replies.append(reply)
        history += [
            {"role": "user", "content": message},
            {"role": "assistant", "content": bubbles.flatten(reply)},
        ]
    return replies


NAME = "Ismingiz nima, sizga qanday murojaat qilsam bo'ladi?"
HANDLES = "Ha, doktor shu bilan shug'ullanadi."
TOPIC = "Qanday masala yoki shikoyat bilan murojaat qilyapsiz?"
ONLINE = (
    "Doktor bilan onlayn videoqo'ng'iroqda konsultatsiya olishingiz mumkin, kelish shart emas. "
    "Onlayn konsultatsiyaga yozib qo'yaymi?"
)
VISIT = (
    "Klinikaga kelib qabulga yozilasizmi yoki doktor bilan onlayn videoqo'ng'iroqda "
    "konsultatsiya olasizmi?"
)
INTRO = "Va alaykum assalom. Men doktor Sultonbekning administratoriman, ismim Dildora."


def place(name: str) -> str:
    return f"{name}, qayerdansiz: Toshkentdanmisiz yoki viloyatdanmi?"


# --- the order: name, place, complaint ---


def test_a_greeting_is_answered_with_the_name_question() -> None:
    replies = _talk(["Assalomu alaykum", "Akmal", "Samarqand", "Prostatit bor"])
    assert bubbles.parts(replies[0]) == [INTRO, NAME]
    assert replies[1] == place("Akmal")
    assert replies[2] == TOPIC
    assert replies[3] == f"{HANDLES} {ONLINE}"


def test_how_to_reach_the_doctor_is_answered_with_the_booking_not_the_numbers() -> None:
    replies = _talk(["Doktor bilan qanday bog'lansam bo'ladi?"])
    last = bubbles.parts(replies[0])[-1]
    assert last == f"Doktor bilan konsultatsiyaga shu yerning o'zida yozib qo'yaman. {NAME}"
    assert "+998" not in replies[0]


def test_a_complaint_first_is_not_asked_again() -> None:
    replies = _talk(["Salom, ereksiya yaxshi emas", "Akmal", "Samarqand"])
    assert bubbles.parts(replies[0]) == [INTRO, f"{HANDLES} {NAME}"]
    assert replies[1] == place("Akmal")
    assert replies[2] == ONLINE


def test_in_tashkent_the_patient_chooses_the_clinic_or_online() -> None:
    replies = _talk(["Prostatit bor", "Sardor", "Toshkent, Chilonzor"])
    assert replies[2] == VISIT


def test_a_patient_abroad_is_offered_online() -> None:
    replies = _talk(["Ereksiya muammosi", "Bilol", "Rossiya"])
    assert replies[2] == ONLINE


def test_abroad_with_the_name_skips_the_place_question() -> None:
    replies = _talk(["Ereksiya muammosi", "Bilol, Rossiyadaman"])
    assert replies[1] == ONLINE


def test_a_question_mid_way_goes_to_the_model_and_the_step_follows() -> None:
    replies = _talk(["Prostatit bor", "Davolanadimi?"])
    assert replies[1] == f"<model> ||| {NAME}"


def test_a_first_message_that_is_a_question_still_gets_the_introduction() -> None:
    replies = _talk(["Prostatit davolanadimi?"])
    intro, model, step = bubbles.parts(replies[0])
    assert intro.startswith("Assalomu alaykum. Men doktor Sultonbekning administratoriman")
    assert step == f"{HANDLES} {NAME}"


def test_a_question_without_a_complaint_is_answered_then_the_name() -> None:
    replies = _talk(["Manzilingiz qayerda?"])
    assert bubbles.parts(replies[0])[-2:] == ["<model>", NAME]


def test_a_price_question_is_scripted_with_room_for_the_card() -> None:
    plan = scripted_flow.plan(
        FLOWS, script="uz-latn", user_message="Prostatit bor, narxi qancha?", history=[], now=NOW
    )
    assert plan is not None and plan.cards


def test_a_price_question_without_a_complaint_asks_the_complaint_in_its_turn() -> None:
    replies = _talk(["Qabul narxi qancha?", "Jasur", "Toshkent", "Prostatit"])
    assert bubbles.parts(replies[0])[-1] == NAME
    assert replies[1] == place("Jasur")
    assert replies[2] == TOPIC
    assert replies[3] == f"{HANDLES} {VISIT}"


def test_a_no_is_left_to_the_model() -> None:
    replies = _talk(["Prostatit bor", "Akmal", "Samarqand", "Yo'q, kerak emas"])
    assert replies[3] == "<model>"


def test_low_desire_alone_still_takes_the_online_flow() -> None:
    replies = _talk(["Jinsiy xohishim pasayib ketdi", "Farrux", "Samarqand"])
    assert replies[2] == "Yoshingiz nechada?"


def test_cyrillic_and_russian() -> None:
    cyr = _talk(["Эрекция яхши эмас", "Ботир", "Фарғона"], "uz-cyrl")
    assert bubbles.parts(cyr[0])[-1] == (
        "Ҳа, доктор шу билан шуғулланади. Исмингиз нима, сизга қандай мурожаат қилсам бўлади?"
    )
    assert cyr[1] == "Ботир, қаердансиз: Тошкентданмисиз ёки вилоятданми?"
    assert cyr[2].startswith("Доктор билан онлайн видеоқўнғироқда")
    ru = _talk(["Проблемы с потенцией", "Руслан", "Ташкент"], "ru")
    assert (
        bubbles.parts(ru[0])[-1]
        == "Да, доктор этим занимается. Как вас зовут, как к вам обращаться?"
    )
    assert ru[2].startswith("Записать вас на приём в клинику или на онлайн-консультацию")


def test_an_emergency_is_left_to_the_model() -> None:
    assert _talk(["Moyagim birdan qattiq og'rib shishdi, 1 soat bo'ldi"]) == ["<model>"]


def test_a_name_given_unasked_is_not_asked_again() -> None:
    replies = _talk(["Ismim Bobur, varikotsele bor ekan"])
    assert bubbles.parts(replies[0])[-1] == f"{HANDLES} {place('Bobur')}"


def test_men_with_a_place_is_not_taken_for_a_name() -> None:
    replies = _talk(["Men Rossiyadaman, ereksiya muammosi bor"])
    assert bubbles.parts(replies[0])[-1] == f"{HANDLES} {NAME}"


def test_a_third_question_moves_on_instead_of_taking_it_for_a_name() -> None:
    replies = _talk(["Prostatit bor", "Ish vaqtingiz?", "Siz botmisiz?"])
    assert replies[1] == f"<model> ||| {NAME}"
    assert replies[2] == "<model> ||| Qayerdansiz: Toshkentdanmisiz yoki viloyatdanmi?"


# --- the call, only when the patient asks for it ---

CALL = ["Prostatit bor", "Akmal", "Toshkent", "Administrator qo'ng'iroq qilsin"]


def test_the_call_when_asked_for_names_the_prepayment() -> None:
    replies = _talk([*CALL, "+998 90 123 45 67, ertaga 10 da"])
    assert replies[3].startswith("Operatorimiz qo'ng'iroq qilib hammasini batafsil aytib beradi.")
    assert replies[4].startswith(
        "Rahmat, Akmal. Operatorimiz ertaga 10 da +998 90 123 45 67 raqamiga qo'ng'iroq qiladi."
    )
    assert "50%" in replies[4]
    assert "[[CALLBACK:+998 90 123 45 67|Prostatit bor -- 50% oldindan|ertaga 10 da]]" in replies[4]


def test_a_number_without_a_time_asks_the_time_then_confirms() -> None:
    replies = _talk([*CALL, "+998 93 345 67 89", "tushdan keyin"])
    assert replies[4] == "Qaysi vaqtda qo'ng'iroq qilsak qulay?"
    assert replies[5].startswith(
        "Rahmat, Akmal. Operatorimiz tushdan keyin +998 93 345 67 89 raqamiga qo'ng'iroq qiladi."
    )


def test_a_number_given_early_skips_ahead() -> None:
    replies = _talk(["Prostatit bor", "Sardor, raqamim +998 91 234 56 78, ertalab"])
    assert replies[1].startswith("Rahmat")
    assert "[[CALLBACK:+998 91 234 56 78|" in replies[1]


def test_everything_in_one_message_is_confirmed_at_once() -> None:
    replies = _talk(
        [
            "Salom, men Otabek, Andijondanman, tez bo'shanish bor, raqamim +998 95 890 12 34, "
            "ertaga 10 da qo'ng'iroq qiling"
        ]
    )
    last = bubbles.parts(replies[0])[-1]
    assert last.startswith(
        f"{HANDLES} Rahmat, Otabek. "
        "Operatorimiz ertaga 10 da +998 95 890 12 34 raqamiga qo'ng'iroq qiladi."
    )
    assert "|ertaga 10 da]]" in last


def test_an_evening_call_time_is_moved_into_the_operators_hours() -> None:
    replies = _talk([*CALL, "+998 90 123 45 67, soat 19 da", "16:00"])
    assert replies[4] == (
        "Operatorlarimiz 09:00 dan 16:00 gacha qo'ng'iroq qiladi. Shu oraliqda qaysi vaqt qulay?"
    )
    assert replies[5].startswith("Rahmat, Akmal. Operatorimiz 16:00 da +998 90 123 45 67")


def test_once_confirmed_the_model_carries_on() -> None:
    replies = _talk([*CALL, "+998 90 123 45 67 ertaga", "Rahmat"])
    assert replies[5] == "<model>"


def test_a_number_a_digit_short_is_asked_again_with_an_example() -> None:
    replies = _talk([*CALL, "90 123 45 6, ertaga 10 da"])
    assert replies[4].startswith("Raqam to'liq emas shekilli: +998 90123456.")
    assert "masalan +998 90 123 45 67" in replies[4]
    assert "CALLBACK" not in replies[4]


def test_a_number_with_a_digit_too_many_is_asked_again() -> None:
    replies = _talk([*CALL, "+998 90 123 45 678"])
    assert replies[4].startswith("Raqamda ortiqcha raqam bor shekilli")


def test_a_corrected_number_after_the_check_is_confirmed() -> None:
    replies = _talk([*CALL, "90 123 45 6 ertaga 10 da", "+998 90 123 45 67"])
    assert "+998 90 123 45 67 raqamiga qo'ng'iroq qiladi" in replies[5]
    assert "|ertaga 10 da]]" in replies[5]


def test_a_new_number_after_the_confirmation_is_taken_as_a_correction() -> None:
    replies = _talk([*CALL, "+998 90 123 45 67 ertaga 10 da", "+998 91 234 56 78"])
    assert "+998 91 234 56 78 raqamiga qo'ng'iroq qiladi" in replies[5]
    assert "|ertaga 10 da]]" in replies[5]


def test_a_foreign_number_asks_for_telegram_or_whatsapp() -> None:
    replies = _talk(
        [
            "Ereksiya muammosi",
            "Bilol",
            "Rossiya",
            "qo'ng'iroq qiling",
            "+7 999 123 45 67 kechqurun",
            "Telegram bor",
        ]
    )
    assert replies[4].startswith("Bu chet el raqami: +79991234567.")
    assert "CALLBACK" not in replies[4]
    assert replies[5].startswith(
        "Rahmat, Bilol. Operatorimiz kechqurun Telegram orqali +79991234567"
    )
    assert "(chet el, Telegram) -- 50% oldindan" in replies[5]


def test_a_foreign_number_with_a_bare_yes_asks_which_app() -> None:
    replies = _talk([*CALL, "89991234567", "ha", "WhatsApp"])
    assert replies[4].startswith("Bu chet el raqami: +79991234567.")
    assert replies[5] == "Telegram yoki WhatsApp -- qaysi biri orqali bog'lanaylik?"
    assert "WhatsApp orqali" in replies[6]


def test_the_time_is_taken_out_of_a_message_with_everything_in_it() -> None:
    assert (
        scripted_flow._when(
            "salom, men alisher, qovuq og'riyapti, raqamim +998 99 789 01 23, "
            "soat 3 da qo'ng'iroq qiling"
        )
        == "soat 3 da"
    )
    assert scripted_flow._when("+998 90 123 45 67, tushdan keyin") == "tushdan keyin"
    assert scripted_flow._when("+998 99 789 01 23, после 18:00") == "после 18:00"
    assert scripted_flow._when("+998 93 345 67 89") is None


# --- the booking: a time, half paid, the card ---

SLOTS = [
    datetime(2026, 11, 3, 14, 0, tzinfo=CLINIC_TIMEZONE),
    datetime(2026, 11, 3, 15, 30, tzinfo=CLINIC_TIMEZONE),
    datetime(2026, 11, 4, 10, 0, tzinfo=CLINIC_TIMEZONE),
    datetime(2026, 11, 4, 11, 30, tzinfo=CLINIC_TIMEZONE),
]


def _book(messages: list[str], script: str = "uz-latn") -> list[str]:
    history: list[dict[str, Any]] = []
    replies: list[str] = []
    for message in messages:
        plan = scripted_flow.plan(
            FLOWS, script=script, user_message=message, history=history, now=NOW, slots=SLOTS
        )
        reply = plan.reply if plan and plan.reply else "<model>"
        replies.append(reply)
        history += [
            {"role": "user", "content": message},
            {"role": "assistant", "content": bubbles.flatten(reply)},
        ]
    return replies


def test_outside_tashkent_the_patient_books_online_and_pays_half() -> None:
    replies = _book(
        ["Prostatit bor", "Sardor", "Samarqand", "ha", "ertaga 10:00", "+998 90 123 45 67"]
    )
    assert replies[2] == ONLINE
    assert replies[3] == (
        "Bugun 14:00 va 15:30 da, ertaga 10:00 va 11:30 da bo'sh vaqtlar bor. "
        "Qaysi biri sizga qulay?"
    )
    book, price, card = bubbles.parts(replies[4])
    assert book == "Yaxshi, ertaga 10:00 ga onlayn konsultatsiyaga yozib qo'yishim mumkin."
    assert price == (
        "Konsultatsiya narxi 330 000 so'm. Qabulga yozilish uchun 50% -- 165 000 so'm "
        "oldindan to'lanadi."
    )
    assert card.startswith("To'lov uchun karta: 9860 1501 1048 3446")
    assert "chek rasmini" in card
    assert replies[5].startswith(
        "Rahmat, Sardor. Chek tekshirilgach siz ertaga 10:00 ga yozilasiz."
    )
    assert "1-2 soat oldin" in replies[5]
    assert "[[CALLBACK:+998 90 123 45 67|ONLAYN ertaga 10:00 -- 50% oldindan" in replies[5]


def test_in_tashkent_the_patient_books_the_clinic_and_pays_half() -> None:
    replies = _book(
        [
            "Prostatit bor",
            "Sardor",
            "Toshkent",
            "Klinikaga kelaman",
            "bugun 14:00",
            "To'ladim, +998 90 123 45 67",
        ]
    )
    assert replies[2] == VISIT
    assert replies[3].startswith("Bugun 14:00 va 15:30 da")
    book, price, card = bubbles.parts(replies[4])
    assert book == "Yaxshi, bugun 14:00 ga klinikaga qabulga yozib qo'yishim mumkin."
    assert "165 000 so'm" in price
    assert "[[CALLBACK:+998 90 123 45 67|KLINIKA bugun 14:00 -- 50% oldindan" in replies[5]


def test_in_tashkent_online_is_booked_online() -> None:
    replies = _book(["Prostatit bor", "Sardor", "Toshkent", "onlayn", "bugun 14:00"])
    assert bubbles.parts(replies[4])[0] == (
        "Yaxshi, bugun 14:00 ga onlayn konsultatsiyaga yozib qo'yishim mumkin."
    )


def test_hormones_take_the_ordinary_price() -> None:
    replies = _book(["Gormonlarimni tekshirtirmoqchiman", "Jasur", "Buxoro", "ha", "bugun 14:00"])
    assert "330 000 so'm" in replies[4] and "165 000 so'm" in replies[4]


def test_a_taken_hour_is_answered_with_the_free_ones() -> None:
    replies = _book(["Prostatit bor", "Sardor", "Samarqand", "onlayn", "ertaga 9:00"])
    assert replies[4].startswith("09:00 band. Bugun 14:00 va 15:30 da")


def test_an_evening_hour_takes_the_evening_price() -> None:
    replies = _book(["Prostatit bor", "Sardor", "Samarqand", "onlayn", "ertaga kechki 8 da"])
    assert "990 000 so'm" in replies[4] and "495 000 so'm" in replies[4]
    assert "ish vaqtidan tashqari" in replies[4]


def test_choosing_the_call_takes_the_call_path() -> None:
    replies = _book(["Prostatit bor", "Sardor", "Samarqand", "Administrator qo'ng'iroq qilsin"])
    assert replies[3].startswith("Operatorimiz qo'ng'iroq qilib hammasini batafsil aytib beradi.")


def test_no_free_time_hands_over_to_the_administrator() -> None:
    plan = None
    history: list[dict[str, Any]] = []
    for message in ["Prostatit bor", "Sardor", "Samarqand", "onlayn"]:
        plan = scripted_flow.plan(
            FLOWS, script="uz-latn", user_message=message, history=history, now=NOW, slots=[]
        )
        assert plan is not None and plan.reply is not None
        history += [
            {"role": "user", "content": message},
            {"role": "assistant", "content": bubbles.flatten(plan.reply)},
        ]
    assert plan is not None and plan.reply is not None
    assert plan.reply.startswith("Yaqin kunlarga onlayn konsultatsiyada bo'sh vaqt qolmadi.")


def test_russian_booking() -> None:
    replies = _book(["Проблемы с потенцией", "Руслан", "Москва", "да", "завтра в 11:30"], "ru")
    assert replies[3].startswith("Свободно: Сегодня в 14:00 и 15:30, завтра в 10:00 и 11:30.")
    assert "Хорошо, могу записать вас завтра на 11:30 на онлайн-консультацию." in replies[4]
    assert "165 000 сум" in replies[4]


# --- the card the complaint calls for ---


def test_a_urology_complaint_brings_the_day_card() -> None:
    plan = scripted_flow.plan(
        FLOWS, script="uz-latn", user_message="Prostatit bor", history=[], now=NOW
    )
    assert plan is not None and plan.card_topic == "day"


def test_a_steroid_complaint_brings_the_evening_card() -> None:
    plan = scripted_flow.plan(
        FLOWS,
        script="uz-latn",
        user_message="Steroid kursidan keyin muammo bor",
        history=[],
        now=NOW,
    )
    assert plan is not None and plan.card_topic == "evening"


def test_a_steroid_patient_is_offered_only_the_evening_online() -> None:
    replies = _book(
        ["Steroid kursidan keyin muammo", "Jasur", "Toshkent", "bugun 10:00", "bugun 20:00"]
    )
    # Tashkent or not: sport pharmacology is online, in the evening only.
    assert replies[2].startswith("Sport farmakologiyasi bo'yicha doktor faqat onlayn")
    assert replies[3].startswith("Sport farmakologiyasi bo'yicha doktor faqat onlayn")
    assert "990 000 so'm" in replies[4] and "495 000 so'm" in replies[4]
    assert "onlayn konsultatsiyaga" in replies[4]
