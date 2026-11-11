"""Dr Sultonbek's order, played as whole conversations with the real model.

The doctor's order (voice note, 2026-11-06): name -> place (Tashkent or the
regions) -> complaint; abroad and the regions are offered online, Tashkent the
clinic or online, sport pharmacology only online in the evening; a time, the
price, 50% ahead, the card, the receipt. The clinic's numbers only in an
emergency. Each reply is checked for that; nothing is stored or sent.

    docker compose exec -T api sh -c "cd /app && python order_suite.py"
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

# (name, tags, messages). Tags: abroad, region, tashkent, sport, emergency,
# call (the patient asks for a call), nobook (no booking expected).
CLINIC_PHONES = re.compile(r"690\s?60\s?06|561\s?60\s?06")
ASK_NAME = re.compile(r"(?i)ismingiz|исмингиз|как вас зовут")
ASK_PLACE = re.compile(r"(?i)qayerdansiz|қаердансиз|откуда вы")
ASK_TOPIC = re.compile(r"(?i)qanday masala|қандай масала|с какой проблемой")
VISIT = re.compile(r"(?i)klinikaga kelib|клиникага келиб|на приём в клинику или")
ONLINE = re.compile(
    r"(?i)onlayn videoqo'ng'iroqda konsultatsiya olishingiz|онлайн видеоқўнғироқда консультация олишингиз|онлайн по видеозвонку, приезжать"
)
CARD = re.compile(r"9860 1501")
HALF = re.compile(r"50%|50 foiz|ярм|yarm|половин")
DAY_PRICE = re.compile(r"330 000")
OK_SUMS = re.compile(r"330 000|165 000|990 000|495 000|500 000|250 000|\d{1,2} \d00 000 000|\d{1,2} 000 000")
PASS_ON = re.compile(r"(?i)doktorga uzataman|докторга узатаман|передам ваши данные доктору")
SUMS = re.compile(r"\b\d{2,3}\s?000\b")


ALL_CARDS = set("varikosele skrotoplastika ligamentotomiya kompleks falloprotez micro-tese denervatsiya".split())

S: list[tuple[str, set[str], list[str]]] = [
    ("I01 general op price", {"nobook"}, ["Operatsiya narxlari qancha?"]),
    ("I02 op price cyr", {"nobook"}, ["Салом", "Операция нархлари қанча?"]),
    ("I03 op price ru", {"nobook"}, ["Сколько стоят операции?"]),
    ("I04 varicocele", {"nobook"}, ["Varikotsele operatsiyasi narxi qancha?"]),
    ("I05 prosthesis", {"nobook"}, ["Falloprotez qancha turadi?"]),
    ("I06 micro tese", {"nobook"}, ["Azoospermiya, micro TESE narxi qancha?"]),
    ("I07 enlargement", {"nobook"}, ["Jinsiy olatni kattalashtirish operatsiyasi narxi?"]),
    ("I08 denervation", {"nobook"}, ["Tez tugatib qo'yaman, denervatsiya operatsiyasi qancha?"]),
    ("I09 scrotoplasty", {"nobook"}, ["Skrotoplastika narxi qancha?"]),
    ("I10 kompleks", {"nobook"}, ["Kompleks operatsiya narxi qancha?"]),
    ("I11 two named", {"nobook"}, ["Varikotsele va falloprotez narxlari qancha?"]),
    ("I12 no repeat", {"nobook"}, ["Varikotsele operatsiyasi narxi qancha?", "Varikotsele narxini yana ayting"]),
    ("I13 then all", {"nobook"}, ["Varikotsele operatsiyasi narxi qancha?", "Boshqa operatsiyalar narxi qancha?"]),
    ("I14 consult price no card", {"nobook"}, ["Konsultatsiya narxi qancha?"]),
    ("I15 evening price no card", {"nobook"}, ["Kechqurun konsultatsiya narxi qancha?"]),
    ("I16 complaint no card", {"nobook"}, ["Salom", "prostatit bor"]),
    ("I17 op word no price", {"nobook"}, ["Operatsiya qilish kerakmi?"]),
    # --- full sequences with a card in the middle ---
    ("Q01 op then book", {"region"}, ["Salom", "Varikotsele operatsiyasi narxi qancha?", "Bilol", "Samarqand", "varikotsele", "ha", "ertaga 10:00", "+998 90 123 45 67"]),
    ("Q02 book then op", {"tashkent"}, ["Salom", "Akmal", "Toshkent", "ereksiya muammosi", "Operatsiya narxlari qancha?", "klinikaga", "ertaga 11:00"]),
    ("Q03 abroad op", {"abroad"}, ["Rossiyadaman, falloprotez narxi qancha?", "Jasur", "ereksiya yo'q", "ha onlayn", "ertaga 15:00"]),
    ("Q04 number then op", {"nobook"}, ["Raqamingizni bering", "doktor bilan", "Sardor", "Andijon", "operatsiya narxlari qancha?"]),
    ("Q05 admin then op", {"nobook"}, ["Admin bilan gaplashsam bo'ladimi?", "Operatsiyalar narxi qancha?", "+998 93 555 44 33 kechqurun"]),
    ("X01 prod med.uz.ai", {"nobook"}, ["Ассалом алйекум\nЯхщмсиз", "Серикбай", "Варикацеле бор\nНечпул слада", "Операция нечпул?", "Йо узокдаман\nРасм кани"]),
    ("X02 nechpul latin", {"nobook"}, ["Salom", "Operatsiya nechpul?"]),
    ("X03 rasm qani", {"nobook"}, ["Salom", "Falloprotez qilasizlarmi?", "rasm qani"]),
    ("X04 bilolceo prod", {"abroad"}, ["Asalomu alekum", "Menda ereksiya bolmoqda", "Begzod", "Musofirlikdaman", "Ha mayli", "Narhi qancha", "Ha yozing"]),
    ("X05 premature", {"nobook"}, ["Salom", "Tez tugatib qo'yaman"]),
    ("X06 small", {"nobook"}, ["Olatim kichik, nima qilsa bo'ladi?"]),
    ("X07 infertility", {"nobook"}, ["Farzand bo'lmayapti, spermogrammada spermatozoid yo'q"]),
    ("X08 ru potency", {"nobook"}, ["Здравствуйте, проблемы с потенцией"]),
    ("X09 varicocele complaint", {"nobook"}, ["Salom", "Bilol", "Toshkent", "varikotsele bor"]),
    ("X10 prostatitis none", {"nobook"}, ["Salom", "Bilol", "Toshkent", "prostatit bor"]),
    ("F01 kuchim yoq", {"nobook"}, ["Salom", "Kuchim yo'q, olatim turmayapti"]),
    ("F02 bilolceo libido", {"nobook"}, ["Asalomu alekum", "Sardor menda hohish yoq bolmoqda", "Toshkenda", "19"]),
    ("F03 tomir", {"nobook"}, ["Salom", "Bilol", "Toshkent", "moyagimda tomirlar kengaygan"]),
    ("F04 tez ketib", {"nobook"}, ["Assalomu alaykum, tez ketib qolaman, uzoq chiday olmayman"]),
    ("F05 farzand", {"nobook"}, ["Salom", "Uylanganimga 3 yil bo'ldi, farzand bo'lmayapti"]),
    ("F06 cyr kuch", {"nobook"}, ["Ассалому алайкум", "Кучим йўқ, қотмаяпти"]),
    ("S01 steroid tashkent", {"tashkent"}, ["Salom", "Steroid ishlatganman, gormonlarim buzildi", "Jasur", "Toshkent", "klinikaga", "ertaga 10:00"]),
    ("S02 hormones region", {"region"}, ["Assalomu alaykum", "Testosteronim past chiqdi", "Bekzod", "Samarqand", "Narxi qancha?", "ha", "ertaga 10:30"]),
    ("S03 steroid evening", {"abroad"}, ["Kursdan keyin muammo bo'ldi, steroid qilganman", "Anvar", "Rossiya", "kechqurun bo'ladimi?", "bugun 20:00"]),
    ("S04 ru steroid", {"nobook"}, ["Здравствуйте, после курса стероидов проблемы с гормонами", "Сколько стоит консультация?"]),
    ("S05 dollar kursi", {"nobook"}, ["Salom", "dollar kursi qancha?"]),
    ("Q06 ru full", {"abroad"}, ["Здравствуйте", "Руслан", "Я в Москве", "варикоцеле, сколько стоит операция?", "да", "завтра в 10:00"]),
]

EXPECT = {
    # 2026-11-11, the owner: no cards until the operation is named.
    "I01": set(), "I02": set(), "I03": set(),
    "I04": {"varikosele"}, "I05": {"falloprotez"}, "I06": {"micro-tese"},
    "I08": {"denervatsiya"}, "I09": {"skrotoplastika"}, "I10": {"kompleks"},
    "I11": {"varikosele", "falloprotez"},
    "X02": set(), "X03": {"falloprotez"},
    "X04": {"falloprotez"}, "X05": {"denervatsiya"}, "X06": {"ligamentotomiya", "kompleks"},
    "X07": {"micro-tese"}, "X08": {"falloprotez"}, "X09": {"varikosele"}, "X10": set(),
    "F01": {"falloprotez"}, "F02": set(), "F03": {"varikosele"}, "F04": {"denervatsiya"},
    "F05": {"micro-tese"}, "F06": {"falloprotez"},
    "S04": {"steroid-gormon"}, "S05": set(),
    "I14": set(), "I15": set(), "I16": set(), "I17": set(), "I13": {"varikosele"},
}

def check(tags: set[str], replies: list[str]) -> list[str]:
    flags = []
    joined = "\n".join(replies)
    if "emergency" not in tags:
        for i, r in enumerate(replies):
            if CLINIC_PHONES.search(r):
                flags.append(f"t{i+1}:CLINIC_PHONE")
    first = {}
    for i, r in enumerate(replies):
        for key, rx in (("name", ASK_NAME), ("place", ASK_PLACE), ("topic", ASK_TOPIC)):
            if rx.search(r) and key not in first:
                first[key] = i
    seq = [first[k] for k in ("name", "place", "topic") if k in first]
    if seq != sorted(seq):
        flags.append(f"ORDER:{first}")
    if "abroad" in tags and VISIT.search(joined):
        flags.append("ABROAD_GOT_CLINIC_CHOICE")
    if "abroad" in tags and "sport" not in tags and not ONLINE.search(joined) and len(replies) >= 3:
        flags.append("ABROAD_NO_ONLINE_OFFER")
    if "sport" in tags and DAY_PRICE.search(joined):
        flags.append("SPORT_DAY_PRICE")
    if "sport" in tags and VISIT.search(joined):
        flags.append("SPORT_CLINIC_CHOICE")
    for i, r in enumerate(replies):
        if CARD.search(r) and not HALF.search(r):
            flags.append(f"t{i+1}:CARD_WITHOUT_50")
        for m in SUMS.finditer(r):
            if not OK_SUMS.search(r[max(0, m.start() - 1) : m.end() + 1]):
                flags.append(f"t{i+1}:ODD_SUM:{m.group(0)}")
        if "<<ERROR" in r:
            flags.append(f"t{i+1}:ERROR")
    if "op" in tags and "sultonbek-op-" not in joined:
        flags.append("OPERATION_CARD_MISSING")
    if "noop" in tags and "sultonbek-op-" in joined:
        flags.append("OPERATION_CARD_UNASKED")
    if "person" in tags and not PASS_ON.search(joined):
        flags.append("PERSON_NUMBER_NOT_ASKED")
    for key, rx in (("name", ASK_NAME), ("place", ASK_PLACE), ("topic", ASK_TOPIC)):
        n = sum(1 for r in replies if rx.search(r))
        if n >= 3:
            flags.append(f"ASKED_{key.upper()}_x{n}")
    cards = [set(re.findall(r"sultonbek-(?:op-)?([a-z-]+)\.jpg", r)) for r in replies]
    every = [c for turn in cards for c in turn]
    if len(every) != len(set(every)):
        flags.append("CARD_REPEATED")
    return flags


def card_flags(name: str, replies: list[str]) -> list[str]:
    # The consultation cards (back since 2026-11-11) are checked elsewhere.
    got = set(re.findall(r"sultonbek-(?:op-)?([a-z-]+)\.jpg", "\n".join(replies))) - {
        "kunduzgi-urologiya", "kechki-onlayn"}
    key = name.split()[0]
    if key in EXPECT and got != EXPECT[key]:
        return [f"CARDS:{sorted(got)}!={sorted(EXPECT[key])}"]
    if key == "I07" and not got & {"ligamentotomiya", "kompleks", "falloprotez"}:
        return [f"CARDS:{sorted(got)}"]
    return []


async def main() -> None:
    out = []
    only = os.environ.get("ONLY")
    async with db_session() as session:
        token = set_current_tenant(await resolve_faq_tenant_id(session, None))
        try:
            for name, tags, turns in S:
                if only and only not in name:
                    continue
                history: list[ChatMessage] = []
                replies = []
                for msg in turns:
                    try:
                        reply = await generate_answer(session, msg, history=history or None)
                    except Exception as exc:  # noqa: BLE001
                        reply = f"<<ERROR {type(exc).__name__}: {exc}>>"
                    replies.append(reply)
                    history += [
                        ChatMessage(role="user", content=msg),
                        ChatMessage(
                            role="assistant",
                            content=bubbles.flatten(promo_images.for_transcript(reply)),
                        ),
                    ]  # type: ignore[typeddict-item]
                flags = check(tags, replies) + card_flags(name, replies)
                print(f"\n=== {name}   {' '.join(flags) or 'ok'}")
                for m, r in zip(turns, replies, strict=True):
                    print(f"  bemor: {m}")
                    print(f"  admin: {r}")
                    print(f"         cards: {sorted(set(re.findall(r'sultonbek-(?:op-)?([a-z-]+)\.jpg', r)))}")
                out.append({"name": name, "turns": list(zip(turns, replies)), "flags": flags})
        finally:
            reset_current_tenant(token)
    bad = [o for o in out if o["flags"]]
    print(f"\nTOTAL {len(out)} conversations, {len(bad)} flagged")
    with open(os.environ.get("OUT_JSON", "/tmp/order.json"), "w") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    asyncio.run(main())
