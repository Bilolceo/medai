"""Patterns versus the model at reading a patient's message, on labelled cases.

Each case: the administrator's last message, the patient's message, and what
is in it. Both readers are scored field by field. Run in the api container:

    docker compose exec -T -e OPENAI_MODEL=gpt-5.4-mini api python scripts/scenarios/reader_eval.py
"""

import asyncio
import json
import os

from app.core.config import get_settings
from app.rag.llm import _select_llm_provider  # noqa: PLC2701
from app.services import message_reader, phone_check, scripted_flow as sf

ASK_NAME = "Ha, doktor shu bilan shug'ullanadi. Ismingiz nima?"
ASK_PLACE = "Akmal, qayerdansiz?"
ASK_PHONE = (
    "Operatorimiz qo'ng'iroq qilib hammasini batafsil aytib beradi. "
    "Telefon raqamingizni va qaysi vaqtda qo'ng'iroq qilsak qulayligini yozing."
)
ASK_TIME = "Qaysi vaqtda qo'ng'iroq qilsak qulay?"
HELLO = "Va alaykum assalom. Men doktor Sultonbekning administratoriman, ismim Dildora. Sizga qanday yordam bera olamiz?"

# (last bot message, patient message, expected). Keys left out are not scored.
CASES: list[tuple[str | None, str, dict]] = [
    # real chats, 2026-11-05
    (None, "Km bn gaplashvoma", {"kind": "none", "question": True, "price": False}),
    (HELLO, "Kmsz sz oziz tanshtrmyszmi", {"kind": "none", "question": True, "name": None}),
    (HELLO, "Menda ereksiya bolmoqda", {"kind": "urology", "question": False}),
    (ASK_NAME, "Bilol", {"name": "Bilol", "abroad": False}),
    (ASK_PLACE, "Toshkendaman lekin hozr rassiyada", {"abroad": True, "name": None}),
    (ASK_PHONE, "Hozr men Rassiyadaman", {"abroad": True, "phone": False, "name": None}),
    (ASK_PHONE, "974880666", {"phone": True, "time": False}),
    (ASK_TIME, "18:40 da bugun", {"time": True, "question": False}),
    (ASK_TIME, "16:00", {"time": True}),
    (None, "Narhi qancha qabul", {"price": True, "question": True}),
    (None, "Puli qancha", {"price": True}),
    (None, "Menga soat 19:00 da qongiroq qiling va men rasiyadaman qanday qilasz", {"abroad": True, "time": True}),
    (None, "Ishvoht nechida tugaydi kechi", {"question": True, "price": False, "kind": "none"}),
    # complaints, spelled every way
    (None, "Salom, ereksiya yaxshi emas", {"kind": "urology"}),
    (None, "erkaklik kuchim pasaygan", {"kind": "urology"}),
    (None, "tez tugatib qo'yaman", {"kind": "urology"}),
    (None, "siyganda achishadi", {"kind": "urology"}),
    (None, "o'g'limni sunnat qildirmoqchiman", {"kind": "urology"}),
    (None, "Проблемы с потенцией", {"kind": "urology"}),
    (None, "Эрекция яхши эмас", {"kind": "urology"}),
    (None, "jinsiy xohishim pasayib ketdi", {"kind": "libido_only"}),
    (None, "xotinimga umuman qiziqishim qolmadi", {"kind": "libido_only"}),
    (None, "Пропало желание", {"kind": "libido_only"}),
    (None, "xohish ham yo'q ereksiya ham yomon", {"kind": "urology"}),
    (None, "qabulga yozilish xohishim bor", {"kind": "urology"}),
    (None, "kursdan keyin gormonlarim buzildi", {"kind": "steroid"}),
    (None, "testosteron ukol qilganman, endi nima qilay", {"kind": "steroid"}),
    (None, "Moyagim birdan qattiq og'rib shishdi", {"kind": "emergency"}),
    (None, "8 soatdan beri siya olmayapman", {"kind": "emergency"}),
    (None, "Assalomu alaykum", {"kind": "none", "question": False}),
    (None, "Ob-havo qanday?", {"kind": "none", "question": True}),
    # names
    (ASK_NAME, "akmal", {"name": "Akmal"}),
    (ASK_NAME, "Ismim Bobur", {"name": "Bobur"}),
    (ASK_NAME, "Men Rossiyadaman", {"name": None, "abroad": True}),
    (ASK_NAME, "Ismim kerakmi? Shart emas", {"name": None, "question": True}),
    (ASK_NAME, "Siz botmisiz?", {"name": None, "question": True}),
    (ASK_NAME, "ok", {"name": None}),
    (ASK_NAME, "Руслан", {"name": "Руслан"}),
    (ASK_NAME, "Toshkent", {"name": None}),
    (ASK_NAME, "Doktor o'zi ko'radimi?", {"name": None, "question": True}),
    # places
    (ASK_PLACE, "Samarqanddanman", {"abroad": False}),
    (ASK_PLACE, "Moskvada ishlayman", {"abroad": True}),
    (ASK_PLACE, "koriyadaman hozir", {"abroad": True}),
    (ASK_PLACE, "Qozog'istondan", {"abroad": True}),
    (ASK_PLACE, "Toshkent viloyati, Chirchiq", {"abroad": False}),
    (ASK_PLACE, "Я в Питере", {"abroad": True}),
    # numbers and times
    (ASK_PHONE, "+998 90 123 45 67, ertaga 10 da", {"phone": True, "time": True}),
    (ASK_PHONE, "raqamim 90 123 45 67 tushdan keyin", {"phone": True, "time": True}),
    (ASK_PHONE, "ishdan chiqqach qo'ng'iroq qilinglar, 93 345 67 89", {"phone": True, "time": True}),
    (ASK_PHONE, "Yo'q, raqam bermayman", {"refuses": True, "phone": False}),
    (ASK_PHONE, "keyin o'zim yozaman", {"refuses": True}),
    (ASK_PHONE, "hozircha o'ylab ko'raman", {"refuses": True}),
    (ASK_PHONE, "narxi qancha avval?", {"price": True, "phone": False, "refuses": False}),
    (ASK_PHONE, "Telegram bor", {"phone": False, "refuses": False}),
    (ASK_TIME, "tushlikdan keyin", {"time": True}),
    (ASK_TIME, "istalgan vaqt", {"time": True}),
    (ASK_TIME, "ertaga ertalabroq", {"time": True}),
    (ASK_TIME, "после обеда", {"time": True}),
    (ASK_TIME, "qachon bo'lsa ham", {"time": True}),
    (ASK_TIME, "qancha turadi?", {"price": True, "time": False}),
    # all in one
    (None, "Salom, men Otabek, Andijondanman, tez bo'shanish bor, raqamim +998 95 890 12 34, ertaga 10 da qo'ng'iroq qiling",
     {"kind": "urology", "name": "Otabek", "abroad": False, "phone": True, "time": True}),
    (None, "men alisher buxorodanman qovuq og'riyapti 99 789 01 23 soat 3 da", {"kind": "urology", "name": "Alisher", "phone": True, "time": True}),
]


