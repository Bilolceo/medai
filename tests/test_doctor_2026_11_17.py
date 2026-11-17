"""Dr Sultonbek's own test of the bot, 2026-11-17 (his voice notes over the
screenshots): the complaint before the name, "qayerdansiz?" alone, a word on
the complaint, the video call and the time -- the price only once the time is
agreed; "tez bo'shanish operatsiyasi" is selective denervation; the
prosthesis is talked over by telephone first, free; a voice note is answered
"eshita olmayman, yozib yuboring"; "bugun 14:00га" is 14:00, not midnight."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, time
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import Message, MessageSender
from app.services import bubbles, price_guard, promo_images, scripted_flow, voice_notes
from app.services.appointment import CLINIC_TIMEZONE
from app.services.doctor_template import build_settings
from tests.conftest import Seed
from tests.test_operation_cards import CARDS
from tests.test_scripted_flow import FACTS

SETTINGS = build_settings("urolog-intake", FACTS)
FLOWS = SETTINGS["scripted_flows"]
NOW = datetime(2026, 11, 17, 7, 30, tzinfo=CLINIC_TIMEZONE)
SLOTS = [
    datetime(2026, 11, 17, 9, 0, tzinfo=CLINIC_TIMEZONE),
    datetime(2026, 11, 17, 10, 30, tzinfo=CLINIC_TIMEZONE),
    datetime(2026, 11, 18, 9, 0, tzinfo=CLINIC_TIMEZONE),
    datetime(2026, 11, 18, 10, 30, tzinfo=CLINIC_TIMEZONE),
]


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


# --- the time: "14:00га" ------------------------------------------------------


def test_a_clock_with_a_cyrillic_suffix_is_that_hour() -> None:
    # Live, 2026-11-17: "манда буган 14:00га буш буламан" -> "00:00 банд".
    assert scripted_flow.parse_time("манда буган 14:00га буш буламан") == time(14, 0)
    assert scripted_flow.parse_time("15:30да") == time(15, 30)
    assert scripted_flow.parse_time("soat 10:00 ga") == time(10, 0)


def test_a_taken_hour_with_a_cyrillic_suffix_names_that_hour() -> None:
    plans = _talk(
        ["Простатит бор", "Сардор", "Самарқанд", "манда буган 14:00га буш буламан"], "uz-cyrl"
    )
    last = plans[-1]
    assert last is not None and last.reply is not None
    assert last.reply.startswith("14:00 банд.")


# --- the order -----------------------------------------------------------------


def test_the_doctors_order_complaint_name_place_info_video_call_time_then_price() -> None:
    plans = _talk(["Assalomu alaykum", "Tez bo'shanish muammosi bor", "Aziz", "Surxondaryo"])
    greeting, complaint, name, place = plans
    assert greeting is not None and greeting.reply is not None
    assert bubbles.parts(greeting.reply)[-1] == "Sizga qanday yordam bera olamiz?"
    assert complaint is not None and complaint.reply is not None
    assert complaint.reply.endswith("Ismingiz nima, sizga qanday murojaat qilsam bo'ladi?")
    assert name is not None and name.reply == "Aziz, qayerdansiz?"
    # A word on the complaint by the model, then the video call and the times.
    assert place is not None and place.reply is None and place.instruction is not None
    assert "possible causes" in place.instruction
    assert place.then is not None
    assert place.then.startswith("Bu masala bo'yicha doktor bilan video qo'ng'iroq orqali")
    assert "Qaysi biri sizga qulay?" in place.then
    assert "330 000" not in place.then and place.card_topic is None


def test_the_agreed_time_brings_the_card_the_first_stage_and_pay_to_book() -> None:
    plans = _talk(["Tez bo'shanish muammosi bor", "Aziz", "Surxondaryo", "ertaga 09:00"])
    booked = plans[-1]
    assert booked is not None and booked.reply is not None
    book, price, pay = bubbles.parts(booked.reply)
    assert book == "Yaxshi, ertaga 09:00 ga onlayn konsultatsiyaga yozib qo'yishim mumkin."
    assert "birinchi bosqichi uchun 50%" in price and "165 000" in price
    assert pay.endswith("sizni ertaga 09:00 ga yozib qo'yaman.")
    assert booked.card_topic == "day" and booked.card_caption is False


def test_the_booking_card_goes_without_the_rasmda_caption() -> None:
    card = {"topic": "day", "title": "Kunduzgi konsultatsiya: narx va tartib", "url": "u"}
    reply = promo_images.attach_scripted("Yaxshi. ||| Narxi 330 000.", card, caption=False)
    assert "rasmda" not in reply and reply.endswith(
        "[[IMAGE:u|Kunduzgi konsultatsiya: narx va tartib]]"
    )


def test_the_place_is_never_asked_as_tashkent_or_the_regions() -> None:
    for flow in FLOWS.values():
        for texts in flow["texts"].values():
            for line in texts.values():
                assert "viloyatdanmi" not in str(line) and "вилоятданми" not in str(line)


# --- operations ----------------------------------------------------------------


def test_premature_ejaculation_surgery_is_selective_denervation() -> None:
    message = "Tez bo'shanishga operatsiya narxi qancha?"
    assert promo_images.OPERATION_NAMES.search(message)
    found = promo_images.operations_asked(CARDS, user_message=message, own_turns=[])
    assert [c["topic"] for c in found] == ["op:denervatsiya"]
    # Not the list of the seven kinds: the operation is known.
    plan = scripted_flow.plan(FLOWS, script="uz-latn", user_message=message, history=[], now=NOW)
    assert plan is not None and plan.reply is not None and "Qaysi biri kerak?" not in plan.reply


def test_the_complaint_alone_is_not_the_operation() -> None:
    assert not promo_images.OPERATIONS["op:denervatsiya"][0].search("Tez bo'shanish muammosi")


def test_an_operation_price_brings_no_consultation_card() -> None:
    plan = scripted_flow.plan(
        FLOWS,
        script="uz-latn",
        user_message="Varikotsele operatsiyasi qancha turadi?",
        history=[],
        now=NOW,
    )
    assert plan is not None and not plan.cards and "330 000" not in (plan.reply or "")


def test_the_operation_line_names_no_consultation_price() -> None:
    for texts in FLOWS["intake"]["texts"].values():
        assert "330 000" not in texts["op_first_consult"]


# --- the prosthesis: a free telephone talk with the doctor ---------------------


def test_the_prosthesis_talk_first_is_a_free_call_not_the_paid_consultation() -> None:
    plans = _talk(
        [
            "Falloprotez haqida ma'lumot bering",
            "Sardor",
            "Qirg'izistondanman",
            "Avval doktorni o'zlari bilan telefonda gaplashib olsam",
            "+998 90 123 45 67",
            "ertaga 11:00 da",
        ]
    )
    offer, again, phone, confirmed = plans[2], plans[3], plans[4], plans[5]
    # Offered right after the place, instead of the paid online consultation.
    assert offer is not None and offer.then is not None
    assert offer.then.startswith("Protez bo'yicha doktor bilan avval telefonda bepul")
    assert again is not None
    assert again.reply == "Telefon raqamingizni yozing, doktor shu raqamga qo'ng'iroq qiladi."
    assert phone is not None and phone.reply == "Qaysi vaqtda qo'ng'iroq qilsak qulay?"
    assert confirmed is not None and confirmed.reply is not None
    assert "Doktor ertaga 11:00 da +998 90 123 45 67 raqamiga qo'ng'iroq qiladi" in confirmed.reply
    assert "PROTEZ: DOKTOR BILAN BEPUL TELEFON SUHBATI" in confirmed.reply
    assert "50%" not in confirmed.reply
    assert scripted_flow.finished(FLOWS, [{"role": "assistant", "content": confirmed.reply}])


def test_talking_to_the_doctor_about_another_operation_is_the_consultation() -> None:
    plans = _talk(["Varikotsele bor", "Avval doktor bilan telefonda gaplashsam bo'ladimi?"])
    last = plans[-1]
    assert last is None or "bepul" not in (last.reply or "")


# --- voice notes ---------------------------------------------------------------


async def test_the_clinic_s_own_voice_line_is_said(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    seed.tenant_a.settings = {**(seed.tenant_a.settings or {}), "scripted_flows": FLOWS}
    conversation = seed.a.conversation
    db_session.add(
        Message(
            conversation_id=conversation.id,
            sender=MessageSender.PATIENT,
            content="Salom, savolim bor edi",
            channel="instagram",
        )
    )
    await db_session.flush()
    with as_tenant(seed.tenant_a.id):
        line = await voice_notes.reply_for(db_session, conversation.id)
    assert line == (
        "Kechirasiz, ovozli xabarni eshita olmayman. "
        "Iltimos, savolingizni matn qilib yozib yuboring."
    )


def test_the_model_s_own_offer_before_the_offer_is_dropped() -> None:
    answer = (
        "Protez operatsiyasida odatda beldan pastga narkoz beriladi. "
        "Qirg'izistondan bo'lsangiz, doktor bilan video qo'ng'iroq orqali onlayn "
        "maslahatlashish mumkin."
    )
    assert scripted_flow.drop_offer(answer) == (
        "Protez operatsiyasida odatda beldan pastga narkoz beriladi."
    )
    assert scripted_flow.drop_offer("Onlayn maslahatlashish mumkin.") == ""


def test_the_prosthesis_asked_about_is_offered_the_free_call_not_online() -> None:
    plans = _talk(["Falloprotez haqida ma'lumot bering", "Sardor", "Qirg'izistondanman"])
    after_place = plans[-1]
    assert after_place is not None and after_place.then is not None
    assert after_place.then.startswith("Protez bo'yicha doktor bilan avval telefonda bepul")
    assert "video" not in after_place.then


def test_an_erection_complaint_is_still_offered_online() -> None:
    plans = _talk(["Ereksiya muammosi", "Sardor", "Samarqand"])
    after_place = plans[-1]
    assert after_place is not None and after_place.then is not None
    assert after_place.then.startswith("Bu masala bo'yicha doktor bilan video")


def test_how_long_is_not_how_much() -> None:
    assert not price_guard.asks_price("Operatsiya qancha vaqt davom etadi?")
    assert not price_guard.asks_price("Сколько дней лежать?")
    assert price_guard.asks_price("Operatsiya qancha turadi?")


def test_an_operation_price_asked_again_is_that_operation_not_which_one() -> None:
    plans = _talk(["Varikotsele bor", "Rustam", "Moskvadaman", "Operatsiya qancha turadi?"])
    last = plans[-1]
    assert last is None or "Qaysi biri kerak?" not in (last.reply or "")


def test_the_consultation_card_does_not_answer_an_operation_price() -> None:
    day = {"topic": "day", "title": "Kunduzgi konsultatsiya: narx va tartib", "url": "d"}
    assert (
        promo_images.choose(
            [day],
            user_message="Narxi qancha dedingiz?",
            patient_turns=["Falloprotez"],
            own_turns=[],
            reply="Narxi va paketi rasmda.",
        )
        is None
    )
    assert (
        promo_images.choose(
            [day],
            user_message="Konsultatsiya narxi qancha?",
            patient_turns=["Falloprotez"],
            own_turns=[],
            reply="Konsultatsiya narxi va tartibi rasmda.",
        )
        == day
    )
