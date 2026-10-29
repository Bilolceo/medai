"""The doctor's own Telegram bot: their appointments, and cancelling them.

Not a patient channel. Patients write to the doctor on Instagram; this bot
has one audience, the doctor and whoever else is named in
DOCTOR_TELEGRAM_USERNAMES. Four buttons under the chat:

* 📋 Qabullar -- today's or tomorrow's list.
* 🚫 Kunni bekor qilish -- the doctor is not coming: close the day to new
  bookings, cancel everything on it, tell each patient.
* 👤 Bemorni bekor qilish -- the doctor is leaving early: tick the patients
  who cannot be seen, cancel just those, tell just them.
* 🔓 Yopilgan kunlar -- reopen a day closed by mistake.

New appointments, from any source, are announced as they are made.

Everything that cancels asks once more before it acts, and reports back who
was told on Instagram and who could not be -- booked by phone, or outside
Instagram's 24-hour window -- with their numbers, so somebody rings them. A
patient who crosses Tashkent to a closed door because a message silently
failed is the failure this is built around.

Menus live in one message that is edited in place, so the chat reads as an
app rather than a scroll of stale keyboards. Runs by long polling from the
worker: the deployment sits behind a tunnel whose address changes, and a
webhook pointed at yesterday's address is a bot gone quiet without saying so.
"""

import asyncio
import html
import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import db_session
from app.core.tenant_context import reset_current_tenant, set_current_tenant
from app.models.appointment import ACTIVE_STATUSES, Appointment, AppointmentStatus
from app.models.lead import Lead
from app.models.message import Message, MessageSender
from app.models.tenant import Tenant
from app.models.user import User
from app.services import weekly_report
from app.services.appointment import CLINIC_TIMEZONE, DAYS_OFF_KEY, days_off_from
from app.services.complaints import patient_words
from app.services.conversation import last_inbound_at, record_outbound_message, reply_context_for
from app.services.delivery import send_reply
from app.services.sheets import AppointmentRow, mirror_appointment, summarise_problem

logger = logging.getLogger(__name__)

API = "https://api.telegram.org"
POLL_TIMEOUT_SECONDS = 8

CHATS_KEY = "doctor_telegram_chats"
OFFSET_KEY = "doctor_telegram_offset"
NOTIFIED_UNTIL_KEY = "doctor_telegram_notified_until"
LEADS_NOTIFIED_UNTIL_KEY = "doctor_telegram_leads_notified_until"
# Per chat: the day being picked from and the appointment ids ticked so far.
# Kept here rather than in the button data, because a button that says "the
# third patient" names somebody else the moment a booking lands before them.
PICKS_KEY = "doctor_telegram_picks"
# The last day of the week whose report went out, so a week is sent once.
WEEKLY_SENT_KEY = "doctor_telegram_weekly_sent"

BTN_LIST = "📋 Qabullar"
BTN_DAY_OFF = "🚫 Kunni bekor qilish"
BTN_PICK = "👤 Bemorni bekor qilish"
BTN_CLOSED = "🔓 Yopilgan kunlar"
BTN_REPORT = "📊 Hisobot"

MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": BTN_LIST}, {"text": BTN_DAY_OFF}],
        [{"text": BTN_PICK}, {"text": BTN_CLOSED}],
        [{"text": BTN_REPORT}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": "Menyudan tanlang",
}

_WEEKDAYS = ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba", "Yakshanba"]
_MONTHS = [
    "yanvar", "fevral", "mart", "aprel", "may", "iyun",
    "iyul", "avgust", "sentabr", "oktabr", "noyabr", "dekabr",
]  # fmt: skip

# Typed rather than tapped: "bugun ishga bora olmayman", "ertaga kelolmayman".
# Matched loosely, because a match only opens the same confirmation the
# button does.
_ABSENT = re.compile(
    r"(bora?\s*olma|borolma|kela?\s*olma|kelolma|chiqa?\s*olma|chiqolma|ishlamayman|"
    r"kelmayman|bormayman|не\s*смогу|не\s*приду|не\s*выйду|не\s*буду)",
    re.IGNORECASE,
)
_TOMORROW = re.compile(r"(ertaga|завтра)", re.IGNORECASE)


# --- presentation ---------------------------------------------------------------


def _b(text: str) -> str:
    return f"<b>{html.escape(text)}</b>"


def _long_date(day: date) -> str:
    return f"{_WEEKDAYS[day.weekday()]}, {day.day}-{_MONTHS[day.month - 1]}"


def _day_word(day: date, today: date) -> str:
    if day == today:
        return "Bugun"
    if day == today + timedelta(days=1):
        return "Ertaga"
    return f"{day:%d.%m.%Y}"


def _btn(text: str, data: str) -> dict[str, str]:
    return {"text": text, "callback_data": data}


