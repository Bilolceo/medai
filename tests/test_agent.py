"""The agent (app.services.agent): the model's answer, and the guards around it."""

import json
from datetime import datetime
from typing import Any

from app.rag.llm import ChatMessage, LLMProvider
from app.services import agent, callbacks, handoff, promo_images
from app.services.appointment import CLINIC_TIMEZONE

NOW = datetime(2026, 11, 21, 14, 0, tzinfo=CLINIC_TIMEZONE)
SLOTS = [datetime(2026, 11, 23, h, 0, tzinfo=CLINIC_TIMEZONE) for h in (9, 10, 11)]
IMAGES = [
    {"topic": "day", "url": "https://x/day.jpg", "title": "Kunduzgi konsultatsiya: narx va tartib"},
    {"topic": "op:falloprotez", "url": "https://x/fp.jpg", "title": "Falloprotez: narx va paket"},
    {
        "topic": "op:denervatsiya",
        "url": "https://x/dn.jpg",
        "title": "Selektiv denervatsiya: narx va paket",
    },
]


class Scripted(LLMProvider):
    """Answers in turn; records what it was asked."""

    def __init__(self, *answers: dict[str, Any] | str) -> None:
        self.answers = list(answers)
        self.systems: list[str] = []
        self.seen: list[list[ChatMessage]] = []

    async def generate(self, system_prompt: str, messages: list[ChatMessage]) -> str:
        self.systems.append(system_prompt)
        self.seen.append(messages)
        answer = self.answers.pop(0)
        return answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)


async def _decide(provider: LLMProvider, message: str, history: list[Any] | None = None) -> Any:
    return await agent.decide(
        provider,
        profile="sultonbek",
        user_message=message,
        history=history,
        now_local=NOW,
        slots=SLOTS,
        images=IMAGES,
        script="uz-latn",
    )


def test_the_profile_is_named_by_the_settings_and_must_exist() -> None:
    assert agent.profile_of({"agent_profile": "sultonbek"}) == "sultonbek"
    assert agent.profile_of({"agent_profile": "../etc"}) is None
    assert agent.profile_of({"agent_profile": "nobody"}) is None
    assert agent.profile_of({}) is None


def test_the_prompt_carries_the_rules_services_book_and_script() -> None:
    prompt = agent.system_prompt("sultonbek", now_local=NOW, slots=SLOTS, cards_sent=["day"])
    assert "Qachondan beri" in prompt or "qachondan beri" in prompt
    assert "30 000 000 so'm" in prompt
    assert "Today is full" in prompt and "09:00" in prompt
    assert "CARDS SENT in this conversation\nday" in prompt


def test_each_card_knows_its_own_price() -> None:
    prices = agent.card_prices("sultonbek")
    assert prices[30_000_000] == "op:falloprotez"
    assert prices[330_000] == "day"
    assert prices[9_000_000] == "op:denervatsiya"


async def test_an_operation_price_said_brings_its_card_even_if_forgotten() -> None:
    model = Scripted(
        {"messages": ["Falloprotez narxi 30 000 000 so'm."], "cards": [], "lead": None}
    )
    answer = await _decide(model, "inplant narxi qancha")
    assert answer is not None and answer.cards == ["op:falloprotez"]
    assert "THE PATIENT WRITES IN: Uzbek in LATIN" in model.systems[0]


async def test_a_made_up_sum_is_corrected_once() -> None:
    model = Scripted(
        {"messages": ["Narxi 25 000 000 so'm."], "cards": []},
        {"messages": ["Falloprotez narxi 30 000 000 so'm."], "cards": ["op:falloprotez"]},
    )
    answer = await _decide(model, "protez narxi")
    assert answer is not None and answer.messages == ["Falloprotez narxi 30 000 000 so'm."]
    assert "25 000 000" in str(model.seen[1][-1]["content"])


