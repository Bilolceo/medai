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
CARDS = build_settings("urolog-intake", {**FACTS, **OPS})["promo_images"]


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


def test_a_complaint_alone_brings_no_operation_card() -> None:
    assert _asked("Varikotsele bor") == []
    assert _asked("Tez tugatib qo'yaman") == []
    assert _asked("Spermogrammada spermatozoid yo'q") == []
    assert _asked("Ereksiya yaxshi emas") == []


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