@dataclass(frozen=True)
class Booking:
    appointment: Appointment
    patient: User | None

    @property
    def name(self) -> str:
        if self.appointment.patient_name:
            return self.appointment.patient_name
        if self.patient is not None:
            if self.patient.name:
                return self.patient.name
            if self.patient.username:
                return f"@{self.patient.username}"
        return "Ismi ko'rsatilmagan"

    @property
    def phone(self) -> str | None:
        return self.appointment.patient_phone or (self.patient.phone if self.patient else None)

    @property
    def local(self) -> datetime:
        return self.appointment.scheduled_at.astimezone(CLINIC_TIMEZONE)

    @property
    def short_id(self) -> str:
        return self.appointment.id.hex[:12]

    def card(self) -> str:
        """Two lines: time and name, then how to reach them."""
        confirmed = self.appointment.status == AppointmentStatus.CONFIRMED
        mark = "✅" if confirmed else "🕐"
        first = f"{mark} <b>{self.local:%H:%M}</b> — {html.escape(self.name)}"
        details = []
        if self.phone:
            details.append(f"📞 <code>{html.escape(self.phone)}</code>")
        if self.patient is not None and self.patient.username:
            details.append(f"📷 @{html.escape(self.patient.username)}")
        return first + (f"\n      {'  ·  '.join(details)}" if details else "")

    def plain(self) -> str:
        phone = f" · {self.phone}" if self.phone else ""
        return f"{self.local:%H:%M} — {self.name}{phone}"


# --- transport ------------------------------------------------------------------


