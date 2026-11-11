"""Dr Sultonbek's low-libido flow, said word for word and in order."""

from datetime import datetime
from typing import Any

import pytest

from app.services import bubbles, scripted_flow
from app.services.appointment import CLINIC_TIMEZONE
from app.services.doctor_template import build_settings

FACTS = {
    "DOCTOR_FULL_NAME_OF": "Sultonbek Norkuzievning",
    "DOCTOR_SHORT": "doktor Sultonbek",
    "ADMIN_NAME": "Dildora",
    "SPECIALTY": "urolog-androlog",
    "CITY_TO": "Toshkentga",
    "TELEGRAM": "@docSultonbekk",
    "PHONES": "+998 77 690 60 06",
    "ADDRESS": "CityMed klinikasi, Toshkent shahri, Farobiy ko'chasi 415-uy",
    "WORK_HOURS": "Dushanbadan shanbagacha 09:00–17:00, yakshanba dam olish",
    "ONLINE_PRICE": "330 000 so'm",
    "ONLINE_PREPAY": "165 000 so'm",
    "CARD": "9860 1501 1048 3446",
    "CARD_NAME": "Sultonbek Norkuziev",
    "ONLINE_HOURS": "09:00–16:00",
    "EVENING_PRICE": "990 000 so'm",
    "EVENING_PREPAY": "495 000 so'm",
    "EVENING_HOURS": "19:00–22:00",
}
FLOW = build_settings("urolog-intake", FACTS)["scripted_flow"]
# A Tuesday: tomorrow is a working day.
TUESDAY = datetime(2026, 11, 3, 11, 0, tzinfo=CLINIC_TIMEZONE)


def _talk(messages: list[str], script: str = "uz-latn", now: datetime = TUESDAY) -> list[str]:
    """Play the patient's messages; the scripted replies only (None where the
    model would answer)."""
    history: list[dict[str, Any]] = []
    replies: list[str] = []
    for message in messages:
        plan = scripted_flow.plan_libido(
            FLOW, script=script, user_message=message, history=history, now=now
        )
        reply = plan.reply if plan and plan.reply else f"<model>{plan.then if plan else ''}"
        replies.append(reply)
        history += [
            {"role": "user", "content": message},
            {"role": "assistant", "content": bubbles.flatten(reply)},
        ]
    return replies


def test_the_whole_flow_goes_in_the_doctors_order_and_words() -> None:
    replies = _talk(
        [
            "Assalomu alaykum, jinsiy xohishim pasayib ketdi",
            "Farrux",
            "Samarqand",
            "38",
            "Ertaga 10 da",
        ]
    )
    first = bubbles.parts(replies[0])
    assert first == [
        "Va alaykum assalom. Men doktor Sultonbekning administratoriman, ismim Dildora.",
        "Ismingiz nima, sizga qanday murojaat qilsam bo'ladi?",
    ]
    assert replies[1] == "Farrux, qayerdansiz: Toshkentdanmisiz yoki viloyatdanmi?"
    assert replies[2] == "Yoshingiz nechada?"
    causes, online, today = bubbles.parts(replies[3])
    assert causes.startswith(
        "Xohish pasayishining sabablari har xil bo'ladi: erkaklik gormonlarining"
    )
    assert "doktor Sultonbek bilan maslahatlashganingiz yaxshi bo'ladi" in causes
    assert online.startswith("Agar shikoyatingiz faqat jinsiy aloqaga xohish pastligi")
    assert today == (
        "Bugun onlayn konsultatsiyaga joylarimiz to'lgan, ertaga bo'sh joylarimiz bor. "
        "Ertaga soat nechiga sizga qulay?"
    )
    slot, paid, prepay, card = bubbles.parts(replies[4])
    assert slot == (
        "Yaxshi, ertaga bizda 10:00 ga bo'sh joy bor ekan, "
        "sizni shu vaqtga navbatga yozib qo'yishim mumkin."
    )
    assert "330 000 so'm" in paid
    assert prepay == (
        "Konsultatsiyaning birinchi bosqichi uchun to'lovning birinchi yarmini (165 000 so'm) "
        "kiritib qo'ysangiz, sizni ertaga 10:00 ga yozib qo'yaman."
    )
    assert "9860 1501 1048 3446" in card


