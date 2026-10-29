"""The doctor's own Telegram bot: their appointments, the people waiting on
them, and cancelling a day.

Not a patient channel. Patients write to the doctor on Instagram; this bot
has one audience, the doctor and whoever else is named in
DOCTOR_TELEGRAM_USERNAMES (everything) or DOCTOR_TELEGRAM_VIEWERS (looking,
not cancelling). Four buttons under the chat:

* 📋 Qabullar -- a day's list. From it the doctor closes the day (not coming:
  cancel everything, tell each patient) or ticks the patients who cannot be
  seen (leaving early), and reopens a day closed by mistake.
* 📞 Raqamlar -- call-back numbers nobody has rung, each with a button to
  say it has been done.
* 📊 Hisobot -- the last seven days, bot and operators.
* 🩺 Holat -- is the assistant answering, who is waiting.

Said without being asked: new and moved bookings, call-back requests, a
patient who has waited on a person for half an hour, the assistant going
quiet, the morning's list, the Monday report. Each is sent once, and one that
could not be delivered is tried again rather than lost.

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
from time import monotonic
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import db_session
from app.core.tenant_context import reset_current_tenant, set_current_tenant
from app.models.appointment import ACTIVE_STATUSES, Appointment, AppointmentStatus
from app.models.lead import Lead, LeadStatus
from app.models.message import Message, MessageSender
from app.models.tenant import Tenant
from app.models.user import User
from app.services import doctor_watch, weekly_report
from app.services.appointment import (
    CLINIC_TIMEZONE,
    DAYS_OFF_KEY,
    WORK_DAYS,
    WORK_END,
    WORK_START,
    days_off_from,
)
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
# Who is who: {"username in lower case": telegram user id}. A handle can be
# given up and taken by somebody else; the numeric id cannot.
USERS_KEY = "doctor_telegram_users"
# Appointment short id -> the time it was last announced at, so a booking is
# announced again only when its time moved, not whenever anything touched it.
ANNOUNCED_KEY = "doctor_telegram_announced"
# Conversation id -> the needs_doctor_since already escalated.
ESCALATED_KEY = "doctor_telegram_escalated"
SUMMARY_SENT_KEY = "doctor_telegram_summary_sent"
SILENT_ALERT_KEY = "doctor_telegram_silent_alert"
BOT_OFF_ALERT_KEY = "doctor_telegram_bot_off_alert"

# A notice that still cannot be delivered after this long is given up on, so
# one blocked chat cannot hold back every notice behind it for ever.
GIVE_UP_AFTER = timedelta(hours=6)
# More than this waiting at once is told as a count, not one message each.
CATCH_UP_LIMIT = 6
SUMMARY_FROM = time(8, 30)
SUMMARY_UNTIL = time(12, 0)
TELEGRAM_TEXT_LIMIT = 4000  # under Telegram's 4096

BTN_LIST = "📋 Qabullar"
BTN_LEADS = "📞 Raqamlar"
BTN_REPORT = "📊 Hisobot"
BTN_STATUS = "🩺 Holat"
# The older keyboard, still on phones that have not pressed /start since.
BTN_DAY_OFF = "🚫 Kunni bekor qilish"
BTN_PICK = "👤 Bemorni bekor qilish"
BTN_CLOSED = "🔓 Yopilgan kunlar"

MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": BTN_LIST}, {"text": BTN_LEADS}],
        [{"text": BTN_REPORT}, {"text": BTN_STATUS}],
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


def _clip(text: str) -> str:
    """Telegram refuses a message over 4096 characters outright, which for a
    long list means nothing at all. Cut at a line, and say it was cut."""
    if len(text) <= TELEGRAM_TEXT_LIMIT:
        return text
    cut = text[:TELEGRAM_TEXT_LIMIT].rpartition("\n")[0] or text[:TELEGRAM_TEXT_LIMIT]
    return cut + "\n\n<i>… (ro'yxat uzun, qolganini dashboardda ko'ring)</i>"


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
        retry_after = (body.get("parameters") or {}).get("retry_after")
        if not body.get("ok") and isinstance(retry_after, int) and retry_after <= 30:
            # Flood control: Telegram says how long to wait, and a notice
            # dropped here is a booking the doctor never hears about.
            await asyncio.sleep(retry_after + 1)
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
    ) -> bool:
        """True when Telegram took the message. Callers that must not lose a
        notice move on only when it did."""
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": _clip(text),
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self._call("sendMessage", **payload) is not None

    async def edit(
        self, chat_id: int, message_id: int, text: str, reply_markup: dict[str, Any] | None = None
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": _clip(text),
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


DOCTOR = "doctor"
VIEWER = "viewer"


def _names(raw: str) -> set[str]:
    return {name.strip().lstrip("@").lower() for name in raw.split(",") if name.strip()}


async def _authorize(
    session: AsyncSession, tenant: Tenant, user: dict[str, Any], settings: Settings
) -> str | None:
    """DOCTOR, VIEWER, or None for anybody else.

    A handle on the list is tied to the Telegram id that first used it. The
    handle alone is not enough after that: Telegram lets a handle be given up
    and taken by a stranger, and this bot lists patients by name and number
    and cancels their visits. A numeric id may be put on the list instead of a
    handle, for someone with no username.
    """
    user_id = user.get("id")
    if not isinstance(user_id, int):
        return None
    doctors = _names(settings.doctor_telegram_usernames)
    viewers = _names(settings.doctor_telegram_viewers)

    def role(name: str) -> str | None:
        if name in doctors:
            return DOCTOR
        return VIEWER if name in viewers else None

    if role(str(user_id)):
        return role(str(user_id))
    bound: dict[str, int] = dict(tenant.settings.get(USERS_KEY) or {})
    handle = (user.get("username") or "").lstrip("@").lower()
    if handle and role(handle):
        holder = bound.get(handle)
        if holder is None:
            tenant.settings = {**tenant.settings, USERS_KEY: {**bound, handle: user_id}}
            await session.commit()
            return role(handle)
        return role(handle) if holder == user_id else None
    # Changed handle: still them if their id was bound to a name still listed.
    for name, holder in bound.items():
        if holder == user_id and role(name):
            return role(name)
    return None


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


def _home(role: str) -> str:
    text = (
        "👨‍⚕️ <b>Doktor paneli</b>\n\n"
        "📋 <b>Qabullar</b> — kunlik ro'yxat"
        + (", kunni yopish, bemorni bekor qilish" if role == DOCTOR else "")
        + "\n"
        "📞 <b>Raqamlar</b> — qo'ng'iroq kutayotganlar\n"
        "📊 <b>Hisobot</b> — oxirgi 7 kun: bot va operatorlar\n"
        "🩺 <b>Holat</b> — bot javob beryaptimi, kim kutyapti\n\n"
        "🔔 O'zi keladi: yangi qabul, qo'ng'iroq so'rovi, 30 daqiqadan beri javob "
        "kutayotgan bemor, bot to'xtasa ogohlantirish, ertalabki xulosa, dushanba hisoboti."
    )
    if role != DOCTOR:
        text += "\n\n<i>Sizda faqat ko'rish huquqi bor.</i>"
    return text


def _ago(moment: datetime, now: datetime) -> str:
    minutes = max(int((now - moment).total_seconds() // 60), 0)
    if minutes < 60:
        return f"{max(minutes, 1)} daqiqa"
    if minutes < 48 * 60:
        return f"{minutes // 60} soat"
    return f"{minutes // 1440} kun"


def _choose_day(title: str, prefix: str, today: date, *, closed: bool = False) -> Screen:
    tomorrow = today + timedelta(days=1)
    later = [today + timedelta(days=n) for n in (2, 3, 4)]
    rows = [
        [
            _btn(f"Bugun · {today:%d.%m}", f"{prefix}:{today.isoformat()}"),
            _btn(f"Ertaga · {tomorrow:%d.%m}", f"{prefix}:{tomorrow.isoformat()}"),
        ],
        [
            _btn(f"{_WEEKDAYS[d.weekday()][:3]} · {d:%d.%m}", f"{prefix}:{d.isoformat()}")
            for d in later
        ],
    ]
    if closed:
        rows.append([_btn("🔓 Yopilgan kunlar", "m:closed")])
    rows.append([_btn("✖️ Yopish", "x")])
    return f"{title}\n\nQaysi kun?", {"inline_keyboard": rows}


def _list_screen(
    day: date, today: date, bookings: list[Booking], closed: bool, *, role: str = DOCTOR
) -> Screen:
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
    rows = [
        [
            _btn("🔄 Yangilash", f"l:{day.isoformat()}"),
            _btn(f"➡️ {_day_word(other, today)}", f"l:{other.isoformat()}"),
        ]
    ]
    if role == DOCTOR:
        actions = [_btn("🚫 Kunni yopish", f"o:{day.isoformat()}")]
        if bookings:
            actions.append(_btn("👤 Bemorni bekor qilish", f"p:{day.isoformat()}"))
        rows.append(actions)
        if closed:
            rows.append([_btn("🔓 Kunni qayta ochish", f"r:{day.isoformat()}")])
    rows.append([_btn("⬅️ Kunlar", "m:list"), _btn("✖️ Yopish", "x")])
    return text, {"inline_keyboard": rows}


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
        "inline_keyboard": [
            [_btn(yes, f"O:{day.isoformat()}")],
            [_btn("⬅️ Orqaga", f"l:{day.isoformat()}")],
        ]
    }


def _pick_screen(day: date, today: date, bookings: list[Booking], ticked: set[str]) -> Screen:
    head = f"👤 <b>Bemorni bekor qilish</b>\n<i>{_long_date(day)}</i>"
    back = _btn("⬅️ Orqaga", f"l:{day.isoformat()}")
    if not bookings:
        return (
            f"{head}\n\n<i>{_day_word(day, today)} hech kim yozilmagan.</i>",
            {"inline_keyboard": [[back]]},
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
    footer = [back]
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
            {"inline_keyboard": [[_btn("⬅️ Kunlar", "m:list"), _btn("✖️ Yopish", "x")]]},
        )
    return (
        "🔓 <b>Yopilgan kunlar</b>\n\nOchmoqchi bo'lgan kunni tanlang. Bekor qilingan qabullar "
        "o'z-o'zidan tiklanmaydi.",
        {
            "inline_keyboard": [
                *[[_btn(f"🔓 {_long_date(d)} · ochish", f"r:{d.isoformat()}")] for d in days],
                [_btn("⬅️ Kunlar", "m:list"), _btn("✖️ Yopish", "x")],
            ]
        },
    )


def _lead_label(lead: Lead) -> str:
    return lead.patient_name or lead.phone or "Raqam"


def _leads_screen(leads: list[Lead], now: datetime, *, role: str = DOCTOR) -> Screen:
    if not leads:
        return (
            "📞 <b>Raqamlar</b>\n\n"
            "<i>Bog'lanilmagan raqam yo'q — hammasiga qo'ng'iroq qilingan.</i>",
            {"inline_keyboard": [[_btn("🔄 Yangilash", "m:leads"), _btn("✖️ Yopish", "x")]]},
        )
    shown = leads[:10]
    lines = [f"📞 <b>Raqamlar</b> — bog'lanilmagan: {len(leads)} ta", ""]
    for number, lead in enumerate(shown, start=1):
        who = f"<b>{html.escape(lead.patient_name)}</b> · " if lead.patient_name else ""
        lines.append(f"{number}. {who}☎️ <code>{html.escape(lead.phone or '—')}</code>")
        details = []
        if lead.convenient_time:
            details.append(f"⏰ {html.escape(lead.convenient_time)}")
        if lead.topic:
            details.append(f"📝 {html.escape(lead.topic)}")
        details.append(f"🕐 {_ago(lead.created_at, now)} oldin")
        lines.append("      " + "  ·  ".join(details))
    if len(leads) > len(shown):
        lines.append(f"\n<i>… va yana {len(leads) - len(shown)} ta (dashboardda)</i>")
    rows = [
        [_btn(f"✅ {number}. {_lead_label(lead)}"[:40], f"k:{lead.id.hex[:12]}")]
        for number, lead in enumerate(shown, start=1)
    ]
    lines.append("\n<i>Qo'ng'iroq qilingach, shu raqamning tugmasini bosing.</i>")
    rows.append([_btn("🔄 Yangilash", "m:leads"), _btn("✖️ Yopish", "x")])
    return "\n".join(lines), {"inline_keyboard": rows}


def _status_screen(health: doctor_watch.Health, now: datetime) -> Screen:
    lines = ["🩺 <b>Holat</b>", ""]
    if health.bot_on:
        lines.append("🤖 Yordamchi: ✅ yoqilgan")
    else:
        lines.append("🤖 Yordamchi: ⛔ <b>o'chirilgan</b> — bemorlarga javob bermayapti")
    if health.last_bot_reply:
        lines.append(f"💬 Oxirgi javob: {_ago(health.last_bot_reply, now)} oldin")
    if health.silent:
        lines.append(f"⚠️ Bot javob bermagan: <b>{health.silent} ta</b> suhbat")
    if health.waiting:
        lines.append(f"\n🙋 <b>Javob kutayotganlar: {len(health.waiting)} ta</b>")
        for chat in health.waiting[:5]:
            said = html.escape(chat.said[:60])
            lines.append(f"• {html.escape(chat.who)} — {_ago(chat.since, now)}: <i>{said}</i>")
        if len(health.waiting) > 5:
            lines.append(f"… va yana {len(health.waiting) - 5} ta")
    else:
        lines.append("\n🙋 Javob kutayotgan bemor yo'q")
    if health.waiting_old:
        lines.append(f"🗄 Eski (7 kundan oshgan): {health.waiting_old} ta")
    if health.open_leads:
        oldest = f" (eng eskisi {_ago(health.oldest_lead, now)})" if health.oldest_lead else ""
        lines.append(f"\n📞 Bog'lanilmagan raqamlar: <b>{health.open_leads} ta</b>{oldest}")
    else:
        lines.append("\n📞 Bog'lanilmagan raqam yo'q")
    return "\n".join(lines), {
        "inline_keyboard": [
            [_btn("🔄 Yangilash", "m:status"), _btn("📞 Raqamlar", "m:leads")],
            [_btn("✖️ Yopish", "x")],
        ]
    }


# --- handling -------------------------------------------------------------------


async def _in_tenant(tenant: Tenant, coro: Any) -> Any:
    token = set_current_tenant(tenant.id)
    try:
        return await coro
    finally:
        reset_current_tenant(token)


# Buttons that change something about the doctor's day, or the patients on it.
_DOCTOR_ONLY_KINDS = {"o", "O", "p", "t", "r"}
_DOCTOR_ONLY_DATA = {"m:off", "m:pick", "m:closed", "c:ask", "c:do", "c:back"}
_DOCTOR_ONLY_ANSWER = "Bu amal faqat doktor uchun"


async def _leads_view(session: AsyncSession, tenant: Tenant, role: str) -> Screen:
    leads = await _in_tenant(tenant, doctor_watch.open_leads(session, tenant, limit=200))
    return _leads_screen(leads, datetime.now(UTC), role=role)


async def _status_view(session: AsyncSession, tenant: Tenant) -> Screen:
    now = datetime.now(UTC)
    return _status_screen(await _in_tenant(tenant, doctor_watch.health(session, tenant, now)), now)


async def _mark_rung(
    session: AsyncSession, tenant: Tenant, short_id: str
) -> tuple[Lead | None, bool]:
    """(the lead, whether this press is what marked it). A lead rung from the
    dashboard a minute ago is found but not marked again."""
    leads = (
        (
            await _in_tenant(
                tenant,
                session.execute(select(Lead).where(Lead.tenant_id == tenant.id)),
            )
        )
        .scalars()
        .all()
    )
    lead = next((candidate for candidate in leads if candidate.id.hex[:12] == short_id), None)
    if lead is None:
        return None, False
    if lead.status != LeadStatus.NEW:
        return lead, False
    lead.status = LeadStatus.CONTACTED
    await session.commit()
    logger.info("doctor_lead_marked_contacted", extra={"lead_id": str(lead.id)})
    return lead, True


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
    role = await _authorize(session, tenant, callback.get("from") or {}, settings)
    if role is None:
        await api.answer_callback(callback["id"], "Bu bot faqat doktor uchun")
        return

    data = callback.get("data", "")
    kind, _, arg = data.partition(":")
    if role != DOCTOR and (kind in _DOCTOR_ONLY_KINDS or data in _DOCTOR_ONLY_DATA):
        await api.answer_callback(callback["id"], _DOCTOR_ONLY_ANSWER)
        return

    async def show(screen: Screen) -> None:
        await api.edit(chat_id, message_id, screen[0], screen[1])

    def parse_day() -> date | None:
        try:
            day = date.fromisoformat(arg)
        except ValueError:
            return None
        return day if day >= today else None

    def has_closed() -> bool:
        return role == DOCTOR and any(d >= today for d in days_off_from(tenant.settings))

    if data == "x":
        await api.answer_callback(callback["id"])
        await api.edit(chat_id, message_id, "✔️ <i>Yopildi</i>")
        return
    if data == "m:list":
        await api.answer_callback(callback["id"])
        await show(_choose_day("📋 <b>Qabullar</b>", "l", today, closed=has_closed()))
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
    if data == "m:closed":
        await api.answer_callback(callback["id"])
        await show(_closed_screen(tenant, today))
        return
    if data == "m:leads":
        await api.answer_callback(callback["id"])
        await show(await _leads_view(session, tenant, role))
        return
    if data == "m:status":
        await api.answer_callback(callback["id"])
        await show(await _status_view(session, tenant))
        return
    if kind == "k":
        lead, marked = await _mark_rung(session, tenant, arg)
        if lead is None:
            await api.answer_callback(callback["id"], "Bu raqam topilmadi")
            return
        await api.answer_callback(
            callback["id"], "Belgilandi" if marked else "Allaqachon belgilangan"
        )
        if str(message.get("text") or "").startswith("📞 Raqamlar"):
            await show(await _leads_view(session, tenant, role))
        else:
            await show(
                (
                    f"✅ <b>Bog'lanildi</b> — {html.escape(_lead_label(lead))}\n"
                    f"☎️ <code>{html.escape(lead.phone or '—')}</code>",
                    None,
                )
            )
        return

    if kind in {"l", "o", "O", "p", "r"}:
        day = parse_day()
        if day is None:
            await api.answer_callback(callback["id"], "Bu kun o'tib ketgan")
            return
        if kind == "l":
            await api.answer_callback(callback["id"])
            bookings = await _in_tenant(tenant, bookings_on(session, tenant.id, day))
            await show(
                _list_screen(day, today, bookings, day in days_off_from(tenant.settings), role=role)
            )
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
    sender = message.get("from") or {}
    role = await _authorize(session, tenant, sender, settings)
    if role is None:
        logger.warning(
            "doctor_telegram_refused",
            extra={"username": sender.get("username"), "chat_id": chat_id},
        )
        await api.send(chat_id, "🔒 Kechirasiz, bu bot faqat doktor uchun.")
        return

    await _remember_chat(session, tenant, chat_id)
    text = (message.get("text") or "").strip()

    def has_closed() -> bool:
        return role == DOCTOR and any(d >= today for d in days_off_from(tenant.settings))

    if text in {BTN_DAY_OFF, BTN_PICK, BTN_CLOSED} and role != DOCTOR:
        await api.send(chat_id, f"🔒 {_DOCTOR_ONLY_ANSWER}.", MAIN_KEYBOARD)
        return
    if text == BTN_LIST:
        screen = _choose_day("📋 <b>Qabullar</b>", "l", today, closed=has_closed())
    elif text == BTN_LEADS:
        screen = await _leads_view(session, tenant, role)
    elif text == BTN_STATUS:
        screen = await _status_view(session, tenant)
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
        if role != DOCTOR:
            await api.send(chat_id, f"🔒 {_DOCTOR_ONLY_ANSWER}.", MAIN_KEYBOARD)
            return
        day = today + timedelta(days=1) if _TOMORROW.search(text) else today
        bookings = await _in_tenant(tenant, bookings_on(session, tenant.id, day))
        screen = _day_off_confirm(day, today, bookings)
    else:
        await api.send(chat_id, _home(role), MAIN_KEYBOARD)
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
            sent = [await api.send(chat_id, text) for text in messages]
            delivered = delivered or all(sent)
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


def _watermark(tenant: Tenant, key: str) -> datetime | None:
    raw = tenant.settings.get(key)
    return datetime.fromisoformat(raw) if raw else None


def _chats(tenant: Tenant) -> list[int]:
    return [c for c in tenant.settings.get(CHATS_KEY, []) if isinstance(c, int)]


async def _remember(session: AsyncSession, tenant: Tenant, **updates: Any) -> None:
    tenant.settings = {**tenant.settings, **updates}
    await session.commit()


async def _tell(
    api: BotAPI, chats: Sequence[int], text: str, markup: dict[str, Any] | None = None
) -> bool:
    """Send to every chat; True when at least one took it. Whoever blocked the
    bot must not make a notice look undelivered to the others, nor stop them."""
    delivered = False
    for chat_id in chats:
        try:
            delivered = bool(await api.send(chat_id, text, markup)) or delivered
        except Exception:  # noqa: BLE001 - one chat must not stop the others
            logger.warning("doctor_telegram_send_failed", exc_info=True)
    return delivered


_last_no_chats_warning = 0.0


def _warn_no_chats(tenant: Tenant) -> None:
    """The one way notices fail in silence: nobody has opened the bot, so there
    is no chat to send to. Said every ten minutes, not every poll."""
    global _last_no_chats_warning
    if monotonic() - _last_no_chats_warning > 600:
        _last_no_chats_warning = monotonic()
        logger.warning("doctor_telegram_no_chats", extra={"tenant_id": str(tenant.id)})


def _in_work_hours(local: datetime) -> bool:
    return local.weekday() in WORK_DAYS and WORK_START <= local.time() < WORK_END


def _booking_lines(
    title: str, booking: Booking, today: date, reason: str | None, *, show_reason: bool = True
) -> list[str]:
    day = booking.local.date()
    lines = [title, "", f"👤 <b>{html.escape(booking.name)}</b>"]
    if booking.phone:
        lines.append(f"📞 <code>{html.escape(booking.phone)}</code>")
    if booking.patient is not None and booking.patient.username:
        lines.append(f"📷 @{html.escape(booking.patient.username)}")
    lines.append(f"📅 {_day_word(day, today)} · <i>{_long_date(day)}</i>")
    lines.append(f"🕐 <b>{booking.local:%H:%M}</b>")
    if show_reason:
        lines += ["", f"📝 <b>Sabab:</b> {html.escape(reason) if reason else '<i>aytilmagan</i>'}"]
    return lines


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


async def _announce_bookings(
    api: BotAPI, session: AsyncSession, tenant: Tenant, chats: list[int], since: datetime
) -> None:
    """New bookings, moved ones, and ones somebody else cancelled.

    Each is saved as told the moment it is told. Saving once at the end meant
    a failure half-way through told the doctor about the first booking again
    on the next poll; and one that Telegram refused was skipped for good.
    """
    now = datetime.now(UTC)
    today = datetime.now(CLINIC_TIMEZONE).date()
    announced: dict[str, str] = dict(tenant.settings.get(ANNOUNCED_KEY) or {})
    # Changed as well as new: a patient who moves their time edits a row, not
    # makes one. Matched on whichever timestamp is later.
    touched = func.greatest(Appointment.created_at, Appointment.updated_at)
    rows = (
        await session.execute(
            select(Appointment, User, touched.label("touched"))
            .outerjoin(User, Appointment.user_id == User.id)
            .where(
                Appointment.tenant_id == tenant.id,
                touched > since,
                Appointment.status.in_([*ACTIVE_STATUSES, AppointmentStatus.CANCELLED]),
            )
            .order_by(touched)
        )
    ).all()
    if not rows:
        return

    if len(rows) > CATCH_UP_LIMIT:
        # Opened the bot after a long while, or a burst: a count and the day's
        # list is more use than a screenful of cards.
        active = sum(
            1 for appointment, _p, _t in rows if appointment.status != AppointmentStatus.CANCELLED
        )
        sent = await _tell(
            api,
            chats,
            f"📬 <b>Yangi xabarlar:</b> {active} ta yangi yoki o'zgargan qabul.\n"
            "Ro'yxatni quyidagi tugmalardan ko'ring.",
            {
                "inline_keyboard": [
                    [
                        _btn("📋 Bugun", f"l:{today.isoformat()}"),
                        _btn("📋 Ertaga", f"l:{(today + timedelta(days=1)).isoformat()}"),
                    ]
                ]
            },
        )
        if sent:
            for appointment, _p, _t in rows:
                if appointment.status != AppointmentStatus.CANCELLED:
                    announced[appointment.id.hex[:12]] = appointment.scheduled_at.isoformat()
            await _remember(
                session,
                tenant,
                **{NOTIFIED_UNTIL_KEY: rows[-1][2].isoformat(), ANNOUNCED_KEY: announced},
            )
            return
        rows = []  # fall through to retry on the next poll

    for appointment, patient, moment in rows:
        booking = Booking(appointment, patient)
        key = appointment.id.hex[:12]
        scheduled = appointment.scheduled_at.isoformat()
        day = booking.local.date()
        markup = {
            "inline_keyboard": [
                [_btn(f"📋 {_day_word(day, today)}gi qabullar", f"l:{day.isoformat()}")]
            ]
        }
        text: str | None = None
        if appointment.status == AppointmentStatus.CANCELLED:
            # Told only if we had announced it, and not when the doctor did
            # the cancelling from this very bot.
            if key in announced and "Doktor bu" not in (appointment.notes or ""):
                text = "\n".join(
                    _booking_lines(
                        "❌ <b>Qabul bekor qilindi</b>", booking, today, None, show_reason=False
                    )
                )
            announced.pop(key, None)
        elif appointment.created_at > since:
            reason = await _patient_reason(session, appointment)
            text = "\n".join(_booking_lines("🆕 <b>Yangi qabul</b>", booking, today, reason))
        elif announced.get(key) != scheduled:
            reason = await _patient_reason(session, appointment)
            text = "\n".join(
                _booking_lines("🔄 <b>Qabul vaqti o'zgardi</b>", booking, today, reason)
            )
        if text is not None:
            if not await _tell(api, chats, text, markup):
                if now - moment <= GIVE_UP_AFTER:
                    logger.warning("doctor_telegram_booking_retry")
                    return
                logger.error(
                    "doctor_telegram_booking_given_up",
                    extra={"appointment_id": str(appointment.id)},
                )
            logger.info(
                "doctor_telegram_booking_announced",
                extra={"appointment_id": str(appointment.id), "chats": len(chats)},
            )
        if appointment.status != AppointmentStatus.CANCELLED:
            announced[key] = scheduled
        cutoff = (now - timedelta(days=1)).isoformat()
        announced = {k: v for k, v in announced.items() if v >= cutoff}
        await _remember(
            session, tenant, **{NOTIFIED_UNTIL_KEY: moment.isoformat(), ANNOUNCED_KEY: announced}
        )


async def _announce_leads(
    api: BotAPI, session: AsyncSession, tenant: Tenant, chats: list[int], since: datetime
) -> None:
    now = datetime.now(UTC)
    leads = (
        await session.execute(
            select(Lead, User)
            .outerjoin(User, Lead.user_id == User.id)
            .where(Lead.tenant_id == tenant.id, Lead.created_at > since)
            .order_by(Lead.created_at)
        )
    ).all()
    if len(leads) > CATCH_UP_LIMIT:
        if await _tell(
            api,
            chats,
            f"📞 <b>{len(leads)} ta</b> yangi qo'ng'iroq so'rovi kutyapti.",
            {"inline_keyboard": [[_btn("📞 Raqamlarni ko'rish", "m:leads")]]},
        ):
            await _remember(
                session, tenant, **{LEADS_NOTIFIED_UNTIL_KEY: leads[-1][0].created_at.isoformat()}
            )
        return
    for lead, patient in leads:
        who = lead.patient_name or (patient.name if patient else None)
        lines = ["📞 <b>Qo'ng'iroq qilishni so'radi</b>", ""]
        if who:
            lines.append(f"👤 <b>{html.escape(who)}</b>")
        if patient is not None and patient.username:
            lines.append(f"📷 @{html.escape(patient.username)}")
        lines.append(f"☎️ <code>{html.escape(lead.phone or '—')}</code>")
        if lead.convenient_time:
            lines.append(f"⏰ <b>Qulay vaqt:</b> {html.escape(lead.convenient_time)}")
        lines += [
            "",
            f"📝 <b>Sabab:</b> {html.escape(lead.topic) if lead.topic else '<i>aytilmagan</i>'}",
        ]
        markup = {"inline_keyboard": [[_btn("✅ Bog'lanildi", f"k:{lead.id.hex[:12]}")]]}
        if not await _tell(api, chats, "\n".join(lines), markup):
            if now - lead.created_at <= GIVE_UP_AFTER:
                logger.warning("doctor_telegram_lead_retry")
                return
            logger.error("doctor_telegram_lead_given_up", extra={"lead_id": str(lead.id)})
        await _remember(session, tenant, **{LEADS_NOTIFIED_UNTIL_KEY: lead.created_at.isoformat()})


async def announce_new_bookings(api: BotAPI, session: AsyncSession, tenant: Tenant) -> None:
    """Tell the doctor about every appointment and call-back request created
    or changed since the last look."""
    chats = _chats(tenant)
    since = _watermark(tenant, NOTIFIED_UNTIL_KEY)
    leads_since = _watermark(tenant, LEADS_NOTIFIED_UNTIL_KEY)
    if since is None or leads_since is None:
        # First run: start from now rather than announcing the whole history.
        now = datetime.now(UTC).isoformat()
        await _remember(
            session,
            tenant,
            **{
                NOTIFIED_UNTIL_KEY: tenant.settings.get(NOTIFIED_UNTIL_KEY) or now,
                LEADS_NOTIFIED_UNTIL_KEY: tenant.settings.get(LEADS_NOTIFIED_UNTIL_KEY) or now,
            },
        )
        return
    if not chats:
        # Nothing is marked as told: the doctor gets what they missed, as a
        # count, the moment they first open the bot.
        _warn_no_chats(tenant)
        return
    await _announce_bookings(api, session, tenant, chats, since)
    await _announce_leads(api, session, tenant, chats, leads_since)


# --- watching -------------------------------------------------------------------


async def escalate_waiting(
    api: BotAPI, session: AsyncSession, tenant: Tenant, chats: list[int], now: datetime
) -> None:
    """Tell the doctor about a patient who has waited on a person too long.

    Operators answer most chats within minutes; these are the ones nobody got
    to. Told once per wait, in working hours only -- what piled up overnight
    arrives at nine as one message.
    """
    if not _in_work_hours(now.astimezone(CLINIC_TIMEZONE)):
        return
    waiting, _old = await doctor_watch.waiting_chats(
        session, tenant, now, longer_than=doctor_watch.ESCALATE_AFTER
    )
    told: dict[str, str] = dict(tenant.settings.get(ESCALATED_KEY) or {})
    # Forget chats that are no longer waiting, so the record stays small and a
    # chat that waits again is told about again.
    current = {str(chat.conversation_id): chat.since.isoformat() for chat in waiting}
    kept = {k: v for k, v in told.items() if current.get(k) == v}
    fresh = [chat for chat in waiting if kept.get(str(chat.conversation_id)) is None]
    if not fresh:
        if kept != told:
            await _remember(session, tenant, **{ESCALATED_KEY: kept})
        return
    if len(fresh) <= 3:
        for chat in fresh:
            text = (
                "🙋 <b>Bemor javob kutmoqda</b>\n\n"
                f"👤 {html.escape(chat.who)}\n"
                f"🕐 {_ago(chat.since, now)} dan beri\n"
                f"💬 <i>{html.escape(chat.said[:200])}</i>"
            )
            if await _tell(api, chats, text):
                kept[str(chat.conversation_id)] = chat.since.isoformat()
    else:
        lines = [f"🙋 <b>{len(fresh)} ta bemor javob kutmoqda</b>", ""]
        for chat in fresh[:8]:
            lines.append(
                f"• {html.escape(chat.who)} — {_ago(chat.since, now)}: "
                f"<i>{html.escape(chat.said[:60])}</i>"
            )
        if len(fresh) > 8:
            lines.append(f"… va yana {len(fresh) - 8} ta")
        if await _tell(
            api,
            chats,
            "\n".join(lines),
            {"inline_keyboard": [[_btn("🩺 Holat", "m:status")]]},
        ):
            kept.update({str(chat.conversation_id): chat.since.isoformat() for chat in fresh})
    await _remember(session, tenant, **{ESCALATED_KEY: kept})


async def watch_assistant(
    api: BotAPI, session: AsyncSession, tenant: Tenant, chats: list[int], now: datetime
) -> None:
    """Say so when the assistant stops answering patients, and when it is back.

    Two ways it goes quiet: somebody switched it off and forgot, or it is on
    and not answering (the Instagram connection, the server). The second has
    no symptom on our side, and the clinic learns of it from a patient.
    """
    local = now.astimezone(CLINIC_TIMEZONE)
    state: dict[str, Any] = dict(tenant.settings.get(BOT_OFF_ALERT_KEY) or {})
    bot_on = bool(tenant.settings.get("bot_replies_enabled", True))
    if not bot_on:
        if not state:
            await _remember(
                session, tenant, **{BOT_OFF_ALERT_KEY: {"since": now.isoformat(), "told": False}}
            )
        elif not state.get("told") and _in_work_hours(local):
            since = datetime.fromisoformat(state["since"])
            if now - since >= timedelta(minutes=30) and await _tell(
                api,
                chats,
                "⛔ <b>Yordamchi o'chirilgan</b>\n\nBemorlarga bot javob bermayapti. "
                "Dashboarddagi tugma bilan yoqiladi.",
            ):
                await _remember(session, tenant, **{BOT_OFF_ALERT_KEY: {**state, "told": True}})
        return
    if state:
        if state.get("told"):
            await _tell(api, chats, "✅ <b>Yordamchi yana yoqildi</b> — bemorlarga javob beryapti.")
        await _remember(session, tenant, **{BOT_OFF_ALERT_KEY: {}})

    if not _in_work_hours(local):
        return
    silent_at = _watermark(tenant, SILENT_ALERT_KEY)
    if silent_at is not None and now - silent_at < timedelta(hours=3):
        return
    health = await doctor_watch.health(session, tenant, now)
    if health.silent >= doctor_watch.BOT_SILENT_COUNT and await _tell(
        api,
        chats,
        f"⚠️ <b>Bot javob bermayotgan ko'rinadi</b>\n\n{health.silent} ta suhbatda bemor "
        "10 daqiqadan beri javob kutyapti, bot jim. Instagram ulanishi yoki serverni "
        "tekshirish kerak.",
        {"inline_keyboard": [[_btn("🩺 Holat", "m:status")]]},
    ):
        await _remember(session, tenant, **{SILENT_ALERT_KEY: now.isoformat()})


async def morning_summary(
    api: BotAPI, session: AsyncSession, tenant: Tenant, chats: list[int], now: datetime
) -> None:
    """One message at half past eight: today's patients, who is waiting, which
    numbers nobody has rung. Once a day, never on a day off, and not after noon
    -- a summary of the morning at three is noise."""
    local = now.astimezone(CLINIC_TIMEZONE)
    today = local.date()
    if (
        local.weekday() not in WORK_DAYS
        or tenant.settings.get(SUMMARY_SENT_KEY) == today.isoformat()
        or not (SUMMARY_FROM <= local.time() < SUMMARY_UNTIL)
    ):
        return
    bookings = await bookings_on(session, tenant.id, today)
    health = await doctor_watch.health(session, tenant, now)
    closed = today in days_off_from(tenant.settings)
    lines = ["☀️ <b>Xayrli tong!</b>", f"<i>{_long_date(today)}</i>", ""]
    if closed:
        lines.append("🚫 Bugun kun yopilgan.")
    elif bookings:
        lines.append(f"📋 <b>Bugun {len(bookings)} ta qabul:</b>")
        lines += [f"{b.local:%H:%M} — {html.escape(b.name)}" for b in bookings[:15]]
        if len(bookings) > 15:
            lines.append(f"… va yana {len(bookings) - 15} ta")
    else:
        lines.append("📋 Bugunga hozircha hech kim yozilmagan.")
    if health.waiting:
        lines.append(f"\n🙋 Javob kutayotgan bemor: <b>{len(health.waiting)} ta</b>")
    if health.open_leads:
        lines.append(f"📞 Bog'lanilmagan raqam: <b>{health.open_leads} ta</b>")
    if not health.bot_on:
        lines.append("\n⛔ <b>Yordamchi o'chirilgan</b>")
    if await _tell(
        api,
        chats,
        "\n".join(lines),
        {
            "inline_keyboard": [
                [_btn("📋 Qabullar", f"l:{today.isoformat()}"), _btn("📞 Raqamlar", "m:leads")]
            ]
        },
    ):
        await _remember(session, tenant, **{SUMMARY_SENT_KEY: today.isoformat()})


_next_watch = 0.0
WATCH_EVERY_SECONDS = 120


async def watch(api: BotAPI, session: AsyncSession, tenant: Tenant) -> None:
    """The slower checks, run from the poll loop every couple of minutes. Each
    is guarded on its own: one failing must not silence the rest."""
    global _next_watch
    if monotonic() < _next_watch:
        return
    _next_watch = monotonic() + WATCH_EVERY_SECONDS
    chats = _chats(tenant)
    if not chats:
        return
    now = datetime.now(UTC)
    for check in (morning_summary, escalate_waiting, watch_assistant):
        try:
            await check(api, session, tenant, chats, now)
        except Exception:  # noqa: BLE001 - see above
            await session.rollback()
            logger.exception("doctor_telegram_watch_failed", extra={"check": check.__name__})


async def run_forever(stop: asyncio.Event) -> None:
    """The worker's background loop: poll Telegram, announce new bookings."""
    settings = get_settings()
    token = settings.doctor_telegram_bot_token
    if not token:
        return
    if not (_names(settings.doctor_telegram_usernames) or _names(settings.doctor_telegram_viewers)):
        # The bot is on and refuses everyone: no chat is ever saved, and no
        # notice or report reaches anybody. Easy to miss, so it is loud.
        logger.error(
            "doctor_telegram_nobody_allowed: DOCTOR_TELEGRAM_BOT_TOKEN is set but "
            "DOCTOR_TELEGRAM_USERNAMES is empty, so the bot refuses everyone"
        )
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
                    await watch(api, session, tenant)
                finally:
                    reset_current_tenant(token_ctx)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep polling through a bad minute
            logger.exception("doctor_telegram_loop_failed")
            await asyncio.sleep(10)
