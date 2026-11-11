"""Long conversations with the real model: one patient, many turns, positive and negative.

    docker compose exec -T -e OPENAI_MODEL=gpt-5.4-mini api sh -c "cd /app && python long_suite.py"
"""

import asyncio
import json
import os
import re

from app.core.db import db_session
from app.core.faq_seeding import resolve_faq_tenant_id
from app.core.tenant_context import reset_current_tenant, set_current_tenant
from app.rag.llm import ChatMessage
from app.services import bubbles, promo_images
from app.services.answer import generate_answer

OK_SUMS = re.compile(r"330 000|165 000|990 000|495 000|500 000|250 000|\d{1,2} \d00 000 000|\d{1,2} 000 000")
SUMS = re.compile(r"\b\d{1,3}(?: \d{3}){1,3}\b")
CARD_RE = re.compile(r"sultonbek-(?:op-)?([a-z-]+)\.jpg")
DIAGNOSIS = re.compile(r"(?i)sizda\s+\w+\s+(?:bor|kasal)|ташхис|diagnoz|сизда\s+\w+\s+бор")

# (name, turns). A turn is a message, or (message, {expectations}).
# expectations: cards=set of card names expected exactly in that reply; nocards=True.
S = [
 ("L1 operatsiya umumiy -> tur -> savollar", [
  "Assalomu alaykum",
  ("Sizlarda operatsiyalar bormi?", {"nocards": True}),
  ("Varikotsele operatsiyasi", {"cards": {"varikosele"}}),
  "Bobur", "Toshkent, Chilonzor",
  "Operatsiya qancha vaqt davom etadi?",
  "Narkoz bilanmi yoki ukol bilanmi?",
  "Qancha kunda tuzalaman, ishga qachon chiqaman?",
  "Narxi qancha deb aytdingiz, yana bir ayting",
  "Boshqa operatsiyalar ham bormi, narxlari qancha?",
  "Klinikaga kelaman",
  "Ertaga 10:30",
  "Kartani yana yuboring",
  "To'ladim, chekni yuboraman",
  "Rahmat",
 ]),
 ("L2 negativ: qo'pol, shubha, chegirma", [
  "Salom, siz botmisiz yoki odammisiz?",
  "Doktorni o'zi bilan gaplashmoqchiman, raqamini bering",
  "Nega bermaysiz, bu nima deganingiz",
  "Boshqa klinikada 200 ming, sizlarda nega qimmat?",
  "Chegirma qiling, 2 kishi boramiz",
  "Bo'lmasa bormayman",
  "Mayli, Jasur, Samarqanddanman",
  "Ereksiya muammosi bor, 2 yildan beri",
  "Bu davolanadimi umuman? Yoki pul yig'asizlarmi",
  "Onlayn qanaqa bo'ladi, telefon orqalimi?",
  "Ha, onlayn",
  "Payshanba 10:30",
  "Kartaga emas, naqd to'lasam bo'ladimi?",
  "Hop, to'layman",
 ]),
 ("L3 fikr o'zgaradi, ko'p savol, kirill va lotin aralash", [
  "Ассалом алейкум доктор",
  "Простатит бор дейишди, сизда даволанадими",
  "Акмал, Андижондан",
  "Онлайн бўладими ёки келишим керакми?",
  "Yoq, kelaman klinikaga",
  "Эртага бўладими соат 15 да?",
  "Hmm, 15 bolmasa 9 ga",
  "Токи йўқ, онлайн қилайлик, йўл узоқ",
  "Онлайн қандай ўтади, қанча давом этади?",
  "Таҳлилларим бор, уларни кўрадими?",
  "Ҳа, эртага 9:00 га ёзинг",
  "Карта рақами?",
  "Чекни ташладим",
  "+998 91 234 56 78",
 ]),
 ("L4 operatsiya nomsiz narx, keyin tur, keyin rasm qani", [
  "Operatsiya narxi qancha?",
  ("Falloprotez", {"cards": {"falloprotez"}}),
  "Sardor, Buxoro",
  "1 komponentli bilan 3 komponentli farqi nima?",
  "Narxi qancha dedingiz?",
  ("Rasm qani, ko'rinmadi", {"cards": {"falloprotez"}}),
  "Kafolat beriladimi?",
  "Operatsiyadan keyin jinsiy aloqa qachon mumkin?",
  "Onlayn konsultatsiya qilaylik",
  "Ertaga 9 da",
  "ok to'ladim",
 ]),
 ("L5 steroid, kechki, yosh", [
  "Salom. Men sportchiman, kurs qilganman, testosteron tushib ketgan",
  "Diyor, 24 yosh, Toshkent",
  "Qaysi tahlillarni topshirishim kerak?",
  "Narxi qancha?",
  "Kechqurun 20:00 da bo'ladimi?",
  "Nega kechqurun qimmat?",
  "Mayli, kunduzi 11 da",
  "Karta raqami",
 ]),
 ("L6 libido oqimi, ko'p savol", [
  "Salom, jinsiy xohishim umuman yo'q bo'lib qoldi",
  "Nima sabab bo'lishi mumkin?",
  "Javohir",
  "Farg'onadanman",
  "31",
  "Bu dori bilan tuzaladimi?",
  "Ertaga 14:00",
  "To'lov qancha?",
  "Karta raqamini yana yozing",
 ]),
 ("L7 shoshilinch va tibbiy savollar, rasm", [
  "Salom, moyagim shishib og'riyapti, kechadan beri",
  "Qaysi dori ichsam bo'ladi hozircha?",
  "Rasm yuborsam ko'rasizmi?",
  "📎 Rasm yubordim",
  "Shuni qarab bering, nima bu?",
  "Alisher, Toshkent",
  "Bugun kelsam bo'ladimi?",
  "13:00 ga",
 ]),
 ("L8 chet el, telegram, vaqt farqi", [
  "Здравствуйте, я в Москве, можно консультацию онлайн?",
  "Рустам",
  "Варикоцеле, левая сторона",
  "Сколько стоит операция?",
  "А консультация?",
  "Можно по телеграму?",
  "У нас разница во времени 2 часа, в 9:00 по Ташкенту это 7 утра у меня, можно позже?",
  "Тогда завтра в 10:30",
  "Оплатил, чек скину в телеграм",
 ]),
]


