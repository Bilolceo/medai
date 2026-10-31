"""app.services.doctor_template: a new doctor's bot from a template and facts."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from app.services import doctor_template
from app.services.appointment import DEFAULT_SCHEDULE, schedule_from
from app.services.doctor_template import TemplateError, build_settings

FACTS: dict[str, Any] = json.loads(Path("data/doctor.example.json").read_text())


def _facts(**changes: Any) -> dict[str, Any]:
    facts = dict(FACTS)
    for key, value in changes.items():
        if value is None:
            facts.pop(key, None)
        else:
            facts[key] = value
    return facts


def test_the_example_facts_make_a_complete_bot() -> None:
    settings = build_settings("urolog", FACTS)

    assert settings["clinic_phone_numbers"] == FACTS["PHONES"]
    assert settings["brand_title"] == "Dr. Vali"
    assert settings["brand_subtitle"] == "Urolog"
    assert len(settings["strict_rules"]) == 12
    assert settings["assistant_examples"].startswith("1. Salomlashuv")
    # The prompt is written in the doctor's name.
    assert "Aliyev Vali Salimovichning Instagram sahifasiga" in settings["assistant_prompt"]
    assert "doktor Valining administratoriman" in settings["assistant_prompt"]


def test_nothing_is_left_unfilled_and_nothing_of_another_doctor_is_left() -> None:
    settings = build_settings("urolog", FACTS)
    everything = json.dumps(settings, ensure_ascii=False)

    assert not re.search(r"<<[A-Z_]+>>", everything)
    # The doctor the template was made from, his numbers and his address.
    for stranger in ("Temur", "Akhmadaliev", "Axmadaliyev", "99 899", "310 40 40", "Moyqo"):
        assert stranger not in everything, stranger
    # The one greeting the owner asked for, not the old one.
    assert "Qanday yordam bera olamiz?" in settings["assistant_prompt"]
    assert "Nima masalada" not in everything and "nima masalada" not in everything


def test_every_price_in_the_rules_is_the_doctors_own() -> None:
    settings = build_settings("urolog", _facts(PRICE_CONSULT="150 000 so'm", PRICE_IMPLANT=None))
    rules = "\n".join(settings["strict_rules"])

    assert "150 000 so'm" in rules
    assert "300 000" not in rules and "30 mln" not in rules


def test_a_service_the_doctor_does_not_offer_leaves_no_trace() -> None:
    """No implant price: its rule, its types rule and the examples that quote
    it are left out -- never written with a gap, never with somebody else's."""
    settings = build_settings("urolog", _facts(PRICE_IMPLANT=None))

    assert len(settings["strict_rules"]) == 10
    text = settings["assistant_examples"] + "\n".join(settings["strict_rules"])
    assert "implant" not in text.lower().replace("implantmi", "")
    # The example list is renumbered with no hole in it.
    numbers = [int(n) for n in re.findall(r"^(\d+)\. ", settings["assistant_examples"], re.M)]
    assert numbers == list(range(1, len(numbers) + 1))


def test_a_doctor_with_no_prices_at_all_still_gets_a_working_bot() -> None:
    settings = build_settings(
        "urolog",
        _facts(PRICE_CONSULT=None, PRICE_FAST=None, PRICE_IMPLANT=None, SERVICES_CHOICE=None),
    )

    assert len(settings["strict_rules"]) == 8
    assert not re.search(r"so'm|mln|\$", "\n".join(settings["strict_rules"]))


def test_a_missing_required_fact_is_named() -> None:
    with pytest.raises(TemplateError, match="PHONES"):
        build_settings("urolog", _facts(PHONES=None))
    with pytest.raises(TemplateError, match="ADDRESS"):
        build_settings("urolog", _facts(ADDRESS="   "))


def test_a_misspelled_fact_is_refused_rather_than_ignored() -> None:
    with pytest.raises(TemplateError, match="PRICE_CONSULTT"):
        build_settings("urolog", _facts(PRICE_CONSULTT="300 000 so'm"))


def test_an_unknown_template_lists_the_ones_there_are() -> None:
    with pytest.raises(TemplateError, match="urolog"):
        build_settings("stomatolog", FACTS)
    assert "urolog" in doctor_template.available()


def test_a_new_doctors_bot_starts_switched_off() -> None:
    assert build_settings("urolog", FACTS)["bot_replies_enabled"] is False
    assert build_settings("urolog", _facts(BOT_ENABLED="true"))["bot_replies_enabled"] is True


