"""Dr Sultonbek's ordinary sequence: the doctor handles it, name, place,
telephone and time, then the operator rings -- said as written."""

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


def test_the_whole_sequence_in_the_doctors_words() -> None:
    replies = _talk(
        ["Salom, ereksiya yaxshi emas", "Akmal", "Samarqand", "+998 90 123 45 67, ertaga 10 da"]
    )
    assert bubbles.parts(replies[0]) == [
        "Va alaykum assalom. Men doktor Sultonbekning administratoriman, ismim Dildora.",
        "Ha, doktor shu bilan shug'ullanadi. Ismingiz nima?",
    ]
    assert replies[1] == "Akmal, qayerdansiz?"
    assert replies[2].startswith("Operatorimiz qo'ng'iroq qilib hammasini batafsil aytib beradi.")
    assert replies[3].startswith(
        "Rahmat, Akmal. Operatorimiz ertaga 10 da +998 90 123 45 67 raqamiga qo'ng'iroq qiladi."
    )
    assert "[[CALLBACK:+998 90 123 45 67|Salom, ereksiya yaxshi emas|ertaga 10 da]]" in replies[3]


def test_a_bare_greeting_is_answered_with_the_introduction() -> None:
    replies = _talk(["Assalomu alaykum", "Prostatit bor"])
    assert bubbles.parts(replies[0]) == [
        "Va alaykum assalom. Men doktor Sultonbekning administratoriman, ismim Dildora.",
        "Sizga qanday yordam bera olamiz?",
    ]
    assert replies[1] == "Ha, doktor shu bilan shug'ullanadi. Ismingiz nima?"


def test_a_number_without_a_time_asks_the_time_then_confirms() -> None:
    replies = _talk(["Varikotsele bor", "Bobur", "Toshkent", "+998 93 345 67 89", "tushdan keyin"])
    assert replies[3] == "Qaysi vaqtda qo'ng'iroq qilsak qulay?"
    assert replies[4].startswith(
        "Rahmat, Bobur. Operatorimiz tushdan keyin +998 93 345 67 89 raqamiga qo'ng'iroq qiladi."
    )
    assert "[[CALLBACK:+998 93 345 67 89|" in replies[4]


def test_a_number_given_early_skips_ahead() -> None:
    replies = _talk(["Prostatit bor", "Sardor, raqamim +998 91 234 56 78, ertalab"])
    assert replies[1].startswith("Rahmat")
    assert "[[CALLBACK:+998 91 234 56 78|" in replies[1]


def test_an_evening_call_time_is_moved_into_the_operators_hours() -> None:
    replies = _talk(["Prostatit bor", "Akmal", "Toshkent", "+998 90 123 45 67, soat 19 da"])
    assert (
        replies[3]
        == "Operatorlarimiz 09:00 dan 16:00 gacha qo'ng'iroq qiladi. Shu oraliqda qaysi vaqt qulay?"
    )


def test_a_patient_abroad_hears_about_the_online_consultation() -> None:
    replies = _talk(["Ereksiya muammosi", "Bilol", "Rossiya"])
    first, second = bubbles.parts(replies[2])
    assert "onlayn videoqo'ng'iroqda konsultatsiya" in first
    assert second.startswith("Operatorimiz qo'ng'iroq qilib")


def test_a_question_mid_way_goes_to_the_model_and_the_step_follows() -> None:
    replies = _talk(["Prostatit bor", "Davolanadimi?"])
    assert replies[1] == "<model> ||| Ismingiz nima?"


def test_a_first_message_that_is_a_question_still_gets_the_introduction() -> None:
    replies = _talk(["Prostatit davolanadimi?"])
    intro, model, step = bubbles.parts(replies[0])
    assert intro.startswith("Assalomu alaykum. Men doktor Sultonbekning administratoriman")
    assert step == "Ha, doktor shu bilan shug'ullanadi. Ismingiz nima?"


def test_a_price_question_is_scripted_with_room_for_the_card() -> None:
    plan = scripted_flow.plan(
        FLOWS, script="uz-latn", user_message="Prostatit bor, narxi qancha?", history=[], now=NOW
    )
    assert plan is not None and plan.cards


def test_a_no_is_left_to_the_model() -> None:
    replies = _talk(["Prostatit bor", "Akmal", "Toshkent", "Yo'q, raqam bermayman"])
    assert replies[3] == "<model>"


def test_once_confirmed_the_model_carries_on() -> None:
    replies = _talk(["Prostatit bor", "Akmal", "Toshkent", "+998 90 123 45 67 ertaga", "Rahmat"])
    assert replies[4] == "<model>"


def test_low_desire_alone_still_takes_the_online_flow() -> None:
    replies = _talk(["Jinsiy xohishim pasayib ketdi", "Farrux", "Samarqand"])
    assert replies[2] == "Yoshingiz nechada?"


