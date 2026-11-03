"""A reply with ||| in it goes out as several messages and is kept as one."""

from app.services import bubbles


def test_a_reply_without_a_break_is_one_message() -> None:
    assert bubbles.parts("Ismingiz nima?") == ["Ismingiz nima?"]
    assert bubbles.flatten("Ismingiz nima?") == "Ismingiz nima?"


def test_the_break_splits_the_reply_in_order_and_drops_empty_parts() -> None:
    reply = (
        "Men doktor Sultonbekning administratoriman, ismim Dildora. "
        "||| Sizga qanday yordam bera olamiz? |||"
    )
    assert bubbles.parts(reply) == [
        "Men doktor Sultonbekning administratoriman, ismim Dildora.",
        "Sizga qanday yordam bera olamiz?",
    ]


def test_the_transcript_keeps_one_row_with_a_blank_line_at_the_break() -> None:
    reply = "Assalomu alaykum. Men administratorman.|||Ismingiz nima?"
    assert bubbles.flatten(reply) == "Assalomu alaykum. Men administratorman.\n\nIsmingiz nima?"
