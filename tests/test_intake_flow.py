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
WHO = "Siz doktor bilan gaplashmoqchimisiz yoki administrator bilanmi?"
DOCTOR_CONSULT = (
    "Doktor bilan muloqot qilish uchun konsultatsiyaga yozilish kerak, konsultatsiya uchun "
    "50% oldindan to'lov qilinadi. Shu yerning o'zida yozib qo'yaman."
)
ADMIN_BUSY = (
    "Hozir administratorlarimizda vaqt biroz tig'iz. Raqamingizni qoldiring va sizga qaysi "
    "vaqt qulay ekanini yozing -- administratorimiz o'zi qayta bog'lanadi."
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
    assert last == f"{DOCTOR_CONSULT} {NAME}"
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
    # Asked in the last line already: the model answers and asks again in its own words.
    assert replies[1] == "<model> ||| "


def test_a_first_message_that_is_a_question_still_gets_the_introduction() -> None:
    replies = _talk(["Prostatit davolanadimi?"])
    intro, model, step = bubbles.parts(replies[0])
    assert intro.startswith("Assalomu alaykum. Men doktor Sultonbekning administratoriman")
    assert step == f"{HANDLES} {NAME}"


def test_a_question_without_a_complaint_is_answered_then_the_name() -> None:
    replies = _talk(["Manzilingiz qayerda?"])
    assert bubbles.parts(replies[0])[-2:] == ["<model>", NAME]


PRICE_DAY = (
    "Konsultatsiya narxi 330 000 so'm -- onlayn ham, klinikada ham bir xil. "
    "Yozilish uchun 50% -- 165 000 so'm oldindan to'lanadi."
)


def test_a_price_question_is_answered_in_words_without_a_consultation_card() -> None:
    # Dr Sultonbek took the two consultation cards away (2026-11-07).
    plan = scripted_flow.plan(
        FLOWS, script="uz-latn", user_message="Prostatit bor, narxi qancha?", history=[], now=NOW
    )
    assert plan is not None and plan.reply is not None and not plan.cards
    assert bubbles.parts(plan.reply)[-1] == f"{HANDLES} {PRICE_DAY} {NAME}"


def test_the_evening_price_in_words_for_sport() -> None:
    replies = _talk(["Steroid kursidan keyin gormonlarim buzildi", "Narxi qancha?"])
    assert "Kechki onlayn konsultatsiya narxi 990 000 so'm" in replies[1]


def test_a_price_question_without_a_complaint_asks_the_complaint_in_its_turn() -> None:
    replies = _talk(["Qabul narxi qancha?", "Jasur", "Toshkent", "Prostatit"])
    assert bubbles.parts(replies[0])[-1] == f"{PRICE_DAY} {NAME}"
    assert replies[1] == place("Jasur")
    assert replies[2] == TOPIC
    assert replies[3] == f"{HANDLES} {VISIT}"


def test_a_no_at_the_choice_asks_for_the_number_then_is_left_to_the_model() -> None:
    replies = _talk(["Prostatit bor", "Akmal", "Samarqand", "Yo'q, kerak emas", "yo'q"])
    assert replies[3].startswith("Mayli. Telefon raqamingizni qoldiring")
    assert replies[4] == "<model>"


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
    # Asked in the last line already: the model answers and asks again in its own words.
    assert replies[1] == "<model> ||| "
    assert replies[2] == "<model> ||| "


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


# --- from the mock run of 2026-11-06 ---


def test_a_place_in_the_first_message_is_not_asked_again() -> None:
    replies = _talk(["Moskvada ishlayman, prostatit bor", "Sardor"])
    assert bubbles.parts(replies[0])[-1] == f"{HANDLES} {NAME}"
    assert replies[1] == ONLINE


def test_name_place_and_complaint_at_once_go_straight_to_the_choice() -> None:
    replies = _talk(["Salom men Bobur, Rossiyadaman, ereksiya muammosi"])
    assert bubbles.parts(replies[0])[-1] == f"{HANDLES} {ONLINE}"


def test_abroad_said_after_tashkent_is_offered_online_not_the_clinic() -> None:
    replies = _talk(["Salom", "Nodir", "Toshkent", "Hozr men Rassiyadaman, prostatit bor"])
    assert replies[3] == f"{HANDLES} {ONLINE}"


def test_asking_for_the_call_at_the_choice_asks_for_the_number() -> None:
    replies = _talk(["Prostatit bor", "Rustam", "Toshkent", "qo'ng'iroq qilinglar"])
    assert replies[3].startswith("Operatorimiz qo'ng'iroq qilib")


def test_no_name_moves_on_to_the_place_once() -> None:
    replies = _talk(["Salom", "Ismimni aytmayman", "Buxoro"])
    assert replies[1] == "Qayerdansiz: Toshkentdanmisiz yoki viloyatdanmi?"
    assert replies[2] == TOPIC


def test_a_complaint_given_for_the_place_is_taken_and_the_place_asked_again() -> None:
    replies = _talk(["Salom", "Otabek", "prostatit bor", "Samarqand"])
    assert replies[2] == f"{HANDLES} Qayerdansiz: Toshkentdanmisiz yoki viloyatdanmi?"
    assert replies[3] == ONLINE


def test_a_dollar_question_brings_no_price_card() -> None:
    plan = scripted_flow.plan(
        FLOWS, script="uz-latn", user_message="Dollar kursi qancha?", history=[], now=NOW
    )
    assert plan is None or not plan.cards


def test_a_number_alone_first_asks_the_name() -> None:
    replies = _talk(["+998 90 123 45 67"])
    assert bubbles.parts(replies[0])[-1] == NAME


def test_a_woman_without_a_gynaecological_word_is_the_models() -> None:
    assert _talk(["Men ayolman, erim haqida so'ramoqchiman"]) == ["<model>"]


def test_paid_without_a_number_asks_for_the_receipt_and_the_number() -> None:
    replies = _book(["Prostatit bor", "Aziz", "Termiz", "ha", "ertaga 10:00", "to'ladim"])
    assert replies[5].startswith("Chek rasmini va telefon raqamingizni")


def test_a_foreign_number_at_payment_is_marked_for_telegram() -> None:
    replies = _book(
        ["Prostatit bor", "Bilol", "Germaniya", "ha", "ertaga 10:00", "+49 151 2345 6789"]
    )
    assert "ONLAYN (chet el raqami: Telegram/WhatsApp)" in replies[5]


# --- asked for a number: the doctor (the consultation) or the administrator ---


def test_asking_for_a_number_asks_whether_the_doctor_or_the_administrator() -> None:
    replies = _talk(["Raqamingizni bering"])
    assert bubbles.parts(replies[0])[-1] == WHO
    assert "+998" not in replies[0]


def test_the_doctor_is_the_consultation_half_paid_ahead_then_the_sequence() -> None:
    replies = _talk(["Raqam bering", "doktor bilan", "Akmal", "Samarqand", "prostatit bor"])
    assert replies[1] == f"{DOCTOR_CONSULT} {NAME}"
    assert replies[2] == place("Akmal")
    assert replies[3] == TOPIC
    assert replies[4] == f"{HANDLES} {ONLINE}"


def test_wanting_the_doctor_said_outright_needs_no_question() -> None:
    replies = _talk(["Doktor bilan gaplashmoqchiman", "Akmal", "Toshkent", "prostatit bor"])
    assert bubbles.parts(replies[0])[-1] == f"{DOCTOR_CONSULT} {NAME}"
    assert replies[3] == f"{HANDLES} {VISIT}"


def test_the_administrator_is_busy_and_rings_back_at_the_patients_time() -> None:
    replies = _talk(["Telefon raqam bering", "administrator bilan", "90 123 45 67", "ertaga 11 da"])
    assert replies[1] == ADMIN_BUSY
    assert replies[2] == "Qaysi vaqtda qo'ng'iroq qilsak qulay?"
    assert replies[3].startswith("Rahmat. Operatorimiz ertaga 11 da +998 90 123 45 67")
    assert "[[CALLBACK:+998 90 123 45 67|ADMINISTRATOR BILAN GAPLASHMOQCHI" in replies[3]


def test_the_administrator_asked_outright_with_number_and_time() -> None:
    replies = _talk(["Admin bilan gaplashsam bo'ladimi", "+998 90 123 45 67, soat 15 da"])
    assert bubbles.parts(replies[0])[-1] == ADMIN_BUSY
    assert "[[CALLBACK:+998 90 123 45 67|ADMINISTRATOR BILAN GAPLASHMOQCHI" in replies[1]


def test_a_number_asked_mid_sequence_asks_who() -> None:
    replies = _talk(["Prostatit bor", "Akmal", "admin raqamini bering"])
    assert replies[2] == ADMIN_BUSY


def test_asking_who_in_russian() -> None:
    replies = _talk(["Дайте номер телефона", "с доктором"], "ru")
    assert bubbles.parts(replies[0])[-1] == "Вы хотите поговорить с доктором или с администратором?"
    assert replies[1].startswith("Чтобы поговорить с доктором, нужно записаться на консультацию")


def test_a_hesitant_patient_is_asked_for_the_number_once() -> None:
    replies = _book(
        ["Prostatit bor", "Sardor", "Samarqand", "o'ylab ko'raman", "+998 90 123 45 67", "rahmat"]
    )
    assert replies[3].startswith("Mayli. Telefon raqamingizni qoldiring")
    assert replies[4].startswith("Rahmat, Sardor. Administratorimiz ish vaqtida")
    assert (
        "[[CALLBACK:+998 90 123 45 67|IKKILANDI, qayta qo'ng'iroq -- Prostatit bor|" in replies[4]
    )
    assert replies[5] == "<model>"


def test_too_dear_after_the_card_asks_for_the_number() -> None:
    replies = _book(["Prostatit bor", "Sardor", "Samarqand", "ha", "ertaga 10:00", "qimmat ekan"])
    assert replies[5].startswith("Mayli. Telefon raqamingizni qoldiring")


def test_a_hesitant_no_without_a_number_is_left_to_the_model() -> None:
    replies = _book(["Prostatit bor", "Sardor", "Samarqand", "hozircha kerak emas", "yo'q"])
    assert replies[3].startswith("Mayli. Telefon raqamingizni qoldiring")
    assert replies[4] == "<model>"


def test_asking_for_the_number_again_gets_the_same_step() -> None:
    replies = _talk(["Doktor bilan qanday bog'lansam bo'ladi?", "Doktor raqamini bering"])
    assert replies[1].endswith(NAME)
    assert "+998" not in replies[1]


def test_a_gynaecological_question_is_answered_without_promising_a_gynaecologist() -> None:
    replies = _talk(["Hayzim kechikyapti"])
    last = bubbles.parts(replies[0])[-1]
    assert last.startswith("Bu ginekologik masala, uni ginekolog ko'radi.")
    assert "yozib" not in last


# --- from the med.uz.ai chat of 2026-11-07 ---


def test_a_name_sent_with_other_messages_at_once_is_taken() -> None:
    replies = _talk(["Ассалом алейкум", "Сардор\nОперация нархини кўрмоқчиман\nВодянка"], "uz-cyrl")
    assert replies[1] == "Сардор, қаердансиз: Тошкентданмисиз ёки вилоятданми?"


def test_hydrocele_is_a_complaint() -> None:
    replies = _talk(["Salom", "Sardor", "Samarqand", "Moyakda og'riq, vodyanka menimcha"])
    assert replies[3] == f"{HANDLES} {ONLINE}"


def test_the_price_asked_again_after_the_card_is_said_in_words() -> None:
    history = [
        {"role": "user", "content": "Prostatit bor"},
        {
            "role": "assistant",
            "content": f"{HANDLES} {NAME}\n\n📎 Kunduzgi konsultatsiya: narx va tartib",
        },
        {"role": "user", "content": "Sardor"},
        {"role": "assistant", "content": place("Sardor")},
        {"role": "user", "content": "Samarqand"},
        {"role": "assistant", "content": ONLINE},
    ]
    plan = scripted_flow.plan(
        FLOWS, script="uz-latn", user_message="Puli necha pul?", history=history, now=NOW
    )
    assert plan is not None and plan.reply is not None
    assert plan.reply.startswith(
        "Konsultatsiya narxi 330 000 so'm -- onlayn ham, klinikada ham bir xil. "
        "Yozilish uchun 50% -- 165 000 so'm oldindan to'lanadi."
    )
    # Samarqand: online only, so the clinic is not offered again.
    assert plan.reply.endswith(" Onlayn konsultatsiyaga yozib qo'yaymi?")
    assert "klinikaga kelasizmi" not in plan.reply


def test_a_named_day_gets_that_days_times() -> None:
    replies = _book(["Prostatit bor", "Sardor", "Samarqand", "ha", "Chorshanbada-chi?"])
    assert replies[4] == "Ertaga 10:00 va 11:30 da bo'sh vaqtlar bor. Qaysi biri sizga qulay?"


def test_a_day_with_no_room_says_so_and_offers_the_free_ones() -> None:
    replies = _book(["Prostatit bor", "Sardor", "Samarqand", "ha", "payshanbada bo'ladimi?"])
    assert replies[4].startswith("Payshanba bo'sh vaqt qolmagan. Bugun 14:00 va 15:30 da")


def test_a_spaced_hour_is_read_as_the_hour() -> None:
    assert scripted_flow.parse_time("15 00га?") == scripted_flow.time(15, 0)
    assert scripted_flow.parse_time("+998 90 123 45 67, ertaga 10 da") == scripted_flow.time(10, 0)


def test_online_misspelt_is_online() -> None:
    replies = _book(["Prostatit bor", "Sardor", "Samarqand", "онлаен"])
    assert replies[3].startswith("Bugun 14:00 va 15:30 da")


def test_at_work_then_is_not_a_no() -> None:
    replies = _book(["Prostatit bor", "Sardor", "Samarqand", "ha", "Men ishda bo'laman unda"])
    assert not replies[4].startswith("Mayli. Telefon raqamingizni qoldiring")


def test_a_day_after_the_number_was_asked_goes_back_to_the_times() -> None:
    replies = _book(
        ["Prostatit bor", "Sardor", "Samarqand", "o'ylab ko'raman", "chorshanbada-chi?"]
    )
    assert replies[4] == "Ertaga 10:00 va 11:30 da bo'sh vaqtlar bor. Qaysi biri sizga qulay?"


def test_a_time_with_a_question_mark_is_still_a_time() -> None:
    replies = _book(["Prostatit bor", "Sardor", "Samarqand", "ha", "ertaga 10:00 ga?"])
    assert bubbles.parts(replies[4])[0].startswith("Yaxshi, ertaga 10:00 ga onlayn")


def test_later_is_not_the_evening() -> None:
    from app.services import promo_images

    assert not promo_images._EVENING.search("Кечроқ йўқми?")
    assert not promo_images._EVENING.search("kechroq bo'ladimi")


def test_another_day_after_the_card_offers_that_days_times_and_books_again() -> None:
    replies = _book(
        ["Prostatit bor", "Sardor", "Samarqand", "ha", "bugun 14:00", "chorshanbada-chi?", "11:30"]
    )
    assert replies[5] == "Ertaga 10:00 va 11:30 da bo'sh vaqtlar bor. Qaysi biri sizga qulay?"
    assert bubbles.parts(replies[6])[0] == (
        "Yaxshi, ertaga 11:30 ga onlayn konsultatsiyaga yozib qo'yishim mumkin."
    )


# --- from the real chats of 2026-11-05..07 ---


def test_the_address_asked_with_the_price_is_answered_too() -> None:
    replies = _talk(["1- komponentli protez narxi ?\nManzil qayerda ?"])
    assert "Manzil: CityMed klinikasi" in bubbles.parts(replies[0])[-1]


def test_a_date_of_birth_is_not_a_phone_number() -> None:
    from app.services import phone_check

    assert phone_check.read("Исмим Тохир 12.12.1979 йилман") is None
    assert phone_check.read("raqamim 90 123 45 67") is not None


def test_a_greeting_for_the_name_asks_the_name_again() -> None:
    replies = _talk(["Doktor bn qanday bogʻlansam boladi", "Ассаламу алейкум"])
    assert replies[1] == NAME


def test_a_weekday_with_an_hour_books_that_day() -> None:
    # NOW is Tuesday 3 November: Wednesday is tomorrow, with 11:30 free.
    replies = _book(["Prostatit bor", "Sardor", "Samarqand", "ha", "chorshanba 11:30 ga"])
    assert bubbles.parts(replies[4])[0] == (
        "Yaxshi, ertaga 11:30 ga onlayn konsultatsiyaga yozib qo'yishim mumkin."
    )


def test_a_weekday_evening_takes_that_day() -> None:
    replies = _book(["Prostatit bor", "Sardor", "Samarqand", "ha", "juma 20:00"])
    assert bubbles.parts(replies[4])[0] == (
        "Yaxshi, juma 20:00 ga onlayn konsultatsiyaga yozib qo'yishim mumkin."
    )


def test_a_sunday_offer_is_read_as_sunday() -> None:
    history = [
        {"role": "user", "content": "Prostatit bor"},
        {"role": "assistant", "content": f"{HANDLES} {NAME}"},
        {"role": "user", "content": "Sardor"},
        {"role": "assistant", "content": place("Sardor")},
        {"role": "user", "content": "Samarqand"},
        {"role": "assistant", "content": ONLINE},
        {"role": "user", "content": "ha"},
        {
            "role": "assistant",
            "content": "Yaxshi, yakshanba 15:00 ga onlayn konsultatsiyaga yozib qo'yishim mumkin.",
        },
    ]
    plan = scripted_flow.plan(
        FLOWS, script="uz-latn", user_message="+998 90 123 45 67", history=history, now=NOW
    )
    assert plan is not None and plan.reply is not None
    assert "yakshanba 15:00 ga yozilasiz" in plan.reply


def test_the_sequence_is_finished_after_the_payment_is_confirmed() -> None:
    replies = _book(
        ["Prostatit bor", "Sardor", "Samarqand", "ha", "ertaga 10:00", "+998 90 123 45 67"]
    )
    history = []
    for message, reply in zip(
        ["Prostatit bor", "Sardor", "Samarqand", "ha", "ertaga 10:00", "+998 90 123 45 67"],
        replies,
        strict=True,
    ):
        history += [
            {"role": "user", "content": message},
            {"role": "assistant", "content": bubbles.flatten(reply)},
        ]
    assert scripted_flow.finished(FLOWS, history)
    assert not scripted_flow.finished(FLOWS, history[:4])


def test_wanting_the_doctor_is_no_complaint_so_the_complaint_is_still_asked() -> None:
    replies = _talk(["Doktor bilan gaplashmoqchiman", "Akmal", "Toshkent", "prostatit bor"])
    assert HANDLES not in replies[0]
    assert replies[2] == TOPIC
    assert replies[3] == f"{HANDLES} {VISIT}"


def test_no_number_for_the_administrator_goes_on_with_the_sequence() -> None:
    replies = _talk(["Administrator bilan gaplashsam bo'ladimi?", "Bobur", "Samarqand", "ereksiya"])
    assert replies[1] == place("Bobur")
    assert replies[2] == TOPIC
    assert replies[3] == f"{HANDLES} {ONLINE}"


def test_a_refused_number_for_the_administrator_then_the_whole_sequence() -> None:
    replies = _talk(
        ["Admin bilan gaplashmoqchiman", "salom", "raqam bermayman", "Sardor", "Buxoro"]
    )
    assert replies[1] == "Telefon raqamingizni yozing, operatorimiz bog'lanadi."
    assert replies[2] == NAME
    assert replies[3] == place("Sardor")
    assert replies[4] == TOPIC


def test_no_number_at_the_choice_after_the_administrator_is_the_choice_still() -> None:
    replies = _talk(
        ["Administrator bilan gaplashsam bo'ladimi?", "Bobur", "Samarqand", "ereksiya", "ha"]
    )
    assert replies[4].startswith("Dushanba") or "bo'sh vaqt" in replies[4]


def test_raqam_bermayman_is_a_no_not_a_request_for_a_number() -> None:
    assert scripted_flow.contact_wish("raqam bermayman") is None
    assert scripted_flow.contact_wish("raqamingizni bering") == "ask"


def test_the_choice_asked_twice_takes_the_latest_answer() -> None:
    # Mock run 2026-11-09: the choice, a question about operation prices, the
    # choice again with the cards, "klinikaga" -- booked online by mistake.
    history = [
        {"role": "user", "content": "Akmal"},
        {"role": "assistant", "content": "Akmal, qayerdansiz: Toshkentdanmisiz yoki viloyatdanmi?"},
        {"role": "user", "content": "Toshkent"},
        {"role": "assistant", "content": TOPIC},
        {"role": "user", "content": "ereksiya muammosi"},
        {"role": "assistant", "content": f"{HANDLES} {VISIT}"},
        {"role": "user", "content": "Operatsiya narxlari qancha?"},
        {
            "role": "assistant",
            "content": "Operatsiya narxi va paketi rasmda. "
            "Onlayn konsultatsiyaga yozib qo'yaymi yoki klinikaga kelasizmi?",
        },
        {"role": "user", "content": "klinikaga"},
        {"role": "assistant", "content": "Bugun 14:00 va 15:30 da bo'sh vaqtlar bor."},
    ]
    plan = scripted_flow.plan(
        FLOWS, script="uz-latn", user_message="bugun 14:00", history=history, now=NOW, slots=SLOTS
    )
    assert plan is not None and plan.reply is not None
    assert "klinikaga" in plan.reply.split("|||")[0].lower()
    assert "onlayn" not in plan.reply.split("|||")[0].lower()


def test_a_complaint_with_a_question_is_not_asked_again() -> None:
    replies = _talk(["Руслан", "Я в Москве", "варикоцеле, сколько стоит операция?"], "ru")
    assert "С какой проблемой" not in replies[2]
    assert replies[2].endswith("Записать вас на онлайн-консультацию?")


def test_a_complaint_with_no_in_it_is_not_a_refusal() -> None:
    replies = _talk(["Salom", "Jasur", "Rossiyadaman", "ereksiya yo'q"])
    assert replies[3] == f"{HANDLES} {ONLINE}"
    replies = _talk(["Salom", "Jasur", "Rossiyadaman", "prostatit", "ereksiya ham yo'q"])
    assert replies[4] == f"{HANDLES} {ONLINE}"


def test_abroad_in_other_words_is_never_offered_the_clinic() -> None:
    # Production chat 2026-11-09: "Musofirlikdaman", then "Narhi qancha" was
    # answered with "...yoki klinikaga kelasizmi?".
    replies = _talk(
        ["Salom", "Menda ereksiya bolmoqda", "Begzod", "Musofirlikdaman", "Narhi qancha"]
    )
    assert replies[3] == ONLINE
    assert "klinikaga kelasizmi" not in replies[4].lower()
    assert replies[4].endswith(" Onlayn konsultatsiyaga yozib qo'yaymi?")


def test_the_price_after_a_complaint_card_is_the_consultations() -> None:
    # Mock 2026-11-09: the prosthesis card came with "ereksiya", and "Narhi
    # qancha" was answered with the prosthesis's 30 000 000.
    card = "\n\n📎 Falloprotez: narx va paket"
    history = [
        {"role": "user", "content": "Menda ereksiya bolmoqda"},
        {"role": "assistant", "content": f"{HANDLES} {NAME}{card}"},
        {"role": "user", "content": "Begzod"},
        {"role": "assistant", "content": place("Begzod")},
        {"role": "user", "content": "Musofirlikdaman"},
        {"role": "assistant", "content": ONLINE},
    ]
    plan = scripted_flow.plan(
        FLOWS, script="uz-latn", user_message="Narhi qancha", history=history, now=NOW
    )
    assert plan is not None and plan.reply is not None
    assert "30 000 000" not in plan.reply
    assert "330 000" in plan.reply


SPORT_FACTS = {**FACTS, "SPORT_PRICE": "500 000 so'm", "SPORT_PREPAY": "250 000 so'm"}
SPORT_FLOWS = build_settings("urolog-intake", SPORT_FACTS)["scripted_flows"]


def _sport_book(messages: list[str]) -> list[str]:
    history: list[dict[str, Any]] = []
    replies: list[str] = []
    for message in messages:
        plan = scripted_flow.plan(
            SPORT_FLOWS,
            script="uz-latn",
            user_message=message,
            history=history,
            now=NOW,
            slots=SLOTS,
        )
        reply = plan.reply if plan and plan.reply else "<model>"
        replies.append(reply)
        history += [
            {"role": "user", "content": message},
            {"role": "assistant", "content": bubbles.flatten(reply)},
        ]
    return replies


def test_steroids_with_their_own_price_are_booked_by_day() -> None:
    # Dr Sultonbek's card, 2026-11-09: two-stage, 500 000, by day, online or offline.
    replies = _sport_book(
        [
            "Steroid ishlatganman, gormonlarim buzildi",
            "Jasur",
            "Toshkent",
            "klinikaga",
            "ertaga 10:00",
        ]
    )
    assert replies[2] == VISIT
    assert replies[3].startswith("Bugun 14:00 va 15:30 da")
    assert "ertaga 10:00 ga klinikaga" in replies[4]
    assert "500 000 so'm" in replies[4] and "250 000 so'm" in replies[4]
    assert "990 000" not in replies[4]


def test_steroids_in_the_evening_keep_the_evening_price() -> None:
    replies = _sport_book(
        ["Steroid kursidan keyin muammo", "Jasur", "Samarqand", "ha", "bugun 20:00"]
    )
    assert replies[2] == ONLINE
    assert "990 000" in replies[4]


def test_the_steroid_price_asked_is_said_in_words() -> None:
    replies = _sport_book(["Testosteron ukol qilganman", "Jasur", "Samarqand", "Narxi qancha?"])
    assert "500 000 so'm" in replies[3] and "250 000 so'm" in replies[3]
    assert "990 000" not in replies[3]


def _offered(message: str, history_patient: str = "Varikotsele") -> str:
    texts = FLOWS["intake"]["texts"]["uz-latn"]
    history = [
        {"role": "user", "content": history_patient},
        {"role": "assistant", "content": texts["online_choice"]},
        {"role": "user", "content": "Ha"},
        {"role": "assistant", "content": texts["offer_slots"].format(slots="ertaga 09:00 da")},
    ]
    plan = scripted_flow.plan(
        FLOWS, script="uz-latn", user_message=message, history=history, now=NOW
    )
    return plan.reply if plan and plan.reply else "<model>"


def test_kecha_for_a_booking_is_the_evening() -> None:
    # Production chat 2026-11-09: "kecha qilib bera oladilarmi" got "kecha bo'lmaydi".
    reply = _offered("Menga online kasultatsiya kecha qilib bera oladilarmi")
    assert "19:00 dan 22:00 gacha" in reply and "990 000" in reply


def test_yesterday_in_the_past_is_not_the_evening() -> None:
    assert "990 000" not in _offered("Kecha yozgan edim, javob kelmadi")


def test_the_evening_asked_in_the_present_tense_is_offered() -> None:
    reply = _offered("Kechqurun kelsam bo'ladi")
    assert "990 000" in reply


def test_excuse_me_is_not_the_evening() -> None:
    assert "990 000" not in _offered("Kechirasiz, ertaga 09:00 bo'ladi")


def test_the_hour_alone_picks_the_time_offered() -> None:
    # Production chat 2026-11-09, the other flow: "15" to the times offered.
    texts = FLOWS["intake"]["texts"]["uz-latn"]
    history = [
        {"role": "user", "content": "Varikotsele"},
        {"role": "assistant", "content": texts["online_choice"]},
        {"role": "user", "content": "Ha"},
        {"role": "assistant", "content": texts["offer_slots"].format(slots="ertaga 15:00 da")},
    ]
    slots = [NOW.replace(day=4, hour=15, minute=0)]
    plan = scripted_flow.plan(
        FLOWS, script="uz-latn", user_message="15", history=history, now=NOW, slots=slots
    )
    assert plan is not None and plan.reply is not None and "15:00" in plan.reply


def test_an_operation_asked_for_at_the_slots_gets_the_doctor_decides_line() -> None:
    # Live chat 2026-11-10: "Операциячи манга операция керак" got the times again, nothing else.
    texts = FLOWS["intake"]["texts"]["uz-latn"]
    history = [
        {"role": "user", "content": "Varikotsele"},
        {"role": "assistant", "content": texts["online_choice"]},
        {"role": "user", "content": "Ha"},
        {"role": "assistant", "content": texts["offer_slots"].format(slots="ertaga 09:00 da")},
    ]
    plan = scripted_flow.plan(
        FLOWS,
        script="uz-latn",
        user_message="Operatsiyachi, menga operatsiya kerak",
        history=history,
        now=NOW,
        slots=[NOW.replace(day=4, hour=9, minute=0)],
    )
    assert plan is not None and plan.reply is not None
    assert "doktor ko'rikda aniqlaydi" in plan.reply and "09:00" in plan.reply


def test_the_price_of_an_operation_without_a_card_is_the_doctors_to_say() -> None:
    # Live chat 2026-11-10: "Гидроцели нечпул" got the consultation price alone.
    texts = FLOWS["intake"]["texts"]["uz-latn"]
    history = [
        {"role": "user", "content": "Gidrotsele bor"},
        {"role": "assistant", "content": texts["ask_name"]},
        {"role": "user", "content": "Sardor"},
        {"role": "assistant", "content": texts["ask_place_named"].format(name="Sardor")},
        {"role": "user", "content": "Toshkent"},
        {"role": "assistant", "content": texts["visit_choice"]},
    ]
    plan = scripted_flow.plan(
        FLOWS, script="uz-latn", user_message="Gidrotsele nechpul?", history=history, now=NOW
    )
    assert plan is not None and plan.reply is not None
    assert plan.reply.startswith("Operatsiya kerakligi va usulini doktor ko'rikda aniqlaydi")
    assert "330 000" in plan.reply


def _history(step: str) -> list[dict[str, str]]:
    texts = FLOWS["intake"]["texts"]["uz-latn"]
    h = [
        {"role": "user", "content": "Varikotsele bor"},
        {"role": "assistant", "content": texts["ask_name"]},
    ]
    if step == "ask_name":
        return h
    h += [
        {"role": "user", "content": "Sardor"},
        {"role": "assistant", "content": texts["ask_place_named"].format(name="Sardor")},
    ]
    if step == "ask_place":
        return h
    h += [
        {"role": "user", "content": "Toshkent"},
        {"role": "assistant", "content": texts["visit_choice"]},
    ]
    if step == "online_choice":
        return h
    h += [
        {"role": "user", "content": "Klinikaga"},
        {
            "role": "assistant",
            "content": texts["offer_slots"].format(slots="ertaga 09:00 va 10:30 da"),
        },
    ]
    return h


def _at(step: str, message: str) -> scripted_flow.Plan | None:
    slots = [NOW.replace(day=4, hour=9, minute=0), NOW.replace(day=4, hour=10, minute=30)]
    return scripted_flow.plan(
        FLOWS, script="uz-latn", user_message=message, history=_history(step), now=NOW, slots=slots
    )


def test_how_many_days_is_not_a_price_question() -> None:
    # The doctor, 2026-11-11: the bot answers the price to anything with "qancha".
    plan = _at("ask_name", "Qancha kunda tuzalaman?")
    assert (
        plan is not None
        and plan.reply is None
        and plan.then is None
        and "Ismingiz" in (plan.instruction or "")
    )


def test_a_question_about_the_operation_at_the_place_step_is_answered_first() -> None:
    plan = _at("ask_place", "Operatsiya qancha vaqt davom etadi?")
    assert plan is not None and plan.reply is None
    assert plan.then is None and "Qayerdansiz" in (plan.instruction or "")


def test_hesitation_at_the_name_step_goes_to_the_model() -> None:
    # Nothing was offered yet: the model answers, not the next step's question.
    assert _at("ask_name", "Rahmat, o'ylab ko'raman") is None


def test_a_question_about_life_after_the_operation_is_the_models_at_the_slots() -> None:
    plan = _at("offer_slots", "Operatsiyadan keyin ishga chiqsam bo'ladimi?")
    assert (
        plan is not None and plan.reply is None and "09:00" in (plan.then or plan.instruction or "")
    )


def test_the_step_question_is_not_repeated_word_for_word() -> None:
    # The doctor, 2026-11-11: "qayta-qayta so'rayapti". The second time the model rephrases.
    texts = FLOWS["intake"]["texts"]["uz-latn"]
    history = _history("online_choice") + [
        {"role": "user", "content": "Doktor o'zi ko'radimi?"},
        {"role": "assistant", "content": "Ha, doktor o'zi ko'radi. " + texts["choice_again"]},
    ]
    plan = scripted_flow.plan(
        FLOWS, script="uz-latn", user_message="Narkoz bilan qilinadimi?", history=history, now=NOW
    )
    # Asked twice already: the model takes the turn with the whole conversation.
    assert plan is None or (plan.reply is None and plan.then is None)
