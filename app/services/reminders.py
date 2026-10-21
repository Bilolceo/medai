"""Reminding patients about an appointment before it happens.

Ported from the Telegram worker's cron job, onto the shared delivery path so
a patient is reminded on whichever platform they booked through rather than
only on Telegram.

Five things are different from the version this replaces, and all five were
ways a patient could silently not be reminded:

* A reminder is marked sent only when it actually went out. The original set
  the flag unconditionally, so any appointment it could not deliver to was
  recorded as reminded and never tried again.
* It sends with the clinic's own channel credentials, resolved from the
  patient's channel. The original sent everything with one global bot token,
  which is wrong the moment a second clinic exists.
* It asks the database for the appointments that are due instead of loading
  every future appointment in the system and filtering them in Python.
* A patient reachable on Instagram is reminded too.
* A booking made at short notice gets one reminder, not two at once. The
  original's fixed 22-26h and 1-3h windows also meant a reminder was lost
  outright if the worker happened to be down when its window passed; these
  are catch-up windows instead.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import ChannelAdapter
from app.core.tenant_context import reset_current_tenant, set_current_tenant
from app.models.appointment import ACTIVE_STATUSES, Appointment
from app.models.message import MessageSender
from app.models.user import User
from app.services import language
from app.services.appointment import CLINIC_TIMEZONE, UNASSIGNED_DOCTOR_NAME
from app.services.conversation import recent_history, record_outbound_message, reply_context_for
from app.services.delivery import send_reply

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReminderWindow:
    """One reminder, and the column that records having sent it."""

    lead_time: timedelta
    flag: str
    template: str
    # Used when no doctor has been assigned. The assistant books a time, not
    # a person, so this is the ordinary case rather than the exception -- and
    # the doctor template renders "Tayinlanmagan qabulida", which is what a
    # patient actually received this morning.
    template_unassigned: str
    # The same two in the other alphabets patients write in, keyed by the
    # script app.services.language names ("uz-cyrl", "ru"). A patient who
    # wrote Cyrillic throughout got the Latin one and answered "Лотинча
    # тушунмадим" (live, 2026-10-20).
    wording: dict[str, tuple[str, str]]


# Ordered longest lead first — see _due_windows for why the order matters.
#
# TODO(IGB-?): the wording is fixed here; it should come from tenants.settings
# once there is somewhere to put a clinic's own text.
REMINDER_WINDOWS: tuple[ReminderWindow, ...] = (
    ReminderWindow(
        lead_time=timedelta(hours=24),
        flag="reminder_24h_sent",
        template=(
            "Eslatma: ertaga, {date} kuni soat {time} da {doctor} qabulida "
            "ko'rikka yozilgansiz. Sizni kutamiz!"
        ),
        template_unassigned=(
            "Eslatma: ertaga, {date} kuni soat {time} da qabulga yozilgansiz. Sizni kutamiz!"
        ),
        wording={
            "uz-cyrl": (
                "Эслатма: эртага, {date} куни соат {time} да {doctor} қабулида "
                "кўрикка ёзилгансиз. Сизни кутамиз!",
                "Эслатма: эртага, {date} куни соат {time} да қабулга ёзилгансиз. Сизни кутамиз!",
            ),
            "ru": (
                "Напоминание: завтра, {date}, в {time} вы записаны на приём к {doctor}. Ждём вас!",
                "Напоминание: завтра, {date}, в {time} вы записаны на приём. Ждём вас!",
            ),
        },
    ),
    ReminderWindow(
        lead_time=timedelta(hours=2),
        flag="reminder_2h_sent",
        template=(
            "Eslatma: bugun soat {time} da {doctor} qabulida ko'rikka kutilmoqdasiz. Sizni kutamiz!"
        ),
        template_unassigned=("Eslatma: bugun soat {time} da qabulga yozilgansiz. Sizni kutamiz!"),
        wording={
            "uz-cyrl": (
                "Эслатма: бугун соат {time} да {doctor} қабулида кўрикка кутилмоқдасиз. "
                "Сизни кутамиз!",
                "Эслатма: бугун соат {time} да қабулга ёзилгансиз. Сизни кутамиз!",
            ),
            "ru": (
                "Напоминание: сегодня в {time} вас ждут на приёме у {doctor}. Ждём вас!",
                "Напоминание: сегодня в {time} вы записаны на приём. Ждём вас!",
            ),
        },
    ),
)


def window_name(window: ReminderWindow) -> str:
    """A log-friendly name for a window, derived from its lead time."""
    hours = int(window.lead_time.total_seconds() // 3600)
    return f"{hours}h"


@dataclass(frozen=True)
class ReminderRun:
    """What one pass did, for the caller to log."""

    sent: int
    failed: int
    skipped: int


def _due_windows(
    appointment: Appointment, now: datetime
) -> tuple[ReminderWindow | None, list[ReminderWindow]]:
    """Which reminder to send for this appointment, and which to write off.

    Returns (the one to send, the ones whose moment has already passed).

    A booking made ninety minutes ahead is inside both windows at once. The
    patient should get one message — the most urgent — and the day-before
    reminder should be recorded as done rather than arriving straight after
    it. Windows are ordered longest-first, so the last due one is always the
    most urgent.
    """
    remaining = appointment.scheduled_at - now
    due = [
        window
        for window in REMINDER_WINDOWS
        if not getattr(appointment, window.flag) and remaining <= window.lead_time
    ]
    if not due:
        return None, []
    return due[-1], due[:-1]


def _message(appointment: Appointment, window: ReminderWindow, script: str = "uz-latn") -> str:
    local = appointment.scheduled_at.astimezone(CLINIC_TIMEZONE)
    doctor = appointment.doctor_name
    named, unassigned = window.wording.get(script, (window.template, window.template_unassigned))
    template = unassigned if not doctor or doctor == UNASSIGNED_DOCTOR_NAME else named
    return template.format(
        date=local.strftime("%d.%m.%Y"),
        time=local.strftime("%H:%M"),
        doctor=doctor,
    )


async def _script_for(session: AsyncSession, appointment: Appointment, patient: User) -> str:
    """The alphabet the patient has been writing in: their own messages in
    the conversation decide, newest first; a booking made without one falls
    back on the language noted on the patient, then on Latin Uzbek."""
    if appointment.conversation_id is not None:
        turns = await recent_history(session, appointment.conversation_id)
        written = [t for t in turns if t.get("role") == "user"]
        if written:
            return language.conversation_script(written[:-1], written[-1]["content"])
    if patient.preferred_language in ("uz-cyrl", "ru"):
        return patient.preferred_language
    return "uz-latn"


async def _pending(session: AsyncSession, now: datetime) -> Sequence[tuple[Appointment, User]]:
    """Appointments that could need a reminder right now, with their patient.

    Filtered in SQL rather than in Python: the longest lead time bounds how
    far ahead to look, the status filter drops cancelled bookings, and the
    join to users drops the ones with nobody to message — an operator's
    walk-in booking has no User row and no way to be reminded.

    Crosses tenants deliberately: this runs from a cron with no tenant of
    its own, and the caller sets one per appointment before touching
    anything tenant-scoped.
    """
    horizon = now + max(window.lead_time for window in REMINDER_WINDOWS)
    unsent = or_(*[getattr(Appointment, window.flag).is_(False) for window in REMINDER_WINDOWS])

    stmt = (
        select(Appointment, User)
        .join(User, Appointment.user_id == User.id)
        .where(
            Appointment.status.in_(ACTIVE_STATUSES),
            Appointment.scheduled_at > now,
            Appointment.scheduled_at <= horizon,
            unsent,
        )
        .order_by(Appointment.scheduled_at)
    )
    return list((await session.execute(stmt)).tuples().all())


async def send_due_reminders(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    adapter: ChannelAdapter | None = None,
) -> ReminderRun:
    """Send every reminder that has come due, and record having sent it.

    Commits per appointment rather than once at the end: a failure partway
    through must not undo the reminders already delivered, or the next run
    would send them a second time.

    `now` and `adapter` are injectable for the same reason they are
    elsewhere in this codebase — tests drive the clock and the transport
    directly rather than waiting for either.
    """
    current = now or datetime.now(UTC)
    sent = failed = skipped = 0

    for appointment, patient in await _pending(session, current):
        to_send, expired = _due_windows(appointment, current)
        if to_send is None:
            continue

        token = set_current_tenant(appointment.tenant_id)
        try:
            for window in expired:
                # Recorded as done without being sent: its moment passed
                # before the booking existed, and leaving it unset would
                # deliver a "see you tomorrow" after the "see you in two
                # hours".
                setattr(appointment, window.flag, True)
                skipped += 1

            reply_context = None
            if appointment.conversation_id is not None:
                reply_context = await reply_context_for(session, appointment.conversation_id)
            text = _message(appointment, to_send, await _script_for(session, appointment, patient))

            delivered_over = await send_reply(
                session,
                channel_id=patient.channel_id,
                recipient_external_id=patient.external_id,
                text=text,
                # A reminder is the clinic starting a conversation, not
                # answering one, so it is measured against the patient's own
                # last message — which is exactly what a platform's reply
                # window is for. Outside it the reminder is skipped, not
                # forced.
                last_user_message_at=appointment.created_at,
                reply_context=reply_context,
                adapter=adapter,
            )

            if delivered_over is None:
                # Left unset on purpose. The next run tries again, and the
                # reminder still arrives if whatever blocked it clears in
                # time.
                failed += 1
                logger.warning(
                    "reminder_not_delivered",
                    extra={
                        "appointment_id": str(appointment.id),
                        "window": window_name(to_send),
                    },
                )
                await session.commit()
                continue

            setattr(appointment, to_send.flag, True)
            if appointment.conversation_id is not None:
                # Into the transcript, so an operator reading the
                # conversation sees what the clinic has already said. A
                # booking made from the dashboard has no conversation to
                # record it in.
                await record_outbound_message(
                    session,
                    conversation_id=appointment.conversation_id,
                    channel_type=delivered_over,
                    text=text,
                    sender=MessageSender.BOT,
                )
            await session.commit()
            sent += 1
            logger.info(
                "reminder_sent",
                extra={
                    "appointment_id": str(appointment.id),
                    "window": window_name(to_send),
                    "channel_type": delivered_over,
                },
            )
        finally:
            reset_current_tenant(token)

    return ReminderRun(sent=sent, failed=failed, skipped=skipped)