async def test_a_sum_in_words_is_corrected_too() -> None:
    model = Scripted(
        {"messages": ["Narxi o'ttiz million so'm."]},
        {"messages": ["Narxi 30 000 000 so'm."]},
    )
    answer = await _decide(model, "protez narxi")
    assert answer is not None and answer.messages == ["Narxi 30 000 000 so'm."]


async def test_a_sum_still_wrong_after_the_correction_is_not_sent() -> None:
    model = Scripted(
        {"messages": ["Narxi 25 000 000 so'm.", "Qachondan beri bor?"]},
        {"messages": ["Narxi 26 000 000 so'm.", "Qachondan beri bor?"]},
    )
    answer = await _decide(model, "protez narxi")
    assert answer is not None and answer.messages == ["Qachondan beri bor?"]


async def test_a_card_already_sent_or_unknown_is_dropped() -> None:
    history = [
        {"role": "user", "content": "konsultatsiya narxi"},
        {
            "role": "assistant",
            "content": "Narxi rasmda.\n\n📎 Kunduzgi konsultatsiya: narx va tartib",
        },
    ]
    model = Scripted({"messages": ["Yana bir bor."], "cards": ["day", "op:heart"]})
    answer = await _decide(model, "qancha edi", history)
    assert answer is not None and answer.cards == []
    assert "CARDS SENT in this conversation\nday" in model.systems[0]


async def test_not_json_is_asked_again_then_given_up() -> None:
    assert await _decide(Scripted("salom", "hali ham matn"), "salom") is None
    answer = await _decide(Scripted("salom", {"messages": ["Va alaykum assalom."]}), "salom")
    assert answer is not None and answer.messages == ["Va alaykum assalom."]


def test_the_reply_carries_bubbles_cards_and_the_lead() -> None:
    answer = agent.Answer(
        messages=["Rahmat, Aziz.", "Chek tekshirilgach yozilasiz."],
        cards=["op:falloprotez"],
        lead={
            "phone": "+998 90 123 45 67",
            "note": "ONLAYN dushanba 09:00",
            "when": "dushanba 09:00",
        },
    )
    reply = agent.render(
        answer,
        images=IMAGES,
        clinic_phone_numbers="+998 77 690 60 06",
        patient_texts=["+998 90 123 45 67"],
    )
    text, lead = callbacks.extract(reply)
    text, flagged = handoff.extract(text)
    assert lead is not None and lead.phone == "+998901234567" and lead.when == "dushanba 09:00"
    assert not flagged
    assert "|||" in text
    assert [m.group("title") for m in promo_images.MARKER.finditer(text)] == [
        "Falloprotez: narx va paket"
    ]


def test_the_patients_own_number_is_said_back_and_a_stranger_is_not() -> None:
    answer = agent.Answer(messages=["+998 93 555 44 33 ga qo'ng'iroq qilamiz. +998 90 000 00 01"])
    reply = agent.render(
        answer,
        images=IMAGES,
        clinic_phone_numbers="+998 77 690 60 06",
        patient_texts=["raqamim +998 93 555 44 33"],
    )
    assert "+998 93 555 44 33" in reply
    assert "+998 90 000 00 01" not in reply


async def test_an_emergency_the_model_missed_gets_the_doctors_line() -> None:
    model = Scripted(
        {"messages": ["Moyagim birdan qattiq og'rib shishib ketdi"]},
        {"messages": ["Qachondan beri?"]},
    )
    answer = await _decide(model, "Moyagim birdan qattiq og'rib shishib ketdi, isitmam bor")
    assert answer is not None and answer.handoff
    assert answer.messages[0].startswith("Bu kechiktirib bo'lmaydigan holat")


async def test_an_emergency_answered_well_is_left_alone() -> None:
    line = "Bu kechiktirib bo'lmaydigan holat, bugunoq keling. Qayerdasiz?"
    answer = await _decide(
        Scripted({"messages": [line]}), "Siya olmayapman, qorin qattiq og'riyapti"
    )
    assert answer is not None and answer.messages == [line]


