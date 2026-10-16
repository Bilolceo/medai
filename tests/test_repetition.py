"""app.services.repetition: a reply that says again what was already said."""

from app.services import repetition

EARLIER = [
    "Doktor bu bilan shug'ullanadi — aniq sababini va davolash usulini faqat ko'rikdan keyin "
    "aytadi.",
    "Klinika manzili: Toshkent sh., Yunusobod tumani, Moyqo'rg'on ko'chasi 11A-uy.",
]


def test_a_sentence_already_sent_is_caught() -> None:
    reply = (
        "Hozir uyda biror narsa qilish tavsiya etilmaydi. Doktor bu bilan shug'ullanadi — "
        "aniq sababini va davolash usulini faqat ko'rikdan keyin aytadi."
    )
    assert repetition.repeated(reply, EARLIER)


def test_the_same_answer_reworded_at_the_edges_is_caught() -> None:
    reply = (
        "Ha, doktor bu bilan shug'ullanadi, aniq sababini va davolash usulini ko'rikdan keyin "
        "aytadi."
    )
    assert repetition.repeated(reply, EARLIER)


def test_something_new_is_not_a_repeat() -> None:
    reply = (
        "Odatda bu holat davolanadi. Qabulga avvalgi tahlil natijalaringiz va ichayotgan "
        "dorilaringiz ro'yxatini olib keling."
    )
    assert repetition.repeated(reply, EARLIER) == []


def test_an_address_or_a_goodbye_may_be_said_again() -> None:
    assert repetition.repeated(EARLIER[1], EARLIER) == []
    assert repetition.repeated("Rahmat, salomat bo'ling.", ["Rahmat, salomat bo'ling."]) == []


def test_the_first_reply_has_nothing_to_repeat() -> None:
    assert repetition.repeated(EARLIER[0], []) == []


def test_the_same_advice_with_one_word_swapped_is_a_repeat() -> None:
    """Simulation, 2026-10-16: "Eng to'g'ri yo'l" then "Eng to'g'ri chora",
    the rest of the reply the same."""
    first = (
        "Eng toʻgʻri yoʻl — shifokorga koʻrinish: doktor koʻrik va tekshiruv oʻtkazadi, kerak "
        "boʻlsa tahlil yoki UZI belgilaydi."
    )
    second = (
        "Eng toʻgʻri chora — shifokorga koʻrinish: doktor koʻrik oʻtkazadi va kerak boʻlsa "
        "tahlil yoki UZI belgilaydi."
    )
    assert repetition.repeated(second, [first])