def test_after_the_card_the_model_carries_on() -> None:
    replies = _talk(["Libido past", "Akmal", "Toshkent", "40", "soat 2 da", "To'ladim"])
    assert "14:00" in replies[4]
    assert replies[5].startswith("<model>")


def test_a_price_question_is_answered_and_the_same_question_asked_again() -> None:
    replies = _talk(["Xohishim yo'q bo'lib qoldi", "Narxi qancha?", "Bobur"])
    price, again = bubbles.parts(replies[1])
    assert "330 000 so'm" in price and "165 000 so'm" in price
    assert again == "Ismingiz nima, sizga qanday murojaat qilsam bo'ladi?"
    assert replies[2] == "Bobur, qayerdansiz: Toshkentdanmisiz yoki viloyatdanmi?"


def test_a_price_said_early_is_not_said_again_with_the_slot() -> None:
    replies = _talk(["Xohish past", "Narxi qancha?", "Bobur", "Toshkent", "35", "11:00"])
    assert not any(p.startswith("Bizda konsultatsiyalar pulli") for p in bubbles.parts(replies[5]))
    assert "11:00" in replies[5]


def test_another_question_goes_to_the_model_and_the_step_question_follows() -> None:
    replies = _talk(["Jinsiy xohishim pasaygan", "Doktor o'zi ko'radimi?"])
    # The name was asked already: the model answers and asks for it in its own words.
    assert replies[1] == "<model>None"


def test_an_hour_outside_both_windows_is_asked_again() -> None:
    replies = _talk(["Xohish pasaygan", "Sardor", "Buxoro", "30", "Ertaga 17:30 da"])
    assert replies[4].startswith(
        "Onlayn konsultatsiya kunduzi 09:00 dan 16:00 gacha, kechqurun 19:00 dan 22:00 gacha."
    )


def test_an_evening_hour_gets_the_evening_price() -> None:
    replies = _talk(["Xohish pasaygan", "Sardor", "Buxoro", "30", "Narxi qancha?", "kechki 8 da"])
    slot, why, prepay, card = bubbles.parts(replies[5])
    assert "20:00" in slot
    assert "990 000 so'm" in why and "ish vaqtidan tashqari" in why
    assert "495 000 so'm" in prepay and "20:00" in prepay
    assert "9860" in card


def test_no_hour_gets_an_example() -> None:
    replies = _talk(["Xohish pasaygan", "Sardor", "Buxoro", "30", "Ertalab"])
    assert replies[4] == "Soatini yozing, masalan 10:00 yoki 14:30."


def test_saturday_asks_for_monday() -> None:
    saturday = datetime(2026, 11, 7, 11, 0, tzinfo=CLINIC_TIMEZONE)
    replies = _talk(["Xohish pasaygan", "Sardor", "Buxoro", "30"], now=saturday)
    assert "dushanba kuni bo'sh joylarimiz bor. Dushanba kuni soat nechiga" in replies[3]


@pytest.mark.parametrize(
    "message",
    [
        "Ereksiya ham yomon, xohish ham past",
        "Xohish yo'q, siydik ham qiyin",
        "Qabulga yozilish xohishim bor",
        "Prostatit bor",
    ],
)
def test_other_complaints_and_other_wishes_are_left_to_the_ordinary_sequence(message: str) -> None:
    assert (
        scripted_flow.plan_libido(
            FLOW, script="uz-latn", user_message=message, history=[], now=TUESDAY
        )
        is None
    )


