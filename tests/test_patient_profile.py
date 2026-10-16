"""Who the patient is, kept where ten turns of history cannot lose it."""

import pytest

from app.rag.llm import ChatMessage
from app.services.patient_profile import (
    Found,
    asked_for_a_name,
    asks_for_language,
    looks_like_a_name,
    normalise_phone,
    read_turn,
)

# --- the number ---------------------------------------------------------


@pytest.mark.parametrize(
    "written",
    [
        "93 951 11 11",
        "+998 93 951 11 11",
        "998939511111",
        "+998939511111",
        "93-951-11-11",
        "Asadbek Risqiyev 93 951 11 11",
        "raqamim 93 951 11 11, qo'ng'iroq qiling",
    ],
)
def test_one_patient_is_one_number_however_they_type_it(written: str) -> None:
    assert normalise_phone(written) == "+998939511111"


@pytest.mark.parametrize(
    "written",
    [
        "2026-09-24",  # a date
        "12:00",  # a time
        "35 yoshdaman",  # an age
        "narxi 350000 so'm",  # a price
        "123456789",  # nine digits, no operator code
        "salom",
    ],
)
def test_what_is_not_a_number_is_not_stored_as_one(written: str) -> None:
    assert normalise_phone(written) is None


# --- the name -----------------------------------------------------------


def test_the_answer_to_the_question_is_the_name() -> None:
    assert read_turn("Asadbek Risqiyev", asked_for_name=True) == Found(name="Asadbek Risqiyev")


def test_a_name_and_a_number_in_one_message_are_both_read() -> None:
    found = read_turn("Asadbek Risqiyev 93 951 11 11", asked_for_name=True)

    assert found.name == "Asadbek Risqiyev"
    assert found.phone == "+998939511111"


def test_a_patient_who_says_their_name_is_taken_at_their_word() -> None:
    """No question needed: they said it as a fact."""
    assert read_turn("Ismim Asadbek Risqiyev", asked_for_name=False).name == "Asadbek Risqiyev"
    assert read_turn("меня зовут Ирина Петрова", asked_for_name=False).name == "Ирина Петрова"


def test_somebody_mentioned_in_passing_is_not_the_patient() -> None:
    """The failure this guard exists for: "Asadbek aka bilan gaplashmoqchiman"
    names somebody else, and nobody's record should change because of it.
    """
    assert read_turn("Asadbek aka bilan gaplashmoqchiman", asked_for_name=False).name is None


@pytest.mark.parametrize(
    "answer", ["ha", "rahmat", "salom", "yaxshi", "uzi", "qabul", "12:00", "93 951 11 11"]
)
def test_words_shaped_like_a_name_that_are_not_one(answer: str) -> None:
    assert not looks_like_a_name(answer)


def test_a_sentence_is_not_a_name() -> None:
    assert not looks_like_a_name("ertaga ertalabga yozilsam bo'ladimi aka")


def test_a_correction_is_recognised_as_one() -> None:
    found = read_turn("yo'q, ismim aslida Asadbek Risqiyev", asked_for_name=False)

    assert found.name == "Asadbek Risqiyev"
    assert found.corrects is True


# --- reading the question above the answer ------------------------------


def test_the_assistants_last_question_decides_what_a_bare_reply_is() -> None:
    history: list[ChatMessage] = [
        {"role": "user", "content": "qabulga yozilmoqchiman"},
        {"role": "assistant", "content": "To'liq ism-familiyangizni yozing."},
    ]

    assert asked_for_a_name(history) is True

    history.append({"role": "user", "content": "Asadbek Risqiyev"})
    history.append({"role": "assistant", "content": "Telefon raqamingizni yozing."})

    assert asked_for_a_name(history) is False


@pytest.mark.parametrize(
    ("message", "language"),
    [
        # Refusing Russian is not asking for it. These were all saved as
        # "ru", and the patient was answered in Russian from then on.
        ("Ruscha emas, o'zbekcha yozing", "uz-latn"),
        ("ruscha emas ozbekcha yozing iltimos", "uz-latn"),
        ("Men ruscha bilmayman", "uz-latn"),
        ("Rus tilida yozmang", "uz-latn"),
        # Every way of asking for Uzbek, not only "o'zbekcha yozing".
        ("uzbekcha yozing", "uz-latn"),
        ("o'zbek tilida iltimos", "uz-latn"),
        ("ўзбекча ёзинг", "uz-cyrl"),
        ("пишите по-узбекски", "uz-cyrl"),
        # Asking for Russian, and refusing Uzbek, still mean Russian.
        ("давайте по-русски", "ru"),
        ("ruscha yozing", "ru"),
        ("Я не понимаю по-узбекски", "ru"),
        ("o'zbekcha bilmayman ruscha yozing", "ru"),
        ("buyragim ogriyapti", None),
    ],
)
def test_a_language_request_is_read_with_its_negation(message: str, language: str | None) -> None:
    assert asks_for_language(message) == language


@pytest.mark.parametrize(
    ("text", "number"),
    [
        # Live, 2026-10-15: nine digits that happen to begin 998.
        ("998772882", "+998998772882"),
        ("998931209810", "+998931209810"),
        ("90 111 22 33", "+998901112233"),
    ],
)
def test_one_shape_for_every_number(text: str, number: str) -> None:
    assert normalise_phone(text) == number


@pytest.mark.parametrize("word", ["Hop", "Ho‘p", "xo'p", "Ok", "Albatta", "Хоп"])
def test_an_acknowledgement_is_not_a_name(word: str) -> None:
    """Live, 2026-10-15: "Hop" (xo'p) was booked as the patient's name."""
    assert not looks_like_a_name(word)
