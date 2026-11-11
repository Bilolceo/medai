"""The operation package cards: only when the patient names the operation."""

from app.services import bubbles, promo_images, scripted_flow
from app.services.doctor_template import build_settings
from tests.test_intake_flow import FLOWS, NOW
from tests.test_scripted_flow import FACTS

BASE = "https://example.test/promo/op-"
OPS = {
    f"PROMO_OP_{key}_URL": f"{BASE}{key.lower()}.jpg"
    for key in (
        "VARIKOSELE",
        "SKROTOPLASTIKA",
        "LIGAMENTOTOMIYA",
        "KOMPLEKS",
        "FALLOPROTEZ",
        "MICRO_TESE",
        "DENERVATSIYA",
    )
}
PRICES = {f"OP_PRICE_{k.split('_', 2)[2].rsplit('_URL', 1)[0]}": "9 000 000 so'm" for k in OPS}
SETTINGS = build_settings("urolog-intake", {**FACTS, **OPS, **PRICES})
CARDS = SETTINGS["promo_images"]


def _asked(message: str, own: list[str] | None = None) -> list[str]:
    found = promo_images.operations_asked(CARDS, user_message=message, own_turns=own or [])
    return [str(c["topic"]) for c in found]


def test_the_template_carries_the_seven_operation_cards() -> None:
    topics = {c["topic"] for c in CARDS}
    assert {"op:varikosele", "op:falloprotez", "op:micro-tese", "op:denervatsiya"} <= topics


def test_an_operation_named_brings_its_card() -> None:
    assert _asked("Varikotsele operatsiyasi qancha turadi?") == ["op:varikosele"]
    assert _asked("Falloprotez qo'yasizlarmi?") == ["op:falloprotez"]
    assert _asked("Micro-TESE qilasizlarmi?") == ["op:micro-tese"]
    assert _asked("Selektiv denervatsiya narxi") == ["op:denervatsiya"]
    assert _asked("Скротопластика сколько стоит?") == ["op:skrotoplastika"]


def test_enlargement_brings_both_enlargement_cards() -> None:
    assert _asked("Olatni kattalashtirish narxi qancha?") == ["op:ligamentotomiya", "op:kompleks"]


def test_a_complaint_brings_the_card_of_its_operation() -> None:
    # The owner's order, 2026-11-09: by what the patient writes about.
    assert _asked("Varikotsele bor") == ["op:varikosele"]
    assert _asked("Tez tugatib qo'yaman") == ["op:denervatsiya"]
    assert _asked("Spermogrammada spermatozoid yo'q") == ["op:micro-tese"]
    assert _asked("Ereksiya yaxshi emas") == ["op:falloprotez"]
    assert _asked("Menda ereksiya bolmoqda") == ["op:falloprotez"]
    assert _asked("Olatim kichik") == ["op:ligamentotomiya", "op:kompleks"]
    assert _asked("Проблемы с потенцией") == ["op:falloprotez"]


def test_a_complaint_with_no_operation_brings_none() -> None:
    assert _asked("Prostatit bor") == []
    assert _asked("Siydik tez-tez kelyapti") == []
    assert _asked("Jinsiy xohishim yo'q") == []


def test_each_operation_card_goes_once() -> None:
    sent = [f"Ha.\n\n{promo_images.sent_line('Falloprotez: narx va paket')}"]
    assert _asked("Falloprotez qancha?", sent) == []


def test_the_card_comes_with_the_doctor_decides_line_before_the_question() -> None:
    cards = promo_images.operations_asked(CARDS, user_message="Falloprotez qancha?", own_turns=[])
    reply = promo_images.attach_operations(
        "Salom. ||| Ha, doktor shu bilan shug'ullanadi. Ismingiz nima?", cards
    )
    last = bubbles.parts(promo_images.MARKER.sub("", reply))[-1]
    assert last == (
        "Ha, doktor shu bilan shug'ullanadi. Operatsiya kerakligi va usulini doktor ko'rikda "
        "aniqlaydi. Ismingiz nima?"
    )
    assert "[[IMAGE:https://example.test/promo/op-falloprotez.jpg|" in reply


def test_an_operation_named_is_a_complaint_for_the_flow() -> None:
    plan = scripted_flow.plan(
        FLOWS, script="uz-latn", user_message="Falloprotez qo'yasizlarmi?", history=[], now=NOW
    )
    assert plan is not None
    text = plan.reply or plan.then or ""
    assert "Ha, doktor shu bilan shug'ullanadi." in text