def test_the_phones_in_the_rules_are_only_the_doctors() -> None:
    """The phone guard lets through only the numbers in the settings, so the
    rules must not name any other."""
    settings = build_settings("urolog", FACTS)
    allowed = {re.sub(r"\D", "", n) for n in FACTS["PHONES"].split(",")}
    written = re.findall(
        r"\+?998[\d\s-]{9,}", "\n".join(settings["strict_rules"]) + settings["assistant_prompt"]
    )
    assert {re.sub(r"\D", "", n) for n in written} <= allowed


# --- storing it ----------------------------------------------------------------


async def test_the_bot_is_stored_on_the_tenant_and_the_old_settings_come_back(
    db_session: Any, seed: Any
) -> None:
    seed.tenant_a.settings = {"review_last_run": "2026-10-20", "bot_replies_enabled": True}
    await db_session.flush()
    settings = build_settings("urolog", FACTS)

    before = await doctor_template.apply_to_tenant(
        db_session, settings, tenant_name=seed.tenant_a.name
    )

    assert before == {"review_last_run": "2026-10-20", "bot_replies_enabled": True}
    await db_session.refresh(seed.tenant_a)
    stored = seed.tenant_a.settings
    assert stored["assistant_prompt"] == settings["assistant_prompt"]
    assert stored["clinic_phone_numbers"] == FACTS["PHONES"]
    # Kept: what the template does not make. Replaced: what it does -- off.
    assert stored["review_last_run"] == "2026-10-20"
    assert stored["bot_replies_enabled"] is False


async def test_a_doctor_whose_bot_is_already_tuned_is_never_overwritten(
    db_session: Any, seed: Any
) -> None:
    seed.tenant_a.settings = {"assistant_prompt": "hand-tuned, live"}
    await db_session.flush()
    settings = build_settings("urolog", FACTS)

    with pytest.raises(TemplateError, match="already has a prompt"):
        await doctor_template.apply_to_tenant(db_session, settings, tenant_name=seed.tenant_a.name)
    await db_session.refresh(seed.tenant_a)
    assert seed.tenant_a.settings["assistant_prompt"] == "hand-tuned, live"

    await doctor_template.apply_to_tenant(
        db_session, settings, tenant_name=seed.tenant_a.name, force=True
    )
    await db_session.refresh(seed.tenant_a)
    assert seed.tenant_a.settings["assistant_prompt"] == settings["assistant_prompt"]


async def test_with_two_doctors_in_one_database_the_tenant_must_be_named(
    db_session: Any, seed: Any
) -> None:
    settings = build_settings("urolog", FACTS)

    with pytest.raises(TemplateError, match="exactly one tenant"):
        await doctor_template.apply_to_tenant(db_session, settings)


# --- the week -------------------------------------------------------------------


def test_the_schedule_facts_become_the_appointment_books_settings() -> None:
    settings = build_settings("urolog", FACTS)

    assert settings["work_days"] == [1, 2, 3, 4, 5]
    assert settings["work_start"] == "10:00"
    assert settings["work_end"] == "18:00"
    assert settings["slot_minutes"] == 30
    # ...and the appointment book reads them back as given.
    week = schedule_from(settings)
    assert week.work_days == frozenset({0, 1, 2, 3, 4})
    assert (week.work_start.hour, week.work_end.hour, week.slot_minutes) == (10, 18, 30)


def test_a_doctor_who_gives_no_schedule_leaves_the_book_on_its_default() -> None:
    settings = build_settings(
        "urolog", _facts(WORK_DAYS=None, WORK_START=None, WORK_END=None, SLOT_MINUTES=None)
    )

    assert not {"work_days", "work_start", "work_end", "slot_minutes"} & settings.keys()
    assert schedule_from(settings) == DEFAULT_SCHEDULE


@pytest.mark.parametrize(
    ("fact", "value", "message"),
    [
        ("WORK_DAYS", "1-8", "WORK_DAYS"),
        ("WORK_DAYS", "har kuni", "WORK_DAYS"),
        ("WORK_START", "9 dan", "HH:MM"),
        ("WORK_END", "25:00", "HH:MM"),
        ("SLOT_MINUTES", "3", "SLOT_MINUTES"),
        ("SLOT_MINUTES", "yarim soat", "SLOT_MINUTES"),
    ],
)
def test_a_schedule_fact_the_book_could_not_read_is_refused(
    fact: str, value: str, message: str
) -> None:
    """Not silently replaced with the default -- which would open another
    clinic's hours for this doctor."""
    with pytest.raises(TemplateError, match=message):
        build_settings("urolog", _facts(**{fact: value}))


def test_a_clinic_that_closes_before_it_opens_is_refused() -> None:
    with pytest.raises(TemplateError, match="before"):
        build_settings("urolog", _facts(WORK_START="18:00", WORK_END="10:00"))
