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
    ("complaint, cooperative", ["Assalomu alaykum", "Ereksiya yaxshi emas", "Akmal", "Samarqanddanman", "+998 90 123 45 67, tushdan keyin"]),
    ("complaint first, then asks price mid-way", ["Tez bo'shanish muammosi bor", "Narxi qancha bo'ladi avval shuni ayting", "Sardor", "Toshkent"]),
    ("price first", ["Qabul narxi qancha?", "Jasur", "Buxoro", "+998 91 234 56 78 ertalab"]),
    ("address first, then complaint", ["Manzilingiz qayerda?", "Ismim Bobur, varikotsele bor ekan", "Toshkent Chilonzor", "+998 93 345 67 89, kechki 7"]),
    ("refuses name", ["Prostatit bor", "Ismim kerakmi? Shart emas", "Qarshidanman", "+998 94 456 78 90"]),
    ("abroad", ["Men Rossiyadaman, kela olmayman, nima qilay", "Dilshod", "Moskva", "+7 999 123 45 67, kechqurun"]),
    ("treatment questions", ["Varikotsele qanday davolanadi, operatsiya shartmi?", "Operatsiya narxi?", "Aziz", "Namangan"]),
    ("declines", ["Erkaklar muammosi", "Yo'q, raqam bermayman, keyin o'zim yozaman"]),
    ("wants booking directly", ["Qabulga yozilmoqchiman", "Sherzod", "Toshkent, Sergeli", "+998 97 567 89 01, soat 3 da qo'ng'iroq qiling"]),
    ("cyrillic", ["Ассалому алайкум, менда мояк оғрияпти", "Ботир", "Фарғонадан", "+998 90 678 90 12"]),
    ("russian", ["Здравствуйте, у меня проблемы с потенцией", "Руслан", "Ташкент", "+998 99 789 01 23, после 18:00"]),
    ("already gave everything in one go", ["Salom, men Otabek, Andijondanman, tez bo'shanish bor, raqamim +998 95 890 12 34, ertaga 10 da qo'ng'iroq qiling"]),
    ("asks why name", ["Siydik qiyin chiqyapti", "Nega ismimni so'rayapsiz?", "Mayli, Komil", "Toshkent"]),
    ("emergency inside flow", ["Moyagim birdan qattiq og'rib shishdi, 1 soat bo'ldi"]),
    ("medicine", ["Prostatitga qaysi antibiotik yaxshi?", "Farrux", "Jizzax"]),
    ("asks own number back", ["Prostatit bor", "Rustam", "Toshkent", "+998 90 123 45 67", "Raqamim nima edi?"]),
    ("russian confirms with number", ["Здравствуйте, варикоцеле", "Тимур", "Ташкент", "+998 99 111 22 33, после обеда"]),
    ("asks price three times", ["Narxi qancha?", "Taxminan bo'lsa ham ayting, qancha?", "Yo'q, menga hozir raqam kerak, narxi qancha?", "Mayli, Karim", "Toshkent"]),
    ("asks the same question twice", ["Qayerdasizlar?", "Manzil qayer dedingiz?", "Ulug'bek", "Toshkent"]),
    ("only ok replies", ["Assalomu alaykum", "ok", "ha", "xo'p", "Jamshid"]),
    ("long story", ["Salom. Men 34 yoshdaman, 2 yildan beri prostatit, 3 ta doktorga bordim, antibiotik ichdim, foydasi bo'lmadi, uyquda ham og'riq, ishga ham ta'sir qilyapti, xotinim ham xafa, nima qilsam bo'ladi, sizlarda davolanadimi, qancha vaqt ketadi, narxi qancha?", "Bekzod", "Namangan"]),
    ("thanks and goodbye", ["Prostatit bor", "Nodir", "Toshkent", "+998 90 555 66 77, ertaga ertalab", "Rahmat", "Xayr"]),
    ("libido full flow", ["Jinsiy xohishim pasayib ketdi", "Farrux", "Samarqand", "38", "Ertaga 10 da", "To'ladim, raqamim +998 90 123 45 67"]),
    ("libido price first", ["Xohish yo'q, onlayn konsultatsiya narxi qancha?", "Karim", "Toshkent", "41", "Ertaga 11 da"]),
    ("libido plus erection (not online)", ["Xohish ham past, ereksiya ham yomon", "Sanjar", "Buxoro"]),
    ("libido cyrillic", ["Жинсий хоҳишим пасайган", "Ботир", "Фарғона", "35", "Эртага 14 да"]),
    ("libido asks why age", ["Istak yo'q bo'lib qoldi", "Doston", "Namangan", "Yoshim nimaga kerak?", "33"]),
    ("libido doctor himself mid-flow", ["Assalomu alaykum", "Jinsiy xohishim pasayib ketdi", "Doktor o'zi ko'radimi?", "Jasur", "Toshkent", "29", "Bugun bo'lmaydimi?", "Ertaga 15:00", "To'ladim, raqamim +998 90 111 22 33"]),
    ("bilol replay", ["Km bn gaplashvoma", "Kmsz sz oziz tanshtrmyszmi", "Menda ereksiya", "Bilol", "Rossiya", "Puli qancha", "Menga soat 19:00 da qongiroq qiling va men rasiyadaman qanday qilasz", "Ishvoht nechida tugaydi kechi"]),
    ("card urology", ["Prostatit bor, konsultatsiya narxi qancha?", "Akmal"]),
    ("card unknown topic", ["Narxlar qancha?", "Steroid kursidan keyin gormon muammosi"]),
    ("card evening", ["Ishdan keyin kechqurun konsultatsiya bormi, narxi qancha?"]),
    ("card cyrillic", ["Эрекция яхши эмас, нархи қанча?"]),
    ("libido evening", ["Xohishim pasaygan", "Sardor", "Toshkent", "35", "kechki 8 da"]),
    ("libido russian", ["Здравствуйте, пропало влечение", "Руслан", "Ташкент", "40", "Сколько стоит?", "в 12"]),
]

EMPATHY = re.compile(r"(?i)xijolat|tushunaman|afsus|ko'p uchraydi|кўп учрайди|хижолат")
PRICE = re.compile(r"(?!500 000|250 000|330 000)\b\d{2,3}\s?000\s?so.m|(?!500 000|250 000|330 000)\b\d{3}\s?000\b|\$")
ASK_NAME = re.compile(r"(?i)ismingiz|исмингиз|как (вас|к вам)|ваше имя")
ASK_PLACE = re.compile(r"(?i)qayerdan|қаердан|откуда")
ASK_PHONE = re.compile(r"(?i)raqam|рақам|номер|телефон")
CLINIC_PHONE_AS_PATIENT = re.compile(r"(?i)(siz bergan raqam|raqamingiz|на номер|ваш номер)[^\n]{0,20}\+998 77 690 60 06")


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
                    for f in style_flags(reply, [h["content"] for h in history if h["role"] == "assistant"]):
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
                print(f"\n=== {name}   order asked: {' -> '.join(order) or '-'}   {' '.join(flags) or 'ok'}")
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