def test_the_operation_price_asked_again_is_said_in_words() -> None:
    flows = SETTINGS["scripted_flows"]
    history = [
        {"role": "user", "content": "1- komponentli protez narxi ?"},
        {
            "role": "assistant",
            "content": "Ha, doktor shu bilan shug'ullanadi. Ismingiz nima, sizga qanday "
            "murojaat qilsam bo'ladi?\n\n📎 Falloprotez: narx va paket",
        },
    ]
    plan = scripted_flow.plan(
        flows,
        script="uz-latn",
        user_message="1- komponentli protez narxi ?",
        history=history,
        now=NOW,
    )
    assert plan is not None and plan.reply is not None
    assert plan.reply.startswith(
        "Falloprotez (bir komponentli protez) narxi 9 000 000 so'm, barcha xarajatlar narx ichida."
    )


def test_kazakh_spelling_of_enlargement_brings_the_cards() -> None:
    assert _asked("Жыныси оладны каталаштырыш оператсиасы неш пул болады") == [
        "op:ligamentotomiya",
        "op:kompleks",
    ]


def test_operation_prices_asked_without_a_name_bring_no_card() -> None:
    # The owner, 2026-11-11: "operatsiya" alone never sends all the cards --
    # the reply asks which operation; the card comes once it is named.
    assert _asked("Operatsiya narxlari qancha?") == []
    assert _asked("Операция нархи қанча?") == []
    assert _asked("Operatsiya qilasizlarmi?") == []
    assert _asked("Operatsiyalar bormi sizlarda?") == []
    assert _asked("Varikotsele operatsiyasi narxi qancha?") == ["op:varikosele"]
    assert _asked("Operatsiya narxi qancha?", ["📎 Falloprotez: narx va paket"]) == []


def _cards(
    message: str, patient: list[str] | None = None, own: list[str] | None = None, reply: str = ""
) -> list[str]:
    found = promo_images.operations_asked(
        CARDS, user_message=message, own_turns=own or [], patient_turns=patient or [], reply=reply
    )
    return [str(c["topic"]) for c in found]


def test_nechpul_is_a_price_question() -> None:
    # Production chat 2026-11-09: "Операция нечпул?"; since 2026-11-11 no card
    # until the operation is named -- but the one talked about, yes.
    assert _cards("Операция нечпул?") == []
    assert _cards("Операция нечпул?", patient=["Варикоцеле бор"]) == ["op:varikosele"]


def test_a_misspelt_varicocele_with_nechpul_sends_its_card() -> None:
    assert _cards("Варикацеле бор\nНечпул слада") == ["op:varikosele"]


def test_asking_where_the_picture_is_sends_the_cards_talked_about() -> None:
    patient = ["Варикацеле бор", "Операция нечпул?"]
    assert _cards("Расм кани", patient=patient) == ["op:varikosele"]
    assert len(_cards("rasm qani", patient=["Operatsiya qilish kerakmi?"])) == 7


def test_the_patients_own_photo_sends_no_card() -> None:
    assert _cards("rasm yubordim", patient=["Varikotsele bor"]) == []
    assert _cards("Расм кани", patient=["Салом", "Простатит"]) == []


def test_a_reply_that_points_at_the_picture_carries_it() -> None:
    reply = "Операциянинг нархи ва пакети расмда, қайси операция кераклигини доктор аниқлайди."
    assert len(_cards("Операция қанақа бўлади?", reply=reply)) == 7
    assert _cards("Salom", reply="Rasmda qizarish ko'rinyapti.") == []


def test_the_picture_asked_again_is_sent_again() -> None:
    own = [promo_images.sent_line(str(c["title"])) for c in CARDS if c["topic"] == "op:varikosele"]
    assert _cards("Расм кани", patient=["Варикацеле бор"], own=own) == ["op:varikosele"]


def test_a_complaint_in_everyday_words_brings_its_card() -> None:
    # The owner, 2026-11-09: patients do not write the medical term.
    assert _asked("Kuchim yo'q") == ["op:falloprotez"]
    assert _asked("Olatim turmayapti") == ["op:falloprotez"]
    assert _asked("Кучим йўқ, турмаяпти") == ["op:falloprotez"]
    assert _asked("Не стоит") == ["op:falloprotez"]
    assert _asked("Moyagimda tomir bor") == ["op:varikosele"]
    assert _asked("Tez ketib qolaman") == ["op:denervatsiya"]
    assert _asked("Olatim kichkina") == ["op:ligamentotomiya", "op:kompleks"]
    assert _asked("Farzand bo'lmayapti") == ["op:micro-tese"]
    assert _asked("Moyagim osilib qolgan") == ["op:skrotoplastika"]


def test_everyday_words_with_no_operation_bring_none() -> None:
    for message in ("Xohishim yo'q", "Moyagim og'riyapti", "Siydik tez-tez", "Salom"):
        assert _asked(message) == [], message


SPORT_CARDS = build_settings(
    "urolog-intake",
    {**FACTS, **OPS, **PRICES, "PROMO_SPORT_URL": "https://example.test/promo/sport.jpg"},
)["promo_images"]