def test_one_sign_is_no_emergency() -> None:
    assert not agent.is_emergency("Birdan boshlandi")
    assert agent.is_emergency("qon ketyapti")
    assert agent.is_emergency("shishib ketdi, isitma bor")


async def test_an_echo_of_the_patient_is_asked_again() -> None:
    model = Scripted({"messages": ["Inplant narxi qancha"]}, {"messages": ["30 000 000 so'm."]})
    answer = await _decide(model, "Inplant narxi qancha")
    assert answer is not None and answer.messages == ["30 000 000 so'm."]


async def test_the_same_reply_twice_is_asked_again() -> None:
    history = [
        {"role": "user", "content": "Ereksiya muammosi bor"},
        {"role": "assistant", "content": "Bu holat qachondan beri bor?"},
    ]
    model = Scripted(
        {"messages": ["Bu holat qachondan beri bor?"]}, {"messages": ["Davolanganmisiz?"]}
    )
    answer = await _decide(model, "2 yil", history)
    assert answer is not None and answer.messages == ["Davolanganmisiz?"]


async def test_an_operation_card_needs_a_price_asked_or_said() -> None:
    model = Scripted({"messages": ["Ha, qilinadi."], "cards": ["op:denervatsiya"]})
    answer = await _decide(model, "Tez bo'shanishga operatsiya qilasizlarmi?")
    assert answer is not None and answer.cards == []
    model = Scripted({"messages": ["Narxi rasmda."], "cards": ["op:denervatsiya"]})
    answer = await _decide(model, "denervatsiya narxi qancha")
    assert answer is not None and answer.cards == ["op:denervatsiya"]


async def test_a_lead_with_half_a_number_is_not_sent() -> None:
    model = Scripted(
        {"messages": ["Raqam to'liq emas."], "lead": {"phone": "+998 90 12", "note": "x"}}
    )
    answer = await _decide(model, "+998 90 12")
    assert answer is not None and answer.lead is None


async def test_a_complaint_about_the_clinic_goes_to_a_person() -> None:
    model = Scripted({"messages": ["Tushunarli, tekshiramiz."]})
    answer = await _decide(model, "Pulimni qaytaring, aldadinglar!")
    assert answer is not None and answer.handoff


async def test_stuck_twice_on_the_same_reply_gives_up() -> None:
    history = [
        {"role": "user", "content": "Varikotsele bor"},
        {"role": "assistant", "content": "Bu holat qachondan beri bor?"},
    ]
    model = Scripted(
        {"messages": ["Bu holat qachondan beri bor?"]},
        {"messages": ["Bu holat qachondan beri bor?"]},
    )
    assert await _decide(model, "1 yil", history) is None


def test_latin_letters_in_a_cyrillic_word_are_made_cyrillic() -> None:
    assert agent._tidy_cyrillic("Oldин бу бўйича") == "Олдин бу бўйича"
    for slip in ("администраторииман", "администраторман", "администратори ман"):
        assert agent._tidy_cyrillic(f"Мен {slip}") == "Мен администраториман"
    assert agent._tidy_cyrillic("Oldin bu bo'yicha") == "Oldin bu bo'yicha"


async def test_the_same_question_asked_again_may_get_the_same_answer() -> None:
    history = [
        {"role": "user", "content": "konsultatsiya narxi qancha"},
        {"role": "assistant", "content": "Narxi 330 000 so'm."},
    ]
    answer = await _decide(
        Scripted({"messages": ["Narxi 330 000 so'm."]}), "konsultatsiya narxi qancha", history
    )
    assert answer is not None and answer.messages == ["Narxi 330 000 so'm."]


