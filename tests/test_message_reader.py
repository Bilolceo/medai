"""The model's reading of a message, and the flows acting on it."""

import json
from datetime import datetime
from typing import Any

from app.services import bubbles, message_reader, scripted_flow
from app.services.appointment import CLINIC_TIMEZONE
from tests.test_intake_flow import FLOWS

NOW = datetime(2026, 11, 3, 11, 0, tzinfo=CLINIC_TIMEZONE)


def _reading(**fields: Any) -> message_reader.Reading:
    reading = message_reader.parse(json.dumps(fields))
    assert reading is not None
    return reading


def test_parse_takes_the_json_even_wrapped_in_words() -> None:
    reading = message_reader.parse(
        'Here: {"complaint_kind": "urology", "name": "Bilol", "age": 33}'
    )
    assert reading is not None
    assert (reading.complaint_kind, reading.name, reading.age) == ("urology", "Bilol", 33)


def test_parse_refuses_what_is_not_json_and_cleans_odd_values() -> None:
    assert message_reader.parse("no idea") is None
    reading = message_reader.parse('{"complaint_kind": "cancer", "time_hhmm": "soon", "age": 500}')
    assert reading is not None
    assert (reading.complaint_kind, reading.time_hhmm, reading.age) == ("none", None, None)


def _plan(message: str, history: list[dict[str, str]], **fields: Any) -> scripted_flow.Plan | None:
    return scripted_flow.plan(
        FLOWS,
        script="uz-latn",
        user_message=message,
        history=history,
        now=NOW,
        reading=_reading(**fields),
    )


def test_a_complaint_only_the_model_understood_starts_the_sequence() -> None:
    plan = _plan("tez tugatib qo'yaman", [], complaint_kind="urology", complaint="tez tugatish")
    assert plan is not None and plan.reply is not None
    assert bubbles.parts(plan.reply)[-1] == "Ha, doktor shu bilan shug'ullanadi. Ismingiz nima?"


def test_the_sequence_holds_on_the_bots_own_line_after_such_a_start() -> None:
    history = [
        {"role": "user", "content": "tez tugatib qo'yaman"},
        {
            "role": "assistant",
            "content": "Assalomu alaykum. Men doktor Sultonbekning administratoriman, "
            "ismim Dildora.\n\nHa, doktor shu bilan shug'ullanadi. Ismingiz nima?",
        },
    ]
    plan = _plan("bilol", history, name="Bilol")
    assert plan is not None and plan.reply == "Bilol, qayerdansiz?"


def test_a_misspelt_country_the_model_understood_brings_the_online_line() -> None:
    history = [
        {"role": "user", "content": "ereksiya muammosi"},
        {"role": "assistant", "content": "Ha, doktor shu bilan shug'ullanadi. Ismingiz nima?"},
        {"role": "user", "content": "Bilol"},
        {"role": "assistant", "content": "Bilol, qayerdansiz?"},
    ]
    plan = _plan("raseyadaman hozir", history, abroad=True, place="Rossiya")
    assert plan is not None and plan.reply is not None
    assert "onlayn videoqo'ng'iroqda" in bubbles.parts(plan.reply)[0]


def test_a_time_the_model_understood_confirms_the_call() -> None:
    history = [
        {"role": "user", "content": "prostatit"},
        {"role": "assistant", "content": "Ha, doktor shu bilan shug'ullanadi. Ismingiz nima?"},
        {"role": "user", "content": "Akmal"},
        {"role": "assistant", "content": "Akmal, qayerdansiz?"},
        {"role": "user", "content": "Toshkent"},
        {
            "role": "assistant",
            "content": "Operatorimiz qo'ng'iroq qilib hammasini batafsil aytib beradi. "
            "Telefon raqamingizni va qaysi vaqtda qo'ng'iroq qilsak qulayligini yozing.",
        },
    ]
    plan = _plan(
        "ishdan chiqqach qo'ng'iroq qilinglar, 93 345 67 89",
        history,
        phone="93 345 67 89",
        time="ishdan chiqqach",
    )
    assert plan is not None and plan.reply is not None
    assert plan.reply.startswith("Rahmat, Akmal. Operatorimiz ishdan chiqqach +998 93 345 67 89")


def test_a_soft_no_the_model_understood_is_respected() -> None:
    history = [
        {"role": "user", "content": "prostatit"},
        {"role": "assistant", "content": "Ha, doktor shu bilan shug'ullanadi. Ismingiz nima?"},
    ]
    assert _plan("hozircha o'ylab ko'raman", history, refuses=True) is None


def test_an_emergency_the_model_understood_goes_to_the_model() -> None:
    assert _plan("qornim pastida chidab bo'lmas og'riq", [], complaint_kind="emergency") is None


def test_a_time_with_call_me_in_it_is_just_the_time() -> None:
    plan = _plan(
        "Salom, men Otabek, tez bo'shanish bor, raqamim +998 95 890 12 34, "
        "ertaga 10 da qo'ng'iroq qiling",
        [],
        complaint_kind="urology",
        name="Otabek",
        phone="+998 95 890 12 34",
        time="ertaga 10 da qo'ng'iroq qiling",
    )
    assert plan is not None and plan.reply is not None
    assert "Operatorimiz ertaga 10 da +998 95 890 12 34 raqamiga" in plan.reply