def flags_for(turns, replies):
    flags = []
    prev_q = None
    for i, (turn, reply) in enumerate(zip(turns, replies, strict=True)):
        msg, exp = (turn if isinstance(turn, tuple) else (turn, {}))
        if not reply.strip() or reply.startswith("<<ERROR"):
            flags.append(f"T{i+1}:EMPTY_OR_ERROR")
        cards = set(CARD_RE.findall(reply))
        if exp.get("nocards") and cards:
            flags.append(f"T{i+1}:CARDS_UNEXPECTED:{sorted(cards)}")
        if "cards" in exp and cards != exp["cards"]:
            flags.append(f"T{i+1}:CARDS:{sorted(cards)}!={sorted(exp['cards'])}")
        bad = [m for m in SUMS.findall(reply) if not OK_SUMS.search(m) and not re.fullmatch(r"9860( \d{4}){3}", m)]
        if bad:
            flags.append(f"T{i+1}:SUM:{bad}")
        if DIAGNOSIS.search(reply):
            flags.append(f"T{i+1}:DIAGNOSIS?")
        text = promo_images.MARKER.sub("", reply)
        parts = [p.strip() for p in text.split("|||") if p.strip()]
        last_q = next((p for p in reversed(parts) if "?" in p), None)
        if last_q and prev_q and last_q == prev_q:
            flags.append(f"T{i+1}:REPEATED_QUESTION")
        prev_q = last_q
    return flags


async def main() -> None:
    out = []
    only = os.environ.get("ONLY")
    async with db_session() as session:
        token = set_current_tenant(await resolve_faq_tenant_id(session, None))
        try:
            for name, turns in S:
                if only and only not in name:
                    continue
                history: list[ChatMessage] = []
                replies = []
                for turn in turns:
                    msg = turn[0] if isinstance(turn, tuple) else turn
                    try:
                        reply = await generate_answer(session, msg, history=history or None)
                    except Exception as exc:  # noqa: BLE001
                        reply = f"<<ERROR {type(exc).__name__}: {exc}>>"
                    replies.append(reply)
                    history += [
                        ChatMessage(role="user", content=msg),
                        ChatMessage(role="assistant", content=bubbles.flatten(promo_images.for_transcript(reply))),
                    ]  # type: ignore[typeddict-item]
                flags = flags_for(turns, replies)
                print(f"\n=== {name}   {' '.join(flags) or 'ok'}")
                for turn, r in zip(turns, replies, strict=True):
                    msg = turn[0] if isinstance(turn, tuple) else turn
                    print(f"  bemor: {msg}")
                    print(f"  admin: {promo_images.MARKER.sub(lambda m: '[RASM ' + (m.group('title') or '') + ']', r)}")
                out.append({"name": name, "turns": [(t[0] if isinstance(t, tuple) else t) for t in turns], "replies": replies, "flags": flags})
        finally:
            reset_current_tenant(token)
    print(f"\nTOTAL {len(out)} conversations, {sum(1 for o in out if o['flags'])} flagged")
    with open(os.environ.get("OUT_JSON", "/tmp/long.json"), "w") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    asyncio.run(main())