def _sport(message: str, own: list[str] | None = None) -> list[str]:
    found = promo_images.operations_asked(SPORT_CARDS, user_message=message, own_turns=own or [])
    return [str(c["topic"]) for c in found]


def test_steroids_and_hormones_bring_their_card_once() -> None:
    assert _sport("Steroid ishlatganman") == ["sport"]
    assert _sport("Gormonlarim buzildi") == ["sport"]
    assert _sport("Testosteron past chiqdi") == ["sport"]
    assert _sport("После курса проблемы") == ["sport"]
    sent = [promo_images.sent_line("Anabolik steroidlar va gormonal muammolar: konsultatsiya")]
    assert _sport("Steroid ishlatganman", sent) == []


def test_the_steroid_card_has_no_operation_note() -> None:
    card = [c for c in SPORT_CARDS if c["topic"] == "sport"]
    reply = promo_images.attach_operations("Ismingiz nima?", card)
    assert "Operatsiya" not in reply


def test_a_dollar_course_is_not_a_steroid_course() -> None:
    assert _sport("Dollar kursi qancha?") == []


def test_misspelt_varicocele_brings_its_card() -> None:
    # Production chat 2026-11-09: "Varakatsel bn" sent no card.
    assert _asked("Varakatsel bn") == ["op:varikosele"]


def test_sperm_retrieval_in_everyday_words_is_micro_tese() -> None:
    assert _asked("Тухумдан уруг топиш операция клалми?") == ["op:micro-tese"]
    assert _asked("Тухимдан уриг топиш") == ["op:micro-tese"]
    assert _asked("Moyakdan urug' topish mumkinmi?") == ["op:micro-tese"]


def test_operation_price_after_one_operation_sends_only_that_card() -> None:
    # Production chat 2026-11-09: "Нечпул операция" after denervation sent six cards.
    sent = [promo_images.sent_line("Selektiv denervatsiya: narx va paket")]
    assert _cards("Нечпул операция", patient=["Денервация нечпул"], own=sent) == []
    assert _cards("Операция нечпул", patient=["Варикоцеле бор"]) == ["op:varikosele"]
    # "Boshqa operatsiyalar?" too: the reply asks which, no flood of cards.
    varik = [promo_images.sent_line("Varikosele operatsiyasi: narx va paket")]
    assert (
        _cards("Boshqa operatsiyalar narxi qancha?", patient=["Varikotsele bor"], own=varik) == []
    )
    # The complaint and the question in one message: that card alone.
    assert _cards("Moyagimda tomir bor, operatsiya narxi qancha?") == ["op:varikosele"]


def test_a_lab_result_about_sperm_is_a_complaint_not_an_operation_asked() -> None:
    flows = SETTINGS["scripted_flows"]
    ask = "Ha, doktor shu bilan shug'ullanadi. Ismingiz nima, sizga qanday murojaat qilsam bo'ladi?"
    history = [
        {"role": "user", "content": "Tahlilda urug' topilmadi"},
        {"role": "assistant", "content": f"{ask}\n\n📎 Micro-TESE: narx va paket"},
    ]
    plan = scripted_flow.plan(
        flows, script="uz-latn", user_message="Narxi qancha?", history=history, now=NOW
    )
    assert plan is not None and plan.reply is not None
    assert "9 000 000" not in plan.reply


def test_the_price_in_words_is_of_the_operation_talked_about_last() -> None:
    # Production chat 2026-11-09: all seven prices in words after Micro-TESE was asked.
    flows = SETTINGS["scripted_flows"]
    ask = "Ha, doktor shu bilan shug'ullanadi. Ismingiz nima, sizga qanday murojaat qilsam bo'ladi?"
    history = [
        {"role": "user", "content": "Denervatsiya narxi?"},
        {"role": "assistant", "content": f"{ask}\n\n📎 Selektiv denervatsiya: narx va paket"},
        {"role": "user", "content": "Moyakdan urug' topish operatsiyasi narxi?"},
        {"role": "assistant", "content": f"{ask}\n\n📎 Micro-TESE: narx va paket"},
    ]
    plan = scripted_flow.plan(
        flows, script="uz-latn", user_message="Narxi qancha?", history=history, now=NOW
    )
    assert plan is not None and plan.reply is not None
    assert plan.reply.startswith("Micro-TESE narxi 9 000 000 so'm")
    assert "denervatsiya" not in plan.reply.lower()


def test_micro_tese_is_spelt_right_in_cyrillic() -> None:
    line = SETTINGS["scripted_flows"]["intake"]["operation_prices"]["Micro-TESE: narx va paket"]
    assert line["uz-cyrl"].startswith("Микро-TESE нархи")
