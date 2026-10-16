from app.rag.llm import ChatMessage
from app.services.language import conversation_script, reply_fits, reply_script


def test_the_alphabet_is_the_conversations_own_not_the_last_message() -> None:
    """A telephone number and a time have no alphabet. Deciding per message
    answered "93 444 444" in Cyrillic in the middle of a Latin conversation.
    """
    history: list[ChatMessage] = [
        {"role": "user", "content": "Man kelasi seshanba 10:00ga qabulga yozilmoqchiman"},
        {"role": "assistant", "content": "Исмингизни ёзинг"},
        {"role": "user", "content": "Asadbek Risqiyev"},
    ]

    assert conversation_script(history, "93 444 444") == "uz-latn"
    assert conversation_script(history, "11:00") == "uz-latn"
    # What the assistant wrote does not count, or one reply in the wrong
    # alphabet would justify the next one.
    assert conversation_script(history, "Buyrak ogrigi") == "uz-latn"


def test_a_patient_who_writes_cyrillic_is_answered_in_cyrillic() -> None:
    history: list[ChatMessage] = [{"role": "user", "content": "буйрагим оғрияпти"}]

    assert conversation_script(history, "16:20") == "uz-cyrl"
    assert conversation_script(None, "Здравствуйте") == "ru"


def test_uzbek_typed_on_a_russian_keyboard_is_still_uzbek() -> None:
    """Without "қ", "ў", "ғ", "ҳ" these were answered in Russian."""
    assert reply_script("Рахмат") == "uz-cyrl"
    assert reply_script("Канча сом") == "uz-cyrl"
    assert reply_script("Тошкент да янги Тошмида даволанган ман бир йил олдин") == "uz-cyrl"
    assert reply_script("Салом доктор, качон кабул килсангиз буларкан") == "uz-cyrl"


def test_russian_is_answered_in_russian() -> None:
    assert reply_script("Сколько стоит приём?") == "ru"
    assert reply_script("Добрый день") == "ru"
    assert reply_script("Где вы находитесь") == "ru"
    assert reply_script("Как записаться к врачу") == "ru"
    assert reply_script("Привет") == "ru"


def test_a_short_ok_does_not_switch_the_alphabet() -> None:
    history: list[ChatMessage] = [{"role": "user", "content": "Салом, кабулга ёзилмокчиман"}]

    assert conversation_script(history, "ok") == "uz-cyrl"


def test_anything_unrecognised_is_uzbek_latin() -> None:
    assert reply_script("hello") == "uz-latn"
    assert reply_script("") == "uz-latn"


_RUSSIAN_CHAT: list[ChatMessage] = [
    {"role": "user", "content": "Здравствуйте, хочу записаться к урологу"},
    {"role": "assistant", "content": "Здравствуйте! На какой день вам удобно?"},
]


def test_a_short_russian_reply_keeps_a_russian_conversation_in_russian() -> None:
    """ "Понятно" and "Да, давайте" have no "ы" or "ь"; they tied, the tie went
    to Uzbek, and the Russian patient was answered in Uzbek mid-conversation."""
    for message in ("Понятно", "Да, давайте", "Адрес?", "Доктор принимает?", "Алишер 90 123 45 67"):
        assert conversation_script(_RUSSIAN_CHAT, message) == "ru", message


def test_a_short_uzbek_reply_keeps_an_uzbek_cyrillic_conversation_in_uzbek() -> None:
    history: list[ChatMessage] = [{"role": "user", "content": "Салом, кабулга ёзилмокчиман"}]

    for message in ("Рахмат", "Алишер", "качон келсам булады"):
        assert conversation_script(history, message) == "uz-cyrl", message


def test_russian_typed_in_latin_letters_is_answered_in_russian() -> None:
    assert reply_script("Zdravstvuyte, skolko stoit UZI?") == "ru"
    assert reply_script("privet, mne nado k vrachu") == "ru"
    assert reply_script("uzi qilasizlami") == "uz-latn"
    assert reply_script("doktr qachon ishlidi") == "uz-latn"


def test_an_english_reply_does_not_pass_as_uzbek_or_russian() -> None:
    assert not reply_fits("Hi. What do you need help with?", "uz-latn")
    assert not reply_fits("Hello. Какое время вам удобно?", "ru")
    assert reply_fits("Va alaykum assalom! Qanday yordam kerak edi?", "uz-latn")


def test_a_word_spelled_in_both_alphabets_does_not_pass() -> None:
    assert not reply_fits("Да, доктор принимает. Қай соатда келishingiz қулай?", "ru")
    assert not reply_fits("Ёзувингиз тасдиқланди, уролог Темуr", "uz-cyrl")
    assert reply_fits("Ва алайкум ассалом. Қачон келишингиз қулай?", "uz-cyrl")


def test_an_age_written_with_da_is_uzbek_not_russian() -> None:
    """Live, 2026-10-16: "Ешим 38 да" was answered "Я — администратор..."."""
    assert reply_script("Ешим 38 да") == "uz-cyrl"
    history: list[ChatMessage] = [
        {"role": "user", "content": "Сахр бор"},
        {"role": "assistant", "content": "Нима масалада эдингиз?"},
        {"role": "user", "content": "Уруг чикмеди умуман"},
    ]
    assert conversation_script(history, "Ешим 38 да") == "uz-cyrl"


def test_russian_da_is_still_russian() -> None:
    assert reply_script("Да, давайте запишусь на завтра") == "ru"