def test_cyrillic_and_russian_are_said_in_their_own_alphabet() -> None:
    cyr = _talk(["Жинсий хоҳишим пасайган", "Ботир", "Фарғона", "35", "Эртага 14 да"], "uz-cyrl")
    assert bubbles.parts(cyr[0])[-1] == "Исмингиз нима, сизга қандай мурожаат қилсам бўлади?"
    assert cyr[1] == "Ботир, қаердансиз: Тошкентданмисиз ёки вилоятданми?"
    assert "14:00" in cyr[4] and "165 000 сўм" in cyr[4]
    ru = _talk(["Пропало влечение", "Руслан", "Ташкент", "40", "в 12"], "ru")
    assert bubbles.parts(ru[0])[-1] == "Как вас зовут, как к вам обращаться?"
    assert ru[2] == "Сколько вам лет?"
    assert "12:00" in ru[4] and "165 000 сум" in ru[4]


def test_a_clinic_without_the_flow_is_untouched() -> None:
    assert (
        scripted_flow.plan_libido(
            None, script="uz-latn", user_message="Xohish past", history=[], now=TUESDAY
        )
        is None
    )


def test_the_model_answer_loses_its_own_question() -> None:
    assert scripted_flow.drop_questions("Ha, doktor o'zi ko'radi. Ismingiz nima?") == (
        "Ha, doktor o'zi ko'radi."
    )


def test_an_old_name_question_in_the_same_chat_does_not_skip_the_first_step() -> None:
    history = [
        {"role": "user", "content": "Salom"},
        {"role": "assistant", "content": "Va alaykum assalom. Ismingiz nima?"},
        {"role": "user", "content": "Akmal"},
        {"role": "assistant", "content": "Akmal, qayerdansiz?"},
    ]
    plan = scripted_flow.plan_libido(
        FLOW, script="uz-latn", user_message="Xohishim pasayib ketdi", history=history, now=TUESDAY
    )
    assert plan is not None and plan.reply is not None
    assert bubbles.parts(plan.reply)[-1] == "Ismingiz nima, sizga qanday murojaat qilsam bo'ladi?"


def test_a_sentence_the_model_said_is_not_repeated_in_the_step() -> None:
    step = "Ha, doktor shu bilan shug'ullanadi. Ismingiz nima?"
    answer = "Ha, doktor shu bilan shug'ullanadi. Davolashni doktor ko'rikda belgilaydi."
    assert scripted_flow.drop_repeats(step, answer) == "Ismingiz nima?"


def test_the_name_given_with_the_complaint_is_not_asked_again() -> None:
    # Production chat 2026-11-09: "Sardor menda hohish yoq bolmoqda" to
    # "Ismingiz nima?" got the name question again; "Toshkenda" became the name.
    history = [
        {"role": "user", "content": "Asalomu alekum"},
        {
            "role": "assistant",
            "content": "Va alaykum assalom. Men doktor Sultonbekning administratoriman, "
            "ismim D.\n\nIsmingiz nima, sizga qanday murojaat qilsam bo'ladi?",
        },
    ]
    plan = scripted_flow.plan_libido(
        FLOW,
        script="uz-latn",
        user_message="Sardor menda hohish yoq bolmoqda",
        history=history,
        now=TUESDAY,
    )
    assert plan is not None and plan.reply is not None
    assert plan.reply.startswith("Sardor, qayerdansiz")


def test_a_place_to_the_name_question_is_not_a_name() -> None:
    replies = _talk(["Xohishim yo'q", "Toshkenda"])
    assert "Toshkenda," not in replies[1]
    assert "yosh" in replies[1].lower()


def test_a_sentence_is_not_a_name() -> None:
    replies = _talk(["Xohishim yo'q", "Bilmayman nima qilishni endi"])
    assert "Bilmayman," not in replies[1]


def test_the_hour_alone_is_the_time() -> None:
    # Production chat 2026-11-09: "15" to "Ertaga soat nechiga?" was not taken.
    replies = _talk(["Jinsiy xohishim pasayib ketdi", "Sardor", "Toshkent", "19", "15"])
    assert "15:00" in replies[-1]
    assert scripted_flow._bare_hour("19 yosh") is None
    assert scripted_flow._bare_hour("3") is not None and scripted_flow._bare_hour("3").hour == 15
