"""A new doctor's bot, from a template and a short list of facts.

What makes the bot *this doctor's* is a handful of settings kept in the
database: the prompt and the worked examples the model copies its style from,
the strict rules (prices, the numbers it may give), and the practice facts
(address, hours, phones). Writing those by hand for each doctor is how a
price from one doctor's rules ends up in another's replies.

A template (``app/doctor_templates/<name>/``) holds them once, with
``<<PLACEHOLDERS>>`` where the doctor's own facts go, and as small modules: an
example or a rule that needs a fact the doctor does not have -- a price for a
service they do not offer -- is left out rather than written with a gap. This
module fills a template from a doctor's facts and returns the settings; it
touches no database, so what it makes can be read and tested before it is
applied (see ``scripts/apply_template.py``).
"""

import json
import re
from collections.abc import Mapping
from datetime import time
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models.tenant import Tenant
from app.services.appointment import _SLOT_MINUTES_RANGE

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "doctor_templates"
_TOKEN = re.compile(r"<<([A-Z0-9_]+)>>")

# The practice facts live in their own settings, which the answer pipeline
# already reads (see app.services.answer._clinic_facts).
DEFAULT_DEBOUNCE_SECONDS = 15

# The doctor's week, as the appointment book reads it (see
# app.services.appointment.schedule_from): the fact name on the left, the
# setting it becomes on the right.
_SCHEDULE_FACTS = {
    "WORK_DAYS": "work_days",
    "WORK_START": "work_start",
    "WORK_END": "work_end",
    "SLOT_MINUTES": "slot_minutes",
}


class TemplateError(ValueError):
    """The template or the doctor's facts cannot make a bot."""


def available() -> list[str]:
    return sorted(p.name for p in TEMPLATES_DIR.iterdir() if (p / "template.json").is_file())


def _clean(facts: Mapping[str, Any]) -> dict[str, str]:
    return {
        key: value.strip()
        for key, value in facts.items()
        if isinstance(value, str) and value.strip()
    }


def _derived(facts: dict[str, str]) -> dict[str, str]:
    """Spellings of a fact the template uses in another place, made here so
    the doctor writes each fact once."""
    out = dict(facts)
    if "PRICE_FAST" in facts:
        out["PRICE_FAST_CAP"] = facts["PRICE_FAST"][:1].upper() + facts["PRICE_FAST"][1:]
    return out