async def test_a_reply_in_the_wrong_alphabet_is_written_again() -> None:
    model = Scripted(
        {"messages": ["Bu holat qachondan beri bor?"]},
        {"messages": ["Бу ҳолат қачондан бери бор?"]},
    )
    answer = await agent.decide(
        model,
        profile="sultonbek",
        user_message="Тез бўшаниш бор",
        history=None,
        now_local=NOW,
        slots=SLOTS,
        images=IMAGES,
        script="uz-cyrl",
    )
    assert answer is not None and answer.messages == ["Бу ҳолат қачондан бери бор?"]


async def test_a_card_written_into_the_text_is_taken_out() -> None:
    model = Scripted(
        {
            "messages": ["Narxi 9 000 000 so'm.\n\n📎 op:denervatsiya\n\nAvval konsultatsiya."],
            "cards": ["op:denervatsiya"],
        }
    )
    answer = await _decide(model, "denervatsiya narxi qancha")
    assert answer is not None
    assert answer.messages == ["Narxi 9 000 000 so'm.\n\nAvval konsultatsiya."]
    assert answer.cards == ["op:denervatsiya"]


async def test_a_receipt_asked_for_without_the_card_gets_the_card_line() -> None:
    """2026-11-22: the time changed, "to'lab, chek yuboring" came without the card."""
    history = [
        {"role": "user", "content": "kunduzi kelsam bo'ladimi"},
        {"role": "assistant", "content": "Ertaga 09:00 yoki 10:00 da bo'sh vaqt bor."},
    ]
    model = Scripted(
        {
            "messages": [
                "Yaxshi, ertaga 10:00 ga yozib qo'yishim mumkin.",
                "To'lab, chek rasmini va telefon raqamingizni yuboring.",
            ]
        }
    )
    answer = await _decide(model, "10 00", history)
    assert answer is not None
    assert answer.messages[1] == "To'lov uchun karta: 9860 1501 1048 3446 (Sultonbek Norkuziev)"
    assert answer.messages[2].startswith("To'lab, chek")


async def test_the_card_line_is_not_repeated_right_after_it_was_given() -> None:
    history = [
        {"role": "user", "content": "10:00"},
        {"role": "assistant", "content": "To'lov uchun karta: 9860 1501 1048 3446."},
    ]
    model = Scripted({"messages": ["Ha, chek rasmini yuboring."]})
    answer = await _decide(model, "chekni tashlaymi?", history)
    assert answer is not None and answer.messages == ["Ha, chek rasmini yuboring."]


def test_tsiya_is_written_the_cyrillic_way() -> None:
    assert agent._tidy_cyrillic("аввал консультатсия") == "аввал консультация"
    assert agent._tidy_cyrillic("аввал консултация") == "аввал консультация"


async def test_the_name_given_earlier_is_not_asked_again() -> None:
    """2026-11-24, the doctor: "yana savolim bor" after the card, and the
    name Ayub gave earlier was asked again."""
    history = [
        {"role": "assistant", "content": "Ismingiz nima, sizga qanday murojaat qilsam bo'ladi?"},
        {"role": "user", "content": "ayub"},
        {"role": "assistant", "content": "Ayub, qayerdansiz?"},
        {"role": "user", "content": "jizzahdan"},
        {"role": "assistant", "content": "To'lov uchun karta: 9860 1501 1048 3446."},
    ]
    model = Scripted(
        {"messages": ["Buni ko'rikda doktor baholaydi.", "Ismingiz nima?"]},
        {"messages": ["Buni ko'rikda doktor baholaydi.", "Ayub, chekni kutamiz."]},
    )
    answer = await _decide(model, "testosteron olsam bo'ladimi", history)
    assert answer is not None and answer.messages[1] == "Ayub, chekni kutamiz."
    assert "already told" in model.systems[1]


async def test_a_known_question_asked_twice_is_cut_out() -> None:
    history = [
        {"role": "assistant", "content": "Ayub, qayerdansiz?"},
        {"role": "user", "content": "jizzahdan"},
    ]
    model = Scripted(
        {"messages": ["Tushunarli. Qayerdansiz?"]},
        {"messages": ["Yaxshi. Qayerdansiz?"]},
    )
    answer = await _decide(model, "ha", history)
    assert answer is not None and answer.messages == ["Tushunarli."]