def patterns(last: str | None, message: str) -> dict:
    """What the scripted flow's patterns make of the message."""
    if sf._EMERGENCY.search(message):
        kind = "emergency"
    elif sf._about_desire(message) and not sf._OTHER_COMPLAINT.search(message):
        kind = "libido_only"
    elif sf._STEROID_WORDS.search(message) if hasattr(sf, "_STEROID_WORDS") else False:
        kind = "steroid"
    elif sf._COMPLAINT.search(message):
        kind = "steroid" if any(w in message.lower() for w in ("steroid", "kurs", "gormon", "testosteron")) else "urology"
    else:
        kind = "none"
    name = None
    found = sf._GIVEN_NAME.search(message)
    if found and not sf._ABROAD.search(found.group(1)) and not found.group(1).lower().endswith(("man", "dan", "da")):
        name = found.group(1)
    elif last and "Ismingiz nima" in last:
        word = message.split()[0].strip(".,!") if message.split() else ""
        if len(message.split()) <= 2 and sf._NAME_WORD.match(word) and not sf._is_question(message):
            name = word[:1].upper() + word[1:]
    return {
        "kind": kind,
        "name": name,
        "abroad": bool(sf._ABROAD.search(message)),
        "phone": phone_check.read(message) is not None,
        "time": sf._when(message) is not None,
        "question": sf._is_question(message),
        "price": bool(sf._ASKS_PRICE.search(message)) and sf._is_question(message),
        "refuses": bool(sf._REFUSE.search(message)) and not sf._is_question(message),
    }


def from_reading(r: message_reader.Reading | None) -> dict:
    if r is None:
        return {}
    return {
        "kind": r.complaint_kind,
        "name": r.name,
        "abroad": r.abroad,
        "phone": r.phone is not None,
        "time": r.time is not None,
        "question": r.question is not None or r.who_are_you,
        "price": r.asks_price,
        "refuses": r.refuses,
    }


def score(got: dict, want: dict) -> list[str]:
    wrong = []
    for key, value in want.items():
        have = got.get(key)
        if key == "name" and isinstance(have, str) and isinstance(value, str):
            ok = have.lower() == value.lower()
        else:
            ok = have == value
        if not ok:
            wrong.append(f"{key}: want {value!r}, got {have!r}")
    return wrong


async def main() -> None:
    settings = get_settings().model_copy(update={"openai_reasoning_effort": "low"})
    provider = _select_llm_provider(settings)
    totals = {"patterns": [0, 0], "model": [0, 0]}
    report = []
    readings = await asyncio.gather(
        *(message_reader.read(provider, msg, last) for last, msg, _ in CASES)
    )
    for (last, message, want), reading in zip(CASES, readings, strict=True):
        line = {"message": message}
        for name, got in (("patterns", patterns(last, message)), ("model", from_reading(reading))):
            wrong = score(got, want)
            totals[name][0] += len(want) - len(wrong)
            totals[name][1] += len(want)
            line[name] = wrong
        report.append(line)
    for name, (ok, n) in totals.items():
        print(f"{name:9s} {ok}/{n} fields right ({100 * ok / n:.0f}%)")
    print()
    for line in report:
        if line["patterns"] or line["model"]:
            print(f"- {line['message'][:70]}")
            if line["patterns"]:
                print(f"    patterns: {'; '.join(line['patterns'])}")
            if line["model"]:
                print(f"    model:    {'; '.join(line['model'])}")
    with open(os.environ.get("OUT_JSON", "/tmp/reader_eval.json"), "w") as fh:
        json.dump({"totals": totals, "report": report}, fh, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    asyncio.run(main())
