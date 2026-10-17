"""app.services.spelling: misspellings seen in live replies."""

from app.services import spelling


def test_the_cyrillic_introduction_is_spelled_right() -> None:
    assert (
        spelling.fix("Мен доктор Темурнинг администраториаман.")
        == "Мен доктор Темурнинг администраториман."
    )


def test_ekkan_after_a_description_is_ekan() -> None:
    assert spelling.fix("Juda ta’sirli ekkan.") == "Juda ta’sirli ekan."


def test_ekkan_meaning_planted_is_left_alone() -> None:
    assert spelling.fix("Bog'ga daraxt ekkan.") == "Bog'ga daraxt ekkan."


def test_a_reply_without_mistakes_is_unchanged() -> None:
    text = "Assalomu alaykum. Men doktor Temurning administratoriman."
    assert spelling.fix(text) == text


def test_the_introduction_split_in_two_is_joined() -> None:
    assert (
        spelling.fix("Мен доктор Темурнинг администратори ман — нима масалада эдингиз?")
        == "Мен доктор Темурнинг администраториман — нима масалада эдингиз?"
    )


def test_yozilmoqchi_in_cyrillic_is_spelled_right() -> None:
    assert spelling.fix("Язилмоқчи бўлсангиз, ёзинг.") == "Ёзилмоқчи бўлсангиз, ёзинг."
