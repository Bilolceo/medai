"""Dr Sultonbek's intake flow, played as whole conversations, locally.

Each scenario is a list of patient messages; the assistant's own replies are
fed back as history, so the order of questions (name -> place -> phone and
time) is what is being read. Nothing is stored or sent.
"""

import asyncio
import json
import os
import re
import sys

from app.core.db import db_session
from app.core.faq_seeding import resolve_faq_tenant_id
from app.core.tenant_context import reset_current_tenant, set_current_tenant
from app.rag.llm import ChatMessage
from app.services.answer import generate_answer
from app.services import bubbles, promo_images
from style_check import style_flags

SCENARIOS: list[tuple[str, list[str]]] = [
    # --- the ordinary sequence: Tashkent, the administrator calls ---
    ("A1 tashkent call", ["Assalomu alaykum", "Ereksiya yaxshi emas", "Akmal", "Toshkent", "+998 90 123 45 67, ertaga 10 da"]),
    ("A2 tashkent number then time", ["Varikotsele bor", "Bobur", "Toshkent, Chilonzor", "+998 93 345 67 89", "tushdan keyin"]),
    ("A3 everything in one", ["Salom, men Otabek, Andijondanman, tez bo'shanish bor, raqamim +998 95 890 12 34, ertaga 10 da qo'ng'iroq qiling"]),
    ("A4 complaint only the model reads", ["erkaklik kuchim pasaygan", "Akmal", "Toshkent", "ishdan chiqqach qo'ng'iroq qilinglar, 93 345 67 89"]),
    # --- numbers ---
    ("B1 short number", ["Prostatit bor", "Akmal", "Toshkent", "90 123 45 6 ertaga 10 da", "+998 90 123 45 67"]),
    ("B2 foreign number", ["Ereksiya muammosi", "Bilol", "Toshkent", "+7 999 123 45 67 kechqurun", "Telegram bor"]),
    ("B3 evening call time", ["Prostatit bor", "Akmal", "Toshkent", "+998 90 123 45 67, soat 19 da", "16:00"]),
    # --- outside Tashkent: the choice and the online booking ---
    ("C1 online booking", ["Prostatit bor", "Sardor", "Samarqand", "Onlayn", "ertaga 10:00", "+998 91 234 56 78"]),
    ("C2 choice call", ["Siydik qiyin chiqyapti", "Komil", "Buxoro", "Administrator qo'ng'iroq qilsin", "+998 94 456 78 90 tushdan keyin"]),
    ("C3 abroad typo then online", ["Menda ereksiya bolmoqda", "Bilol", "Toshkendaman lekin hozr rassiyada", "onlayn bo'lsin", "bugun 15:30"]),
    ("C4 evening online", ["Prostatit bor", "Sardor", "Namangan", "onlayn", "ertaga kechki 8 da"]),
    ("C5 taken hour", ["Varikotsele", "Aziz", "Farg'ona", "ha", "ertaga 9:00"]),
    ("C6 russian online", ["Здравствуйте, проблемы с потенцией", "Руслан", "Москва", "онлайн", "завтра в 11:00", "+998 99 111 22 33"]),
    ("C7 cyrillic", ["Ассалому алайкум, менда мояк оғрияпти", "Ботир", "Фарғонадан", "онлайн", "эртага 10:00"]),
    # --- questions, prices, no ---
    ("D1 questions in the middle", ["Prostatit bor", "Doktor o'zi ko'radimi?", "Akmal", "Buxoro", "narxi qancha?", "onlayn", "ertaga 11:00"]),
    ("D2 price first", ["Qabul narxi qancha?", "Prostatit", "Jasur", "Toshkent"]),
    ("D3 who are you", ["Km bn gaplashvoma", "Kmsz sz oziz tanshtrmyszmi", "Menda ereksiya"]),
    ("D4 declines", ["Erkaklar muammosi", "Yo'q, raqam bermayman, keyin o'zim yozaman"]),
    ("D5 soft no", ["Prostatit bor", "Akmal", "Toshkent", "hozircha o'ylab ko'raman"]),
    ("D6 hours after hours", ["Ishvoht nechida tugaydi kechi"]),
    ("D7 emergency", ["Moyagim birdan qattiq og'rib shishdi, 1 soat bo'ldi"]),
    ("D8 medicine", ["Prostatitga qaysi antibiotik yaxshi?", "Farrux", "Toshkent"]),
    # --- low desire: the doctor's online script ---
    ("F1 steroid evening", ["Steroid kursidan keyin gormonlarim buzildi", "Jasur", "Toshkent", "onlayn", "bugun 20:00", "+998 90 555 66 77"]),
    ("E1 libido full", ["Jinsiy xohishim pasayib ketdi", "Farrux", "Samarqand", "38", "Ertaga 10 da", "To'ladim, raqamim +998 90 123 45 67"]),
    ("E2 libido evening", ["Xohishim pasaygan", "Sardor", "Toshkent", "35", "kechki 8 da"]),
    ("E3 libido russian", ["Здравствуйте, пропало влечение", "Руслан", "Ташкент", "40", "Сколько стоит?", "в 12"]),
]