async def test_a_placeholder_copied_from_the_rules_is_taken_out() -> None:
    answer = await _decide(Scripted({"messages": ["<Name>, qayerdansiz?"]}), "ayub")
    assert answer is not None and answer.messages == ["Qayerdansiz?"]


async def test_a_line_already_said_is_not_said_again() -> None:
    """2026-11-25, bilolceo: the testosterone line three replies running."""
    line = "Testosteronni qayerdan va qanday olishni doktor konsultatsiyada tushuntirib beradi."
    history = [
        {"role": "user", "content": "Menga testesterton kerak szda bormi"},
        {"role": "assistant", "content": f"{line}\n\nQanday masala bo'yicha yozyapsiz?"},
    ]
    model = Scripted({"messages": [line, "Bu holat qachondan beri bor?"]})
    answer = await _decide(model, "Shu testesteron boyicha kerak", history)
    assert answer is not None and answer.messages == ["Bu holat qachondan beri bor?"]


async def test_a_reply_that_only_repeats_is_written_again() -> None:
    line = "Testosteronni qayerdan va qanday olishni doktor konsultatsiyada tushuntirib beradi."
    history = [
        {"role": "user", "content": "testosteron kerak"},
        {"role": "assistant", "content": line},
    ]
    model = Scripted({"messages": [line]}, {"messages": ["Sport bilan shug'ullanasizmi?"]})
    answer = await _decide(model, "sportchiman", history)
    assert answer is not None and answer.messages == ["Sport bilan shug'ullanasizmi?"]


async def test_no_times_before_the_place_and_the_choice() -> None:
    """2026-11-25, med.uz.ai: "Качонга борсам болади?" with the name, and the
    times came before the place was ever asked."""
    model = Scripted(
        {
            "messages": ["Sardor, ertaga 09:00 yoki 10:00 da bo'sh vaqt bor."],
            "known": {"place": None, "visit": None},
        },
        {
            "messages": ["Ertaga bo'sh vaqtlar bor. Sardor, qayerdansiz?"],
            "known": {"place": None, "visit": None},
        },
    )
    answer = await _decide(model, "Qachon borsam bo'ladi? Sardor")
    assert answer is not None and answer.messages == [
        "Ertaga bo'sh vaqtlar bor. Sardor, qayerdansiz?"
    ]
    assert "has not yet told where they are from" in model.systems[1]


async def test_times_are_fine_once_place_and_choice_are_known() -> None:
    model = Scripted(
        {
            "messages": ["Ertaga 09:00 yoki 10:00 da bo'sh vaqt bor."],
            "known": {"place": "Jizzax", "visit": "online"},
        }
    )
    answer = await _decide(model, "kunduzi")
    assert answer is not None and answer.messages == ["Ertaga 09:00 yoki 10:00 da bo'sh vaqt bor."]


async def test_one_retry_for_all_step_problems_and_no_new_one_taken() -> None:
    """The checks run together: a retry that drops the times but asks the
    name again is not taken, and the times and the known question both go."""
    history = [
        {"role": "assistant", "content": "Ismingiz nima, sizga qanday murojaat qilsam bo'ladi?"},
        {"role": "user", "content": "Sardor"},
    ]
    model = Scripted(
        {
            "messages": ["Ertaga 09:00 da bo'sh vaqt bor.", "Sardor, qayerdansiz?"],
            "known": {"place": None, "visit": None},
        },
        {
            "messages": ["Bo'sh vaqtlar bor.", "Ismingiz nima?"],
            "known": {"place": None, "visit": None},
        },
    )
    answer = await _decide(model, "qachon borsam bo'ladi", history)
    assert len(model.systems) == 2  # one retry, not one per check
    assert "has not yet told where they are from" in model.systems[1]
    assert answer is not None and answer.messages == ["Sardor, qayerdansiz?"]
