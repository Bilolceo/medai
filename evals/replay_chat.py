"""Replay a live chat against the current code, with NO OpenAI: for each
patient burst after FROM, the real history before it (the last 30 rows, as
production passes), and what the bot would say now.

    python replay.py HH:MM [chat file]
"""

import asyncio
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evals.run_golden import NoEmbed, NoModel  # noqa: E402

REAL = os.environ.get("REAL") == "1"

from app.core.db import db_session  # noqa: E402
from app.core.faq_seeding import resolve_faq_tenant_id  # noqa: E402
from app.core.tenant_context import reset_current_tenant, set_current_tenant  # noqa: E402
from app.services import agent as _agent  # noqa: E402
from app.services import answer as _answer  # noqa: E402
from app.services import promo_images  # noqa: E402
from app.services.answer import generate_answer  # noqa: E402

if "--agent" in sys.argv:  # AGENT_ON
    _agent.profile_of = lambda settings: "sultonbek"  # type: ignore[assignment]

_read = _answer.message_reader.read


async def _spy(*a, **k):  # type: ignore[no-untyped-def]
    r = await _read(*a, **k)
    if r is not None:
        keep = (
            "operations",
            "asks_price",
            "complaint_kind",
            "question",
            "operation_unnamed",
            "off_topic",
        )
        print(
            "   READ:", {k: v for k, v in r.raw.items() if k in keep and v not in (None, False, [])}
        )
    return r


_answer.message_reader.read = _spy  # type: ignore[assignment]

FROM = sys.argv[1] if len(sys.argv) > 1 else "00:00"
PATH = sys.argv[2] if len(sys.argv) > 2 else "/app/chat.txt"

rows = []
for ln in Path(PATH).read_text().split("\n"):
    m = re.match(r"(\d\d:\d\d) ([PB]) (.*)", ln)
    if m:
        t, who, text = m.groups()
        rows.append((t, "user" if who == "P" else "assistant", text.replace(" / ", "\n")))


async def main() -> None:
    async with db_session() as session:
        token = set_current_tenant(await resolve_faq_tenant_id(session, None))
        try:
            i = 0
            while i < len(rows):
                if rows[i][1] != "user":
                    i += 1
                    continue
                j = i
                while j < len(rows) and rows[j][1] == "user":
                    j += 1
                batch = [r[2] for r in rows[i:j] if "Post yoki reels" not in r[2]]
                t = rows[i][0]
                if t >= FROM and batch:
                    history = [{"role": r[1], "content": r[2]} for r in rows[:i]][-(30 - (j - i)) :]
                    text = "\n".join(batch)
                    reply = await generate_answer(
                        session,
                        text,
                        embedding_provider=None if REAL else NoEmbed(),
                        llm_provider=None if REAL else NoModel(),
                        history=history or None,
                    )
                    shown = promo_images.MARKER.sub(
                        lambda m: " [RASM: " + (m.group("title") or "") + "]", reply
                    )
                    real = rows[j][2] if j < len(rows) else "-"
                    print(
                        f"\n{t} bemor: {text!r}\n   JONLI: {real[:200]!r}"
                        f"\n   ENDI:  {shown[:400]!r}"
                    )
                i = j
        finally:
            reset_current_tenant(token)


asyncio.run(main())
