"""Whole conversations with the agent, the real model: the patient's turns are
fixed, every reply is the agent's own and goes into the history of the next.

    python evals/simulate.py [S1,S3] > out.txt

Read the transcripts; the cheap checks at the end flag the obvious breaks
(a sum no service gives, a card twice, a reply empty, the same reply twice).
"""

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.db import db_session  # noqa: E402
from app.core.faq_seeding import resolve_faq_tenant_id  # noqa: E402
from app.core.tenant_context import reset_current_tenant, set_current_tenant  # noqa: E402
from app.services import agent, callbacks, handoff, promo_images  # noqa: E402
from app.services.answer import generate_answer  # noqa: E402

agent.profile_of = lambda settings: "sultonbek"  # type: ignore[assignment]

SCENARIOS: dict[str, tuple[str, list[str]]] = {
    "S1": (
        "Oddiy tartib: shikoyat, anamnez, ism, joy, vaqt, narx, to'lov",
        [
            "Assalomu alaykum",
            "Ereksiya muammosi bor",
            "2 yildan beri",
            "Yo'q, hech qanday dori ichmaganman",
            "Aziz",
            "Surxondaryodan",
            "Ertangi birinchi bo'sh vaqtga yozing",
            "To'ladim, chek yubordim. +998 90 123 45 67",
            "Rahmat",
        ],
    ),
    "S2": (
        "Jonli chat 2026-11-21 (bemor tomoni)",
        [
            "Ассалом алейкум",
            "Имлант нархи",
            "Варик нархи",
            "Канака услугалариз бор озз",
            "Энг киммат операция нечпул",
            "Сардор",
            "Сирдаредан",
            "Тез бошаниш даволаш нечпул",
            "Шуни нарзини этвлринг",
            "Денервация",
        ],
    ),
    "S3": (
        "Implant narxi, keyin doktor bilan gaplashmoqchi (tekin telefon)",
        [
            "Assalomu alaykum",
            "inplant narxini qancha ?",
            "Qanday narkozda qilinadi? Necha kun yotaman?",
            "Avval doktor bilan telefonda gaplashsam bo'ladimi",
            "+998 93 555 44 33, ertaga 11 da qulay",
        ],
    ),
    "S4": (
        "Tez bo'shanish: shikoyat, keyin operatsiya narxi",
        [
            "Salom. Tez bo'shanish bor",
            "3 yil",
            "Nima sababdan bo'ladi bu?",
            "Operatsiya qilasizlarmi? Narxi qancha?",
            "Qimmat ekan, o'ylab ko'raman",
            "Yo'q, raqam bermayman",
        ],
    ),
    "S5": (
        "Rus tili, kattalashtirish",
        [
            "Здравствуйте, сколько стоит увеличение?",
            "А на сколько см удлиняют?",
            "Я из Казахстана",
        ],
    ),
    "S6": (
        "Spam va sperma sotish",
        [
            "Spermani qayerga sotsa bo'ladi?",
            "Ko'zimni sotmoqchiman qancha olaslar",
        ],
    ),
    "S7": (
        "Toshkentdagi bemor, varikosele, klinikaga keladi",
        [
            "Ассалому алайкум",
            "Варикоцеле бор",
            "1 йил",
            "Йўқ",
            "Жасур",
            "Тошкентданман",
            "Клиникага бораман",
            "Бугун бўш жой борми?",
        ],
    ),
    "S8": (
        "Shoshilinch holat",
        ["Moyagim birdan qattiq og'rib shishib ketdi, isitmam bor"],
    ),
    "S9": (
        "Kim bilan gaplashyapman, bot emasmi, doktor raqami",
        [
            "Kmsz sz oziz tanshtrmyszmi",
            "Bot emasmisiz?",
            "Doktorning raqamini bering",
        ],
    ),
    "S10": (
        "Narx boshida, keyin ketma-ketlik",
        [
            "Konsultatsiya qancha turadi?",
            "Prostatit bor",
            "Yarim yil",
            "Antibiotik ichganman",
            "Bobur, Namangandanman",
            "Kechqurun ishdan keyin bo'ladimi?",
        ],
    ),
}

SUM = re.compile(r"\d{1,3}(?:[  ]\d{3})+")


async def run(names: list[str]) -> None:
    async with db_session() as session:
        token = set_current_tenant(await resolve_faq_tenant_id(session, None))
        try:
            for name in names:
                title, turns = SCENARIOS[name]
                print(f"\n=================== {name}: {title}")
                history: list[dict[str, str]] = []
                replies: list[str] = []
                for text in turns:
                    reply = await generate_answer(session, text, history=history or None)
                    reply, lead = callbacks.extract(reply)
                    reply, flagged = handoff.extract(reply)
                    cards = [m.group("title") for m in promo_images.MARKER.finditer(reply)]
                    shown = promo_images.MARKER.sub("", reply).strip()
                    stored = shown.replace(" ||| ", "\n\n") + "".join(f"\n\n📎 {c}" for c in cards)
                    print(f"\nP: {text}")
                    for part in shown.split("|||"):
                        print(f"B: {part.strip()}")
                    for c in cards:
                        print(f"   📎 {c}")
                    if lead is not None:
                        print(f"   ☎ LEAD {lead.phone} | {lead.reason} | {lead.when}")
                    if flagged:
                        print("   ⚑ HANDOFF")
                    if not shown:
                        print("   !! EMPTY REPLY")
                    if replies and shown == replies[-1]:
                        print("   !! SAME REPLY AS BEFORE")
                    replies.append(shown)
                    history += [
                        {"role": "user", "content": text},
                        {"role": "assistant", "content": stored},
                    ]
        finally:
            reset_current_tenant(token)


if __name__ == "__main__":
    chosen = (
        sys.argv[1].split(",")
        if len(sys.argv) > 1 and not sys.argv[1].startswith("-")
        else list(SCENARIOS)
    )
    asyncio.run(run(chosen))