class BotAPI:
    """The handful of Bot API calls this needs, and nothing else."""

    def __init__(self, token: str, http: httpx.AsyncClient | None = None) -> None:
        self._base = f"{API}/bot{token}"
        self._http = http or httpx.AsyncClient(timeout=POLL_TIMEOUT_SECONDS + 10)

    async def _call(self, method: str, **payload: Any) -> Any:
        # The token is in the URL, so neither the URL nor the exception
        # (which quotes it) is logged -- only the method and Telegram's own
        # description.
        response = await self._http.post(f"{self._base}/{method}", json=payload)
        body = response.json()
        if not body.get("ok"):
            description = body.get("description")
            # Pressing the same button twice asks for an identical edit.
            if "message is not modified" not in str(description):
                logger.warning(
                    "doctor_telegram_call_failed",
                    extra={"method": method, "description": description},
                )
            return None
        return body.get("result")

    async def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        result = await self._call(
            "getUpdates",
            offset=offset,
            timeout=POLL_TIMEOUT_SECONDS,
            allowed_updates=["message", "callback_query"],
        )
        if result is None:
            # A refused poll returns at once rather than after the long-poll
            # timeout; without a pause a wrong token is a tight loop.
            await asyncio.sleep(10)
        return result or []

    async def send(
        self, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        await self._call("sendMessage", **payload)

    async def edit(
        self, chat_id: int, message_id: int, text: str, reply_markup: dict[str, Any] | None = None
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        await self._call("editMessageText", **payload)

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        await self._call("answerCallbackQuery", **payload)

    async def delete_webhook(self) -> None:
        # getUpdates refuses to run while a webhook is set.
        await self._call("deleteWebhook")

    async def send_photo(self, chat_id: int, photo: bytes, content_type: str, caption: str) -> None:
        # Multipart, so the stored bytes are sent rather than Instagram's
        # signed link, which Telegram's servers may not be allowed to fetch.
        response = await self._http.post(
            f"{self._base}/sendPhoto",
            data={"chat_id": str(chat_id), "caption": caption[:1024]},
            files={"photo": ("photo", photo, content_type)},
        )
        if not response.json().get("ok"):
            logger.warning(
                "doctor_telegram_call_failed",
                extra={"method": "sendPhoto", "description": response.json().get("description")},
            )
            await self.send(chat_id, html.escape(caption))

    async def set_commands(self) -> None:
        await self._call(
            "setMyCommands", commands=[{"command": "start", "description": "Bosh menyu"}]
        )


# --- data -----------------------------------------------------------------------


def _allowed(username: str | None, settings: Settings) -> bool:
    wanted = {
        name.strip().lstrip("@").lower()
        for name in settings.doctor_telegram_usernames.split(",")
        if name.strip()
    }
    if not username:
        return False
    return username.lstrip("@").lower() in wanted


async def _tenant(session: AsyncSession, settings: Settings) -> Tenant | None:
    if not settings.provision_tenant_name:
        return None
    return (
        await session.execute(select(Tenant).where(Tenant.name == settings.provision_tenant_name))
    ).scalar_one_or_none()


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=CLINIC_TIMEZONE)
    return start, start + timedelta(days=1)


async def bookings_on(session: AsyncSession, tenant_id: uuid.UUID, day: date) -> list[Booking]:
    start, end = _day_bounds(day)
    rows = (
        await session.execute(
            select(Appointment, User)
            .outerjoin(User, Appointment.user_id == User.id)
            .where(
                Appointment.tenant_id == tenant_id,
                Appointment.status.in_(ACTIVE_STATUSES),
                Appointment.scheduled_at >= start,
                Appointment.scheduled_at < end,
            )
            .order_by(Appointment.scheduled_at)
        )
    ).all()
    return [Booking(appointment, patient) for appointment, patient in rows]


async def _set_day_off(session: AsyncSession, tenant: Tenant, day: date, *, off: bool) -> None:
    """Close or reopen a day for booking. Past days are dropped as it goes."""
    today = datetime.now(CLINIC_TIMEZONE).date()
    days = {d for d in days_off_from(tenant.settings) if d >= today}
    if off:
        days.add(day)
    else:
        days.discard(day)
    tenant.settings = {**tenant.settings, DAYS_OFF_KEY: sorted(d.isoformat() for d in days)}
    await session.commit()
    logger.info("doctor_day_off_set", extra={"day": day.isoformat(), "off": off})


async def _remember_chat(session: AsyncSession, tenant: Tenant, chat_id: int) -> None:
    chats = [c for c in tenant.settings.get(CHATS_KEY, []) if isinstance(c, int)]
    if chat_id not in chats:
        tenant.settings = {**tenant.settings, CHATS_KEY: [*chats, chat_id]}
        await session.commit()


def _picks(tenant: Tenant, chat_id: int) -> tuple[date | None, list[str]]:
    entry = (tenant.settings.get(PICKS_KEY) or {}).get(str(chat_id)) or {}
    try:
        day = date.fromisoformat(entry["day"])
    except (KeyError, TypeError, ValueError):
        return None, []
    return day, [i for i in entry.get("ids", []) if isinstance(i, str)]


async def _save_picks(
    session: AsyncSession, tenant: Tenant, chat_id: int, day: date | None, ids: Sequence[str]
) -> None:
    picks = dict(tenant.settings.get(PICKS_KEY) or {})
    if day is None:
        picks.pop(str(chat_id), None)
    else:
        picks[str(chat_id)] = {"day": day.isoformat(), "ids": list(ids)}
    tenant.settings = {**tenant.settings, PICKS_KEY: picks}
    await session.commit()


# --- cancelling -----------------------------------------------------------------


def _patient_notice(settings: Settings, booking: Booking, *, whole_day: bool) -> str:
    doctor = settings.doctor_name or "Doktor"
    phone = settings.clinic_phone_numbers
    rebook = (
        f" Boshqa vaqtga yozilish uchun {phone} raqamiga qo'ng'iroq qiling."
        if phone
        else " Boshqa vaqtga yozilish uchun biz bilan bog'laning."
    )
    reason = (
        f"doktor {doctor} {booking.local:%d.%m.%Y} kuni qabul qila olmaydi"
        if whole_day
        else f"doktor {doctor} {booking.local:%d.%m.%Y} kuni soat {booking.local:%H:%M} da "
        "qabul qila olmaydi"
    )
    return (
        f"Assalomu alaykum! Afsuski, {reason}, shuning uchun soat {booking.local:%H:%M} dagi "
        f"qabulingiz bekor qilindi.{rebook} Noqulaylik uchun uzr so'raymiz."
    )


async def cancel_bookings(
    session: AsyncSession, bookings: Sequence[Booking], settings: Settings, *, whole_day: bool
) -> tuple[list[Booking], list[Booking]]:
    """Cancel each booking and tell its patient. Returns (told, not told).

    Cancelled whether or not the patient could be told: the doctor is not
    seeing them either way, and a booking left active keeps the slot looking
    taken. The ones not told are the list somebody has to ring.
    """
    told: list[Booking] = []
    untold: list[Booking] = []
    for booking in bookings:
        appointment, patient = booking.appointment, booking.patient
        delivered = None
        if patient is not None:
            text = _patient_notice(settings, booking, whole_day=whole_day)
            conversation_id = appointment.conversation_id
            last_seen = await last_inbound_at(session, conversation_id) if conversation_id else None
            try:
                delivered = await send_reply(
                    session,
                    channel_id=patient.channel_id,
                    recipient_external_id=patient.external_id,
                    text=text,
                    last_user_message_at=last_seen or appointment.created_at,
                    reply_context=(
                        await reply_context_for(session, conversation_id)
                        if conversation_id
                        else None
                    ),
                )
            except Exception:  # noqa: BLE001 - one patient must not stop the rest
                logger.warning(
                    "doctor_cancel_notice_failed",
                    extra={"appointment_id": str(appointment.id)},
                    exc_info=True,
                )
            if delivered is not None and conversation_id is not None:
                await record_outbound_message(
                    session,
                    conversation_id=conversation_id,
                    channel_type=delivered,
                    text=text,
                    sender=MessageSender.BOT,
                )

        appointment.status = AppointmentStatus.CANCELLED
        note = (
            "Doktor bu kuni qabul qila olmadi."
            if whole_day
            else "Doktor bu vaqtda qabul qila olmadi."
        )
        appointment.notes = f"{appointment.notes}\n{note}" if appointment.notes else note
        await session.commit()

        # The clinic's own spreadsheet, which some people read instead of the
        # dashboard: a booking cancelled here has to read cancelled there too,
        # or somebody rings a patient about a visit that is not happening.
        # Never raises -- the cancellation is already saved.
        await mirror_appointment(
            AppointmentRow(
                appointment_id=appointment.id,
                created_at=appointment.created_at,
                scheduled_at=appointment.scheduled_at,
                patient_name=booking.name,
                phone=booking.phone,
                doctor=appointment.doctor_name,
                channel=appointment.source,
                client_id=patient.external_id if patient else None,
                status=appointment.status,
                cancel_reason=note,
                note=patient_words(appointment.notes) or None,
            )
        )
        (told if delivered is not None else untold).append(booking)
    return told, untold


async def cancel_day(
    session: AsyncSession, tenant_id: uuid.UUID, day: date, settings: Settings
) -> tuple[list[Booking], list[Booking]]:
    return await cancel_bookings(
        session, await bookings_on(session, tenant_id, day), settings, whole_day=True
    )


def _report(title: str, told: list[Booking], untold: list[Booking]) -> str:
    parts = [f"✅ {_b(title)}"]
    if told:
        parts.append(
            f"\n📨 <b>Instagram orqali xabar yuborildi</b> ({len(told)}):\n"
            + "\n".join(booking.card() for booking in told)
        )
    if untold:
        parts.append(
            f"\n⚠️ <b>Xabar yetib bormadi — qo'ng'iroq qiling</b> ({len(untold)}):\n"
            + "\n".join(booking.card() for booking in untold)
        )
    return "\n".join(parts)


# --- screens --------------------------------------------------------------------

Screen = tuple[str, dict[str, Any] | None]

_HOME = (
    "👨‍⚕️ <b>Doktor paneli</b>\n\n"
    "📋 <b>Qabullar</b> — bugungi va ertangi bemorlar\n"
    "🚫 <b>Kunni bekor qilish</b> — kun bo'yi kela olmasangiz\n"
    "👤 <b>Bemorni bekor qilish</b> — faqat ayrim bemorlarni\n"
    "🔓 <b>Yopilgan kunlar</b> — yopilgan kunni qayta ochish\n"
    "📊 <b>Hisobot</b> — oxirgi 7 kun: bot va operatorlar\n\n"
    "🆕 Yangi qabullar va haftalik hisobot (dushanba) shu yerga o'zi keladi."
)


def _choose_day(title: str, prefix: str, today: date) -> Screen:
    tomorrow = today + timedelta(days=1)
    return (
        f"{title}\n\nQaysi kun?",
        {
            "inline_keyboard": [
                [
                    _btn(f"Bugun · {today:%d.%m}", f"{prefix}:{today.isoformat()}"),
                    _btn(f"Ertaga · {tomorrow:%d.%m}", f"{prefix}:{tomorrow.isoformat()}"),
                ],
                [_btn("✖️ Yopish", "x")],
            ]
        },
    )


def _list_screen(day: date, today: date, bookings: list[Booking], closed: bool) -> Screen:
    head = f"📋 <b>{_day_word(day, today)}gi qabullar</b>\n<i>{_long_date(day)}</i>"
    if closed:
        head += "\n🚫 <i>Kun yopilgan — yangi qabul qabul qilinmaydi</i>"
    if bookings:
        body = "\n\n".join(booking.card() for booking in bookings)
        confirmed = sum(1 for b in bookings if b.appointment.status == AppointmentStatus.CONFIRMED)
        footer = f"<b>Jami:</b> {len(bookings)} ta"
        if confirmed:
            footer += f"  ·  ✅ tasdiqlangan: {confirmed}"
        text = f"{head}\n\n{body}\n\n{footer}"
    else:
        text = f"{head}\n\n<i>Hozircha hech kim yozilmagan.</i>"
    other = today + timedelta(days=1) if day == today else today
    return (
        text,
        {
            "inline_keyboard": [
                [
                    _btn("🔄 Yangilash", f"l:{day.isoformat()}"),
                    _btn(f"➡️ {_day_word(other, today)}", f"l:{other.isoformat()}"),
                ],
                [_btn("✖️ Yopish", "x")],
            ]
        },
    )


def _day_off_confirm(day: date, today: date, bookings: list[Booking]) -> Screen:
    word = _day_word(day, today).lower()
    if bookings:
        body = (
            f"🚫 <b>{_day_word(day, today)}gi qabullarni bekor qilish</b>\n"
            f"<i>{_long_date(day)}</i>\n\n"
            f"Shu kuni <b>{len(bookings)} ta</b> bemor yozilgan:\n\n"
            + "\n".join(booking.card() for booking in bookings)
            + "\n\nHammasi bekor qilinadi, bemorlarga Instagram orqali xabar boradi va "
            f"{word} kuni yopiladi — yangi qabul qabul qilinmaydi."
        )
        yes = f"✅ Ha, {len(bookings)} ta qabulni bekor qil"
    else:
        body = (
            f"🚫 <b>{_day_word(day, today)}ni yopish</b>\n<i>{_long_date(day)}</i>\n\n"
            "Bu kunga hech kim yozilmagan. Kun yopilsa, unga qabul qabul qilinmaydi."
        )
        yes = "✅ Ha, kunni yop"
    return body, {
        "inline_keyboard": [[_btn(yes, f"O:{day.isoformat()}")], [_btn("⬅️ Orqaga", "m:off")]]
    }


def _pick_screen(day: date, today: date, bookings: list[Booking], ticked: set[str]) -> Screen:
    head = f"👤 <b>Bemorni bekor qilish</b>\n<i>{_long_date(day)}</i>"
    if not bookings:
        return (
            f"{head}\n\n<i>{_day_word(day, today)} hech kim yozilmagan.</i>",
            {"inline_keyboard": [[_btn("⬅️ Orqaga", "m:pick")]]},
        )
    rows = [
        [
            _btn(
                f"{'☑️' if b.short_id in ticked else '⬜️'}  {b.local:%H:%M} — {b.name}"[:60],
                f"t:{b.short_id}",
            )
        ]
        for b in bookings
    ]
    count = sum(1 for b in bookings if b.short_id in ticked)
    footer = [_btn("⬅️ Orqaga", "m:pick")]
    if count:
        footer.insert(0, _btn(f"🚫 Bekor qilish ({count})", "c:ask"))
    return (
        f"{head}\n\nQabul qila olmaydigan bemorlarni belgilang:",
        {"inline_keyboard": [*rows, footer]},
    )


def _pick_confirm(chosen: list[Booking]) -> Screen:
    return (
        f"👤 <b>{len(chosen)} ta qabulni bekor qilish</b>\n\n"
        + "\n".join(booking.card() for booking in chosen)
        + "\n\nUlarga Instagram orqali xabar boradi. Kun ochiq qoladi.",
        {
            "inline_keyboard": [
                [_btn("✅ Ha, bekor qil", "c:do")],
                [_btn("⬅️ Orqaga", "c:back")],
            ]
        },
    )


def _closed_screen(tenant: Tenant, today: date) -> Screen:
    days = sorted(d for d in days_off_from(tenant.settings) if d >= today)
    if not days:
        return (
            "🔓 <b>Yopilgan kunlar</b>\n\n"
            "<i>Yopilgan kun yo'q — hamma kunlarga yozilish ochiq.</i>",
            {"inline_keyboard": [[_btn("✖️ Yopish", "x")]]},
        )
    return (
        "🔓 <b>Yopilgan kunlar</b>\n\nOchmoqchi bo'lgan kunni tanlang. Bekor qilingan qabullar "
        "o'z-o'zidan tiklanmaydi.",
        {
            "inline_keyboard": [
                *[[_btn(f"🔓 {_long_date(d)} · ochish", f"r:{d.isoformat()}")] for d in days],
                [_btn("✖️ Yopish", "x")],
            ]
        },
    )


# --- handling -------------------------------------------------------------------


async def _in_tenant(tenant: Tenant, coro: Any) -> Any:
    token = set_current_tenant(tenant.id)
    try:
        return await coro
    finally:
        reset_current_tenant(token)


async def _on_callback(
    api: BotAPI, session: AsyncSession, tenant: Tenant, callback: dict[str, Any]
) -> None:
    settings = get_settings()
    today = datetime.now(CLINIC_TIMEZONE).date()
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    if chat_id is None or message_id is None:
        await api.answer_callback(callback["id"])
        return
    if not _allowed((callback.get("from") or {}).get("username"), settings):
        await api.answer_callback(callback["id"], "Bu bot faqat doktor uchun")
        return

    data = callback.get("data", "")
    kind, _, arg = data.partition(":")

    async def show(screen: Screen) -> None:
        await api.edit(chat_id, message_id, screen[0], screen[1])

    def parse_day() -> date | None:
        try:
            day = date.fromisoformat(arg)
        except ValueError:
            return None
        return day if day >= today else None

    if data == "x":
        await api.answer_callback(callback["id"])
        await api.edit(chat_id, message_id, "✔️ <i>Yopildi</i>")
        return
    if data == "m:list":
        await api.answer_callback(callback["id"])
        await show(_choose_day("📋 <b>Qabullar</b>", "l", today))
        return
    if data == "m:off":
        await api.answer_callback(callback["id"])
        await show(_choose_day("🚫 <b>Kunni bekor qilish</b>", "o", today))
        return
    if data == "m:pick":
        await api.answer_callback(callback["id"])
        await _save_picks(session, tenant, chat_id, None, [])
        await show(_choose_day("👤 <b>Bemorni bekor qilish</b>", "p", today))
        return

    if kind in {"l", "o", "O", "p", "r"}:
        day = parse_day()
        if day is None:
            await api.answer_callback(callback["id"], "Bu kun o'tib ketgan")
            return
        if kind == "l":
            await api.answer_callback(callback["id"])
            bookings = await _in_tenant(tenant, bookings_on(session, tenant.id, day))
            await show(_list_screen(day, today, bookings, day in days_off_from(tenant.settings)))
        elif kind == "o":
            await api.answer_callback(callback["id"])
            bookings = await _in_tenant(tenant, bookings_on(session, tenant.id, day))
            await show(_day_off_confirm(day, today, bookings))
        elif kind == "O":
            await api.answer_callback(callback["id"], "Bajarilyapti…")
            await api.edit(
                chat_id, message_id, "⏳ <i>Bekor qilinyapti, bemorlarga xabar yuborilyapti…</i>"
            )
            # Closed before the cancellations, so nobody books into the day
            # in the minute it takes to message its patients.
            await _set_day_off(session, tenant, day, off=True)
            told, untold = await _in_tenant(tenant, cancel_day(session, tenant.id, day, settings))
            logger.info(
                "doctor_day_off_applied",
                extra={"day": day.isoformat(), "told": len(told), "untold": len(untold)},
            )
            title = (
                f"{_day_word(day, today)} ({_long_date(day)}) yopildi"
                if not (told or untold)
                else f"{_long_date(day)}: {len(told) + len(untold)} ta qabul bekor qilindi, "
                "kun yopildi"
            )
            await api.edit(chat_id, message_id, _report(title, told, untold))
        elif kind == "p":
            await api.answer_callback(callback["id"])
            await _save_picks(session, tenant, chat_id, day, [])
            bookings = await _in_tenant(tenant, bookings_on(session, tenant.id, day))
            await show(_pick_screen(day, today, bookings, set()))
        elif kind == "r":
            await _set_day_off(session, tenant, day, off=False)
            await api.answer_callback(callback["id"], "Kun ochildi")
            await show(_closed_screen(tenant, today))
        return

    if kind == "t" or data in {"c:ask", "c:do", "c:back"}:
        day, ids = _picks(tenant, chat_id)
        if day is None or day < today:
            await api.answer_callback(callback["id"], "Qaytadan tanlang")
            await show(_choose_day("👤 <b>Bemorni bekor qilish</b>", "p", today))
            return
        bookings = await _in_tenant(tenant, bookings_on(session, tenant.id, day))
        present = {b.short_id for b in bookings}
        ticked = [i for i in ids if i in present]
        if kind == "t":
            if arg in present:
                ticked = [i for i in ticked if i != arg] if arg in ticked else [*ticked, arg]
            await _save_picks(session, tenant, chat_id, day, ticked)
            await api.answer_callback(callback["id"])
            await show(_pick_screen(day, today, bookings, set(ticked)))
            return
        chosen = [b for b in bookings if b.short_id in ticked]
        if data == "c:back" or not chosen:
            await api.answer_callback(callback["id"])
            await show(_pick_screen(day, today, bookings, set(ticked)))
            return
        if data == "c:ask":
            await api.answer_callback(callback["id"])
            await show(_pick_confirm(chosen))
            return
        await api.answer_callback(callback["id"], "Bajarilyapti…")
        await api.edit(
            chat_id, message_id, "⏳ <i>Bekor qilinyapti, bemorlarga xabar yuborilyapti…</i>"
        )
        told, untold = await _in_tenant(
            tenant, cancel_bookings(session, chosen, settings, whole_day=False)
        )
        await _save_picks(session, tenant, chat_id, None, [])
        logger.info("doctor_patients_cancelled", extra={"told": len(told), "untold": len(untold)})
        await api.edit(
            chat_id,
            message_id,
            _report(f"{_long_date(day)}: {len(chosen)} ta qabul bekor qilindi", told, untold),
        )
        return

    await api.answer_callback(callback["id"])


async def handle_update(
    api: BotAPI, session: AsyncSession, tenant: Tenant, update: dict[str, Any]
) -> None:
    settings = get_settings()
    today = datetime.now(CLINIC_TIMEZONE).date()

    callback = update.get("callback_query")
    if callback:
        await _on_callback(api, session, tenant, callback)
        return

    message = update.get("message")
    if not message:
        return
    chat_id = message["chat"]["id"]
    username = (message.get("from") or {}).get("username")
    if not _allowed(username, settings):
        logger.warning("doctor_telegram_refused", extra={"username": username, "chat_id": chat_id})
        await api.send(chat_id, "🔒 Kechirasiz, bu bot faqat doktor uchun.")
        return

    await _remember_chat(session, tenant, chat_id)
    text = (message.get("text") or "").strip()

    if text == BTN_LIST:
        screen = _choose_day("📋 <b>Qabullar</b>", "l", today)
    elif text == BTN_DAY_OFF:
        screen = _choose_day("🚫 <b>Kunni bekor qilish</b>", "o", today)
    elif text == BTN_PICK:
        await _save_picks(session, tenant, chat_id, None, [])
        screen = _choose_day("👤 <b>Bemorni bekor qilish</b>", "p", today)
    elif text == BTN_CLOSED:
        screen = _closed_screen(tenant, today)
    elif text == BTN_REPORT:
        # Whenever asked, for the last seven days, to whoever asked.
        await api.send(chat_id, "⏳ <i>Hisobot tayyorlanyapti…</i>")
        await deliver_weekly_report(
            api,
            session,
            tenant,
            weekly_report.rolling_week(datetime.now(UTC)),
            chat_ids=[chat_id],
            once=False,
        )
        return
    elif _ABSENT.search(text):
        day = today + timedelta(days=1) if _TOMORROW.search(text) else today
        bookings = await _in_tenant(tenant, bookings_on(session, tenant.id, day))
        screen = _day_off_confirm(day, today, bookings)
    else:
        await api.send(chat_id, _HOME, MAIN_KEYBOARD)
        return
    await api.send(chat_id, screen[0], screen[1])


async def notify_patient_media(
    session: AsyncSession, tenant_id: uuid.UUID, media: Sequence[Any], caption: str
) -> None:
    """Send a patient's photos to everyone on the doctor's bot."""
    settings = get_settings()
    if not settings.doctor_telegram_bot_token:
        return
    tenant = await session.get(Tenant, tenant_id)
    chats = [
        c for c in (tenant.settings if tenant else {}).get(CHATS_KEY, []) if isinstance(c, int)
    ]
    if not chats:
        return
    api = BotAPI(settings.doctor_telegram_bot_token)
    for chat_id in chats:
        images = [m for m in media if m.content]
        if not images:
            await api.send(chat_id, html.escape(caption))
            continue
        for index, item in enumerate(images):
            await api.send_photo(
                chat_id,
                item.content,
                item.content_type or "image/jpeg",
                caption if index == 0 else "",
            )


async def deliver_weekly_report(
    api: BotAPI,
    session: AsyncSession,
    tenant: Tenant,
    window: "weekly_report.Window",
    *,
    chat_ids: Sequence[int] | None = None,
    once: bool = True,
) -> bool:
    """Send the week's report to the doctor. True when it went out.

    The Monday one (``once``) is sent a single time per week: the day it
    covers is remembered. It is not marked sent when nobody has opened the bot
    yet, so it still arrives, late, once somebody does.
    """
    chats = (
        list(chat_ids)
        if chat_ids is not None
        else [c for c in tenant.settings.get(CHATS_KEY, []) if isinstance(c, int)]
    )
    if once and tenant.settings.get(WEEKLY_SENT_KEY) == window.last_day.isoformat():
        return False
    if not chats:
        logger.info("doctor_telegram_weekly_no_chats", extra={"tenant_id": str(tenant.id)})
        return False
    report = await weekly_report.collect(session, tenant, window)
    messages = weekly_report.render(report)
    delivered = False
    for chat_id in chats:
        try:
            for text in messages:
                await api.send(chat_id, text)
            delivered = True
        except Exception:  # noqa: BLE001 - one chat must not stop the others
            logger.warning("doctor_telegram_weekly_failed", exc_info=True)
    if delivered and once:
        tenant.settings = {**tenant.settings, WEEKLY_SENT_KEY: window.last_day.isoformat()}
        await session.commit()
    logger.info(
        "doctor_telegram_weekly_report",
        extra={"window": window.label(), "chats": len(chats), "delivered": delivered},
    )
    return delivered


async def _patient_reason(session: AsyncSession, appointment: Appointment) -> str | None:
    """Why the patient booked: what they said at booking, else their first
    real message -- so the notice is never just a name and a time."""
    words = patient_words(appointment.notes)
    if words:
        return words
    if appointment.conversation_id is None:
        return None
    said = (
        (
            await session.execute(
                select(Message.content)
                .where(
                    Message.conversation_id == appointment.conversation_id,
                    Message.sender == MessageSender.PATIENT,
                )
                .order_by(Message.created_at)
                .limit(30)
            )
        )
        .scalars()
        .all()
    )
    return summarise_problem(list(said)) or None


def _watermark(tenant: Tenant, key: str) -> datetime | None:
    raw = tenant.settings.get(key)
    return datetime.fromisoformat(raw) if raw else None


async def announce_new_bookings(api: BotAPI, session: AsyncSession, tenant: Tenant) -> None:
    """Tell the doctor about every appointment and call-back request created
    since the last look."""
    chats = [c for c in tenant.settings.get(CHATS_KEY, []) if isinstance(c, int)]
    now = datetime.now(UTC).isoformat()
    since = _watermark(tenant, NOTIFIED_UNTIL_KEY)
    leads_since = _watermark(tenant, LEADS_NOTIFIED_UNTIL_KEY)
    if not chats:
        # The one way this can fail in silence: nobody has opened the bot, so
        # there is no chat to send to, and a clinic waiting for a booking
        # notification has no way of knowing that is why. The watermark is
        # still moved on below, exactly as when there is somebody to tell.
        logger.warning("doctor_telegram_no_chats", extra={"tenant_id": str(tenant.id)})
    if since is None or leads_since is None:
        # First run: start from now rather than announcing the whole history.
        tenant.settings = {
            **tenant.settings,
            NOTIFIED_UNTIL_KEY: tenant.settings.get(NOTIFIED_UNTIL_KEY) or now,
            LEADS_NOTIFIED_UNTIL_KEY: tenant.settings.get(LEADS_NOTIFIED_UNTIL_KEY) or now,
        }
        await session.commit()
        return

    today = datetime.now(CLINIC_TIMEZONE).date()
    # Changed as well as new.
    #
    # A patient who moves their time does not make a row, they edit one, and
    # the doctor was told about the visit at nine and never told it had moved
    # to half eleven. Matched on whichever of the two timestamps is later, so
    # a booking is announced when it is made and again whenever it changes.
    touched = func.greatest(Appointment.created_at, Appointment.updated_at)
    rows = (
        await session.execute(
            select(Appointment, User, touched.label("touched"))
            .outerjoin(User, Appointment.user_id == User.id)
            .where(
                Appointment.tenant_id == tenant.id,
                touched > since,
                Appointment.status.in_(ACTIVE_STATUSES),
            )
            .order_by(touched)
        )
    ).all()
    for appointment, patient, _touched in rows:
        booking = Booking(appointment, patient)
        day = booking.local.date()
        # "New" means the row was made since the last look; anything else
        # the patient did to it is a change, and reads as one.
        is_new = appointment.created_at > since
        lines = [
            "🆕 <b>Yangi qabul</b>" if is_new else "🔄 <b>Qabul vaqti o'zgardi</b>",
            "",
            f"👤 <b>{html.escape(booking.name)}</b>",
        ]
        if booking.phone:
            lines.append(f"📞 <code>{html.escape(booking.phone)}</code>")
        if patient is not None and patient.username:
            lines.append(f"📷 @{html.escape(patient.username)}")
        lines.append(f"📅 {_day_word(day, today)} · <i>{_long_date(day)}</i>")
        lines.append(f"🕐 <b>{booking.local:%H:%M}</b>")
        reason = await _patient_reason(session, appointment)
        lines += ["", f"📝 <b>Sabab:</b> {html.escape(reason) if reason else '<i>aytilmagan</i>'}"]
        markup = {
            "inline_keyboard": [
                [_btn(f"📋 {_day_word(day, today)}gi qabullar", f"l:{day.isoformat()}")]
            ]
        }
        logger.info(
            "doctor_telegram_booking_announced",
            extra={
                "appointment_id": str(appointment.id),
                "chats": len(chats),
                "kind": "new" if is_new else "moved",
            },
        )
        for chat_id in chats:
            await api.send(chat_id, "\n".join(lines), markup)

    leads = (
        await session.execute(
            select(Lead, User)
            .outerjoin(User, Lead.user_id == User.id)
            .where(Lead.tenant_id == tenant.id, Lead.created_at > leads_since)
            .order_by(Lead.created_at)
        )
    ).all()
    for lead, patient in leads:
        who = lead.patient_name or (patient.name if patient else None)
        lines = ["📞 <b>Qo'ng'iroq qilishni so'radi</b>", ""]
        if who:
            lines.append(f"👤 <b>{html.escape(who)}</b>")
        if patient is not None and patient.username:
            lines.append(f"📷 @{html.escape(patient.username)}")
        lines.append(f"☎️ <code>{html.escape(lead.phone or '—')}</code>")
        lines += [
            "",
            f"📝 <b>Sabab:</b> {html.escape(lead.topic) if lead.topic else '<i>aytilmagan</i>'}",
        ]
        for chat_id in chats:
            await api.send(chat_id, "\n".join(lines))

    tenant.settings = {
        **tenant.settings,
        NOTIFIED_UNTIL_KEY: max((t for _, _, t in rows), default=since).isoformat(),
        LEADS_NOTIFIED_UNTIL_KEY: max(
            (lead.created_at for lead, _ in leads), default=leads_since
        ).isoformat(),
    }
    if rows or leads:
        await session.commit()


async def run_forever(stop: asyncio.Event) -> None:
    """The worker's background loop: poll Telegram, announce new bookings."""
    settings = get_settings()
    token = settings.doctor_telegram_bot_token
    if not token:
        return
    api = BotAPI(token)
    await api.delete_webhook()
    await api.set_commands()
    logger.info("doctor_telegram_started")
    while not stop.is_set():
        try:
            async with db_session() as session:
                tenant = await _tenant(session, settings)
                if tenant is None:
                    logger.warning("doctor_telegram_no_tenant")
                    await asyncio.sleep(30)
                    continue
                offset = tenant.settings.get(OFFSET_KEY)
            updates = await api.get_updates(offset)
            async with db_session() as session:
                tenant = await _tenant(session, settings)
                assert tenant is not None
                for update in updates:
                    tenant.settings = {**tenant.settings, OFFSET_KEY: update["update_id"] + 1}
                    await session.commit()
                    try:
                        await handle_update(api, session, tenant, update)
                    except Exception:  # noqa: BLE001 - one bad update must not stop the bot
                        await session.rollback()
                        logger.exception("doctor_telegram_update_failed")
                    await session.refresh(tenant)
                token_ctx = set_current_tenant(tenant.id)
                try:
                    await announce_new_bookings(api, session, tenant)
                finally:
                    reset_current_tenant(token_ctx)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep polling through a bad minute
            logger.exception("doctor_telegram_loop_failed")
            await asyncio.sleep(10)
