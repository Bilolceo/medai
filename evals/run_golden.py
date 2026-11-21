"""The golden set: real patient messages, and what the bot must do with them.

Run in the api container, against the local database's clinic:

    python evals/run_golden.py patterns   # no OpenAI: the patterns alone (free)
    python evals/run_golden.py reader     # the model's reading only
    python evals/run_golden.py full       # the whole answer, the real model
    ... [--only g001,g007] [--save]

`patterns` and `full` score the reply (the cards under it, what it must and
must not say); `reader` and `full` score the reading (which operation, price
asked, complaint kind, greeting). The cases are evals/golden/cases.jsonl,
written by evals/golden/build_cases.py.
"""

import asyncio
import json
import re
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.db import db_session  # noqa: E402
from app.core.faq_seeding import resolve_faq_tenant_id  # noqa: E402
from app.core.tenant_context import reset_current_tenant, set_current_tenant  # noqa: E402
from app.rag.embeddings import EMBEDDING_DIMENSIONS, EmbeddingProvider  # noqa: E402
from app.rag.llm import ChatMessage, LLMProvider  # noqa: E402
from app.services import answer, message_reader, promo_images  # noqa: E402

HERE = Path(__file__).resolve().parent / "golden"
OPS = set(message_reader.OPERATIONS)


class NoEmbed(EmbeddingProvider):
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1) for _ in texts]


class NoModel(LLMProvider):
    async def generate(self, system_prompt: str, messages: list[ChatMessage]) -> str:
        return "[MODEL]"


class Counting(LLMProvider):
    """The real provider, its calls counted."""

    calls = 0

    def __init__(self, inner: LLMProvider) -> None:
        self.inner = inner

    async def generate(self, system_prompt: str, messages: list[ChatMessage]) -> str:
        Counting.calls += 1
        return await self.inner.generate(system_prompt, messages)


def _cards(reply: str, topics: dict[str, str]) -> list[str]:
    return [
        topics.get((m.group("title") or "").strip(), "?")
        for m in promo_images.MARKER.finditer(reply)
    ]


def score_reading(reading: Any, expect: dict) -> list[str]:
    if reading is None:
        return ["reader: no reading"]
    bad = []
    if "ops" in expect:
        want, got = set(expect["ops"]), set(reading.operations)
        # Enlargement may be either operation: one of them is enough.
        ok = got == want if len(want) < 2 else (bool(got & want) and got <= want)
        if not ok:
            bad.append(f"ops {sorted(got)} != {sorted(want)}")
    if (
        expect.get("price") is not None
        and "price" in expect
        and reading.asks_price != expect["price"]
    ):
        bad.append(f"asks_price {reading.asks_price} != {expect['price']}")
    if "kind" in expect and reading.complaint_kind != expect["kind"]:
        bad.append(f"kind {reading.complaint_kind} != {expect['kind']}")
    if expect.get("greeting") and not reading.greeting_only:
        bad.append("not read as a greeting")
    if expect.get("question") and not (reading.question or reading.who_are_you):
        bad.append("question not read")
    return bad


def score_reply(reply: str, cards: list[str], expect: dict) -> list[str]:
    bad = []
    op_cards = [c for c in cards if c.startswith("op:")]
    if expect.get("cards_any") and not set(cards) & set(expect["cards_any"]):
        bad.append(f"cards {cards}, want one of {expect['cards_any']}")
    for c in expect.get("no_cards", []):
        if c in cards:
            bad.append(f"card {c} must not come")
    if expect.get("cards_none") and cards:
        bad.append(f"cards {cards}, want none")
    if expect.get("cards_none_ops") and op_cards:
        bad.append(f"operation cards {op_cards}, want none")
    # The patterns alone: a reply the model would write cannot be read.
    if expect.get("must") and "[MODEL]" not in reply and not re.search(expect["must"], reply):
        bad.append(f"missing /{expect['must']}/")
    if expect.get("must_not") and re.search(expect["must_not"], reply):
        bad.append(f"has /{expect['must_not']}/")
    return bad


async def run(mode: str, only: set[str], save: bool) -> None:
    cases = [json.loads(line) for line in (HERE / "cases.jsonl").read_text().splitlines() if line]
    if only:
        cases = [c for c in cases if c["id"] in only]
    real = Counting(answer.get_llm_provider()) if mode != "patterns" else None
    if real is not None:
        reader = Counting(answer.get_reader_llm_provider())
        answer.get_reader_llm_provider = lambda: reader  # type: ignore[assignment]
    readings: dict[str, Any] = {}
    original_read = message_reader.read

    async def spy(*args: Any, **kwargs: Any) -> Any:
        reading = await original_read(*args, **kwargs)
        readings["last"] = reading
        return reading

    answer.message_reader.read = spy  # type: ignore[assignment]
    results, failed, started = [], Counter(), time.monotonic()
    async with db_session() as session:
        token = set_current_tenant(await resolve_faq_tenant_id(session, None))
        try:
            from sqlalchemy import text

            settings = (await session.execute(text("select settings from tenants"))).scalar() or {}
            topics = {
                str(i.get("title", "")): str(i.get("topic", ""))
                for i in settings.get("promo_images") or []
            }
            for case in cases:
                readings.pop("last", None)
                history = case["history"] or None
                reading = reply = None
                if mode == "reader":
                    last_bot = next(
                        (
                            t["content"]
                            for t in reversed(case["history"])
                            if t["role"] == "assistant"
                        ),
                        None,
                    )
                    reading = await spy(reader, case["message"], last_bot)
                else:
                    reply = await answer.generate_answer(
                        session,
                        case["message"],
                        embedding_provider=NoEmbed() if mode == "patterns" else None,
                        llm_provider=NoModel() if mode == "patterns" else None,
                        history=history,
                    )
                    reading = readings.get("last")
                bad = []
                if mode in ("reader", "full"):
                    bad += score_reading(reading, case["expect"])
                cards = _cards(reply or "", topics)
                if mode in ("patterns", "full"):
                    bad += score_reply(reply or "", cards, case["expect"])
                for b in bad:
                    failed[b.split(" ")[0]] += 1
                results.append(
                    {
                        "id": case["id"],
                        "message": case["message"],
                        "ok": not bad,
                        "bad": bad,
                        "reply": promo_images.MARKER.sub("", reply or "").strip()[:400],
                        "cards": cards,
                        "reading": (reading.raw if reading is not None else None),
                    }
                )
                mark = "OK " if not bad else "XX "
                print(
                    f"{mark}{case['id']} {case['message'][:60]!r}"
                    + (f"  -> {'; '.join(bad)}" if bad else "")
                )
        finally:
            reset_current_tenant(token)
    passed = sum(r["ok"] for r in results)
    print(
        f"\n{mode}: {passed}/{len(results)} passed ({100 * passed // max(1, len(results))}%), "
        f"{time.monotonic() - started:.0f}s, model calls: {Counting.calls}"
    )
    if failed:
        print("failures by kind:", dict(failed.most_common()))
    if save:
        out = HERE / "results" / f"{datetime.now(UTC):%Y%m%d-%H%M}-{mode}.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(results, ensure_ascii=False, indent=1))
        print("saved", out)


if __name__ == "__main__":
    args = sys.argv[1:]
    mode = args[0] if args and args[0] in ("patterns", "reader", "full") else "patterns"
    only = set(args[args.index("--only") + 1].split(",")) if "--only" in args else set()
    asyncio.run(run(mode, only, "--save" in args))