def _fill(text: str, facts: Mapping[str, str], where: str) -> str:
    def put(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in facts:
            raise TemplateError(f"{where}: <<{key}>> has no value")
        return facts[key]

    return _TOKEN.sub(put, text)


def _wanted(item: Mapping[str, Any], facts: Mapping[str, str]) -> bool:
    return all(key in facts for key in item.get("needs", []))


def build_settings(template: str, doctor: Mapping[str, Any]) -> dict[str, Any]:
    """The tenant settings for this doctor, made from ``template``.

    ``doctor`` maps the template's placeholder names to the doctor's facts
    (see data/doctor.example.json). Raises TemplateError when a required fact
    is missing or a placeholder is left unfilled.
    """
    root = TEMPLATES_DIR / template
    manifest_path = root / "template.json"
    if not manifest_path.is_file():
        raise TemplateError(f"no template named {template!r}; there are {available()}")
    manifest = json.loads(manifest_path.read_text())

    facts = _derived(_clean(doctor))
    missing = [key for key in manifest["required"] if key not in facts]
    if missing:
        raise TemplateError("missing facts: " + ", ".join(missing))
    unknown = sorted(
        key
        for key in doctor
        if key not in manifest["required"]
        and key not in manifest["optional"]
        and not key.startswith("_")
        and key not in _EXTRA_KEYS
    )
    if unknown:
        raise TemplateError("unknown facts (a typo?): " + ", ".join(unknown))

    prompt = _fill((root / manifest["prompt"]).read_text(), facts, "prompt")

    examples = []
    for item in manifest["examples"]:
        if _wanted(item, facts):
            examples.append(_fill((root / item["file"]).read_text().strip(), facts, item["file"]))
    numbered = []
    for number, example in enumerate(examples, start=1):
        title, _, body = example.partition("\n")
        numbered.append(f"{number}. {title.strip()}\n{body}".rstrip())

    rules = [
        _fill((root / item["file"]).read_text().strip(), facts, item["file"])
        for item in manifest["rules"]
        if _wanted(item, facts)
    ]

    subtitle = facts["SPECIALTY"][:1].upper() + facts["SPECIALTY"][1:]
    settings: dict[str, Any] = {
        "assistant_prompt": prompt,
        "assistant_examples": "\n\n".join(numbered),
        "strict_rules": rules,
        "clinic_address": facts["ADDRESS"],
        "clinic_work_hours": facts["WORK_HOURS"],
        "clinic_phone_numbers": facts["PHONES"],
        "brand_title": facts.get("BRAND_TITLE", facts["DOCTOR_SHORT"].replace("doktor", "Dr.")),
        "brand_subtitle": subtitle,
        "debounce_seconds": DEFAULT_DEBOUNCE_SECONDS,
        # A new doctor's bot starts off: it is switched on after the test
        # questions have been read, not before.
        "bot_replies_enabled": str(doctor.get("BOT_ENABLED", "")).lower() == "true",
    }
    if "LANDMARK" in facts:
        settings["clinic_landmark"] = facts["LANDMARK"]
    settings.update(_schedule_settings(facts))
    return settings


def _schedule_settings(facts: Mapping[str, str]) -> dict[str, Any]:
    """The week's settings from the schedule facts, each checked here: a value
    the appointment book would shrug off and replace with the default is an
    error at this desk, not a clinic silently open on another clinic's hours."""
    out: dict[str, Any] = {}
    if "WORK_DAYS" in facts:
        days = [int(part) for part in re.findall(r"\d+", facts["WORK_DAYS"])]
        if not days or any(day not in range(1, 8) for day in days):
            raise TemplateError(
                f"WORK_DAYS: {facts['WORK_DAYS']!r} -- write the ISO weekday numbers, "
                '1 (Monday) to 7 (Sunday), like "1,2,3,4,5,6"'
            )
        out["work_days"] = sorted(set(days))
    clock: dict[str, time] = {}
    for fact in ("WORK_START", "WORK_END"):
        if fact in facts:
            try:
                clock[fact] = time.fromisoformat(facts[fact])
            except ValueError:
                raise TemplateError(f"{fact}: {facts[fact]!r} is not HH:MM") from None
            out[_SCHEDULE_FACTS[fact]] = clock[fact].strftime("%H:%M")
    if len(clock) == 2 and clock["WORK_START"] >= clock["WORK_END"]:
        raise TemplateError("WORK_START must be before WORK_END")
    if "SLOT_MINUTES" in facts:
        try:
            minutes = int(facts["SLOT_MINUTES"])
        except ValueError:
            minutes = -1
        if minutes not in _SLOT_MINUTES_RANGE:
            raise TemplateError(f"SLOT_MINUTES: {facts['SLOT_MINUTES']!r} is not 5 to 240")
        out["slot_minutes"] = minutes
    return out


# Facts that configure the account rather than fill a placeholder.
_EXTRA_KEYS = frozenset({"BRAND_TITLE", "BOT_ENABLED"})


async def apply_to_tenant(
    session: AsyncSession,
    settings: Mapping[str, Any],
    *,
    tenant_name: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Store ``settings`` on the tenant and return what it had before.

    Merges over the tenant's settings, so keys the template does not make (a
    review's last run, say) survive. A tenant that already has a prompt is
    refused unless ``force``: a running doctor's hand-tuned bot is worth more
    than a template, and this is the kind of call made against the wrong
    deployment once.
    """
    query = select(Tenant)
    if tenant_name:
        query = query.where(Tenant.name == tenant_name)
    tenants = list((await session.execute(query)).scalars())
    if len(tenants) != 1:
        raise TemplateError(f"expected exactly one tenant, found {len(tenants)}; name one")
    tenant = tenants[0]
    before = dict(tenant.settings or {})
    if before.get("assistant_prompt") and not force:
        raise TemplateError(f"tenant {tenant.name!r} already has a prompt; use force to replace it")
    tenant.settings = {**before, **settings}
    flag_modified(tenant, "settings")
    await session.commit()
    return before