def test_cyrillic_and_russian() -> None:
    cyr = _talk(["Эрекция яхши эмас", "Ботир", "Фарғона"], "uz-cyrl")
    assert bubbles.parts(cyr[0])[-1] == "Ҳа, доктор шу билан шуғулланади. Исмингиз нима?"
    assert cyr[1] == "Ботир, қаердансиз?"
    ru = _talk(["Проблемы с потенцией", "Руслан", "Ташкент"], "ru")
    assert bubbles.parts(ru[0])[-1] == "Да, доктор этим занимается. Как вас зовут?"
    assert ru[2].startswith("Наш оператор позвонит")


def test_an_emergency_is_left_to_the_model() -> None:
    assert _talk(["Moyagim birdan qattiq og'rib shishdi, 1 soat bo'ldi"]) == ["<model>"]


def test_everything_in_one_message_is_confirmed_at_once() -> None:
    replies = _talk(
        [
            "Salom, men Otabek, Andijondanman, tez bo'shanish bor, raqamim +998 95 890 12 34, "
            "ertaga 10 da qo'ng'iroq qiling"
        ]
    )
    last = bubbles.parts(replies[0])[-1]
    assert last.startswith(
        "Ha, doktor shu bilan shug'ullanadi. Rahmat, Otabek. "
        "Operatorimiz ertaga 10 da +998 95 890 12 34 raqamiga qo'ng'iroq qiladi."
    )
    assert "|ertaga 10 da]]" in last


def test_a_name_given_unasked_is_not_asked_again() -> None:
    replies = _talk(["Ismim Bobur, varikotsele bor ekan"])
    assert (
        bubbles.parts(replies[0])[-1] == "Ha, doktor shu bilan shug'ullanadi. Bobur, qayerdansiz?"
    )


def test_men_with_a_place_is_not_taken_for_a_name() -> None:
    replies = _talk(["Men Rossiyadaman, ereksiya muammosi bor"])
    assert bubbles.parts(replies[0])[-1] == "Ha, doktor shu bilan shug'ullanadi. Ismingiz nima?"


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


def test_a_third_question_moves_on_instead_of_taking_it_for_a_name() -> None:
    replies = _talk(["Prostatit bor", "Ish vaqtingiz?", "Siz botmisiz?"])
    assert replies[1] == "<model> ||| Ismingiz nima?"
    assert replies[2] == "<model> ||| Qayerdansiz?"


def test_a_number_a_digit_short_is_asked_again_with_an_example() -> None:
    replies = _talk(["Prostatit bor", "Akmal", "Toshkent", "90 123 45 6, ertaga 10 da"])
    assert replies[3].startswith("Raqam to'liq emas shekilli: +998 90123456.")
    assert "masalan +998 90 123 45 67" in replies[3]
    assert "CALLBACK" not in replies[3]


def test_a_number_with_a_digit_too_many_is_asked_again() -> None:
    replies = _talk(["Prostatit bor", "Akmal", "Toshkent", "+998 90 123 45 678"])
    assert replies[3].startswith("Raqamda ortiqcha raqam bor shekilli")


def test_a_corrected_number_after_the_check_is_confirmed() -> None:
    replies = _talk(
        ["Prostatit bor", "Akmal", "Toshkent", "90 123 45 6 ertaga 10 da", "+998 90 123 45 67"]
    )
    assert "+998 90 123 45 67 raqamiga qo'ng'iroq qiladi" in replies[4]
    assert "|ertaga 10 da]]" in replies[4]


def test_a_foreign_number_asks_for_telegram_or_whatsapp() -> None:
    replies = _talk(
        ["Ereksiya muammosi", "Bilol", "Rossiya", "+7 999 123 45 67 kechqurun", "Telegram bor"]
    )
    assert replies[3].startswith("Bu chet el raqami: +79991234567.")
    assert "Telegram yoki WhatsApp bormi?" in replies[3]
    assert "CALLBACK" not in replies[3]
    assert replies[4].startswith(
        "Rahmat, Bilol. Operatorimiz kechqurun Telegram orqali +79991234567"
    )
    assert "(chet el, Telegram)" in replies[4]


def test_a_foreign_number_with_a_bare_yes_asks_which_app() -> None:
    replies = _talk(["Prostatit", "Sardor", "Moskva", "89991234567", "ha", "WhatsApp"])
    assert replies[3].startswith("Bu chet el raqami: +79991234567.")
    assert replies[4] == "Telegram yoki WhatsApp -- qaysi biri orqali bog'lanaylik?"
    assert "WhatsApp orqali" in replies[5]


def test_a_new_number_after_the_confirmation_is_taken_as_a_correction() -> None:
    replies = _talk(
        [
            "Prostatit bor",
            "Akmal",
            "Toshkent",
            "+998 90 123 45 67 ertaga 10 da",
            "+998 91 234 56 78",
        ]
    )
    assert "+998 91 234 56 78 raqamiga qo'ng'iroq qiladi" in replies[4]
    assert "|ertaga 10 da]]" in replies[4]