EMPATHY = re.compile(r"(?i)xijolat|tushunaman|afsus|ko'p uchraydi|кўп учрайди|хижолат")
PRICE = re.compile(
    r"(?!330 000|165 000|990 000|495 000)\b\d{2,3}\s?000\s?so.m|(?!330 000|165 000|990 000|495 000)\b\d{3}\s?000\b|\$"
)
ASK_NAME = re.compile(r"(?i)ismingiz|исмингиз|как (вас|к вам)|ваше имя")
ASK_PLACE = re.compile(r"(?i)qayerdan|қаердан|откуда")
ASK_PHONE = re.compile(r"(?i)raqam|рақам|номер|телефон")
CLINIC_PHONE_AS_PATIENT = re.compile(
    r"(?i)(siz bergan raqam|raqamingiz|на номер|ваш номер)[^\n]{0,20}\+998 77 690 60 06"
)


async def main() -> None:
    out = []
    async with db_session() as session:
        token = set_current_tenant(await resolve_faq_tenant_id(session, None))
        try:
            only = os.environ.get("ONLY")
            for name, turns in SCENARIOS:
                if only and only not in name:
                    continue
                history: list[ChatMessage] = []
                lines = []
                flags = []
                for i, msg in enumerate(turns):
                    try:
                        reply = await generate_answer(session, msg, history=history or None)
                    except Exception as exc:  # noqa: BLE001
                        reply = f"<<ERROR {type(exc).__name__}: {exc}>>"
                    lines.append((msg, reply))
                    for f in style_flags(
                        reply, [h["content"] for h in history if h["role"] == "assistant"]
                    ):
                        if "libido" in name and f.startswith("LONG"):
                            continue
                        flags.append(f"t{i+1}:{f}")
                    if CLINIC_PHONE_AS_PATIENT.search(reply):
                        flags.append(f"t{i+1}:CLINIC_PHONE_AS_PATIENT")
                    if PRICE.search(reply):
                        flags.append(f"t{i+1}:PRICE")
                    history += [ChatMessage(role="user", content=msg), ChatMessage(role="assistant", content=bubbles.flatten(promo_images.for_transcript(reply)))]  # type: ignore[typeddict-item]
                order = []
                for _, r in lines:
                    for key, rx in (("name", ASK_NAME), ("place", ASK_PLACE), ("phone", ASK_PHONE)):
                        if rx.search(r) and key not in order:
                            order.append(key)
                print(
                    f"\n=== {name}   order asked: {' -> '.join(order) or '-'}   {' '.join(flags) or 'ok'}"
                )
                for m, r in lines:
                    print(f"  bemor: {m}")
                    print(f"  admin: {r}")
                out.append({"name": name, "turns": lines, "order": order, "flags": flags})
        finally:
            reset_current_tenant(token)
    with open(os.environ.get("OUT_JSON", "/tmp/intake.json"), "w") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    asyncio.run(main())
