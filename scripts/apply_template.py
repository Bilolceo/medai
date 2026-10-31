r"""Give a tenant a doctor's bot: fill a template with the doctor's facts and
store the result as the tenant's settings.

The doctor's facts come from a JSON file shaped like data/doctor.example.json
(see docs/NEW_DOCTOR.md). Nothing is written unless you ask:

    DRY_RUN=1 python scripts/apply_template.py data/dr-karimov.json     # print only
    python scripts/apply_template.py data/dr-karimov.json               # write

Environment:

    TEMPLATE     which template under app/doctor_templates (default "urolog")
    TENANT_NAME  which tenant, when the database has more than one
    DRY_RUN=1    print what would be written and stop
    FORCE=1      overwrite a tenant that already has an assistant_prompt

A tenant that already has a prompt is left alone unless FORCE=1: a running
doctor's hand-tuned bot is worth more than a template, and this script is the
kind of command that gets run against the wrong deployment once.

The tenant's current settings are printed between BACKUP_BEGIN and BACKUP_END
before anything changes, so they can be restored.
"""

import asyncio
import json
import os
import re
import sys
from pathlib import Path

from app.core.db import db_session
from app.services import doctor_template


def _facts() -> dict[str, object]:
    raw = os.environ.get("DOCTOR_JSON")
    if raw is None:
        if len(sys.argv) < 2:
            sys.exit(f"Usage: {sys.argv[0]} <doctor facts .json>   (or DOCTOR_JSON='{{...}}')")
        raw = Path(sys.argv[1]).read_text()
    facts = json.loads(raw)
    if not isinstance(facts, dict):
        sys.exit("The doctor's facts must be a JSON object")
    return facts


async def main() -> None:
    template = os.environ.get("TEMPLATE", "urolog")
    try:
        settings = doctor_template.build_settings(template, _facts())
    except doctor_template.TemplateError as exc:
        sys.exit(f"Cannot build the bot: {exc}")

    print(
        f"template {template}: {len(settings['strict_rules'])} rules, "
        f"{len(re.findall(r'^[0-9]+[.] ', settings['assistant_examples'], re.M))} examples, "
        f"prompt {len(settings['assistant_prompt'])} characters"
    )
    if os.environ.get("DRY_RUN") == "1":
        print(json.dumps(settings, ensure_ascii=False, indent=2))
        return

    async with db_session() as session:
        try:
            before = await doctor_template.apply_to_tenant(
                session,
                settings,
                tenant_name=os.environ.get("TENANT_NAME"),
                force=os.environ.get("FORCE") == "1",
            )
        except doctor_template.TemplateError as exc:
            sys.exit(str(exc))
    print("BACKUP_BEGIN")
    print(json.dumps(before, ensure_ascii=False))
    print("BACKUP_END")
    print(f"written; the bot is {'ON' if settings['bot_replies_enabled'] else 'OFF'}")


if __name__ == "__main__":
    asyncio.run(main())
