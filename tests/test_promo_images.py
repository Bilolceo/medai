"""Which price card goes with a reply, and how it travels."""

from app.services import promo_images

CARDS = [
    {"topic": "day", "title": "Kunduzgi konsultatsiya: narx va tartib", "url": "https://x/day.jpg"},
    {"topic": "steroid", "title": "Steroid: narx va tartib", "url": "https://x/steroid.jpg"},
    {"topic": "evening", "title": "Kechki onlayn: narx va tartib", "url": "https://x/evening.jpg"},
]


def _pick(
    message: str, patient: list[str] | None = None, own: list[str] | None = None, reply: str = ""
) -> str | None:
    card = promo_images.choose(
        CARDS, user_message=message, patient_turns=patient or [], own_turns=own or [], reply=reply
    )
    return None if card is None else str(card["topic"])


def test_a_urology_price_question_gets_the_day_card() -> None:
    assert _pick("Prostatit bor, narxi qancha?") == "day"
    assert _pick("Narxi qancha?", patient=["Ereksiya yaxshi emas"]) == "day"


def test_steroids_and_hormones_get_the_steroid_card() -> None:
    assert _pick("Steroid kursidan keyin gormon tekshirmoqchiman, qancha turadi?") == "steroid"
    assert _pick("Сколько стоит?", patient=["После курса тестостерона проблемы"]) == "steroid"


def test_the_evening_wins_whatever_the_topic() -> None:
    assert (
        _pick("Kechqurun ishdan keyin konsultatsiya narxi qancha?", patient=["Steroid"])
        == "evening"
    )


def test_no_price_question_no_card() -> None:
    assert _pick("Prostatit bor") is None


def test_an_unknown_topic_waits_for_the_answer_to_which_question() -> None:
    assert _pick("Narxlar qancha?") is None
    # Asked again instead of naming the matter: the doctor's ordinary card.
    assert _pick("Taxminan qancha?", own=["Qaysi masala bo'yicha konsultatsiya kerak?"]) == "day"
    assert (
        _pick(
            "Varikotsele",
            patient=["Narxlar qancha?"],
            own=["Qaysi masala bo'yicha konsultatsiya kerak?"],
        )
        == "day"
    )


def test_a_reply_pointing_at_the_picture_brings_the_day_card() -> None:
    assert _pick("Narxi qancha?", reply="Konsultatsiya narxi va tartibi rasmda.") == "day"


def test_a_card_goes_once_per_conversation() -> None:
    own = ["Konsultatsiya narxi va tartibi rasmda.\n\n" + promo_images.sent_line(CARDS[0]["title"])]
    assert _pick("Yana narxi qancha?", patient=["Prostatit"], own=own) is None


def test_the_marker_is_split_off_for_delivery_and_named_in_the_transcript() -> None:
    reply = promo_images.attach("Narxi rasmda. ||| Ismingiz nima?", CARDS[0])
    text, urls = promo_images.split(reply)
    assert text == "Narxi rasmda. ||| Ismingiz nima?"
    assert urls == ["https://x/day.jpg"]
    assert promo_images.for_transcript(reply).endswith("📎 Kunduzgi konsultatsiya: narx va tartib")


def test_a_price_by_phone_sentence_gives_way_to_the_card() -> None:
    reply = (
        "Ha, doktor shu bilan shug'ullanadi. Narxlarni operatorimiz qo'ng'iroqda "
        "aniq aytib beradi, holatga qarab farq qiladi. Ismingiz nima?"
    )
    text, _ = promo_images.split(promo_images.attach(reply, CARDS[0]))
    assert text == (
        "Ha, doktor shu bilan shug'ullanadi. Konsultatsiya narxi va tartibi rasmda. Ismingiz nima?"
    )


def test_the_caption_is_in_the_patients_alphabet() -> None:
    text, _ = promo_images.split(promo_images.attach("Исмингиз нима?", CARDS[0], "uz-cyrl"))
    assert text == "Консультация нархи ва тартиби расмда. Исмингиз нима?"


def test_an_evening_call_wish_gets_the_evening_card_without_a_price_question() -> None:
    assert _pick("Menga soat 19:00 da qo'ng'iroq qiling, Rossiyadaman") == "evening"


def test_after_hours_a_wish_to_book_gets_the_evening_card() -> None:
    card = promo_images.choose(
        CARDS,
        user_message="Qabulga yozilmoqchiman",
        patient_turns=["Prostatit bor"],
        own_turns=[],
        reply="",
        hour=18,
    )
    assert card is not None and card["topic"] == "evening"


def test_in_the_day_a_wish_to_book_alone_sends_no_card() -> None:
    card = promo_images.choose(
        CARDS,
        user_message="Qabulga yozilmoqchiman",
        patient_turns=["Prostatit bor"],
        own_turns=[],
        reply="",
        hour=11,
    )
    assert card is None


def test_the_evening_card_says_what_the_evening_is_and_promises_no_call() -> None:
    reply = (
        "Rossiyada bo'lsangiz ham operator 19:00 da qo'ng'iroq qiladi. Telefon raqamingizni yozing."
    )
    text, _ = promo_images.split(promo_images.attach(reply, CARDS[2]))
    assert "qo'ng'iroq qiladi" not in text
    assert text.startswith("Ish vaqtidan keyin doktor bilan faqat onlayn konsultatsiya bor")


def test_a_sentence_said_word_for_word_last_time_is_not_said_again() -> None:
    last = "Telefon raqamingizni va qaysi vaqtda qo'ng'iroq qilsak qulayligini yozing."
    reply = "Telefon raqamingizni va qaysi vaqtda qo'ng'iroq qilsak qulayligini yozing."
    text, _ = promo_images.split(promo_images.attach(reply, CARDS[0], own_turns=[last]))
    assert text == "Konsultatsiya narxi va tartibi rasmda."


def test_asking_the_hours_after_hours_is_not_a_wish_for_the_evening() -> None:
    for asked in ("Ish vaqtingiz?", "Иш вақтингиз?", "Ishvoht nechida tugaydi"):
        card = promo_images.choose(
            CARDS, user_message=asked, patient_turns=["Prostatit"], own_turns=[], reply="", hour=18
        )
        assert card is None, asked
